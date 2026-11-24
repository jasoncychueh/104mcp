"""The job switch tools: close_jobs, open_jobs and delete_job, with the checks they share.

Answers: given an already-parsed job list (tools/jobs.py's `JobList`) and the already-classified
104 payloads of the pre-checks and the write, what does each tool decide — whether the job list
lets the write go ahead (the requested jobs are on the one page and in the state the action
needs), whether 104's own front end would disable this action for this account and job and with
what words, which body to send, how the write's response reads, and which jobs the re-read job list
confirms. Deliberately does not: issue the HTTP request, decide session/auth failures, or run the
request throttle — tools/helpers.py's `guarded_sequence` owns all three. The pure functions take
parsed content and return values, strings or raise MalformedResponseError; the tools themselves
only check their arguments, go through the guard in order, and hand each response to a pure
function.

The writes and their pre-checks have front-end-code evidence only (offsets below are UTF-8
character indexes into 104's bundles: AJL = all-job-list.abf62d9f.js, CC = chunk-common.c6c97b14.js,
ENT = vip25Entry.b29263c7.js); the two reads in front of the delete (`job_more`,
`company_identity`) are measured. No write's request or response has been measured, so a write's
response is used only to recognise 104's explicit refusals and to quote what 104 said; whether it
took effect is read from the re-read job list, which is measured.
"""

import base64
import binascii
import json
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP

from mcp104.browser.api_client import ENDPOINTS
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    convert_keys,
    guarded_sequence,
    jobno_error,
    malformed_response_payload,
)
from mcp104.tools.registration import register_tool
from mcp104.tools.jobs import (
    JobList,
    JobListAccount,
    JobListRow,
    parse_job_list,
    write_abort_note,
    write_failure_disposition,
)

log = logging.getLogger("104-mcp.job_switch")

# The front end's own words (AJL@42541). A refusal quotes them so the person reads what 104's
# interface says in the same state.
NO_PERMISSION = "此功能您尚無權限使用，請洽貴公司主要人事聯絡人開放權限"
NO_POINT = "您目前可使用的職務數為0"
ATSP_TIP = "此功能請至【104 招募管理 Pro】進行操作"
_STATUS_OFF = "限刊登中會員使用"
_STATUS_PAUSE = "暫停徵才中，目前無法開啟"
_OUT_ENSURE = "保障方案延長期間不得開啟職務"
WEB_NO_MORE_MENU = "此帳號在 104 網頁上沒有這個操作，請到 104 後台"

# Close reasons the front end's questionnaire offers; the request carries their sum.
CLOSE_REASON_CODES: dict[int, str] = {
    1: "透過104找到人才",
    2: "透過其他管道找到人才",
    4: "人選較多面試篩選中",
    8: "主管或承辦人休假",
    16: "暫不補人",
    32: "其他",
}
_OTHER_REASON_CODE = 32
_OTHER_REASON_MAX_CHARS = 40

# The error codes the front end branches on for a failed close (AJL@408479; values AJL@47344).
# The code sits in `error.code` as an integer; helpers' `_envelope_code` reads it as a decimal
# string. The HTTP status those errors arrive with is not known, so the code decides, not the kind.
CLOSE_ERROR_CODES: dict[str, str] = {
    "57": (
        "104 回報職缺已關閉但公司廣告暫停失敗，請到 104 後台的「公司 → 廣告管理」修改暫停徵才設定"
    ),
    "50": "104 回報有職缺關閉失敗",
    "66": "104 回報有職缺不存在",
}

# `message` values and the code the front end branches on after an open (AJL@405100-406100;
# values AJL@42853, CC@814879, ENT@407680).
JOB_LIMIT_OPEN_CODE = "001001"

OPEN_REFUSAL_TEXT: dict[str, str] = {
    "validate": "104 的內容驗證不通過（禁用詞、高風險地區等），請到 104 後台修改職缺內容後再開啟",
    "promise": "104 回報保障方案不能開啟職務",
    "open_failed": "104 回報職務開啟失敗",
    "no_remaining_quota": "104 回報沒有剩餘的職務數",
    "job_limit": "104 回報職務已達可開啟的數量上限",
    "access_denied": "104 回報沒有權限",
}

_OPEN_MESSAGE_OUTCOMES: dict[str, str] = {
    "validate": "validate",
    "promise": "promise",
    "openFailed": "open_failed",
    "NO_REMAIN_JOB": "no_remaining_quota",
    "openPartial": "open_partial",
    "openAll": "open_all",
}


# ── JavaScript-flavoured reads of front-end flags ────────────────────────


def truthy(value: object) -> bool:
    """JavaScript truthiness of a value 104 sent, which is how the front end reads its flags:
    absent, null, false, 0 and "" are not set; every other value — an empty array or object
    included — is."""
    if value is None:
        return False
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value == value and value != 0
    if isinstance(value, str):
        return value != ""
    return True


_NUMERIC_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def no_job_points(po_num: object) -> bool:
    """The front end's `poNum <= 0`: true only for a number, or a numeric string, that is at
    most zero. An absent value is `undefined <= 0`, which is false."""
    if isinstance(po_num, bool):
        return False
    if isinstance(po_num, (int, float)):
        return po_num <= 0
    if isinstance(po_num, str) and _NUMERIC_TEXT.fullmatch(po_num.strip()):
        return float(po_num) <= 0
    return False


def _is_one(value: object) -> bool:
    """`value === 1`."""
    return isinstance(value, int) and not isinstance(value, bool) and value == 1


# ── Job list check ───────────────────────────────────────────────────────


def preflight_rows(requested: Sequence[str], job_list: JobList) -> list[JobListRow] | str:
    """The job list's rows for `requested` in the caller's order, or the error text. This
    tool family reads one page, so a list that is not exactly one page is an error (the
    check and the re-read both need every job in one read); a requested jobno not on the
    list is named in the caller's order. Raises MalformedResponseError when the list gives
    no page count."""
    if job_list.total_page() != 1:
        return (
            f"職缺清單有 {job_list.total_page()} 頁；本工具只處理職缺清單只有一頁的帳號"
            "（核對與確認都需要一次讀到全部職缺），沒有送出寫入"
        )
    by_jobno = {row.jobno: row for row in job_list.rows}
    missing = [jobno for jobno in requested if jobno not in by_jobno]
    if missing:
        return f"這些職缺不在職缺清單上，沒有送出寫入：{'、'.join(missing)}。職缺編號請取自 list_jobs 的 jobno"
    return [by_jobno[jobno] for jobno in requested]


def _verifying(row: JobListRow) -> bool:
    """A job 104 is still verifying: the row's `switch` is "new" or `isInVerify` is set."""
    return row.switch == "new" or truthy(row.is_in_verify)


def _verifying_multi(row: JobListRow) -> bool:
    """The multi-select checkbox's own test (AJL@196978): "new", or `isInVerify === 1`."""
    return row.switch == "new" or _is_one(row.is_in_verify)


def switch_gate(
    action: Literal["open", "close"], rows: Sequence[JobListRow], account: JobListAccount
) -> str | None:
    """The sentence 104's own front end shows when it disables this action for this account
    and these jobs, or None when it leaves the action enabled. Rules are tried in order, the
    first hit wins; several jobs name the affected jobnos.

    One job follows the per-row switch button (AJL@213906); two or more follow the
    multi-select toolbar (open AJL@235900, close AJL@236483). An account whose list shows no
    per-row switch button (`isWeb`, AJL@201457) can only switch through the toolbar, so it is
    held to the toolbar's rules however many jobs are given. The checks that only apply to
    opening are marked."""
    opening = action == "open"
    verb = "開啟" if opening else "關閉"
    permit = account.permit

    if len(rows) == 1 and not truthy(account.is_web):
        row = rows[0]
        if _verifying(row):
            return f"職缺確認中，目前無法{verb}"
        if opening and account.co_status == "off":
            return _STATUS_OFF
        if opening and account.co_status == "pause":
            return _STATUS_PAUSE
        if opening and truthy(account.is_out_ensure):
            return _OUT_ENSURE
        # The button reads `permit[key].result` without asking whether the key exists, so an
        # absent key leaves the button disabled (job__10 opens, job__11 closes).
        key = "job__10" if opening else "job__11"
        if not isinstance(permit, Mapping):
            return NO_PERMISSION
        entry = permit.get(key)
        if not isinstance(entry, Mapping) or entry.get("result") is not True:
            return NO_PERMISSION
        if opening and truthy(account.is_new_model) and no_job_points(account.po_num):
            return NO_POINT
        return None

    verifying = [row.jobno for row in rows if _verifying_multi(row)]
    if verifying:
        return f"職缺確認中，目前無法{verb}：{'、'.join(verifying)}"
    if account.co_status == "off":
        return _STATUS_OFF
    if account.co_status == "pause":
        return _STATUS_PAUSE
    if opening and truthy(account.is_out_ensure):
        return _OUT_ENSURE
    if not isinstance(permit, Mapping):
        return NO_PERMISSION
    # The toolbar only blocks when the key is present and not a granted permit
    # (`i.job__5 && !i.job__5.result`; close: `!o.job__11 || o.job__11.result`).
    key = "job__5" if opening else "job__11"
    entry = permit.get(key)
    if truthy(entry) and (not isinstance(entry, Mapping) or entry.get("result") is not True):
        return NO_PERMISSION
    if opening and truthy(account.is_new_model) and no_job_points(account.po_num):
        return NO_POINT
    return None


_HUNTER_OPEN_REFUSAL = (
    "這個帳號是獵頭或派遣的新制帳號，開啟職缺前 104 會先顯示一個確認框（HunterConfirmModal），"
    "本工具沒有實作這個流程，請到 104 後台開啟"
)


def hunter_open_gate(account: JobListAccount) -> str | None:
    """The refusal for a hunter or dispatch account on the new job-point model, where the
    front end opens a confirmation dialog before opening (AJL@406322), else None."""
    if (truthy(account.is_hunter) or truthy(account.is_dispatch)) and truthy(account.is_new_model):
        return _HUNTER_OPEN_REFUSAL
    return None


def delete_gate(more_permit: object, is_atsp: object) -> str | None:
    """The sentence the front end shows on a disabled delete menu item, or None when it is
    enabled (AJL@205509): enabled needs the single job's `job__6` permit granted and the
    account not an ATS-plan (招募管理 Pro) account. `more_permit` is `result.permit` of the
    single-job "more" read, not the job list's `permit`. An absent `is_atsp` is not set."""
    atsp = truthy(is_atsp)
    granted = (
        isinstance(more_permit, Mapping)
        and isinstance(more_permit.get("job__6"), Mapping)
        and more_permit["job__6"].get("result") is True
    )
    if not granted:
        return ATSP_TIP if atsp else NO_PERMISSION
    if atsp:
        return ATSP_TIP
    return None


# ── Confirmation from the re-read job list ───────────────────────────────


def confirm_switch(
    requested: Sequence[str],
    job_list_after: JobList,
    *,
    want: Literal["on", "off"],
    key: str,
) -> tuple[list[dict], list[str]]:
    """One row per requested jobno, in the caller's order: `key` is "confirmed" when the
    re-read list carries the job with `switch == want`, otherwise "unconfirmed". Jobs that are
    not on the re-read list are unconfirmed and named in the returned warnings."""
    by_jobno = {row.jobno: row for row in job_list_after.rows}
    results = []
    absent = []
    for jobno in requested:
        row = by_jobno.get(jobno)
        if row is None:
            absent.append(jobno)
        results.append({"jobno": jobno, key: "confirmed" if row is not None and row.switch == want else "unconfirmed"})
    warnings = []
    if absent:
        warnings.append(f"重讀職缺清單時找不到這些職缺：{'、'.join(absent)}，記為未確認")
    return results, warnings


def confirm_deleted(jobno: str, job_list_after: JobList) -> tuple[list[dict], list[str]]:
    """The single result row of a delete: "confirmed" when the re-read list is one page and
    no longer carries the job, "unconfirmed" when it still does. A list of more than one page
    cannot show the job is gone — it may sit on another page — so that is unconfirmed with a
    warning."""
    if any(row.jobno == jobno for row in job_list_after.rows):
        return [{"jobno": jobno, "deleted": "unconfirmed"}], []
    if job_list_after.total_page() > 1:
        return (
            [{"jobno": jobno, "deleted": "unconfirmed"}],
            [f"重讀的職缺清單有 {job_list_after.total_page()} 頁，職缺 {jobno} 不在第 1 頁不代表已刪除，記為未確認"],
        )
    return [{"jobno": jobno, "deleted": "confirmed"}], []


def all_unconfirmed(requested: Sequence[str], key: str) -> list[dict]:
    """The result rows when the re-read did not happen or failed: nothing is confirmed."""
    return [{"jobno": jobno, key: "unconfirmed"} for jobno in requested]


def outcome_message(results: Sequence[dict], key: str, *, verb: str) -> str:
    """What the result rows mean, for the caller: the confirmed jobs (and that the evidence
    is the re-read list, not the write's own response) and the unconfirmed ones (not a
    failure; check on 104 or with list_jobs; do not resend)."""
    confirmed = [r["jobno"] for r in results if r[key] == "confirmed"]
    unconfirmed = [r["jobno"] for r in results if r[key] != "confirmed"]
    parts = []
    if confirmed:
        parts.append(
            f"已確認{verb}：重讀職缺清單的結果符合（依據是重讀職缺清單；{verb}請求本身的回應未量測）。"
            f"職缺：{'、'.join(confirmed)}。"
        )
    if unconfirmed:
        parts.append(
            f"這些職缺未確認已{verb}：重讀職缺清單時狀態不符、職缺不在清單上，或重讀失敗。"
            "這不代表失敗——請到 104 後台或用 list_jobs 確認，不要重送。"
            f"職缺：{'、'.join(unconfirmed)}。"
        )
    return "\n".join(parts)


# ── close_jobs: pre-check, body, response ────────────────────────────────

_ADD_ON_CATEGORIES = ("focus", "pickUp", "priority", "jobSort")


@dataclass(frozen=True)
class CloseAddOns:
    """What the close pre-check reports: the add-on categories that hold something (keys
    snake_case, values converted) and whether the account would get hunter job points back."""

    add_ons: dict
    has_hunter_points: bool


def _holds_something(name: str, value: object) -> bool:
    """The front end's "category is not empty" (AJL@409943): absent, null, an empty array or
    an empty object is empty; a non-empty array or object is not. Any other type is not
    something this tool can read as "none", so it raises."""
    if value is None:
        return False
    if isinstance(value, (list, dict)):
        return len(value) > 0
    raise MalformedResponseError(f"預檢查的 data.{name} 不是陣列或物件")


def precheck_add_ons(payload: object) -> CloseAddOns:
    """The close pre-check's family-B payload -> CloseAddOns. Raises MalformedResponseError
    when `data` is not an object or a category is neither empty nor an array or object."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MalformedResponseError("預檢查的 data 缺失或非物件")
    add_ons: dict = {}
    for name in _ADD_ON_CATEGORIES:
        if _holds_something(name, data.get(name)):
            add_ons.update(convert_keys({name: data[name]}))
    return CloseAddOns(add_ons=add_ons, has_hunter_points=_holds_something("hunterJobPoint", data.get("hunterJobPoint")))


def close_precheck_body(raw_jobnos: Sequence[object]) -> dict:
    """The pre-check body the job list's close flow sends (AJL@410359, CC@656674): each job
    with `jobSortBookingDates` null, no close date."""
    return {"jobs": [{"jobNo": raw, "jobSortBookingDates": None} for raw in raw_jobnos]}


def close_body(raw_jobnos: Sequence[object], reasons: Sequence[int], description: str, return_extra: bool) -> dict:
    """The close body, with exactly these keys: the jobs as 104 gave them, the reasons' sum,
    the free-text reason ("" unless reason 32 was chosen), whether to return add-ons, and
    `pauseIfLastJob` true as the front end sends it (AJL@407434)."""
    return {
        "jobNos": list(raw_jobnos),
        "status": sum(reasons),
        "description": description,
        "returnExtra": 1 if return_extra else 0,
        "pauseIfLastJob": True,
    }


_AD_PAUSE_KEYS = (
    "lastJob",
    "noPauseDays",
    "pauseStartdate",
    "pauseEnddate",
    "adOpenDate",
    "setAdPause",
    "returnExtra",
    "adPeriod",
)


def build_ad_pause(metadata: object) -> dict | None:
    """The company-ad pause information of a close response's `metadata`: its eight keys,
    snake_case, None for a key 104 left out — when `lastJob` is set; otherwise None."""
    if not isinstance(metadata, dict) or not truthy(metadata.get("lastJob")):
        return None
    return convert_keys({key: metadata.get(key) for key in _AD_PAUSE_KEYS})


def ad_pause_warning(ad_pause: dict) -> str:
    """Whether the company ad is paused or keeps running, read in the order the front end
    reads it (AJL@407434)."""
    ad_period = ad_pause.get("ad_period")
    remaining = ad_period.get("remaining_days") if isinstance(ad_period, dict) else None
    if isinstance(remaining, (int, float)) and not isinstance(remaining, bool) and remaining <= 0:
        text = "已關閉全部職缺，因為目前是刊登最後一天，公司廣告持續刊登中"
    elif truthy(ad_pause.get("no_pause_days")):
        text = "已關閉全部職缺，因為沒有暫停天數，公司廣告持續刊登中"
    else:
        text = (
            "已關閉全部職缺，公司暫停徵才中"
            f"（暫停 {ad_pause.get('pause_startdate')} 至 {ad_pause.get('pause_enddate')}，"
            f"恢復刊登日 {ad_pause.get('ad_open_date')}）"
        )
    if truthy(ad_pause.get("return_extra")):
        text += "；加值廣告已取消"
    return text


# ── open_jobs: response ──────────────────────────────────────────────────


def open_outcome(payload: object) -> str:
    """The open response's whole envelope -> one outcome. Rules in order, the first hit wins
    (the front end branches the same way: `status` SUCCESS then `message`, else
    ACCESS_DENIED, else the job-limit code — AJL@405100-406100): the refusals `validate`,
    `promise`, `open_failed`, `no_remaining_quota`; the partial `open_partial`; the accepted
    `open_all`; `access_denied`; `job_limit`; and `unrecognised` for anything else, a
    SUCCESS with an unknown `message` included."""
    if not isinstance(payload, dict):
        return "unrecognised"
    status = payload.get("status")
    if status == "SUCCESS":
        message = payload.get("message")
        if isinstance(message, str) and message in _OPEN_MESSAGE_OUTCOMES:
            return _OPEN_MESSAGE_OUTCOMES[message]
    elif status == "ACCESS_DENIED":
        return "access_denied"
    if payload.get("code") == JOB_LIMIT_OPEN_CODE:
        return "job_limit"
    return "unrecognised"


def _partial_warning(payload: dict) -> str:
    """"104 reports N jobs failed to open" with 104's own count when `result` is an integer
    or a digit string, otherwise without a count."""
    result = payload.get("result")
    if isinstance(result, int) and not isinstance(result, bool):
        return f"104 回報有 {result} 筆職缺開啟失敗"
    if isinstance(result, str) and result.isdigit():
        return f"104 回報有 {int(result)} 筆職缺開啟失敗"
    return "104 回報有職缺開啟失敗"


# ── delete_job: ATS plan state ───────────────────────────────────────────

# The front end reads the ATS plan state from the second segment of the `ithp` cookie
# (ENT@436646-437444; its decode function starts at ENT@436646, `atob` at ENT@436739).
_ATS_COOKIE_NAME = "ithp"
_ATS_STATE_KEY = "urn:104:v3:entity:ats_state"
_ATS_ACTIVE_STATES = ("converting", "active")
_ASCII_WHITESPACE = re.compile(r"[ \t\n\f\r]")
_BASE64_ALPHABET = re.compile(r"[A-Za-z0-9+/]*")


def _atob(text: str) -> str | None:
    """JavaScript's `atob` for a string: the characters, one per decoded byte (Latin-1), or
    None where `atob` throws. Strict on purpose: base64.b64decode's default mode drops
    characters outside the alphabet, which would decode strings `atob` rejects."""
    text = _ASCII_WHITESPACE.sub("", text)
    if len(text) % 4 == 0:
        if text.endswith("=="):
            text = text[:-2]
        elif text.endswith("="):
            text = text[:-1]
    if len(text) % 4 == 1:
        return None
    if not _BASE64_ALPHABET.fullmatch(text):
        return None
    padded = text + "=" * (-len(text) % 4)
    try:
        return base64.b64decode(padded, validate=True).decode("latin-1")
    except (binascii.Error, ValueError):
        return None


def _ats_state_active(cookie_value: object) -> bool:
    if not isinstance(cookie_value, str):
        return False
    segments = cookie_value.split(".")
    if len(segments) < 2:
        return False
    decoded = _atob(segments[1])
    if decoded is None:
        return False
    try:
        claims = json.loads(decoded)
    except (ValueError, RecursionError):
        return False
    return isinstance(claims, dict) and claims.get(_ATS_STATE_KEY) in _ATS_ACTIVE_STATES


def ats_plan_active(cookies: Sequence[Mapping]) -> bool:
    """Whether the session's `ithp` cookie says the account has an ATS plan, read the way the
    front end reads it. Any failure to read it — no such cookie, a missing or undecodable
    second segment, no such key — is "no ATS plan". Only the boolean leaves this function;
    the decoded content is never logged or returned."""
    return any(
        _ats_state_active(cookie.get("value")) for cookie in cookies if cookie.get("name") == _ATS_COOKIE_NAME
    )


# ── Shared tool plumbing ─────────────────────────────────────────────────


def _jobnos_error(jobnos: Sequence[str]) -> dict | None:
    """Argument check of a jobno list, ahead of any request: non-empty, every one a job
    number, none repeated."""
    if not jobnos:
        return {"error": "jobnos 是空的。本工具沒有「全部」的選項，請明確列出要處理的 jobno（來自 list_jobs）。"}
    for jobno in jobnos:
        error = jobno_error(jobno)
        if error is not None:
            return error
    duplicated = sorted({jobno for jobno in jobnos if list(jobnos).count(jobno) > 1})
    if duplicated:
        return {"error": f"jobnos 有重複：{'、'.join(duplicated)}。每個職缺只能列一次。"}
    return None


def _reread_failure_warning(exc: GuardAbort | MalformedResponseError) -> str:
    reason = exc.payload.get("error") if isinstance(exc, GuardAbort) else exc.detail
    return f"重讀職缺清單失敗（{reason}），無法確認，每個職缺都記為未確認"


def _write_note(disposition: str, abort: GuardAbort, verb: str) -> str:
    """The warning for a write that aborted but is read back anyway."""
    if disposition == "reread_ambiguous":
        return f"{verb}請求{write_abort_note(abort)}，結果以重讀職缺清單為準"
    return f"104 拒絕了這批{verb}（{abort.payload.get('error')}）；一批裡是否有部分已生效未量測，結果以重讀職缺清單為準"


# ── Tool descriptions ────────────────────────────────────────────────────
#
# Each becomes the tool's actual __doc__. Claude Code cuts a description to 2,048 characters,
# so the evidence and every branch live in CLAUDE.md and only what decides a call is here.

_CLOSE_JOBS_DESCRIPTION = """關閉一個或多個開放中的職缺（走 JSON API；寫入）。⚠ 立刻生效、求職者看得到；呼叫前必須先讓真人確認要關哪些職缺。可用 open_jobs 重新開啟，但重新開啟會使用刊登額度並取消加值廣告。

Args:
    jobnos: 必填，要關閉的 jobno 清單（全數字字串、不可重複，來自 list_jobs）。沒有「全部關閉」。
    reasons: 必填，關閉原因代碼清單（至少一個、不可重複）：1 透過104找到人才、2 透過其他管道找到人才、4 人選較多面試篩選中、8 主管或承辦人休假、16 暫不補人、32 其他。
    other_reason: reasons 含 32 時必填（最多 40 字元）；不含 32 時不可給。
    return_add_ons: 關閉前的預檢查回報有加值廣告時，關閉會一併歸還並結束它們（104 介面沒有「不歸還」的選項）；真人確認後才給 true。
    allow_pausing_company_ads: 關掉最後一個開放中的職缺會讓公司廣告刊期暫停（依前端碼；暫停天數怎麼扣未量測）；真人確認後才給 true。

一次呼叫至多 4 個請求：讀職缺清單、關閉前預檢查、關閉、重讀職缺清單。104 介面不讓人關閉的狀態（職缺確認中、沒有權限等）在送出前就被拒絕；有獵頭職缺點數要歸還的請到 104 後台關閉。

預檢查與關閉的回應都只有前端碼證據、未量測；是否生效只看重讀職缺清單。成功時回傳 {"results": [{"jobno","closed"}], "ad_pause", "message", "warnings"}：closed 只有 "confirmed" 與 "unconfirmed"，照值判斷；unconfirmed 不代表失敗，請到 104 後台或用 list_jobs 確認，不要重送。ad_pause 是 104 回報的公司廣告暫停資訊（沒有時為 null）。

其餘回傳 {"error": str}（節流拒絕另帶 retry_after_seconds；加值廣告待確認另帶 add_ons），沒有 results。
"""

_OPEN_JOBS_DESCRIPTION = """開啟一個或多個已關閉的職缺（走 JSON API；寫入）。⚠ 立刻生效、求職者看得到；呼叫前必須先讓真人確認要開哪些職缺。可用 close_jobs 再關閉。開啟會使用職務刊登額度、並同步取消自動排序這類加值廣告的預約（依前端碼；額度怎麼扣未量測）。

Args:
    jobnos: 必填，要開啟的 jobno 清單（全數字字串、不可重複，來自 list_jobs）。沒有「全部開啟」。
    cancel_add_ons: 開啟前的查詢回報有會被取消的加值廣告預約時，需要真人確認後給 true 才會送出。

一次呼叫至多 4 個請求：讀職缺清單、查詢會被取消的加值廣告預約、開啟、重讀職缺清單。104 介面不讓人開啟的狀態（限刊登、暫停徵才、保障方案延長、沒有職務數、沒有權限等）在送出前就被拒絕；獵頭或派遣新制帳號請到 104 後台開啟。法規檢查參數固定送前端預設值，語意只有前端碼證據、未量測，驗證失敗也不會改值重送。

開啟的回應只有前端碼證據、未量測；是否生效只看重讀職缺清單。成功時回傳 {"results": [{"jobno","opened"}], "refusal", "message", "warnings"}：opened 只有 "confirmed" 與 "unconfirmed"，照值判斷；unconfirmed 不代表失敗，請到 104 後台或用 list_jobs 確認，不要重送。

只開一個職缺而 104 拒絕時回傳 {"error", "refusal"}，refusal 是 validate（內容驗證，請到後台改內容）、promise（保障方案）、open_failed、no_remaining_quota（沒有剩餘職務數）、job_limit（已達數量上限）、access_denied 之一；開多個職缺被拒絕時照常重讀，refusal 放在成功形狀裡。其餘回傳 {"error": str}（節流拒絕另帶 retry_after_seconds；加值廣告待確認另帶 add_ons），沒有 results。
"""

_DELETE_JOB_DESCRIPTION = """刪除一個已關閉的職缺（走 JSON API；寫入）。⚠ 無法復原：會一併刪除這個職缺的應徵履歷與聯絡紀錄（依 104 介面的確認文字；有面試秘書或獵頭資料時還包含那些）。立刻生效、求職者看得到、呼叫前必須先讓真人確認要刪哪個職缺。

Args:
    jobno: 必填，要刪除的 jobno（全數字字串，來自 list_jobs）。一次只刪一個，沒有批次。

只能刪除 list_jobs 顯示已關閉（switch 為 off）、104 標示可刪除、帳號有刪除權限的職缺；開放中的職缺請先用 close_jobs 關閉。ATS 方案或招募管理 Pro 帳號、網頁上沒有這個操作的帳號，請到 104 後台刪除。104 介面不讓人刪除的狀態在送出前就被拒絕。

一次呼叫至多 5 個請求：讀職缺清單、讀這個職缺的刪除權限、讀帳號方案、刪除、重讀職缺清單。權限與帳號方案的讀取已實測，刪除請求本身只有前端碼證據、未量測；是否刪除只看重讀職缺清單。

成功時回傳 {"results": [{"jobno","deleted"}], "message", "warnings"}：deleted 只有 "confirmed" 與 "unconfirmed"，照值判斷；unconfirmed 不代表失敗，請到 104 後台或用 list_jobs 確認，不要重送。其餘回傳 {"error": str}（節流拒絕另帶 retry_after_seconds），沒有 results。
"""


def register_job_switch_tools(mcp: FastMCP) -> None:

    async def close_jobs(
        jobnos: list[str],
        reasons: list[int],
        ctx: Context,
        other_reason: str | None = None,
        return_add_ons: bool = False,
        allow_pausing_company_ads: bool = False,
    ) -> dict:
        error = _jobnos_error(jobnos)
        if error is not None:
            return error
        reasons_error = _close_reasons_error(reasons, other_reason)
        if reasons_error is not None:
            return {"error": reasons_error}
        description = other_reason.strip() if _OTHER_REASON_CODE in reasons else ""

        requested = list(jobnos)
        warnings: list[str] = []
        # Set as the last act before the close is issued: the proof that the write may have
        # reached 104, read by the catch-alls below.
        write_attempted = False

        async def _before_write(_info) -> None:
            nonlocal write_attempted
            write_attempted = True

        try:
            async with guarded_sequence(ctx, slots_needed=4) as (request, _info):
                job_list = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
                rows = preflight_rows(requested, job_list)
                if isinstance(rows, str):
                    return {"error": rows}
                not_open = [row for row in rows if row.switch != "on"]
                if not_open:
                    named = "、".join(f"{row.jobno}（switch={row.switch!r}）" for row in not_open)
                    return {"error": f"這些職缺在職缺清單上不是開放中，沒有送出關閉：{named}"}
                refusal = switch_gate("close", rows, job_list.account)
                if refusal is not None:
                    return {"error": f"{refusal}，沒有送出關閉"}
                open_now = sum(1 for row in job_list.rows if row.switch == "on")
                if open_now - len(requested) == 0 and allow_pausing_company_ads is not True:
                    return {
                        "error": (
                            "這次關閉會讓清單上沒有任何開放中的職缺。依 104 前端碼，關掉最後一個開放中的職缺會讓"
                            "公司廣告刊期暫停（暫停天數怎麼扣未量測）；真人確認後以 allow_pausing_company_ads=true"
                            "再呼叫。沒有送出關閉"
                        )
                    }

                raw_jobnos = [row.raw_jobno for row in rows]
                precheck = precheck_add_ons(
                    await request(ENDPOINTS["close_job_precheck"], body=close_precheck_body(raw_jobnos))
                )
                if precheck.has_hunter_points:
                    return {
                        "error": (
                            "關閉前的預檢查回報這些職缺有獵頭職缺點數要歸還；本工具沒有實作獵頭點數的歸還流程"
                            "（104 介面在這時會顯示「歸還 N 個職務數」的確認），請到 104 後台關閉。沒有送出關閉"
                        )
                    }
                if precheck.add_ons and return_add_ons is not True:
                    return {
                        "error": (
                            "關閉前的預檢查回報這些職缺有加值廣告：關閉會一併歸還並結束這些加值廣告"
                            "（104 介面文案「歸還廣告 N 則，M 則已到期，關閉後將無法使用」；介面在這時只讓人確認或取消，"
                            "沒有「不歸還」的選項）。真人確認後以 return_add_ons=true 再呼叫。沒有送出關閉"
                        ),
                        "add_ons": precheck.add_ons,
                    }
                if not precheck.add_ons and return_add_ons is True:
                    warnings.append("預檢查沒有回報加值廣告，return_add_ons 這個值沒有使用")

                ad_pause = None
                try:
                    payload = await request(
                        ENDPOINTS["close_jobs"],
                        body=close_body(raw_jobnos, reasons, description, bool(precheck.add_ons)),
                        before_request=_before_write,
                    )
                    metadata = payload.get("metadata") if isinstance(payload, dict) else None
                    ad_pause = build_ad_pause(metadata)
                    if ad_pause is not None:
                        warnings.append(ad_pause_warning(ad_pause))
                    failed = metadata.get("failed") if isinstance(metadata, dict) else None
                    if isinstance(failed, int) and not isinstance(failed, bool) and failed > 0:
                        # Whether a success response carries this count is unmeasured; the
                        # front end reads it from error responses.
                        warnings.append(f"104 回報有 {failed} 筆關閉失敗；逐筆結果以重讀職缺清單為準")
                except GuardAbort as e:
                    if e.code in CLOSE_ERROR_CODES:
                        warnings.append(CLOSE_ERROR_CODES[e.code])
                    else:
                        disposition = write_failure_disposition(e, batch_size=len(requested))
                        if disposition == "raise":
                            raise
                        warnings.append(_write_note(disposition, e, "關閉"))

                try:
                    job_list_after = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
                    results, confirm_warnings = confirm_switch(requested, job_list_after, want="off", key="closed")
                    warnings.extend(confirm_warnings)
                except (GuardAbort, MalformedResponseError) as exc:
                    warnings.append(_reread_failure_warning(exc))
                    results = all_unconfirmed(requested, "closed")
                return {
                    "results": results,
                    "ad_pause": ad_pause,
                    "message": outcome_message(results, "closed", verb="關閉"),
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            if not write_attempted:
                log.error("close_jobs: 回應結構異常: %s", exc.detail)
                return malformed_response_payload(exc)
            log.error("close_jobs: 寫入之後的結構異常: %s", exc.detail)
            return _unconfirmed_after_failure(requested, "closed", "關閉", warnings, "ad_pause")
        except Exception:
            log.error("close_jobs: 非預期例外 (write_attempted=%s)", write_attempted, exc_info=True)
            if write_attempted:
                return _unconfirmed_after_failure(requested, "closed", "關閉", warnings, "ad_pause")
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出職缺關閉"}

    register_tool(mcp, close_jobs, _CLOSE_JOBS_DESCRIPTION)

    async def open_jobs(jobnos: list[str], ctx: Context, cancel_add_ons: bool = False) -> dict:
        error = _jobnos_error(jobnos)
        if error is not None:
            return error

        requested = list(jobnos)
        warnings: list[str] = []
        refusal_kind: str | None = None
        write_attempted = False

        async def _before_write(_info) -> None:
            nonlocal write_attempted
            write_attempted = True

        try:
            async with guarded_sequence(ctx, slots_needed=4) as (request, _info):
                job_list = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
                rows = preflight_rows(requested, job_list)
                if isinstance(rows, str):
                    return {"error": rows}
                not_closed = [row for row in rows if row.switch != "off"]
                if not_closed:
                    named = "、".join(f"{row.jobno}（switch={row.switch!r}）" for row in not_closed)
                    return {"error": f"這些職缺在職缺清單上不是已關閉，沒有送出開啟：{named}"}
                refusal = switch_gate("open", rows, job_list.account) or hunter_open_gate(job_list.account)
                if refusal is not None:
                    return {"error": f"{refusal}，沒有送出開啟"}

                raw_jobnos = [row.raw_jobno for row in rows]
                refund = await request(
                    ENDPOINTS["job_refund_points"],
                    params=[("jobNos[]", str(raw)) for raw in raw_jobnos],
                )
                refund_rows = refund.get("data") if isinstance(refund, dict) else None
                if not isinstance(refund_rows, list):
                    raise MalformedResponseError("加值廣告查詢的 data 缺失或非陣列")
                if refund_rows and cancel_add_ons is not True:
                    return {
                        "error": (
                            "開啟前的查詢回報這些職缺有會被取消的加值廣告預約：開啟會同步取消這些加值廣告"
                            "（依前端碼；104 介面在這時顯示「職務開啟時將同步取消加值廣告」的確認）。"
                            "真人確認後以 cancel_add_ons=true 再呼叫。沒有送出開啟"
                        ),
                        "add_ons": convert_keys(refund_rows),
                    }

                # The toolbar always sends "multi", even for one job (AJL@238004), and an account
                # without per-row switch buttons can only use the toolbar.
                one_job_button = len(requested) == 1 and not truthy(job_list.account.is_web)
                form = [
                    ("jobno", ",".join(str(raw) for raw in raw_jobnos)),
                    ("ignoreOrdinance", "1"),
                    ("type", "single" if one_job_button else "multi"),
                ]
                outcome: str | None = None
                result_text: str | None = None
                try:
                    payload = await request(ENDPOINTS["open_jobs"], body=form, before_request=_before_write)
                    outcome = open_outcome(payload)
                    if isinstance(payload, dict) and isinstance(payload.get("result"), str):
                        result_text = payload["result"]
                    if outcome == "open_partial":
                        warnings.append(_partial_warning(payload))
                    elif outcome == "unrecognised":
                        warnings.append("開啟請求的回應無法辨識，結果以重讀職缺清單為準")
                except GuardAbort as e:
                    if e.code == JOB_LIMIT_OPEN_CODE:
                        # No `result_text`: the guard's own sentence is not 104's explanation.
                        outcome = "job_limit"
                    else:
                        disposition = write_failure_disposition(e, batch_size=len(requested))
                        if disposition == "raise":
                            raise
                        warnings.append(_write_note(disposition, e, "開啟"))

                if outcome in OPEN_REFUSAL_TEXT:
                    text = OPEN_REFUSAL_TEXT[outcome]
                    if result_text:
                        text = f"{text}（104 的說明：{result_text}）"
                    if len(requested) == 1:
                        return {"error": f"{text}。沒有開啟任何職缺", "refusal": outcome}
                    refusal_kind = outcome
                    warnings.append(f"104 拒絕了這批開啟：{text}；一批裡是否有部分已生效未量測，結果以重讀職缺清單為準")

                try:
                    job_list_after = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
                    results, confirm_warnings = confirm_switch(requested, job_list_after, want="on", key="opened")
                    warnings.extend(confirm_warnings)
                except (GuardAbort, MalformedResponseError) as exc:
                    warnings.append(_reread_failure_warning(exc))
                    results = all_unconfirmed(requested, "opened")
                return {
                    "results": results,
                    "refusal": refusal_kind,
                    "message": outcome_message(results, "opened", verb="開啟"),
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            if not write_attempted:
                log.error("open_jobs: 回應結構異常: %s", exc.detail)
                return malformed_response_payload(exc)
            log.error("open_jobs: 寫入之後的結構異常: %s", exc.detail)
            return _unconfirmed_after_failure(requested, "opened", "開啟", warnings, "refusal")
        except Exception:
            log.error("open_jobs: 非預期例外 (write_attempted=%s)", write_attempted, exc_info=True)
            if write_attempted:
                return _unconfirmed_after_failure(requested, "opened", "開啟", warnings, "refusal")
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出職缺開啟"}

    register_tool(mcp, open_jobs, _OPEN_JOBS_DESCRIPTION)

    async def delete_job(jobno: str, ctx: Context) -> dict:
        error = jobno_error(jobno)
        if error is not None:
            return error

        requested = [jobno]
        warnings: list[str] = []
        write_attempted = False

        async def _before_write(_info) -> None:
            nonlocal write_attempted
            write_attempted = True

        try:
            async with guarded_sequence(ctx, slots_needed=5) as (request, info):
                job_list = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
                rows = preflight_rows(requested, job_list)
                if isinstance(rows, str):
                    return {"error": rows}
                row = rows[0]
                if truthy(job_list.account.is_web):
                    return {"error": f"{WEB_NO_MORE_MENU}，沒有送出刪除"}
                if row.switch != "off":
                    return {
                        "error": (
                            f"職缺 {row.jobno} 在職缺清單上不是已關閉（switch={row.switch!r}）；只能刪除已關閉的職缺，"
                            "請先用 close_jobs 關閉。沒有送出刪除"
                        )
                    }
                if row.can_delete is not True:
                    return {
                        "error": (
                            f"104 沒有把職缺 {row.jobno} 標成可刪除（職缺清單的 canDelete），"
                            "104 介面在這時不提供刪除。沒有送出刪除"
                        )
                    }
                if ats_plan_active(info.cookies):
                    return {
                        "error": (
                            "這個帳號有 ATS 方案，104 刪除職缺前會先檢查甄試流程中是否還有履歷，"
                            "本工具沒有實作這道檢查，請到 104 後台刪除。沒有送出刪除"
                        )
                    }

                more_params = [("jobno", str(row.raw_jobno))]
                if row.job_setting is not None:
                    more_params.append(("job_setting", str(row.job_setting)))
                more = await request(ENDPOINTS["job_more"], params=more_params)
                if not isinstance(more, dict):
                    raise MalformedResponseError("職缺的「更多」讀取的 result 不是物件")

                identity = await request(ENDPOINTS["company_identity"])
                identity_data = identity.get("data") if isinstance(identity, dict) else None
                if not isinstance(identity_data, dict):
                    raise MalformedResponseError("帳號身分讀取的 data 缺失或非物件")

                refusal = delete_gate(more.get("permit"), identity_data.get("isAtsp"))
                if refusal is not None:
                    return {"error": f"{refusal}，沒有送出刪除"}

                try:
                    payload = await request(
                        ENDPOINTS["delete_job"], body=[("jobno", str(row.raw_jobno))], before_request=_before_write
                    )
                    status = payload.get("status") if isinstance(payload, dict) else None
                    message = payload.get("message") if isinstance(payload, dict) else None
                    message_text = message if isinstance(message, str) and message else None
                    if status == "ACCESS_DENIED":
                        detail = f"：{message_text}" if message_text else ""
                        return {"error": f"104 回報沒有權限{detail}。沒有刪除職缺"}
                    if status != "SUCCESS":
                        shown = f"status={status!r}" + (f"、message={message_text!r}" if message_text else "")
                        warnings.append(f"刪除請求的回應無法辨識（{shown}），結果以重讀職缺清單為準")
                except GuardAbort as e:
                    disposition = write_failure_disposition(e, batch_size=1)
                    if disposition == "raise":
                        raise
                    warnings.append(_write_note(disposition, e, "刪除"))

                try:
                    job_list_after = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
                    results, confirm_warnings = confirm_deleted(requested[0], job_list_after)
                    warnings.extend(confirm_warnings)
                except (GuardAbort, MalformedResponseError) as exc:
                    warnings.append(_reread_failure_warning(exc))
                    results = all_unconfirmed(requested, "deleted")
                return {
                    "results": results,
                    "message": outcome_message(results, "deleted", verb="刪除"),
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            if not write_attempted:
                log.error("delete_job: 回應結構異常: %s", exc.detail)
                return malformed_response_payload(exc)
            log.error("delete_job: 寫入之後的結構異常: %s", exc.detail)
            return _unconfirmed_after_failure(requested, "deleted", "刪除", warnings, "")
        except Exception:
            log.error("delete_job: 非預期例外 (write_attempted=%s)", write_attempted, exc_info=True)
            if write_attempted:
                return _unconfirmed_after_failure(requested, "deleted", "刪除", warnings, "")
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出職缺刪除"}

    register_tool(mcp, delete_job, _DELETE_JOB_DESCRIPTION)


def _close_reasons_error(reasons: Sequence[int], other_reason: str | None) -> str | None:
    """Argument check of close_jobs' reasons, ahead of any request, or the error text."""
    table = "、".join(f"{code} {label}" for code, label in CLOSE_REASON_CODES.items())
    if not reasons:
        return f"reasons 是空的。至少要給一個關閉原因代碼：{table}"
    invalid = [code for code in reasons if isinstance(code, bool) or code not in CLOSE_REASON_CODES]
    if invalid:
        return f"reasons 有不合法的代碼：{invalid}。合法代碼：{table}"
    if len(set(reasons)) != len(reasons):
        return f"reasons 有重複的代碼：{list(reasons)}。合法代碼：{table}"
    if _OTHER_REASON_CODE in reasons:
        if not isinstance(other_reason, str) or not other_reason.strip():
            return f"reasons 含 {_OTHER_REASON_CODE}（其他）時，other_reason 必須是非空白的文字"
        if len(other_reason.strip()) > _OTHER_REASON_MAX_CHARS:
            return f"other_reason 最多 {_OTHER_REASON_MAX_CHARS} 字元（去掉頭尾空白後），收到 {len(other_reason.strip())} 字元"
    elif other_reason is not None:
        return f"reasons 不含 {_OTHER_REASON_CODE}（其他）時不能給 other_reason；請移除它，或把 {_OTHER_REASON_CODE} 加進 reasons"
    return None


def _unconfirmed_after_failure(
    requested: Sequence[str], key: str, verb: str, warnings: Sequence[str], extra_key: str
) -> dict:
    """The success-shaped result for a write that may have been sent but whose outcome cannot
    be told after an internal failure: every job unconfirmed, with `extra_key` (the tool's
    own extra field — "ad_pause" or "refusal", or none) None."""
    results = all_unconfirmed(requested, key)
    payload: dict = {"results": results}
    if extra_key:
        payload[extra_key] = None
    payload["message"] = outcome_message(results, key, verb=verb)
    payload["warnings"] = list(warnings) + [f"發生內部錯誤，無法判斷是否已{verb}；請到 104 後台確認，不要重送"]
    return payload
