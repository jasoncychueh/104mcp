"""The five résumé/job read tools, backed by 104's JSON API (no browser navigation).

Answers: given an already-classified API payload (browser/api_client.classify()'s
success `payload`), how does it become the documented tool-facing response — which keys
does a route's own envelope carry, which fields survive into a row or a résumé detail, and
what does a caller-supplied `filters` dict turn into on the wire. Deliberately does not:
issue the HTTP request, decide session/auth failures, or run the request throttle —
tools/helpers.py's `guarded_api` owns all three. This module receives an already-classified
payload and turns it into the tool-facing dict, or (for filter validation and category
name resolution) refuses before any request is issued at all.

Every non-trivial decision below is a module-level function, mostly leading-underscore
(private to this module but directly importable for testing without a browser or an MCP
Context) — the same pattern tools/messaging.py's `_parse_dialog_detail` already
established, and the reason a test suite written blind to this module's own
implementation can still exercise its decisions without ever driving the MCP tool
wrapper.
"""

import html
import logging
import re

from mcp.server.fastmcp import Context, FastMCP

import mcp104.tools.categories as categories_mod
import mcp104.tools.discovery as discovery_mod
import mcp104.tools.drop_detection as drop_detection_mod
import mcp104.tools.filters as filters_mod
from mcp104.browser.api_client import ENDPOINTS
from mcp104.db.database import ID_SOURCE_RESUME
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    browse_limit_warning as _browse_limit_warning,
    convert_keys,
    extract_browse_limit as _extract_browse_limit,
    guarded_api,
    require_login,
)

log = logging.getLogger("104-mcp.search")

# ── Mechanical key conversion ────────────────────────────────────────────
#
# camelCase -> snake_case, applied recursively at every depth. This is the ONLY
# conversion any payload field goes through in this module: no field is renamed,
# merged, derived or omitted. It is not injective (two differently-cased source keys
# can collide on one snake_case name) — tests/test_search.py asserts no collision over
# the captured fixtures; this function does not guard against it itself, by design (a
# runtime guard here would be a second, untested place implementing the same promise
# the test already makes).
#
# `_snake_case` itself lives in tools/discovery.py and is applied here only
# through tools/helpers.py's `convert_keys`, never re-exported under this
# module's name: `describe_result_fields()` must key its payload on the SAME
# delivered names `_convert_resume_row` below actually produces, and the only way that
# can never drift is for every module to call the identical function rather than two
# independently-maintained copies of one regex.
# ── HTML -> plain text, applied to every string value delivered by the résumé tools ──
#
# Candidates write several résumé fields in 104's rich-text editor, and 104 returns
# those values as the editor's HTML verbatim — `<p>`, `<ol>/<li>`, `<span style=...>`,
# `&nbsp;` — on the list rows (`expJobArr[].expJobNote`, `remark`) and on the détail
# (`auto`, `introduction`, `workDesc`, `specialty[].desc`, `achievement[].desc`, the
# same `expJobNote`). Measured 2026-09-04 on the committed fixtures: the markup is
# ~17% of a 50-row search page and carries no information a caller can use — the
# Agent has no renderer, it re-presents the text to a human. Converting it to plain
# text here (block-level tags become line breaks, every other tag is dropped, entities
# are decoded) keeps the WORDS the candidate wrote and drops only the markup. Strings
# without any tag pass through byte-identical — the restricted-contact placeholders
# (tests T-16) and every code/date/e-mail value are untouched.
_HTML_TAG_RE = re.compile(r"<[A-Za-z/!][^>]*>")
_HTML_BLOCK_TAG_RE = re.compile(
    r"</?(?:p|div|br|li|ol|ul|tr|h[1-6]|table|blockquote)\b[^>]*>", re.IGNORECASE
)


def html_to_text(value: str) -> str:
    """Plain text for a rich-text field: block-level tags (`p`, `div`, `br`, `li`,
    `ol`/`ul`, `tr`, `h1`–`h6`, `table`, `blockquote`) become line breaks, every other
    tag is removed, HTML entities are decoded, `&nbsp;` becomes a space, and blank or
    whitespace-only lines are dropped. A string with no tag at all is returned as-is
    (not even entity-decoded), so values 104 never wrote as HTML stay byte-identical.
    """
    if not _HTML_TAG_RE.search(value):
        return value
    text = _HTML_BLOCK_TAG_RE.sub("\n", value)
    text = _HTML_TAG_RE.sub("", text)
    text = html.unescape(text).replace("\xa0", " ")
    lines = (line.strip() for line in text.split("\n"))
    return "\n".join(line for line in lines if line)


def _convert(value):
    """Recursively convert every dict key at every depth via _snake_case; list elements
    are walked one by one; string values go through `html_to_text` (a no-op for strings
    without markup); every other value (int, bool, None, ...) passes through completely
    unchanged. Used for both the détail response (every field, mechanically) and the
    nested structures inside a row (e.g. expJobArr) once the row's own top-level
    allow-list has already picked which fields to keep.

    The walk itself is tools/helpers.py's `convert_keys` — this module's one
    remaining contribution is the string transform, which is exactly what
    distinguishes it from tools/messaging.py's use of the same walk.
    """
    return convert_keys(value, string_transform=html_to_text)


# ── Résumé row conversion (search / recommend / match) ──────────────────
#
# 104 sends 35 fields on a résumé row across the three list routes: recommend and
# match send exactly the same 32; search sends those 32 plus masterUrl, nationality
# and plastActionDateDesc [M docs/104-site-facts.md §6b.3b — verified 2026-08-14
# against tests/fixtures/rows_{search,recommend,match}.json]. 34 of them are forwarded:
# `masterUrl` (the résumé page's own URL, carrying a percent-encoded copy of the whole
# query) is deliberately NOT — it is only openable by a browser that holds the very
# session this process holds, so no human the Agent talks to can follow it, and its
# ~500 characters per row bought nothing (account holder's decision, 2026-09-04). An
# ALLOW-LIST, not "every field present": unlike the détail response (every field,
# however many 104 adds later, mechanically), a row is a small, previously-measured
# set — a field 104 adds to a row tomorrow is deliberately NOT surfaced until someone
# has looked at it, rather than silently appearing with nobody having vetted it for
# size or content.
#
# The allow-list itself, and its glosses, live in tools/discovery.py's
# RESUME_ROW_FIELD_GLOSS — moved out of this module (not merely referenced from it) so
# the list and the prose explaining what each field means move in one edit, and so
# describe_result_fields() has one definition to generate from, not a second hand-typed
# copy. `k in _RESUME_ROW_FIELDS` below keeps working exactly as it did against the old
# bare frozenset: a dict tests membership against its keys.
_RESUME_ROW_FIELDS = discovery_mod.RESUME_ROW_FIELD_GLOSS


def _convert_resume_row(raw: dict) -> dict:
    """One row from search/recommend/match -> the tool-facing dict.

    idNo -> candidate_id is the single justified rename in this whole feature,
    preserving the existing candidate id space so nothing in the database changes.
    Every other field keeps its 104 name, mechanically snake_cased, never renamed.
    pId is a SECOND, distinct identifier the row also carries — returned and
    documented, never bridged to candidate_id: this row's candidate_id (idNo) is
    for get_resume_detail only. p_id IS the messaging system's own pId, measured
    (6 bit-identical samples, docs/104-site-facts.md §8.12/§8.14/§8.17), and is
    usable directly as the candidate_id argument to send_message/send_inquiry/
    get_conversation. The other direction (idNo -> pId) is still underived —
    when the event route needs an idNo, send_inquiry resolves it itself via a
    separate, measured p_id -> idNo bridge (a dedicated lookup, not a derivation
    from idNo).
    """
    picked = {k: v for k, v in raw.items() if k in _RESUME_ROW_FIELDS}
    if isinstance(picked.get("expJobArr"), list):
        picked["expJobArr"] = [_compact_exp_job(entry) for entry in picked["expJobArr"]]
    converted = _convert(picked)
    if "id_no" in converted:
        converted["candidate_id"] = converted.pop("id_no")
    return converted


def _compact_exp_job(entry):
    """One expJobArr entry with its empty fields removed. An entry carries 19 fields
    and, on the committed fixtures (436 entries, 2026-09-04), 8 of them are empty on
    most jobs (`expFirmLogo`, `expManagementDesc`, `expWage*`, `expJobSkill*`, ...):
    forwarding `"exp_wage_year": null` for 4 jobs × 50 rows was ~30k characters of a
    ~230k page saying nothing. "Empty" is what 104 itself uses for "not filled in" —
    None, "" and [] all occur on the fixtures, nothing else stands in for absence; 0
    and False are values, not absence, and are kept. Row-level fields are NOT
    compacted — a row's key set stays stable and fully published by
    describe_result_fields(); only these nested per-job objects, whose keys are 104's
    own and vary by what the candidate filled in, drop their empties. A non-dict entry
    (never observed) passes through untouched rather than raising.
    """
    if not isinstance(entry, dict):
        return entry
    return {k: v for k, v in entry.items() if not _is_empty_exp_job_value(v)}


def _is_empty_exp_job_value(value) -> bool:
    # Type-by-type on purpose: `value in (None, "", [])` would also be True for
    # 0 == False-style coincidences the moment a comparable sentinel is added.
    return value is None or value == "" or (isinstance(value, list) and not value)


# ── Route-specific container/page extraction ─────────────────────────────
#
# Three separate functions, deliberately not one shared reader, even though search and
# recommend currently use identical key names today: match's row list lives under
# 'resumes', not 'data', and its current-page key is 'thisPage' (camelCase, no
# underscore), not 'this_page' — a shared reader written against the other two routes'
# names would read None on match and report an empty result instead of raising. A
# missing container/page key is always an error (MalformedResponseError, imported
# from tools/helpers.py — see that class's own docstring for why it lives there); a
# container that is present but empty is a legitimate zero-row success and never raises.

def _extract_search_rows(result) -> tuple[list[dict], dict]:
    """search_resumes' own container/page keys: result.data (list) and
    result.pageInfo.{this_page,total_page,total} [M §6b.3b]."""
    if not isinstance(result, dict):
        raise MalformedResponseError("result 缺失或非物件")
    if not isinstance(result.get("data"), list):
        raise MalformedResponseError("result.data 缺失或非陣列")
    page_info = result.get("pageInfo")
    if not isinstance(page_info, dict):
        raise MalformedResponseError("result.pageInfo 缺失或非物件")
    return result["data"], {
        "page": page_info.get("this_page"),
        "total_pages": page_info.get("total_page"),
        "total": page_info.get("total"),
    }


def _extract_recommend_rows(result) -> tuple[list[dict], dict]:
    """list_recommended_resumes' own container/page keys — the SAME names as search's
    today [M §6b.3b], kept as its own function rather than shared with
    _extract_search_rows: match (below) proves the three routes are not guaranteed to
    agree with each other, and a shared reader assuming otherwise is exactly the failure
    mode this module's own section comment above calls out.
    """
    if not isinstance(result, dict):
        raise MalformedResponseError("result 缺失或非物件")
    if not isinstance(result.get("data"), list):
        raise MalformedResponseError("result.data 缺失或非陣列")
    page_info = result.get("pageInfo")
    if not isinstance(page_info, dict):
        raise MalformedResponseError("result.pageInfo 缺失或非物件")
    return result["data"], {
        "page": page_info.get("this_page"),
        "total_pages": page_info.get("total_page"),
        "total": page_info.get("total"),
    }


def _extract_match_rows(result) -> tuple[list[dict], dict]:
    """list_matched_resumes' own container/page keys — DELIBERATELY DIFFERENT from the
    other two routes: the row list is result.resumes (not result.data), and the current
    page is result.pageInfo.thisPage (camelCase, no underscore — not this_page) [M
    §6b.3b]. A reader assuming the other two routes' names would read None here and
    silently report an empty result instead of raising.
    """
    if not isinstance(result, dict):
        raise MalformedResponseError("result 缺失或非物件")
    if not isinstance(result.get("resumes"), list):
        raise MalformedResponseError("result.resumes 缺失或非陣列")
    page_info = result.get("pageInfo")
    if page_info is None and "pageInfo" in result and result["resumes"] == []:
        # 量到的零筆形狀（2026-09-04，updateDateType=2 當日零筆，§8.22）：resumes 是
        # 空陣列、pageInfo 這個鍵在、值是 null。這是合法的空結果，不是結構異常——
        # 判成異常會讓「今天沒有配對」看起來像故障。page 由呼叫端補上（見
        # _build_match_response）。
        return [], {"page": None, "total_pages": 0, "total": 0}
    if not isinstance(page_info, dict):
        raise MalformedResponseError("result.pageInfo 缺失或非物件")
    return result["resumes"], {
        "page": page_info.get("thisPage"),
        "total_pages": page_info.get("total_page"),
        "total": page_info.get("total"),
    }


# ── browse_limit ("quota") + threshold warning ───────────────────────────
#
# `_extract_browse_limit` / `_browse_limit_warning` are imported from
# tools/helpers.py (bound to those names above, unchanged behaviour): every
# tool whose 104 route carries `browseLimit` needs them, including
# tools/resume_files.py, which shares nothing else with this module. See
# helpers.MalformedResponseError's docstring for the precedent that decides
# where a helper shared by two tool modules lives.


# ── applied_filters ───────────────────────────────────────────────────────

def _build_applied_filters(submitted: list[tuple[str, str]]) -> dict:
    """{wire_param: value}: keyed by 104's own wire parameter name (the `[]` array
    suffix stripped — the array suffix is a wire encoding, not part of the parameter's
    identity), value the RESOLVED string(s) actually sent — never the caller's own
    filter-dict key, and never the name they typed for a dataset-backed condition (the
    resolved code is the only evidence of what was actually searched). A repeated
    parameter's several values collect into a list, in submission order. Grouping is
    `tools.filters.group_by_wire_param` — the same canonical-name rule `detect_dropped`
    uses to fold `sent`'s repeated pairs back together, imported rather than
    re-implemented so the two never drift apart on what a parameter's identity is.

    `submitted` is everything actually sent on the wire for this call — every filter
    pair `encode_filters` produced, plus `kws` and `page` — not just the filter pairs:
    an applied_filters that only ever saw the filter pairs would never carry the one
    parameter that determines the entire result set.
    """
    grouped = filters_mod.group_by_wire_param(submitted)
    return {k: (v[0] if len(v) == 1 else v) for k, v in grouped.items()}


# ── Prefix-family warnings ────────────────────────────────────────────────

def _prefix_family_warnings(caller_filters: dict) -> list[str]:
    """One warning per dataset-backed `filters` key whose resolved value is also a
    proper prefix of longer sibling category names — an exact hit that is also a proper
    prefix of other, longer category names resolves rather than refusing, and this
    names those longer candidates in the warnings list instead of hiding them. Re-
    resolves rather than reading encode_filters' return value — encode_filters
    reports only failure, by design; see its own docstring for why. Only meaningful
    AFTER encode_filters has already succeeded once for the same filters (every
    dataset-backed key is then known to resolve, so this never itself raises).

    A sibling that the resolver would itself refuse if the caller re-issued with its
    name is marked with `categories_mod.NON_TERMINAL_FAMILY_MARK_ZH` — and ONLY that
    one, never every sibling: this is prose, not the structured `candidates` payload
    `_filter_error_payload` builds, so an absent mark here carries no signal (nothing
    reads "not marked" as "confirmed selectable"), while marking every selectable
    sibling would bury the rare unselectable one in noise around it. The two consumers
    are deliberately not unified — see `tools.categories.NON_TERMINAL_FAMILY_MARK_ZH`'s
    own comment for why a shared string would quietly re-couple them.
    """
    warnings: list[str] = []
    for key, raw_value in (caller_filters or {}).items():
        condition = filters_mod.CONDITIONS.get(key)
        if condition is None or condition.value_source != "dataset":
            continue
        values = raw_value if isinstance(raw_value, (list, tuple, set)) else [raw_value]
        # condition.key alongside condition.dataset: branch-code acceptance is measured
        # per (file, condition) pair, never inherited by a different condition on the
        # same file (categories._BRANCH_ACCEPTANCE's own comment has the full account).
        dataset = categories_mod.load_dataset(condition.dataset, condition.key)
        for query in values:
            resolution = categories_mod.resolve(dataset, str(query))
            if resolution.status == "resolved" and resolution.candidates:
                siblings = "、".join(
                    f"{c.name}({c.code})"
                    + ("" if c.terminal else categories_mod.NON_TERMINAL_FAMILY_MARK_ZH)
                    for c in resolution.candidates
                )
                warnings.append(
                    f"篩選 '{key}' 的 '{query}' 精確符合一個分類，但也是下列較長同字首"
                    f"分類名稱的字首：{siblings}。若原意是指更廣的範圍，這次搜尋不會"
                    "包含它們。"
                )
    return warnings


# ── Tombstoned search_resumes arguments ───────────────────────────────────

_TOMBSTONE_REPLACEMENTS = {
    "area": "filters={'city': '<地區名稱>'}（工作地）或 filters={'home': '<地區名稱>'}（居住地）",
    "experience": "filters={'work_exp_time': {'mode': 'down'|'up'|'to'|'none'|'all', 'min': ..., 'max': ...}}（年資）",
    "education": "filters={'edu': [<學歷代碼>, ...]}",
}


def _tombstone_error(name: str) -> dict:
    return {
        "error": (
            f"參數 '{name}' 已停用：不會套用、也不會送出任何請求（見 CLAUDE.md）。"
            f"請改用：{_TOMBSTONE_REPLACEMENTS[name]}"
        )
    }


def _check_tombstones(area, experience, education) -> dict | None:
    for name, value in (("area", area), ("experience", experience), ("education", education)):
        if value is not None:
            return _tombstone_error(name)
    return None


# ── Filter/category error -> response payload ─────────────────────────────

def _filter_error_payload(exc: filters_mod.FilterError) -> dict:
    """Covers an unknown filter key, a composite companion/slot-count violation, and an
    ambiguous or unresolvable category name. UnknownFilterKeyError and
    CompositeValidationError already carry the valid-key list / the reason in their own
    __str__; only CategoryResolutionError needs its candidates surfaced separately
    (never as an empty "candidates": [] on a total miss — an empty container on a
    failure shape would look like a successful empty result).

    A candidate carries `code`, `name` and `terminal`, all three present on every entry
    unconditionally — `terminal` is not omitted even when every candidate in one response
    shares its value, because a key that comes and goes is a shape that varies within one
    tool. `tools.categories.resolve()` tries a direct code match before any name tier, so
    either `code` or `name` may be passed straight back into `filters` — the round trip
    this exists for (an ambiguity refusal hands back candidates, and the caller picks
    one) works with the code as reliably as with the exact decorated name. `terminal` is
    what makes that choice possible at all when two candidates share a name byte for
    byte (see `tools.categories.Candidate`'s own docstring for how often that happens):
    without it the caller would be choosing between two indistinguishable labels with
    nothing but an opaque code to tell them apart, which is the exact failure returning
    structured candidates instead of bare codes exists to prevent, reproduced one field
    short of where it was fixed.
    """
    if isinstance(exc, filters_mod.CategoryResolutionError):
        payload: dict = {"error": str(exc)}
        if exc.resolution.candidates:
            payload["candidates"] = [
                {"code": c.code, "name": c.name, "terminal": c.terminal}
                for c in exc.resolution.candidates
            ]
        return payload
    return {"error": str(exc)}


def _malformed_response_payload(exc: MalformedResponseError) -> dict:
    return {"error": f"104 回應結構異常（{exc.detail}），可能是介面已變更，請回報"}


class DroppedFiltersError(Exception):
    """A compared condition's echo disagreed with what was sent — raised by
    _build_search_response, never returned as an empty result.
    """

    def __init__(self, dropped: list[str], total):
        self.dropped = dropped
        self.total = total
        super().__init__(f"dropped={dropped} total={total}")


def _dropped_filters_payload(exc: DroppedFiltersError) -> dict:
    return {
        "error": (
            f"104 未套用下列篩選條件：{', '.join(exc.dropped)}（實際回報筆數："
            f"{exc.total}）。這不是暫時性問題，原樣重送不會改變結果，請調整或移除這些"
            "篩選後再試。"
        )
    }


# ── Pure response builders ────────────────────────────────────────────────
#
# Each takes an already-classified payload (browser/api_client.classify()'s Verdict.
# payload) and returns the tool-facing dict, or raises one of the exceptions above. No
# I/O, no session, no ctx — directly testable against a captured/fixture payload.

def _build_search_response(result: dict, submitted: list[tuple[str, str]], caller_filters: dict) -> dict:
    """`submitted` is every wire parameter this call actually sent — kws, the filter
    pairs `encode_filters` produced, and page — not just the filter pairs. `kws` is a
    compared condition (`CONDITIONS["kws"].echo_evidence == "echoed"` and 104 does echo
    it back), so leaving it out of what reaches detect_dropped/_build_applied_filters
    would mean the one parameter that determines the entire result set is never checked
    and never reported. `page` rides along too; its own table entry
    (`echo_evidence == "not-echoed"`) is what excludes it from the drop comparison, not
    any filtering done here — a parameter is either compared or carries a table entry
    saying why not, and that only holds if the comparison actually sees every parameter
    submitted.
    """
    raw_rows, pagination = _extract_search_rows(result)
    search_form = result.get("searchForm") or {}
    dropped = drop_detection_mod.detect_dropped(submitted, search_form)
    if dropped:
        raise DroppedFiltersError(dropped, pagination.get("total"))

    warnings = _prefix_family_warnings(caller_filters)
    browse_limit = _extract_browse_limit(result)
    quota_warning = _browse_limit_warning(browse_limit)
    if quota_warning:
        warnings.append(quota_warning)

    return {
        "results": [_convert_resume_row(row) for row in raw_rows],
        "pagination": pagination,
        "browse_limit": browse_limit,
        "warnings": warnings,
        "applied_filters": _build_applied_filters(submitted),
    }


def _build_recommend_response(result: dict) -> dict:
    raw_rows, pagination = _extract_recommend_rows(result)
    browse_limit = _extract_browse_limit(result)
    warnings = []
    quota_warning = _browse_limit_warning(browse_limit)
    if quota_warning:
        warnings.append(quota_warning)
    return {
        "results": [_convert_resume_row(row) for row in raw_rows],
        "pagination": pagination,
        "browse_limit": browse_limit,
        "warnings": warnings,
    }


def _build_match_response(result: dict, requested_page: int | None = None) -> dict:
    raw_rows, pagination = _extract_match_rows(result)
    if pagination.get("page") is None and requested_page is not None:
        pagination["page"] = requested_page
    browse_limit = _extract_browse_limit(result)
    warnings = []
    quota_warning = _browse_limit_warning(browse_limit)
    if quota_warning:
        warnings.append(quota_warning)
    return {
        "results": [_convert_resume_row(row) for row in raw_rows],
        "pagination": pagination,
        "browse_limit": browse_limit,
        "warnings": warnings,
    }


def _build_job_list_response(envelope) -> dict:
    """`envelope` is list_jobs' already-classified family-B payload — the WHOLE
    {data, metadata} envelope, not just `data`. `data` is the row list; `metadata`
    carries the server's OWN pagination: {total, page, hasSubDepartment} as measured on
    both job routes [M research/captures/reco_match_bodies.json]. `page`/`total` are
    therefore the server's own reported values, not an echo of what the caller asked
    for. `total_pages` is reported as null rather than guessed: the page size itself is
    unmeasured, so it is not derivable from total+page, and this route is the
    authoritative source for jobno — reporting a wrong total_pages would tell a caller
    the list is complete when it may not be. browse_limit is always null; no quota
    figure exists on this route to report.
    """
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    results = [_convert(row) for row in envelope["data"]]
    return {
        "results": results,
        "pagination": {
            "page": metadata.get("page"),
            "total_pages": None,
            "total": metadata.get("total"),
        },
        "browse_limit": None,
        "warnings": [],
    }


_RESTRICTED_CONTACT_WARNING = (
    "此履歷的聯絡方式受限（contact_privacy = 'private'）：email / phone_desc_all / "
    "call_time_desc 是 104 填入的人類可讀佔位文字，不是真實聯絡資料（call_time_desc "
    "在受限履歷上甚至比未受限履歷的同欄位更長 —— 長度或是否為空都不能拿來判斷是否被遮"
    "蔽）；只有 phone 這個巢狀物件是誠實留空，不是佔位文字。請勿把上述受限欄位當成真實"
    "聯絡方式使用、儲存或回報給使用者；唯一可靠的依據是 contact_privacy 本身。"
)


def _build_resume_detail_response(envelope: dict) -> dict:
    """`envelope` is get_resume_detail's already-classified family-B payload — the WHOLE
    {data, metadata} envelope. `envelope["data"]` is guaranteed, by
    browser/api_client.classify(), to carry a truthy `resume` and a falsy `error`. Every
    field of data['resume'] is returned, mechanically converted, at every depth — no
    field renamed, merged, derived or omitted — under its own "resume" key, a sibling of
    browse_limit and warnings, matching every other tool's shape (results/rows in their
    own named container beside browse_limit/warnings) and matching 104's own envelope
    (browseLimit is a SIBLING of the résumé object there too, not a peer of the résumé's
    own fields).

    Nested rather than flattened: flattening the 113 résumé fields into the top level
    would put 104's field namespace and ours in the same dict, and 104 owns their half
    with no stability guarantee. There is no collision today (the measured 113 fields
    include neither "warnings" nor "browse_limit", not even a near miss) — but a flat
    dict's collision would be silent, data-dependent AND construction-order-dependent
    (whichever key is assigned last wins), and would land on the tool that carries the
    masking warning this whole feature exists to surface. Nesting removes the collision
    surface entirely rather than relying on today's field list staying disjoint from
    ours.
    """
    data = envelope["data"]
    resume = data["resume"]
    converted = _convert(resume)

    warnings = []
    if resume.get("contactPrivacy") == "private":
        warnings.append(_RESTRICTED_CONTACT_WARNING)
    browse_limit = _extract_browse_limit(data)
    quota_warning = _browse_limit_warning(browse_limit)
    if quota_warning:
        warnings.append(quota_warning)

    return {
        "resume": converted,
        "browse_limit": browse_limit,
        "warnings": warnings,
    }


# ── Docstrings, generated from tools/filters.py's CONDITIONS so the filter-key ─────
# ── reference never restates (and so drifts from) the single source of the table. ──
#
# These become each tool's ACTUAL __doc__ (via _register_tool, below the tool
# definitions) — never a parallel description= argument the docstring itself doesn't
# carry; see _register_tool's docstring for why that distinction is the point, not a
# style preference. They cannot be written as literal `"""..."""` docstrings in the
# function bodies themselves because a docstring Python recognises must be a
# compile-time string constant — the first statement in a function body — and this
# text is a runtime f-string built from CONDITIONS.

def _filter_key_reference() -> str:
    """A BARE per-key reference — dataset key/name-vs-code, or a one-word value-source
    marker — deliberately shorn of every value domain (enum's labelled dict, composite's
    structure dict). Those domains totalled 3,343 chars across 33 keys before this
    change, 1,463 of them (133 chars × 11) a single sentence — `categories_mod.
    RESOLVE_ACCEPTS_ZH` — repeated verbatim once per dataset-backed key; both are why
    search_resumes' published description was 5,031 chars against Claude Code's
    measured 2,048-char slice (see CLAUDE.md) and lost 59% of itself, invisibly, before
    this fix — see `_SEARCH_RESUMES_DESCRIPTION` below for where `RESOLVE_ACCEPTS_ZH`
    is now stated exactly once, ahead of this list, rather than per key.

    Every domain this used to spell out inline is still fully available — from
    `browse_filter_values`, `tools/discovery.py`'s zero-request/zero-quota discovery
    tool, pointed at from `_SEARCH_RESUMES_DESCRIPTION` — so nothing here is information
    lost, only moved to a tool with room to state it in full instead of once per key.
    """
    items = []
    for condition in sorted(filters_mod.CONDITIONS.values(), key=lambda c: c.key):
        if condition.provenance != "filter-key" or not condition.shippable:
            continue
        if condition.value_source == "dataset":
            desc = f"名稱/代碼-{condition.dataset}"
        elif condition.value_source == "codes-only":
            desc = "代碼-無資料集"
        elif condition.value_source == "enum":
            desc = "代碼"
        elif condition.value_source == "composite":
            desc = "結構化"
        elif condition.value_source == "numeric":
            desc = "數字"
        else:  # free-text
            desc = "文字"
        items.append(f"{condition.key}({desc})")
    return "、".join(items)


_FILTER_KEY_REFERENCE = _filter_key_reference()

# Moved to tools/discovery.py (SECOND_IDENTIFIER_NOTE) so search_resumes' docstring and
# describe_result_fields' generated payload read the identical text, never two
# hand-synchronised copies — see discovery.py's own module docstring for why the import
# direction runs this module -> discovery.py, never the reverse.
_SECOND_IDENTIFIER_NOTE = discovery_mod.SECOND_IDENTIFIER_NOTE

# What a list row deliberately is NOT (2026-09-04): the same note on all three list
# tools, so an Agent reading any one of them knows where the rest of the résumé lives.
_ROW_NOTE = (
    "列上不含 master_url（需登入的網頁連結，使用者開不了）；exp_job_arr 略過空欄位、"
    "HTML 已轉純文字；完整履歷用 get_resume_detail。"
)

_SEARCH_RESUMES_DESCRIPTION = f"""搜尋 104 履歷（走 JSON API，不再解析頁面 DOM）。

Args:
    keyword: 關鍵字，對應 104 的 kws 參數，必填。
    filters: 選填的篩選條件字典。合法鍵如下；各鍵完整值域/資料集/結構/名稱解析規則
        請呼叫 browse_filter_values(key) 查詢（零請求、零配額）：
{_FILTER_KEY_REFERENCE}
        未知的鍵、composite 條件缺欄位或槽位數超過上限，皆在送出前就拒絕。
    page: 頁碼，預設 1。
    area / experience / education: 已停用（見下）。

⚠ area/experience/education 已停用，傳入非 None 值會回傳錯誤（附替代寫法），不送出請求。

⚠ expect_pay.month 的 'up' 模式在真實資料上幾乎沒有篩選效果（比對的是期望薪資『下限』，
幾乎每個人下限都 ≥ 1 萬），但仍會正確送達、不報錯。

{_SECOND_IDENTIFIER_NOTE}
{_ROW_NOTE}

成功時固定回傳 {{"results": [...], "pagination": {{"page","total_pages","total"}},
"browse_limit": {{"resume_max","on_that_day_count"}}|null, "warnings": [...],
"applied_filters": {{104 的 wire 參數名: 實際送出的值}}}}。total 與 total_pages 是不同
的量，收窄篩選才能縮小結果集；applied_filters 是實際送到 104 的值（已解析為代碼）。

失敗時（未知篩選鍵、composite 驗證失敗、名稱無法唯一解析、104 回報篩選被丟棄、
session 已過期等）回傳 {{"error": str}}，沒有 results 欄位；名稱無法唯一解析時另外會帶
"candidates": [{{"code","name","terminal"}}, ...]（僅在確實有候選項目時出現），這是
刻意的設計 —— 不會替你猜一個代碼。三個鍵每筆候選都一定存在，code 與 name 皆可直接填回
同一個篩選鍵：{categories_mod.CANDIDATE_TERMINAL_ZH}。
"""

_LIST_RECOMMENDED_DESCRIPTION = f"""列出某個職缺「104 推薦」的履歷清單（走 JSON API）。

Args:
    jobno: 職缺代碼，必填 —— 權威來源是 list_jobs() 回傳每筆結果的 jobno 欄位。
    page: 頁碼，預設 1。

未帶 jobno（或職缺不存在）時 104 回傳 HTTP 400，本工具轉譯為
{{"error": "...缺少必要參數..."}}，不是空結果。

⚠ 職缺「已關閉」在這條路由與 list_matched_resumes 的行為不一樣：本工具（推薦）對一個
沒有推薦結果的職缺，回傳的是合法的空結果（{{"results": [], ...}}），不是錯誤；
list_matched_resumes 對已關閉的職缺則是 HTTP 404 + 錯誤訊息（見該工具說明）。兩者的差
異不能互相推論。

{_SECOND_IDENTIFIER_NOTE}
{_ROW_NOTE}

成功時固定回傳 {{"results": [...], "pagination": {{"page","total_pages","total"}},
"browse_limit": {{"resume_max","on_that_day_count"}}|null, "warnings": [...]}}——不論
結果是否為空，形狀都相同。失敗時回傳 {{"error": str}}，沒有 results 欄位。
"""


_LIST_MATCHED_DESCRIPTION = f"""列出某個職缺「配對」的履歷清單（走 JSON API）。

Args:
    jobno: 職缺代碼，必填 —— 權威來源是 list_jobs() 回傳每筆結果的 jobno 欄位。
    page: 頁碼，預設 1。
    update_date_type: 選填，字面透傳到 104 的 updateDateType（不做任何映射）。
        "2" 已量測＝本日更新（即 104 網頁的「當日配對」分頁）；省略＝全部配對。
        其餘代碼（1、3–8，與搜尋篩選鍵 updateDateType 同一套詞彙）在這條路由
        未量測，不驗證、不猜測。只有空白字串在送出前就被拒絕。

未帶 jobno 時 104 回傳 HTTP 400，本工具轉譯為 {{"error": "...缺少必要參數..."}}。

⚠ 職缺「已關閉」時，這條路由回傳 HTTP 404 附 104 自己的說明文字，本工具轉譯為
{{"error": "104 回報找不到資料：..."}}，不是空結果 —— 與 list_recommended_resumes 對
「已關閉/無推薦」職缺回傳合法空結果的行為不同，兩者不能互相推論。

配對清單是推薦清單的子集（switch 為 on 的職缺）；配對回傳的欄位與推薦完全相同。

{_SECOND_IDENTIFIER_NOTE}
{_ROW_NOTE}

成功時固定回傳 {{"results": [...], "pagination": {{"page","total_pages","total"}},
"browse_limit": {{"resume_max","on_that_day_count"}}|null, "warnings": [...]}}——不論
結果是否為空，形狀都相同。失敗時回傳 {{"error": str}}，沒有 results 欄位。
"""


_GET_RESUME_DETAIL_DESCRIPTION = f"""取得特定候選人的完整履歷資料（走 JSON API）。

Args:
    candidate_id: 候選人 id —— search_resumes / list_recommended_resumes /
        list_matched_resumes 回傳結果的 candidate_id（也就是 104 的 idNo）。用於狀態
        紀錄時的 id_source 為 "resume"。

成功時固定回傳 {{"resume": {{...}}, "browse_limit": {{"resume_max","on_that_day_count"}}|null,
"warnings": [...]}}。resume 底下是伺服器提供的每一個履歷欄位（113 個：基本資料、聯絡方式、
學歷、工作經歷含每份工作的說明、自傳、專長、成就、附件清單、求職條件——與 104 網頁詳情頁
讀的是同一份 JSON），鍵一律機械式轉為 snake_case，不重新命名、不合併、不省略任何欄位
（包含巢狀物件與陣列內部）；候選人用富文字編輯器填的值（auto、introduction、work_desc、
exp_job_arr[].exp_job_note、specialty[].desc、achievement[].desc）104 給的是 HTML，本工具
已轉成純文字（段落與清單項目以換行分隔），鍵與欄位數不變。列（row）上沒有的細節都在這裡，
要呈現給使用者請讀這個工具，不要引用任何 104 網址（Agent 的登入不能給網頁用）。——包括
resume 自己的 id_no（原始 idNo）：這裡不套用 candidate_id 的重新命名規則，因為 rows
的 candidate_id 命名規則是為了保留既有資料庫的候選人 id 空間，而這裡的欄位就是履歷
本身的欄位，不是卡片列。resume 巢狀在自己的鍵之下，不與 browse_limit / warnings 混在
同一層——104 自己的欄位命名空間由 104 決定、沒有穩定性保證，攤平會讓兩邊的鍵共處一個
dict，一旦未來真的撞名，哪一個鍵勝出完全取決於組裝順序，而且會靜默發生在恰好帶著遮蔽提醒的
那次呼叫上；巢狀讓這個風險完全不存在，也與另外四個工具「結果放在自己的容器鍵、與
browse_limit/warnings 同層」的形狀一致，同時更貼近 104 自己的信封（browseLimit 在
104 那邊本來就是履歷物件的同層欄位，不是它的一部分）。

⚠⚠ {_RESTRICTED_CONTACT_WARNING}

{_SECOND_IDENTIFIER_NOTE}（此處欄位名稱為 p_id，位於 resume 內）

失敗時回傳 {{"error": str}}。
"""


_LIST_JOBS_DESCRIPTION = """列出本公司職缺清單 —— 這是 jobno 的權威來源（走 JSON API）。

104 的對話與履歷清單都是「候選人 × 職缺」的組合：list_recommended_resumes /
list_matched_resumes / send_message / get_conversation 都需要 jobno。標準流程：

    search_resumes / list_recommended_resumes / list_matched_resumes
        → 找到候選人 (candidate_id)
    list_jobs
        → 選定要查詢/邀約的職缺 (jobno)
    list_recommended_resumes(jobno) / list_matched_resumes(jobno) / send_message(jobno, candidate_id, message)

Args:
    page: 頁碼，預設 1，會送到伺服器。

pagination.page 與 pagination.total 是伺服器自己回報的值（metadata.page /
metadata.total），不是把呼叫端傳入的 page 原樣回顯。⚠ pagination.total_pages 固定為
null，不是量測值也不是猜測值：伺服器沒有回報每頁筆數，僅有 total/page 兩個數字時無法
換算出總頁數，本工具不會用一個未經驗證的假設（例如假設某個常見頁面大小）去填這個欄
位——本路由是 jobno 的權威來源，回報一個錯誤的 total_pages 會讓呼叫端誤以為清單已經
完整而漏掉其餘職缺。

回傳每個職缺的 jobno、job（職缺名稱）、switch（on=開放中/off=已關閉，兩者都會回傳，不
會被篩掉）、role、department（{"id","code","name"}}）等欄位，機械式轉換、不省略。
browse_limit 固定為 null（此路由沒有配額資訊可回報）。

成功時固定回傳 {"results": [...], "pagination": {"page","total_pages","total"},
"browse_limit": null, "warnings": [...]}——不論結果是否為空，形狀都相同。失敗時回傳
{"error": str}，沒有 results 欄位。
"""


def _register_tool(mcp: FastMCP, fn, doc: str) -> None:
    """Register one tool with `doc` as its ACTUAL __doc__ — never as a parallel
    `description=` argument the function's own docstring doesn't carry.

    steering/structure.md's Documentation Standards rule is that the docstring is the
    ONLY text the Agent reads when choosing a tool. A `description=` override is
    technically read by FastMCP too (`Tool.from_function`: `description or fn.__doc__`),
    but a check written against `fn.__doc__` — the natural, framework-independent way to
    assert "does this tool's docstring say X" — would silently miss content that only
    ever lived in a `description=` argument, and the two channels agreeing today gives no
    guarantee they keep agreeing after an edit to only one of them. Binding the
    requirement to `fn.__doc__` itself, not to whichever channel happens to also carry
    the text, is what makes the rule enforceable rather than merely usually-true.

    Some of these tools' full text (search_resumes') is generated from
    tools/filters.py's CONDITIONS table at import time, so it cannot be written as a
    literal triple-quoted docstring — Python only recognises a compile-time string
    constant in that position, never an f-string or any other runtime expression. `doc`
    is therefore assigned here, as a plain attribute, before `require_login`'s
    `@functools.wraps` copies `__doc__` onto the wrapper this actually hands to
    `mcp.tool()` — which is the object whose `__doc__` ends up as `Tool.description`
    once no `description=` argument is given to override it.
    """
    fn.__doc__ = doc
    mcp.tool()(require_login(fn))


def register_search_tools(mcp: FastMCP):

    async def search_resumes(
        keyword: str,
        ctx: Context,
        filters: dict | None = None,
        page: int = 1,
        area: str | None = None,
        experience: str | None = None,
        education: str | None = None,
    ) -> dict:
        tombstone_error = _check_tombstones(area, experience, education)
        if tombstone_error:
            log.warning("search_resumes: 使用了已停用參數，未送出請求: %s", tombstone_error["error"])
            return tombstone_error

        caller_filters = filters or {}
        try:
            encoded_filters = filters_mod.encode_filters(caller_filters)
        except filters_mod.FilterError as exc:
            log.warning("search_resumes: 篩選驗證失敗，未送出請求: %s", exc)
            return _filter_error_payload(exc)

        # Everything actually sent on the wire — kws and page included, not just the
        # filter pairs — because this same list feeds detect_dropped/applied_filters
        # below (via _build_search_response), and kws is a compared, echoed condition.
        submitted = [("kws", keyword), *encoded_filters, ("page", str(page))]

        try:
            async with guarded_api(ctx, ENDPOINTS["search_resumes"], params=submitted) as (result, _info):
                return _build_search_response(result, submitted, caller_filters)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("search_resumes: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)
        except DroppedFiltersError as exc:
            log.warning("search_resumes: 104 未套用下列篩選條件: %s", exc.dropped)
            return _dropped_filters_payload(exc)

    _register_tool(mcp, search_resumes, _SEARCH_RESUMES_DESCRIPTION)

    async def list_recommended_resumes(jobno: str, ctx: Context, page: int = 1) -> dict:
        params = [("jobno", jobno), ("page", str(page))]
        try:
            async with guarded_api(ctx, ENDPOINTS["list_recommended_resumes"], params=params) as (result, _info):
                return _build_recommend_response(result)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_recommended_resumes: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    _register_tool(mcp, list_recommended_resumes, _LIST_RECOMMENDED_DESCRIPTION)

    async def list_matched_resumes(
        jobno: str, ctx: Context, page: int = 1, update_date_type: str | None = None
    ) -> dict:
        if update_date_type is not None and not update_date_type.strip():
            return {"error": "update_date_type 不可為空白"}
        params = [("jobno", jobno), ("page", str(page))]
        if update_date_type is not None:
            params.append(("updateDateType", update_date_type))
        try:
            async with guarded_api(ctx, ENDPOINTS["list_matched_resumes"], params=params) as (result, _info):
                return _build_match_response(result, requested_page=page)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_matched_resumes: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    _register_tool(mcp, list_matched_resumes, _LIST_MATCHED_DESCRIPTION)

    async def get_resume_detail(candidate_id: str, ctx: Context) -> dict:
        app = ctx.request_context.lifespan_context
        params = [("idno", candidate_id)]
        try:
            async with guarded_api(ctx, ENDPOINTS["get_resume_detail"], params=params) as (envelope, info):
                response = _build_resume_detail_response(envelope)
                # Only write a name if the résumé actually carried one — passing name=""
                # would silently overwrite a previously stored real name. account_label
                # comes from the guard's own validated `info`, not a separately-resolved
                # one — see guarded_api's docstring for why that distinction matters.
                name_value = envelope["data"]["resume"].get("userName")
                if name_value:
                    await app.db.upsert_candidate(candidate_id, ID_SOURCE_RESUME, info.account_label, name=name_value)
                return response
        except GuardAbort as e:
            return e.payload

    _register_tool(mcp, get_resume_detail, _GET_RESUME_DETAIL_DESCRIPTION)

    async def list_jobs(ctx: Context, page: int = 1) -> dict:
        params = [("page", str(page))]
        try:
            async with guarded_api(ctx, ENDPOINTS["list_jobs"], params=params) as (envelope, _info):
                return _build_job_list_response(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_jobs: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    _register_tool(mcp, list_jobs, _LIST_JOBS_DESCRIPTION)
