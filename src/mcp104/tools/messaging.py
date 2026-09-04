"""Five messaging tools backed by 104's JSON API (no browser navigation) —
`read_messages` / `get_conversation` / `send_message` / `send_inquiry` /
`list_templates`. Every non-trivial decision (body/param construction, row
conversion, the send taxonomy) is a module-level pure function, testable with no
browser, no HTTP and no MCP `Context`.

Four of the five issue exactly one HTTP request per tool call via
`tools/helpers.py`'s `guarded_api`. `send_inquiry` is this project's one exception
— it issues three, in a fixed order, via
`guarded_api`'s sibling `guarded_sequence` — one lock, one throttle-gate check, one
`_issue_one` per sub-request, no code duplicated between the two entry points.

Deliberately does NOT: issue the HTTP request, decide session/auth failures, or run
the request throttle — `guarded_api`/`guarded_sequence` own all three.

Nothing in this module imports `patchright` at all (not even under `TYPE_CHECKING`)
and it must never gain `from __future__ import annotations` (see CLAUDE.md's known
pitfall #2 — that import makes `ctx` leak into the published inputSchema).
"""

import logging

from mcp.server.fastmcp import Context, FastMCP

import mcp104.tools.discovery as discovery_mod
from mcp104.browser.api_client import ENDPOINTS
from mcp104.browser.session import SessionInfo
from mcp104.db.database import ID_SOURCE_MESSAGE
from mcp104.tools.discovery import _event_labels
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    ToolAbort,
    convert_keys,
    guarded_api,
    guarded_sequence,
    require_login,
)

log = logging.getLogger("104-mcp.messaging")


def _malformed_response_payload(exc: MalformedResponseError) -> dict:
    # Same Chinese string as tools/search.py's own copy and
    # tools/helpers.py's `_error_malformed` — kept as a per-module function
    # rather than a shared import, matching the precedent search.py already
    # set for this exact string (see MalformedResponseError's own
    # docstring in helpers.py for why the CLASS moved but this small
    # formatter did not: messaging.py importing a leading-underscore
    # helper out of search.py would couple two tool modules that
    # otherwise share nothing, and helpers.py already owns the class both
    # modules raise).
    return {"error": f"104 回應結構異常（{exc.detail}），可能是介面已變更，請回報"}


# ── Inbox request body (§6b.8-1) ─────────────────────────────────────────

# The only perPage value ever put on this wire — the front-end's own value
# [M §6b.8-1, §6b.8-5]; whether any other value is accepted is unmeasured,
# so a module constant carrying that fact is better than a parameter
# inviting a value nobody has tried.
INBOX_PER_PAGE = 30


def _inbox_request_body(page: int, job_nos: list[str] | None, candidate_name: str | None) -> dict:
    """Pure. `eventType`/`eventStatus`/`departmentIds` are sent at their measured
    defaults and are NOT parameters — their value domains are unmeasured, and
    `departmentIds` additionally has no established relationship to the
    `department.id` `list_jobs` returns."""
    return {
        "jobNos": job_nos or [],
        "eventType": None,
        "eventStatus": None,
        "candidateName": candidate_name or "",
        "page": page,
        "perPage": INBOX_PER_PAGE,
        "departmentIds": [],
    }


# ── Send body (§6b.9-4) ──────────────────────────────────────────────────

def _send_body(message: str) -> dict:
    """Pure. `{"content": message}` is the whole requirement for a plain-text send
    [M §6b.9-4] — `sc`/`rc`/`ec`/`callbackInfo` are not built at all (a field sent
    for no measured reason is a claim about the protocol nothing supports), and
    `link`/`file` (104's attachment channel) are omitted rather than sent empty."""
    return {"content": message}


# ── candidate_id digit guard ─────────────────────────────────────────────
#
# A résumé-row's candidate_id (idNo, 13-14 digits measured) and its p_id
# (the messaging system's own id, 6-8 digits measured) are two different
# key spaces. All three messaging tools take a pId, not an idNo — feeding
# one the wrong id does not fail loudly, it silently fails to reach anyone
# or, worst case, reaches the wrong person. This threshold is a guardrail
# this project chose (12 digits, comfortably between the two measured
# ranges), not a rule 104 has ever stated — see CLAUDE.md.

_RESUME_ID_GUARD_MESSAGE = (
    "這個 candidate_id 看起來是履歷的 candidate_id（idNo，量到 13–14 位數字），"
    "這裡要的是履歷列上的 p_id（訊息系統自己的 id，量到 6–8 位數字）。"
)


def _looks_like_resume_id(candidate_id: str) -> bool:
    """Pure. All-digit AND at least 12 digits long — a résumé idNo shape, not a
    messaging pId shape. Non-digit strings never trigger this; the guard exists to
    catch the specific idNo/pId mix-up, not to reject unrelated input."""
    return candidate_id.isdigit() and len(candidate_id) >= 12


# ── Read-state: a message-id watermark, not a timestamp (§6b.8-3) ───────

def _watermark(metadata: object) -> int | None:
    """Pure. `metadata.creadAt` parsed as an int — a message-id watermark, not a
    timestamp (read as Unix seconds it gives 2035, as milliseconds 1970
    [M §6b.8-3]). `None` when `metadata` is not a mapping, `creadAt` is absent, or it
    does not parse as an int."""
    if not isinstance(metadata, dict):
        return None
    raw = metadata.get("creadAt")
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def _direction(source: object) -> str:
    """Pure. `source == 0` -> our own sent message, anything else -> received
    [M §6b.8-3]. The only two values ever measured are 0 and 1."""
    return "sent" if source == 0 else "received"


def _read_state(message_id: object, watermark: int | None, direction: str) -> bool | None:
    """Pure, tri-state — keeps the DOM-era semantics exactly [M §6b.8-3]: `None` for
    received messages (read-state is meaningless there — `False` would misread as
    "they never read their own message"), `None` when either value is missing or
    unparsable ("unknown", never "confirmed unread"), otherwise
    `int(message_id) <= watermark`.

    Two measured limits this comparison inherits and does not correct for: the
    watermark is ONE number for the whole conversation, so "read" means "the
    candidate has read up to here" and is not per-message data; and ids are
    monotonic with time except for 1-2 rank inversions within the same second, so a
    watermark landing on a same-second boundary can misjudge one message. Both live
    in this function's callers' docstrings and in describe_result_fields' gloss for
    `read`, not in a per-response warning — they are true of every response this
    tool will ever return, and an always-present warning is not a warning.
    """
    if direction != "sent":
        return None
    if watermark is None:
        return None
    try:
        mid = int(message_id)
    except (TypeError, ValueError):
        return None
    return mid <= watermark


# ── Row conversion ────────────────────────────────────────────────────────
#
# The SAME mechanical camelCase -> snake_case conversion tools/search.py applies,
# walked recursively at every depth — imported from tools/discovery.py (via
# `_snake_case`), never redefined here, for the identical reason search.py imports
# it rather than keeping its own copy: describe_result_fields(row_type=...) must key
# its payload on the SAME delivered names this produces.

# tools/helpers.py's convert_keys, bound to the name this module has always
# used. No `string_transform`: message content is delivered exactly as 104
# sent it, unlike the résumé tools' rich-text fields.
_convert = convert_keys


def _convert_inbox_row(raw: dict) -> dict:
    """One row from read_messages -> the tool-facing dict. `jobNo` -> `job_id` and
    `pId` -> `candidate_id` are the two documented renames (discovery.
    INBOX_ROW_FIELD_GLOSS); `idNo`/`hid` (discovery.INBOX_ROW_EXCLUDED_FIELDS,
    résumé-space-shaped identifiers of unmeasured key space) are dropped entirely,
    never published under any name. `event_progress`/`event_polarity` are added,
    derived from the raw (eventType, eventStatus) pair — not from any already-
    converted field, since the derivation needs the raw ints.
    """
    picked = {
        k: v for k, v in raw.items()
        if k in discovery_mod.INBOX_ROW_FIELD_GLOSS and k not in discovery_mod.INBOX_ROW_EXCLUDED_FIELDS
    }
    converted = _convert(picked)
    if "job_no" in converted:
        converted["job_id"] = converted.pop("job_no")
    if "p_id" in converted:
        converted["candidate_id"] = converted.pop("p_id")
    progress, polarity = _event_labels(raw.get("eventType"), raw.get("eventStatus"))
    converted["event_progress"] = progress
    converted["event_polarity"] = polarity
    return converted


def _convert_message_row(raw: dict, watermark: int | None) -> dict:
    """One row from get_conversation -> the tool-facing dict. `idNo`/`snapshotId`
    (unmeasured key space), `userName` (個資 — the candidate's own name on a
    received message, already on the inbox row; the OPERATOR's own name on a sent
    message) and `source` (replaced by the derived `direction`, never published
    alongside it) are dropped entirely (discovery.MESSAGE_ROW_EXCLUDED_FIELDS).
    `direction`/`read` are added, derived from the raw `source`/`id` — `event` is
    passed through unchanged by the ordinary mechanical conversion, at every
    nesting level, like any other field."""
    picked = {
        k: v for k, v in raw.items()
        if k in discovery_mod.MESSAGE_ROW_FIELD_GLOSS and k not in discovery_mod.MESSAGE_ROW_EXCLUDED_FIELDS
    }
    converted = _convert(picked)
    direction = _direction(raw.get("source"))
    converted["direction"] = direction
    converted["read"] = _read_state(raw.get("id"), watermark, direction)
    return converted


# ── Response builders ────────────────────────────────────────────────────

def _more_pages_warning(pagination: dict, *, extra: str = "") -> str | None:
    """Pure. Shared by both read_messages' and get_conversation's response
    builders — the more-pages signal is a property of `pagination.
    {page,total_pages}`, not a read_messages-specific feature (§3). `extra` lets a
    caller append route-specific guidance WITHOUT this function itself asserting
    anything about a route it cannot see: get_conversation's caller passes the
    oldest-first / newest-page note here because `sort=ASC` is measured for that
    route [C §6b.9-2]; read_messages' sort order is unmeasured, so its caller
    passes nothing rather than let this function guess a shared claim.
    """
    page = pagination.get("page")
    total_pages = pagination.get("total_pages")
    if not isinstance(page, int) or not isinstance(total_pages, int) or page >= total_pages:
        return None
    return (
        f"共 {total_pages} 頁，本次拿到的是第 {page} 頁；其餘資料尚未讀取，"
        f"如需請以 page=N（1..{total_pages}）重新呼叫。" + extra
    )


def _build_inbox_response(envelope: object) -> dict:
    """`envelope` is read_messages' already-classified family-B payload — the WHOLE
    {data, metadata} envelope. A missing `metadata` is an error (both messaging
    routes are recorded carrying `metadata` with all four keys [M §6b.7, §6b.8-5],
    so its absence is a genuine departure); `data` missing or not a list is already
    refused upstream by family B's `is_list` floor, so it is checked again here only
    defensively, matching tools/search.py's own `_build_job_list_response`
    precedent."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        raise MalformedResponseError("metadata 缺失或非物件")

    pagination = {
        "page": metadata.get("page"),
        "total_pages": metadata.get("totalPage"),
        "total": metadata.get("total"),
    }
    warnings = []
    warning = _more_pages_warning(pagination)
    if warning:
        warnings.append(warning)

    return {
        "results": [_convert_inbox_row(row) for row in envelope["data"]],
        "pagination": pagination,
        "browse_limit": None,
        "warnings": warnings,
    }


def _build_conversation_response(envelope: object) -> dict:
    """`envelope` is get_conversation's already-classified family-B payload. Same
    "missing metadata is an error" rule as `_build_inbox_response`. The watermark
    (`_watermark`) is read from this SAME `metadata`, once, and threaded into every
    row's `_convert_message_row` call — it is a single conversation-wide value, not
    per-row data (§4)."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        raise MalformedResponseError("metadata 缺失或非物件")

    watermark = _watermark(metadata)
    pagination = {
        "page": metadata.get("page"),
        "total_pages": metadata.get("totalPage"),
        "total": metadata.get("total"),
    }
    warnings = []
    total_pages = pagination.get("total_pages")
    warning = _more_pages_warning(
        pagination,
        # Unconditional "sort=ASC, oldest to newest" statement, never "this
        # page is the oldest batch": that claim is only
        # true on page 1 — on page 2 of 3 the CURRENT page is neither the
        # oldest nor the newest batch, and the old wording asserted it was
        # regardless of which page fired the warning. The actionable half
        # (which page holds the newest messages) stays, since it is true on
        # every page this warning ever fires on.
        extra=(
            f"此對話以 sort=ASC 由舊到新讀取；最新的訊息在第 {total_pages} 頁，"
            f"請以 page={total_pages} 再讀一次。"
            if isinstance(total_pages, int) else ""
        ),
    )
    if warning:
        warnings.append(warning)

    return {
        "results": [_convert_message_row(row, watermark) for row in envelope["data"]],
        "pagination": pagination,
        "browse_limit": None,
        "warnings": warnings,
    }


# ── list_templates: allow-listed row conversion ─────────────────────────

# Allow-list, not deny-list — matching INBOX_ROW_EXCLUDED_FIELDS'/
# MESSAGE_ROW_EXCLUDED_FIELDS' policy of "the mechanism publishes only what
# it names", applied the other way round because a template row's full key
# set (which includes `files`, only meaningful on the single-template
# route this project never calls) is not itself documented anywhere this
# module owns. `_convert` (already defined above) does the camelCase ->
# snake_case rename for the two keys that need it (typeId -> type_id,
# typeDesc -> type_desc).
_TEMPLATE_ROW_ALLOWED_FIELDS = frozenset({"id", "title", "description", "typeId", "typeDesc"})


def _convert_template_row(raw: dict) -> dict:
    """One row from list_templates -> the tool-facing dict, allow-listed to the
    five measured keys (`id`, `title`, `description`, `typeId`, `typeDesc`).
    `description` is already the complete letter body (51-307 characters
    measured) — there is no separate detail fetch this project performs."""
    picked = {k: v for k, v in raw.items() if k in _TEMPLATE_ROW_ALLOWED_FIELDS}
    return _convert(picked)


def _build_template_list_response(envelope: object) -> dict:
    """`envelope` is list_templates' already-classified family-B payload. Same
    "missing metadata is an error" rule as `_build_inbox_response` — measured
    `metadata` present even on the no-typeId call, so its absence here is a
    genuine departure, handled via the existing MalformedResponseError precedent
    rather than a new failure mode."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        raise MalformedResponseError("metadata 缺失或非物件")

    pagination = {
        "page": metadata.get("page"),
        "total_pages": metadata.get("totalPage"),
        "total": metadata.get("total"),
    }
    warnings = []
    warning = _more_pages_warning(pagination)
    if warning:
        warnings.append(warning)

    return {
        "results": [_convert_template_row(row) for row in envelope["data"]],
        "pagination": pagination,
        "browse_limit": None,
        "warnings": warnings,
    }


# ── send_inquiry: the event/willingness send body ───────────────────────

# The wire key set is measured verbatim [M §8.13/§8.14-1/§8.19] — every key
# except candidate[0].idNo (the reverse bridge's value), contactJobNo (the
# caller's own job_id), content (the caller's message) and templateId is a
# fixed literal, not a parameter. See design's weight-bearing assumption 3
# for why isWithDetail is hardcoded True rather than opened up.
_WILLINGNESS_RC = "13011211"


def _willingness_body(id_no: str, job_id: str, message: str, template_id: str | None, email_cc: list) -> dict:
    """Pure. Assembles the POST /bc-comm/event/willingness body. `id_no` comes
    from the reverse bridge (sub-request 1), never from the caller's own
    candidate_id (which is a pId, a different key space). `templateId` is the
    caller's value verbatim, or the measured empty-string shape when omitted —
    the key is ALWAYS present, there is no third shape. `email_cc` is
    last-info's `data.emailCC`, sent back verbatim including `[]`."""
    return {
        "candidate": [{"idNo": id_no}],
        "contactJobNo": job_id,
        "content": message,
        "templateId": template_id if template_id is not None else "",
        "isRequiredReplyDay": False,
        "replyDay": 1,
        "contact": "",
        "contactTel": "",
        "isWithDetail": True,
        "file": [],
        "ec": "",
        "rc": _WILLINGNESS_RC,
        "emailCC": email_cc,
    }


MAX_INQUIRY_MESSAGE_LENGTH = 1000


# ── send_message: the daily cap, the send log, and the three-way verdict (§6) ──
#
# How to read this section: NOT_SENT (below) is the rule; `_send_verdict` is the one
# function that consumes it; `_log_send_attempt` is the shared, failure-tolerant
# writer. The tool body wires the three together at three call sites (§6c).

# NOT SENT — the CLOSED set, and the only one that is enumerated in code. Every
# member is either a pre-request precondition WE enforce (never reaching 104 at
# all) or a condition where 104 itself answered with a refusal. `missing_param` and
# `header_fault` are family-A-only kinds and so cannot arrive on a messaging route
# at all; named anyway, because a mapping that is exhaustive over the classifier's
# whole vocabulary must not quietly acquire a gap when an endpoint's family changes.
#
# There is deliberately NO `AMBIGUOUS` constant sitting beside this one — a second
# frozenset here is an open invitation to write `if kind in AMBIGUOUS: ... else:
# not_sent`, which inverts the default this whole taxonomy exists to get right.
# AMBIGUOUS is everything else, reached by fallthrough and never listed: today that
# is `malformed`, `unrecognised_status`, `non_json` and `transport` — named here for
# the reader, not written down anywhere `_send_verdict` can see.
NOT_SENT: frozenset[str] = frozenset({
    "not_logged_in", "throttled", "daily_cap", "internal_config",
    # "empty_content" is DOCUMENTARY, not reachable: send_message's own
    # empty/whitespace-only precondition returns {"success": False, ...}
    # directly and never calls _send_verdict at all (no request is issued,
    # so there is no kind to classify). Kept in the set anyway because the
    # set's job is to enumerate every condition this taxonomy considers
    # "not sent" for the READER (§6a's table lists it beside daily_cap),
    # not only the ones _send_verdict happens to be called with.
    "empty_content",
    "expired", "blocked", "challenge", "not_found", "validation", "wrong_host",
    "header_fault", "missing_param",
})


def _send_verdict(kind: str) -> str:
    """Pure. `"not_sent"` when `kind` is in the closed NOT_SENT set, `"ambiguous"`
    otherwise — the default runs INTO ambiguous, and that direction is the whole
    point (§6b): an unknown kind defaulting to ambiguous wastes one daily-cap slot
    and one "go check the console" message; defaulting to "not sent" tells the
    Agent to try again, and the cost of that is a real person messaged twice.
    """
    return "not_sent" if kind in NOT_SENT else "ambiguous"


_SEND_UNCONFIRMED_MESSAGE = (
    "104 已受理這次請求，但送出後的畫面尚未實測，無法確認訊息真的送達。"
    "請至 104 後台確認，不要直接重送。"
)
_SEND_AMBIGUOUS_MESSAGE = (
    "104 的回應無法辨識，不確定訊息是否真的送達。請至 104 後台確認，不要直接重送。"
)


async def _log_send_attempt(app, account_label: str | None, candidate_id: str, id_source: str) -> None:
    """NOT pure — writes to SQLite, and wraps its own failure so that the verdict
    the caller already decided (`unconfirmed`/`ambiguous`) is never replaced by an
    unhandled database error. The rule: writing the log must never suppress the
    verdict, in either direction. One writer, shared by both send_message and
    send_inquiry, called from each tool's own success path, its `except GuardAbort`
    handler when `_send_verdict` says ambiguous, and its `except Exception` handler
    when `send_attempted` was already `True` — so "which verdict logs" stays a
    single decision rather than a property distributed over however many handlers
    exist. `id_source` is always `ID_SOURCE_MESSAGE` from both of today's callers
    (a row keyed by idNo, the OTHER id space, would be a row no tool can ever
    look up again), but the value is the caller's choice, not baked in here.
    """
    try:
        await app.db.log_sent(account_label, candidate_id, id_source)
    except Exception:
        log.error(
            "messaging: log_sent failed for candidate_id=%s (verdict itself is unaffected)",
            candidate_id, exc_info=True,
        )


async def _maybe_mark_contacted(app, candidate_id: str, id_source: str, account_label: str | None) -> None:
    """NOT pure — writes to SQLite, wrapped the same failure-tolerant way as
    `_log_send_attempt` so a DB error here can never flip an already-decided send
    verdict. Only writes `status="contacted"` when the row currently has NO status
    (checked via `get_candidate` first) — `upsert_candidate` overwrites
    unconditionally, and writing "contacted" over an existing "interested" would be
    data loss dressed up as a record. Called on both the confirmed AND the
    unconfirmed/ambiguous paths — `check_already_contacted`'s repeat-contact guard
    is useless if an ambiguous send (the case most likely to have actually reached
    someone) never marks the row at all.
    """
    try:
        existing = await app.db.get_candidate(candidate_id, id_source, account_label)
        if existing is not None and existing.get("status") is not None:
            return
        await app.db.upsert_candidate(candidate_id, id_source, account_label, status="contacted")
    except Exception:
        log.error(
            "messaging: failed to mark candidate_id=%s contacted (verdict itself is unaffected)",
            candidate_id, exc_info=True,
        )


async def _enforce_daily_cap(app, info: SessionInfo) -> None:
    """NOT pure precondition check, shared by send_message's single before_request
    hook and send_inquiry's two call sites (before_first and the third
    sub-request's before_request. Raises the row-1 payload
    (`{"success": False, "error": ...}`) via ToolAbort(kind="daily_cap") when the
    account has reached MAX_DAILY_MESSAGES; returns normally otherwise. Does NOT
    set any send_attempted flag itself — that is each caller's own hook, so the
    LAST statement of THAT hook is unambiguously the caller's, not buried inside a
    shared helper.
    """
    count = await app.db.get_daily_sent_count(info.account_label)
    if count >= app.config.max_daily_messages:
        raise ToolAbort(
            {"success": False, "error": f"已達每日發送上限 {app.config.max_daily_messages} 則"},
            kind="daily_cap",
        )


def _extract_data0(payload: object) -> dict | None:
    """Pure. `payload["data"][0]` when it exists and is itself a mapping,
    otherwise `None` — used by `_build_success_send_result` below. Both
    send-bearing endpoints declare `is_list=True`, so `classify()` has already
    refused anything where `data` is not a list; this only has to handle an empty
    list or a non-mapping first element."""
    if not isinstance(payload, dict):
        return None
    data = payload.get("data")
    if not isinstance(data, list) or not data:
        return None
    first = data[0]
    return first if isinstance(first, dict) else None


def _data_is_empty_list(payload: object) -> bool:
    """Pure. True only when `payload["data"]` is a list AND it is empty — used by
    `_build_success_send_result` to tell that shape apart from a non-empty list
    whose first element is not a mapping (e.g. a bare string). The two shapes must
    not collapse into the same branch: an empty `data` genuinely carries nothing to
    read `pId`/`streamId` from (row 4, alongside a non-empty `failed`), while a
    non-empty `data` with an unreadable first element is 104 answering with
    something this tool cannot parse (row 5) — a fundamentally different claim
    about what 104 did."""
    if not isinstance(payload, dict):
        return False
    data = payload.get("data")
    return isinstance(data, list) and len(data) == 0


def _build_success_send_result(payload: object) -> dict:
    """Pure. `payload` is the ok-verdict payload from the one send-bearing request
    (send_message's single POST, or send_inquiry's third sub-request) — already
    carrying `failed` as a sibling key when 104 sent one (FamilyBShape.sibling_keys).
    Implements the row-3/4/5 dimension of the closed five-row decision table; the
    row-1/2 dimension (send_attempted / `_send_verdict`) is decided by the caller
    BEFORE this is ever reached — by the time this runs, the request definitely
    reached 104 and 104 definitely answered with something classify() accepted as a
    well-formed envelope.

    Row 3 (`confirmed`) requires ALL of: `failed` present as an empty list, `data[0]`
    resolves to a mapping, and both `pId`/`streamId` are present in it — any one
    missing falls through. Row 4 (`failed` present in the result) fires when
    `failed` is present but NOT an empty list, OR `data` is an EMPTY list (`failed`
    present, empty, no row to read `pId`/`streamId` from at all). Everything else —
    `failed` absent, or `data` non-empty but `data[0]` is not a mapping, or `data[0]`
    is a mapping missing `pId`/`streamId` — is row 5. `warnings` is NOT added here;
    every caller adds it unconditionally afterwards.
    """
    has_failed = isinstance(payload, dict) and "failed" in payload
    failed_value = payload.get("failed") if isinstance(payload, dict) else None
    failed_is_empty_list = isinstance(failed_value, list) and len(failed_value) == 0
    data0 = _extract_data0(payload)

    if has_failed and failed_is_empty_list:
        if data0 is not None:
            p_id = data0.get("pId")
            stream_id = data0.get("streamId")
            if p_id is not None and stream_id is not None:
                message_id = None
                msg_ids = data0.get("messageId")
                if isinstance(msg_ids, list) and msg_ids:
                    message_id = msg_ids[0]
                return {
                    "sent": "confirmed",
                    "message_id": message_id,
                    "stream_id": stream_id,
                    "p_id": p_id,
                    "event_id": data0.get("eventId"),
                }
            # failed present & empty, data[0] present but missing pId/streamId -> row 5
            return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE}
        if _data_is_empty_list(payload):
            # failed present & empty, data == [] -> row 4
            return {"sent": "unconfirmed", "message": _SEND_UNCONFIRMED_MESSAGE, "failed": failed_value}
        # failed present & empty, data non-empty but data[0] not a mapping -> row 5
        return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE}

    if has_failed:
        # failed present & non-empty (or not a list at all) -> row 4
        return {"sent": "unconfirmed", "message": _SEND_UNCONFIRMED_MESSAGE, "failed": failed_value}

    # failed absent entirely -> row 5
    return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE}


# ── Shared except-block conclusion for send_message and send_inquiry ────────
#
# Both tools reach the same closed five-row return set out of their locked
# `guarded_api`/`guarded_sequence` region — turning `send_attempted` +
# `_send_verdict` into a return value, and writing sent_log/candidate status
# whenever that verdict says the request may have reached 104. Extracted once
# so the two tools' `except` blocks cannot silently drift apart; the log tag
# (`send_message` vs `send_inquiry`) is the only thing that ever differs
# between the two callers, and it is passed in rather than hard-coded.

async def _conclude_send(
    app, tool_name: str, exc: Exception, send_attempted: bool,
    account_label: str | None, candidate_id: str,
) -> dict:
    """NOT pure — writes sent_log/candidate status via `_log_send_attempt`/
    `_maybe_mark_contacted` on the ambiguous and non-GuardAbort-exception paths.
    `exc` is whatever the caller's own `except GuardAbort as e` / `except
    Exception as exc` block just caught; behaviour must stay byte-identical to
    what each tool's own except block did before this was extracted.

    `GuardAbort`: nothing was ever issued (`send_attempted` still `False`) ->
    the abort's own payload, unchanged, `error`/`success` keys intact —
    `ERROR_CHALLENGE`'s text is the one place that tells an Agent to stop for
    an hour, and re-wrapping it would strip that instruction. A request WAS
    issued (`send_attempted` is `True`) and `_send_verdict` reads the abort's
    `kind` as `"ambiguous"` -> log the attempt, mark the candidate contacted,
    and return the unconfirmed/ambiguous row. Otherwise 104 explicitly
    refused (e.g. validation) -> the guard's own payload, unchanged.

    Any other exception: a non-GuardAbort exception escaping the locked
    region — a defensive catch-all, not a specific known failure mode:
    credentials come from SessionInfo.cookies, a plain attribute with nothing
    left to fail against a dead browser. `send_attempted` is the ordering
    proof: the before_request hook runs strictly before the request, so
    `send_attempted` is `False` only when execution never reached that point,
    i.e. nothing was issued.
    """
    if isinstance(exc, GuardAbort):
        if not send_attempted:
            return exc.payload
        if _send_verdict(exc.kind) == "ambiguous":
            await _log_send_attempt(app, account_label, candidate_id, ID_SOURCE_MESSAGE)
            await _maybe_mark_contacted(app, candidate_id, ID_SOURCE_MESSAGE, account_label)
            return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE, "warnings": []}
        return exc.payload

    log.error("%s: 非預期例外 (send_attempted=%s): %s", tool_name, send_attempted, exc, exc_info=True)
    if send_attempted:
        await _log_send_attempt(app, account_label, candidate_id, ID_SOURCE_MESSAGE)
        await _maybe_mark_contacted(app, candidate_id, ID_SOURCE_MESSAGE, account_label)
        return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE, "warnings": []}
    return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出任何請求"}


def register_messaging_tools(mcp: FastMCP):

    @mcp.tool()
    @require_login
    async def read_messages(
        ctx: Context, page: int = 1,
        job_nos: list[str] | None = None, candidate_name: str | None = None,
    ) -> dict:
        """讀取 104 收件匣列表（走 JSON API，不再解析頁面 DOM；一次呼叫一個請求）。

        Args:
            page: 頁碼，預設 1。
            job_nos: 選填，限定只看這些職缺（值來自 list_jobs 的 jobno）。
            candidate_name: 選填，姓名關鍵字篩選。

        104 一次固定回 30 筆（perPage 沒有暴露成參數——這是前端自己唯一送過的
        值，值域未量測，見 CLAUDE.md）。成功時固定回傳
        {"results": [...], "pagination": {"page","total_pages","total"},
        "browse_limit": null, "warnings": [...]}——browse_limit 固定為 null，
        此路由沒有配額資訊可回報。total_pages 大於 page 時 warnings 會提醒還有
        其餘頁面，需要自行以 page=N 重新呼叫；還沒讀到的頁面不會自動幫你讀。

        失敗時回傳 {"error": str}，沒有 results 欄位。

        欄位意義見 describe_result_fields(row_type="inbox")——event_progress /
        event_polarity 現在是由 (event_type, event_status) 這一對推導出來的中文
        標籤，不是渲染出來的文字；未知配對會回傳帶原始代碼的說明文字，不會猜一個
        標籤或把該列拿掉。
        """
        body = _inbox_request_body(page, job_nos, candidate_name)
        try:
            async with guarded_api(ctx, ENDPOINTS["read_messages"], body=body) as (envelope, _info):
                return _build_inbox_response(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("read_messages: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    async def get_conversation(ctx: Context, job_id: str, candidate_id: str, page: int = 1) -> dict:
        """展開與特定候選人的完整對話紀錄（走 JSON API；一次呼叫一個請求，且不再
        送出已讀回報——這是純讀取，不會讓對方看到已讀，也不會清除 104 主控台上的
        未讀標記，見 CLAUDE.md）。

        Args:
            job_id: 職缺 id（來自 read_messages 回傳的對話列，或 list_jobs 的
                jobno——兩者已驗證是同一個 key space）。
            candidate_id: 候選人 id。同一個 job_id 會對應多個候選人，job_id +
                candidate_id 才能定位到唯一對話串。{{MESSAGING_CANDIDATE_ID_NOTE}}
            page: 頁碼，預設 1，每頁固定 100 筆。

        ⚠ page=1 讀到的是「最舊」的 100 則（104 自己的排序方向，sort=ASC）——如果
        這段對話超過一頁，warnings 會直接點名「最新的訊息在第 N 頁」，不需要自己
        先查總頁數再猜。

        成功時固定回傳 {"results": [...], "pagination": {"page","total_pages",
        "total"}, "browse_limit": null, "warnings": [...]}——與 read_messages
        同一個形狀，呼叫端不必依工具別分支處理。失敗時回傳 {"error": str}，沒有
        results 欄位。

        ⚠ API 會回傳網頁版看不到的訊息（type 決定是否渲染，某些類型網頁版完全不
        顯示，本工具一律照樣回傳）。

        每則訊息含 event 物件（多數為 {}，有值時是完整的結構化招募動作，例如面試
        邀約的時間地點）——⚠ event.content 是我方自己發出的信件本文，不是 104 的
        樣板文字。

        欄位意義（含 direction、read 的三態語意與其已知限制）見
        describe_result_fields(row_type="message")。
        """
        if _looks_like_resume_id(candidate_id):
            return {"error": _RESUME_ID_GUARD_MESSAGE}
        params = [("job_no", job_id), ("p_id", candidate_id), ("page", str(page)), ("perPage", "100"), ("sort", "ASC")]
        try:
            async with guarded_api(ctx, ENDPOINTS["get_conversation"], params=params) as (envelope, _info):
                return _build_conversation_response(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("get_conversation: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    async def send_message(ctx: Context, job_id: str, candidate_id: str, message: str) -> dict:
        """發送一則純文字訊息給候選人，會立刻送到一位真實求職者手上、無法撤回，
        必須先讓真人看過內容再呼叫，沒有 dry-run（走 JSON API；一次呼叫一個
        請求）。要送 104 的「詢問意願」事件（UI 上邀約對話框送出的那種），請改用
        send_inquiry；本工具只送純文字。

        ⚠ 對一段尚不存在的對話也照樣成立——不需要先有過對話紀錄；job_id + 一個
        從未聯繫過的候選人也能直接送出第一則訊息。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）。
            candidate_id: {{MESSAGING_CANDIDATE_ID_NOTE}}
            message: 訊息內容，純文字，沒有長度上限（此路由未量測到任何限制）。
                空白（含只有空白字元）會在送出前就被拒絕，不會浪費一次請求。

        每日上限由 MAX_DAILY_MESSAGES 控制（per 帳號，與 send_inquiry 共用）。

        ⚠ 若 MCP 客戶端在請求已送出後才逾時，這裡看不到任何回傳，但對方可能已經
        收到那則訊息——請改用 read_messages(job_nos=[job_id]) 或 104 後台確認，
        不要重送。

        回傳形狀是封閉集合：
          - {"success": False, "error": str} —— 確定沒有送出（candidate_id 位數
            守衛、訊息空白、或每日上限，三者都在送出前就被擋下）。
          - {"error": str}（節流拒絕時另外多帶 "retry_after_seconds": int）——
            這次請求沒有送出，可能是 104 自己明確拒絕（session 過期、遭封鎖、
            Cloudflare 挑戰、內容驗證失敗——後者會附上 104 逐欄位說明），也可能
            是節流擋下或內部設定錯誤——都是確定沒有送出。
          - {"sent": "confirmed", "message_id", "stream_id", "p_id", "event_id",
            "warnings": [...]} —— 104 的成功回應形狀已解析成功，訊息確定送達。
          - {"sent": "unconfirmed", "message": str, "warnings": [...]}（可能多帶
            "failed"）—— 104 受理了這次請求，但送達與否無法確認，**不要重送**，
            那正是最可能已經送達、只是本工具看不懂回應的情況。`sent` 只有
            "confirmed"／"unconfirmed" 兩個值，判斷請照值比對，不要用「不是
            unconfirmed 就是失敗」這種寫法。
        """
        app = ctx.request_context.lifespan_context

        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}

        if not message.strip():
            return {"success": False, "error": "訊息內容不可為空白"}

        # Initialised BEFORE the guard is entered — every pre-request abort
        # raises before the hook below ever runs, so a handler reading
        # either of these unconditionally must not raise UnboundLocalError
        # on the most ordinary path there is (an expired session). See
        # A control signal (send_attempted) gets its own variable
        # rather than being inferred from whether account_label happens to
        # be set — an empty account_label would otherwise mean "the hook
        # ran, the request may have gone out", the opposite of what an
        # unset local should mean here.
        account_label: str | None = None
        send_attempted = False

        async def _check_daily_cap(info: SessionInfo) -> None:
            nonlocal account_label, send_attempted
            # The SAME SessionInfo guarded_api just validated by identity —
            # not one resolved before queuing on the lock.
            account_label = info.account_label
            await _enforce_daily_cap(app, info)
            send_attempted = True  # LAST statement — proof the hook ran to completion

        body = _send_body(message)
        params = [("job_no", job_id), ("p_id", candidate_id)]
        try:
            async with guarded_api(
                ctx, ENDPOINTS["send_message"], params=params, body=body, before_request=_check_daily_cap,
            ) as (payload, info):
                await _log_send_attempt(app, info.account_label, candidate_id, ID_SOURCE_MESSAGE)
                await _maybe_mark_contacted(app, candidate_id, ID_SOURCE_MESSAGE, info.account_label)
                result = _build_success_send_result(payload)
                result["warnings"] = []
                return result
        except GuardAbort as e:
            return await _conclude_send(app, "send_message", e, send_attempted, account_label, candidate_id)
        except Exception as exc:
            return await _conclude_send(app, "send_message", exc, send_attempted, account_label, candidate_id)

    async def send_inquiry(
        ctx: Context, job_id: str, candidate_id: str, message: str, template_id: str | None = None,
    ) -> dict:
        """送出 104 的「詢問意願」事件——UI 上那個邀約對話框送出的東西，會立刻送到
        一位真實求職者手上、無法撤回，必須先讓真人看過內容再呼叫，沒有
        dry-run。純文字訊息請改用 send_message；未來量到其他招募事件（邀約面試、
        感謝函等）會各自另開一個新工具，不會加到本工具的參數上。

        一次呼叫送出**三個**請求（反向橋 → last-info → 事件本體），是本專案送出
        請求數最多的工具（get_candidate_photo／get_resume_attachment 各送兩個）；
        正常情況下幾秒內完成，最壞情況（三個子請求都逾時）約 50 秒。⚠ 若 MCP 客戶端在最後一個 POST 已送出後才逾時，這裡看
        不到任何回傳，但對方可能已經收到那封信——請改用
        read_messages(job_nos=[job_id]) 或 104 後台確認，不要重送。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）——這封信會掛在
                這個職缺底下。
            candidate_id: 與 send_message 完全一樣。{{MESSAGING_CANDIDATE_ID_NOTE}}
                事件本文實際要的履歷 idNo 由本工具自己用反向橋換算，呼叫端從頭到尾
                只需要認得這一個 id。
            message: 信件本文，純文字，**最多 1000 字元**（超過在送出前拒絕，
                一個請求都不送——這個上限來自 104 對話框介面，API 側真正的上限
                未量測，故往嚴的方向擋下）。與所選範本的內容完全脫鉤：送出的是
                這裡給的文字，不是範本的 description。
            template_id: 選填。省略時送出「不帶範本」的形狀（`templateId` 這個
                鍵仍然會送，值是空字串）；給了就原樣送出，104 自己判斷合不合法
                ——本工具不做範本類型查核。可用 list_templates 找一則範本的 id，
                但建議挑「詢問意願」類（type_id="1"）的範本、或乾脆不帶：帶其他
                類別的範本，104 會把這次送出記成哪一種事件尚未量測。

        每日上限由 MAX_DAILY_MESSAGES 控制（per 帳號，與 send_message 共用）。
        回傳形狀與 send_message 完全相同（"success": False、{"error": ...}、
        "sent": "confirmed"/"unconfirmed" 三大類，見 send_message 的說明）——
        兩者共用同一套判定與同一份程式碼。
        """
        app = ctx.request_context.lifespan_context

        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}
        if not message.strip():
            return {"success": False, "error": "訊息內容不可為空白"}
        if len(message) > MAX_INQUIRY_MESSAGE_LENGTH:
            return {
                "success": False,
                "error": (
                    f"訊息內容過長（{len(message)} 字元），上限 {MAX_INQUIRY_MESSAGE_LENGTH} 字元"
                    "（來源：104 對話框介面上限，API 側真正的上限未量測，故往嚴的方向擋下）"
                ),
            }

        # Same shape as send_message's account_label/send_attempted pair
        # — initialised before entering the guard, and send_attempted
        # is set only as the LAST statement of the SEND request's own
        # before_request hook (_before_third), never by _before_first: the
        # first two sub-requests are GETs that may fail for reasons that
        # have nothing to do with whether the letter went out.
        account_label: str | None = None
        send_attempted = False

        async def _before_first(info: SessionInfo) -> None:
            nonlocal account_label
            account_label = info.account_label
            await _enforce_daily_cap(app, info)

        async def _before_third(info: SessionInfo) -> None:
            nonlocal send_attempted
            await _enforce_daily_cap(app, info)
            send_attempted = True  # LAST statement — proof the send request is about to be issued

        try:
            async with guarded_sequence(ctx, slots_needed=3, before_first=_before_first) as (request, info):
                idno_payload = await request(
                    ENDPOINTS["resolve_candidate_idno"],
                    params=[("job_no", job_id), ("p_id", candidate_id)],
                    pick_data=("idNo",), pick_metadata=(),
                )
                id_no = idno_payload["data"]["idNo"]

                last_info_payload = await request(
                    ENDPOINTS["event_last_info"], pick_data=("emailCC",), pick_metadata=(),
                )
                email_cc = last_info_payload["data"]["emailCC"]
                if not isinstance(email_cc, list):
                    # Present but not a list — the projection only checks
                    # presence, the type check is this tool's own job.
                    # Aborts BEFORE the third request is ever issued;
                    # send_attempted is still False here.
                    raise ToolAbort(
                        {"error": "104 回應結構異常（emailCC 非陣列），可能是介面已變更，請回報"},
                        kind="malformed",
                    )

                willingness_body = _willingness_body(id_no, job_id, message, template_id, email_cc)
                payload = await request(
                    ENDPOINTS["send_willingness_event"], body=willingness_body, before_request=_before_third,
                )
                await _log_send_attempt(app, info.account_label, candidate_id, ID_SOURCE_MESSAGE)
                await _maybe_mark_contacted(app, candidate_id, ID_SOURCE_MESSAGE, info.account_label)
                result = _build_success_send_result(payload)
                result["warnings"] = []
                return result
        except GuardAbort as e:
            return await _conclude_send(app, "send_inquiry", e, send_attempted, account_label, candidate_id)
        except Exception as exc:
            return await _conclude_send(app, "send_inquiry", exc, send_attempted, account_label, candidate_id)

    # `MESSAGING_CANDIDATE_ID_NOTE` is read from the discovery module HERE, at
    # registration time — not via a module-level `from ... import` bound once at
    # import — so a caller (e.g. a test) that monkeypatches the constant BEFORE
    # `register_messaging_tools` runs sees the patched text reach all three
    # descriptions below. Same mechanism `tools/search.py`'s
    # `_SECOND_IDENTIFIER_NOTE` provenance relies on, applied at the call site
    # instead of at import (see this module's own docstring on why: three
    # registered tools must never let their candidate_id explanation drift from
    # this one constant, or from each other).
    note = discovery_mod.MESSAGING_CANDIDATE_ID_NOTE
    get_conversation.__doc__ = get_conversation.__doc__.replace("{{MESSAGING_CANDIDATE_ID_NOTE}}", note)
    send_message.__doc__ = send_message.__doc__.replace("{{MESSAGING_CANDIDATE_ID_NOTE}}", note)
    send_inquiry.__doc__ = send_inquiry.__doc__.replace("{{MESSAGING_CANDIDATE_ID_NOTE}}", note)

    mcp.tool()(require_login(get_conversation))
    mcp.tool()(require_login(send_message))
    mcp.tool()(require_login(send_inquiry))

    @mcp.tool()
    @require_login
    async def list_templates(ctx: Context, type_id: str | None = None, page: int = 1) -> dict:
        """列出這個帳號已存好的罐頭信件範本（走 JSON API；一次呼叫一個請求，
        不佔履歷瀏覽配額，但仍需要登入、仍過節流閘）。用來決定 send_inquiry 要帶
        哪一則範本，但不是它的必要前置步驟——send_inquiry 的 template_id 是選填
        的，完全不呼叫本工具也能送出詢問意願。

        Args:
            type_id: 選填，依範本的歸檔分類篩選——這是帳號自己整理範本用的分類，
                不是本專案選路由的依據（本輪只有一條事件路由）。已知六種：
                "1" 詢問意願、"2" 邀約面試、"3" 感謝函、"4" 到職日期提醒、
                "5" 邀性格測驗、"0" 不分類。省略時回全部範本。
            page: 頁碼，預設 1。

        成功時固定回傳 {"results": [{"id","title","description","type_id",
        "type_desc"}, ...], "pagination": {"page","total_pages","total"},
        "browse_limit": null, "warnings": [...]}——description 就是完整信件
        本文，不需要另外查單則範本。total_pages 大於 page 時 warnings 會提醒還有
        其餘頁面。失敗時回傳 {"error": str}，沒有 results 欄位。
        """
        params: list[tuple[str, str]] = [("page", str(page))]
        if type_id is not None:
            params.append(("typeId", type_id))
        try:
            async with guarded_api(ctx, ENDPOINTS["list_templates"], params=params) as (envelope, _info):
                return _build_template_list_response(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_templates: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)
