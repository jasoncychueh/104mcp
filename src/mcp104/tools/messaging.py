"""The three messaging tools, backed by 104's JSON API (no browser navigation) —
`read_messages` / `get_conversation` / `send_message`. This module was the last one
still reading 104 through the browser; it now follows tools/search.py's shape exactly:
`guarded_api` issues exactly one HTTP request per tool call, and every non-trivial
decision (body/param construction, row conversion, the send taxonomy) is a
module-level pure function, testable with no browser, no HTTP and no MCP `Context`.

Deliberately does NOT: issue the HTTP request, decide session/auth failures, or run
the request throttle — tools/helpers.py's `guarded_api` owns all three, exactly as it
does for tools/search.py's five read tools.

Nothing in this module imports `patchright` at all (not even under `TYPE_CHECKING`) —
the last browser-navigation call site (`send_message`'s DOM click sequence) is gone,
and with it the one thing that made this module's tests require a browser to collect.
"""
from __future__ import annotations

import logging

from mcp.server.fastmcp import Context, FastMCP

import mcp104.tools.discovery as discovery_mod
from mcp104.browser.api_client import ENDPOINTS
from mcp104.browser.session import SessionInfo
from mcp104.db.database import ID_SOURCE_MESSAGE
from mcp104.tools.discovery import _event_labels, _snake_case
from mcp104.tools.helpers import GuardAbort, MalformedResponseError, ToolAbort, guarded_api, require_login

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

def _convert(value):
    if isinstance(value, dict):
        return {_snake_case(k): _convert(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_convert(v) for v in value]
    return value


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


async def _log_send_attempt(app, account_label: str | None, candidate_id: str) -> None:
    """NOT pure — writes to SQLite, and wraps its own failure so that the verdict
    the caller already decided (`unconfirmed`/`ambiguous`) is never replaced by an
    unhandled database error. The rule: writing the log must never suppress the
    verdict, in either direction (§6c). One writer, called from all three sites
    that log — the tool body's own success path, the `except GuardAbort` handler
    when `_send_verdict` says ambiguous, and the `except Exception` handler below
    when the hook had already completed — so "which verdict logs" stays a single
    decision rather than a property distributed over however many handlers exist.
    """
    try:
        await app.db.log_sent(account_label, candidate_id, ID_SOURCE_MESSAGE)
    except Exception:
        log.error(
            "send_message: log_sent failed for candidate_id=%s (verdict itself is unaffected)",
            candidate_id, exc_info=True,
        )


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

    @mcp.tool()
    @require_login
    async def get_conversation(ctx: Context, job_id: str, candidate_id: str, page: int = 1) -> dict:
        """展開與特定候選人的完整對話紀錄（走 JSON API；一次呼叫一個請求，且不再
        送出已讀回報——這是純讀取，不會讓對方看到已讀，也不會清除 104 主控台上的
        未讀標記，見 CLAUDE.md）。

        Args:
            job_id: 職缺 id（來自 read_messages 回傳的對話列，或 list_jobs 的
                jobno——兩者已驗證是同一個 key space）。
            candidate_id: 候選人 id。同一個 job_id 會對應多個候選人，job_id +
                candidate_id 才能定位到唯一對話串。目前唯一可用的來源仍是
                read_messages 已存在的對話列。
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
        params = [("job_no", job_id), ("p_id", candidate_id), ("page", str(page)), ("perPage", "100"), ("sort", "ASC")]
        try:
            async with guarded_api(ctx, ENDPOINTS["get_conversation"], params=params) as (envelope, _info):
                return _build_conversation_response(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("get_conversation: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    @mcp.tool()
    @require_login
    async def send_message(ctx: Context, job_id: str, candidate_id: str, message: str) -> dict:
        """發送訊息給候選人（走 JSON API；一次呼叫一個請求）。job_id 可來自
        list_jobs 或 read_messages；candidate_id 目前唯一可用的來源仍是
        read_messages 已存在的對話列——本工具僅能回覆既有對話。

        Args:
            job_id: 職缺 id。
            candidate_id: 候選人 id（訊息系統的 id 空間，與 search_resumes 等工具
                回傳的 p_id 是否同一個 key space 雙向皆未量測，不可互相推導，見
                CLAUDE.md）。
            message: 訊息內容，純文字。空白（含只有空白字元）會在送出前就被拒絕，
                不會浪費一次請求。

        每日上限由 MAX_DAILY_MESSAGES 控制（per 帳號）。

        回傳三種形狀之一，且只有這三種——但第三種的「沒有送出」有多個成因，不是
        只有「104 拒絕」一種：
          - {"sent": "unconfirmed", "message": str} —— 104 受理了這次請求
            （包含 104 的回應無法辨識、但請求確實送出去了的情況）。104 送出後
            的成功畫面尚未實測，所以無法確認訊息真的送達，只能確認 104 收下了
            這次請求。不要因為「無法辨識」就重送——那正是最可能已經送達、只是
            我方看不懂回應的情況。
          - {"success": False, "error": str} —— 確定沒有送出（每日上限、或訊息
            內容為空白，兩者都在送出前就被擋下）。
          - {"error": str}（節流拒絕時另外多帶 "retry_after_seconds": int）——
            這次請求沒有送出，原因可能是 104 自己明確拒絕（session 過期、遭
            封鎖、Cloudflare 挑戰、找不到這個對話串、或內容驗證失敗——後者會
            附上 104 自己的逐欄位說明文字），也可能是請求根本沒有送到 104：
            本工具自己的節流保護擋下（這種情況額外帶 retry_after_seconds，
            告訴 Agent 等幾秒後再試）、或內部設定錯誤。三者對呼叫端的意義相同
            ——都是確定沒有送出——差別只在「誰拒絕的」與「要不要等待重試」。
        """
        app = ctx.request_context.lifespan_context

        if not message.strip():
            return {"success": False, "error": "訊息內容不可為空白"}

        # Initialised BEFORE the guard is entered — every pre-request abort
        # raises before the hook below ever runs, so a handler reading
        # either of these unconditionally must not raise UnboundLocalError
        # on the most ordinary path there is (an expired session). See
        # §6c: a control signal (hook_completed) gets its own variable
        # rather than being inferred from whether account_label happens to
        # be set — an empty account_label would otherwise mean "the hook
        # ran, the request may have gone out", the opposite of what an
        # unset local should mean here.
        account_label: str | None = None
        hook_completed = False

        async def _check_daily_cap(info: SessionInfo) -> None:
            nonlocal account_label, hook_completed
            # The SAME SessionInfo guarded_api just validated by identity —
            # not one resolved before queuing on the lock.
            account_label = info.account_label
            count = await app.db.get_daily_sent_count(info.account_label)
            if count >= app.config.max_daily_messages:
                raise ToolAbort(
                    {"success": False, "error": f"已達每日發送上限 {app.config.max_daily_messages} 則"},
                    kind="daily_cap",
                )
            hook_completed = True  # LAST statement — proof the hook ran to completion

        body = _send_body(message)
        params = [("job_no", job_id), ("p_id", candidate_id)]
        try:
            async with guarded_api(
                ctx, ENDPOINTS["send_message"], params=params, body=body, before_request=_check_daily_cap,
            ) as (_payload, info):
                await _log_send_attempt(app, info.account_label, candidate_id)
                return {"sent": "unconfirmed", "message": _SEND_UNCONFIRMED_MESSAGE}
        except GuardAbort as e:
            if _send_verdict(e.kind) == "ambiguous":
                await _log_send_attempt(app, account_label, candidate_id)
                return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE}
            # NOT SENT — the guard's own payload, unchanged, `error` key
            # intact (§6d): ERROR_CHALLENGE's text is the one place that
            # tells an Agent to stop for an hour, and re-wrapping it would
            # strip that instruction.
            return e.payload
        except Exception as exc:
            # A non-GuardAbort exception escaping guarded_api's locked
            # region (§6d) — a defensive catch-all, not a specific known
            # failure mode: credentials come from SessionInfo.cookies,
            # a plain attribute with nothing left to fail against
            # a dead browser. hook_completed is the ordering proof: the
            # hook runs strictly before the request, so hook_completed is
            # False only when execution never reached that point, i.e.
            # nothing was issued.
            log.error("send_message: 非預期例外 (hook_completed=%s): %s", hook_completed, exc, exc_info=True)
            if hook_completed:
                await _log_send_attempt(app, account_label, candidate_id)
                return {"sent": "unconfirmed", "message": _SEND_AMBIGUOUS_MESSAGE}
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出任何請求"}
