"""The job read tools: list_applicants, list_jobs, list_refreshable_jobs.

Answers: given an already-classified 104 payload (browser/api_client.classify()'s
success `payload`), how does it become the documented tool-facing response — the
applicant rows and their date groups, the job rows and their per-job match counts, the
refreshable-job list. Deliberately does not: issue the HTTP request, decide session/auth
failures, or run the request throttle — tools/helpers.py's `guarded_api` and
`guarded_sequence` own all three. The pure functions below take already-classified
content and return the tool-facing dict (or raise MalformedResponseError when its shape
departs from what was measured); the tools themselves only check their arguments, go
through a guard, and hand the response to a pure function.

Wire facts: docs/104-site-facts.md §8.28–§8.30.
"""

import copy
import logging
import re
from collections.abc import Sequence
from datetime import date, datetime, timedelta, timezone

from mcp.server.fastmcp import Context, FastMCP

import mcp104.tools.discovery as discovery_mod
from mcp104.browser.api_client import ENDPOINTS
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    SessionUnavailable,
    convert_keys,
    guarded_api,
    guarded_sequence,
    jobno_error,
    normalise_jobno,
    require_login,
)
from mcp104.tools.search import html_to_text

log = logging.getLogger("104-mcp.jobs")


def _malformed_response_payload(exc: MalformedResponseError) -> dict:
    # Same wording as the other tool modules' copies; see MalformedResponseError's own
    # docstring for why the class is shared and this small formatter is per-module.
    return {"error": f"104 回應結構異常（{exc.detail}），可能是介面已變更，請回報"}


# ── Applicant list ───────────────────────────────────────────────────────

# 104's web page asks for the last 60 days; the route accepts the window as two dates.
APPLICANT_WINDOW_DAYS = 60
_TAIPEI = timezone(timedelta(hours=8))
_DATE_FORMAT = "%Y/%m/%d"

# The only keys of an `expJobArr` entry that survive onto an applicant row. A row's
# `expJobNote` alone was 39% of its ~6.2 KB (§8.28).
_EXP_JOB_KEPT_KEYS = ("expFirm", "expTitle", "expStart", "expEnd")

_RESUME_NUMBER_KEYS = (
    ("match", "match"),
    ("notMatch", "not_match"),
    ("isBlack", "is_black"),
    ("recycle", "recycle"),
)


def taipei_today(now: datetime | None = None) -> date:
    """Today's date in UTC+8. `now` None reads the system clock — the one place in
    this module that does; a given `now` is converted to UTC+8 first."""
    if now is None:
        now = datetime.now(timezone.utc)
    return now.astimezone(_TAIPEI).date()


def applicant_date_range(today: date) -> tuple[str, str]:
    """(today - 60 days, today) as "YYYY/MM/DD", the request format that was measured."""
    start = today - timedelta(days=APPLICANT_WINDOW_DAYS)
    return start.strftime(_DATE_FORMAT), today.strftime(_DATE_FORMAT)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _count_int(value: object) -> int | None:
    """A non-negative count as 104 sends it (a digit string, or an int) -> int, else None."""
    if _is_int(value):
        return value if value >= 0 else None
    if isinstance(value, str) and re.fullmatch(r"[0-9]+", value):
        return int(value)
    return None


def _compact_exp_job(entry):
    """One `expJobArr` entry reduced to company, title and start/end. A key 104 left
    out stays out; a non-dict entry (never observed) passes through untouched."""
    if not isinstance(entry, dict):
        return entry
    return {k: entry[k] for k in _EXP_JOB_KEPT_KEYS if k in entry}


def _build_applicant_row(raw: object, input_date: str | None) -> dict:
    if not isinstance(raw, dict):
        raise MalformedResponseError("應徵者列不是物件")
    # Without both identifiers the row cannot be handed to get_resume_detail or to the
    # message tools; 104 putting a date group (or an anonymous row) where a row belongs
    # is a departure from the measured shape, not an applicant.
    for key in ("idNo", "pId"):
        if not isinstance(raw.get(key), str) or not raw[key]:
            raise MalformedResponseError(f"應徵者列缺少 {key}")
    picked = {k: v for k, v in raw.items() if k in discovery_mod.APPLICANT_ROW_FIELD_GLOSS}
    if isinstance(picked.get("expJobArr"), list):
        picked["expJobArr"] = [_compact_exp_job(entry) for entry in picked["expJobArr"]]
    # Same value handling as a résumé row: 104's rich-text fields arrive as HTML.
    row = convert_keys(picked, string_transform=html_to_text)
    if "id_no" in row:
        row["candidate_id"] = row.pop("id_no")
    row["input_date"] = input_date
    return row


def _flatten_resume_groups(groups: object) -> list[dict]:
    """`today` rows first (no date: 104 does not give one for that group), then each
    `others` group in the order 104 gave, its rows carrying the group's `inputDate`."""
    if not isinstance(groups, dict):
        raise MalformedResponseError("resumeGroups 缺失或非物件")
    today, others = groups.get("today"), groups.get("others")
    if not isinstance(today, list) or not isinstance(others, list):
        raise MalformedResponseError("resumeGroups.today / others 缺失或非陣列")
    rows = [_build_applicant_row(raw, None) for raw in today]
    for group in others:
        if not isinstance(group, dict) or not isinstance(group.get("resumes"), list):
            raise MalformedResponseError("resumeGroups.others 的組缺少 resumes 陣列")
        input_date = group.get("inputDate")
        if not isinstance(input_date, str):
            raise MalformedResponseError("resumeGroups.others 的組缺少 inputDate")
        rows.extend(_build_applicant_row(raw, input_date) for raw in group["resumes"])
    return rows


def _applicant_resume_number(value: object) -> dict:
    if not isinstance(value, dict):
        raise MalformedResponseError("resumeNumber 缺失或非物件")
    numbers = {}
    for raw_key, name in _RESUME_NUMBER_KEYS:
        count = _count_int(value.get(raw_key))
        if count is None:
            raise MalformedResponseError(f"resumeNumber.{raw_key} 缺失或不是整數")
        numbers[name] = count
    return numbers


def _applicant_pagination(page_info: object, *, has_rows: bool, requested_page: int) -> dict:
    if page_info is None:
        if has_rows:
            raise MalformedResponseError("pageInfo 為 null 但清單有列")
        return {"page": requested_page, "total_pages": 0, "total": 0}
    if not isinstance(page_info, dict):
        raise MalformedResponseError("pageInfo 缺失或非物件")
    pagination = {}
    for raw_key, name in (("thisPage", "page"), ("total_page", "total_pages"), ("total", "total")):
        value = page_info.get(raw_key)
        if not _is_int(value):
            raise MalformedResponseError(f"pageInfo.{raw_key} 缺失或不是整數")
        pagination[name] = value
    return pagination


def build_applicant_page(
    result: object, *, requested_jobno: str, requested_page: int, date_range: tuple[str, str]
) -> dict:
    """list_applicants' `result` -> the tool-facing page. See the module docstring for
    what raises MalformedResponseError."""
    if not isinstance(result, dict):
        raise MalformedResponseError("result 缺失或非物件")

    rows = _flatten_resume_groups(result.get("resumeGroups"))

    # A free consistency check: 104 answering for a different job, or with an empty
    # shell, must not be read as this job's applicants.
    if normalise_jobno(result.get("jobNo")) != requested_jobno:
        raise MalformedResponseError("回應的職缺與請求的職缺不同")
    job_name = result.get("jobName")
    if not isinstance(job_name, str) or not job_name:
        raise MalformedResponseError("jobName 缺失或為空")

    resume_number = _applicant_resume_number(result.get("resumeNumber"))
    pagination = _applicant_pagination(
        result.get("pageInfo"), has_rows=bool(rows), requested_page=requested_page
    )

    warnings = []
    other_buckets = {
        name: resume_number[name] for name in ("not_match", "is_black", "recycle") if resume_number[name] > 0
    }
    if other_buckets:
        named = "、".join(f"{name}={count}" for name, count in other_buckets.items())
        warnings.append(
            f"resume_number 的其他分桶有人（{named}）：這些應徵者是否包含在 results 內未量測，"
            "不要假設 results 已涵蓋全部應徵者"
        )
    if pagination["total_pages"] > 1:
        warnings.append(
            f"共 {pagination['total_pages']} 頁，第 2 頁以後未量測；本次只回傳第 "
            f"{pagination['page']} 頁，需要時請自行以 page=N 呼叫"
        )

    start_date, end_date = date_range
    return {
        "job": {
            "jobno": requested_jobno,
            "job": job_name,
            "switch": result.get("jobSwitch"),
            "role": result.get("jobRole"),
        },
        "results": rows,
        "pagination": pagination,
        "resume_number": resume_number,
        "date_range": {"start_date": start_date, "end_date": end_date},
        "browse_limit": None,
        "warnings": warnings,
    }


# ── Job list and match counts ────────────────────────────────────────────

# 104 accepts at most 20 `jobNos[]` per match-number request (21 is HTTP 400, §8.29).
MATCH_NUMBER_BATCH_SIZE = 20
# Match counts are looked up for the first 80 jobs of a page only: four requests keep the
# worst case (cold, every request timing out) under the 120 s client timeout.
MATCH_NUMBER_MAX_JOBS = 80

_JOB_ROW_RAW_FIELDS = frozenset(discovery_mod.JOB_ROW_FIELD_GLOSS) - {"todayMatchCount", "totalMatchCount"}


def build_job_page(envelope: object) -> dict:
    """list_jobs' family-B envelope ({data, metadata}) -> the tool-facing page, every
    row's match counts None until `merge_match_counts` fills them. `pagination.page` /
    `.total` are the server's own values; `total_pages` is null because the page size is
    unmeasured and a guessed page count would tell a caller the list is complete when
    it may not be (this route is the authority for jobno)."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        metadata = {}
    results = []
    for raw in envelope["data"]:
        if not isinstance(raw, dict):
            raise MalformedResponseError("職缺列不是物件")
        picked = {k: v for k, v in raw.items() if k in _JOB_ROW_RAW_FIELDS}
        row = convert_keys(picked, string_transform=html_to_text)
        row["jobno"] = normalise_jobno(raw.get("jobno"))
        row["today_match_count"] = None
        row["total_match_count"] = None
        results.append(row)
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


def match_number_batches(jobnos: Sequence[str]) -> list[list[str]]:
    """The first 80 jobnos in their given order, 20 to a batch."""
    covered = list(jobnos[:MATCH_NUMBER_MAX_JOBS])
    return [covered[i : i + MATCH_NUMBER_BATCH_SIZE] for i in range(0, len(covered), MATCH_NUMBER_BATCH_SIZE)]


def merge_match_counts(page: dict, payloads: Sequence[object]) -> dict:
    """Fold each match-number envelope's `data.list` into `page`'s rows by jobno (never
    by position). Returns a new dict; `page` is not modified. A row inside the first 80
    with no entry, or with a count that is not a non-negative integer, keeps both counts
    None and is named in a warning; rows after the 80th keep None with their own
    warning. A payload whose `data.list` is not an array raises MalformedResponseError."""
    by_jobno: dict[str, tuple[object, object]] = {}
    for payload in payloads:
        data = payload.get("data") if isinstance(payload, dict) else None
        entries = data.get("list") if isinstance(data, dict) else None
        if not isinstance(entries, list):
            raise MalformedResponseError("match-number 的 data.list 缺失或非陣列")
        for entry in entries:
            if not isinstance(entry, dict):
                raise MalformedResponseError("match-number 的 data.list 含非物件項目")
            by_jobno[normalise_jobno(entry.get("jobNo"))] = (
                entry.get("todayMatchCount"),
                entry.get("totalMatchCount"),
            )

    merged = copy.deepcopy(page)
    rows = merged["results"]
    missing = []
    for row in rows[:MATCH_NUMBER_MAX_JOBS]:
        today_raw, total_raw = by_jobno.get(row["jobno"], (None, None))
        today, total = _count_int(today_raw), _count_int(total_raw)
        if today is None or total is None:
            missing.append(row["jobno"])
            continue
        row["today_match_count"], row["total_match_count"] = today, total
    if missing:
        merged["warnings"].append(
            f"104 沒有回這些職缺的配對數，或數字無法辨識：{'、'.join(missing)}；"
            "它們的 today_match_count / total_match_count 為 null"
        )
    if len(rows) > MATCH_NUMBER_MAX_JOBS:
        merged["warnings"].append(
            f"配對數只查前 {MATCH_NUMBER_MAX_JOBS} 個職缺；第 {MATCH_NUMBER_MAX_JOBS + 1} 個以後的職缺"
            "today_match_count / total_match_count 為 null"
        )
    return merged


def mark_match_counts_unavailable(page: dict, warning: str) -> dict:
    """Degrade to a job list without match counts: a new dict, every row's counts None,
    `warning` appended. `page` is not modified."""
    degraded = copy.deepcopy(page)
    for row in degraded["results"]:
        row["today_match_count"] = None
        row["total_match_count"] = None
    degraded["warnings"].append(warning)
    return degraded


def _match_counts_failure_warning(error_payload: dict) -> str:
    retry_after = error_payload.get("retry_after_seconds")
    if retry_after is not None:
        return (
            f"配對數查詢被節流拒絕，today_match_count / total_match_count 為 null；"
            f"請 {retry_after} 秒後再呼叫 list_jobs 取得配對數（retry_after_seconds={retry_after}）"
        )
    return f"配對數查詢失敗（{error_payload.get('error')}），today_match_count / total_match_count 為 null"


# ── Refreshable jobs ─────────────────────────────────────────────────────


def build_refreshable_jobs(payload: object) -> dict:
    """over-seven-day-job's family-B envelope ({data: {jobs, userPermit}, metadata}) ->
    the tool-facing page. `appear_date` is 104's string as given."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MalformedResponseError("data 缺失或非物件")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise MalformedResponseError("data.jobs 缺失或非陣列")
    permit = data.get("userPermit")
    if not isinstance(permit, bool):
        raise MalformedResponseError("data.userPermit 缺失或非布林")
    results = []
    for raw in jobs:
        if not isinstance(raw, dict):
            raise MalformedResponseError("可更新職缺列不是物件")
        results.append(
            {
                "jobno": normalise_jobno(raw.get("jobNo")),
                "job": raw.get("jobName"),
                "appear_date": raw.get("appearDate"),
            }
        )
    warnings = []
    if not permit:
        warnings.append("這個帳號沒有更新職缺的權限（user_permit 為 false）；清單僅供參考")
    return {
        "results": results,
        "pagination": {"page": 1, "total_pages": None, "total": len(results)},
        "browse_limit": None,
        "warnings": warnings,
    }


# ── Tool descriptions ────────────────────────────────────────────────────
#
# Each becomes the tool's actual __doc__ (see _register_tool). Field meanings live in
# describe_result_fields, not here: Claude Code cuts a tool description to 2,048
# characters (see CLAUDE.md) and `tests/test_contract_docs.py` holds each to 1,948.

_LIST_APPLICANTS_DESCRIPTION = """列出某個職缺近 60 天的應徵者（走 JSON API；唯讀，不扣 104 的履歷瀏覽配額，也不會把應徵者標成已讀）。

Args:
    jobno: 職缺代碼，必填，全數字字串 —— 權威來源是 list_jobs() 每筆結果的 jobno。
    page: 頁碼，預設 1。

視窗固定是「今天（台北時間）往前 60 天」，回傳的 date_range 是實際送出的起訖日。

成功時固定回傳 {"job": {"jobno","job","switch","role"}, "results": [...],
"pagination": {"page","total_pages","total"}, "resume_number": {"match","not_match",
"is_black","recycle"}, "date_range": {"start_date","end_date"}, "browse_limit": null,
"warnings": [...]}。沒有任何應徵者是合法的空結果（results 為 []、total 為 0），不是錯誤。

results 依 104 給的順序：先是今天投遞的，再依日期分組展開。每列的 input_date 是投遞日，
null 表示 104 的「今天」組（104 不給那一組的日期）。每列帶兩個識別碼：candidate_id 只給
get_resume_detail；p_id 可直接當 send_message / send_inquiry / get_conversation 的
candidate_id。列上只有基本資料與精簡的工作經歷（公司、職稱、起訖）；完整履歷請用
get_resume_detail，大頭照請用 get_candidate_photo；每個欄位的意思見
describe_result_fields(row_type="applicant")。

resume_number.match 是清單內的應徵人數；其餘三桶（not_match、is_black、recycle）有人時
warnings 會點名各桶人數。總頁數大於 1 時 warnings 會提醒第 2 頁以後未量測。

失敗時回傳 {"error": str}，沒有 results 欄位。
"""

_LIST_JOBS_DESCRIPTION = """列出本公司職缺清單 —— 這是 jobno 的權威來源（走 JSON API，唯讀）。

104 的對話與履歷清單都是「候選人 × 職缺」的組合：list_applicants /
list_recommended_resumes / list_matched_resumes / send_message / get_conversation 都需要
jobno。標準流程：list_jobs 選定職缺 (jobno)，再用上面任一工具。

Args:
    page: 頁碼，預設 1，會送到伺服器。

每個職缺回傳 jobno、job（名稱）、switch（on=開放中／off=已關閉，兩者都回傳）、role、
department、apply_count60（近 60 天應徵人數）、today_match_count（今日配對人數）、
total_match_count（目前配對人數）；欄位意思見 describe_result_fields(row_type="job")。

⚠ 配對數另外查：一次呼叫是 1 個職缺清單請求，加上最多 4 個配對數請求（前 80 個職缺，每 20
個一批）。配對數查不到（節流、104 沒回、超過前 80 個）時兩個配對數為 null，原因寫在
warnings，null 不是 0。session 失效時整個呼叫回錯誤。

⚠ pagination.total_pages 固定為 null：伺服器沒有回報每頁筆數，無法換算總頁數，本工具不猜
（本路由是 jobno 的權威來源，猜錯會讓你以為清單已完整）。pagination.page／total 是伺服器
自己回報的值。

不含 104 推薦人數；要該數字請對職缺呼叫 list_recommended_resumes 讀 pagination.total。

成功時固定回傳 {"results": [...], "pagination": {"page","total_pages","total"},
"browse_limit": null, "warnings": [...]}。失敗時回傳 {"error": str}，沒有 results 欄位。
"""

_LIST_REFRESHABLE_JOBS_DESCRIPTION = """列出目前可以「更新」的職缺（走 JSON API，唯讀；104 自己列為可更新的職缺）。

不需要參數。每筆回傳 {"jobno", "job", "appear_date"}：jobno 與 list_jobs 的 jobno 同一個編號
空間，appear_date 是 104 給的字串（量到的格式是 "YYYY-MM-DD HH:MM:SS"）。本工具只讀取，
不會更新任何職缺。

成功時固定回傳 {"results": [...], "pagination": {"page","total_pages","total"},
"browse_limit": null, "warnings": [...]}；這條路由沒有分頁資訊，total_pages 為 null、total 是
這次拿到的筆數。帳號沒有更新權限時 warnings 會說明。沒有可更新的職缺是合法的空結果
（results 為 []）。失敗時回傳 {"error": str}，沒有 results 欄位。
"""


def _register_tool(mcp: FastMCP, fn, doc: str) -> None:
    """Register `fn` with `doc` as its actual __doc__ — set before `require_login`'s
    `functools.wraps` copies it onto the wrapper FastMCP reads as the description."""
    fn.__doc__ = doc
    mcp.tool()(require_login(fn))


def register_job_tools(mcp: FastMCP) -> None:

    async def list_applicants(jobno: str, ctx: Context, page: int = 1) -> dict:
        error = jobno_error(jobno)
        if error is not None:
            return error
        date_range = applicant_date_range(taipei_today())
        # Parameter order is the one that was measured. `ec` is the front end's
        # traffic-source code ("came from the applications page"); it does not change
        # what comes back, so it is fixed rather than a parameter.
        params = [
            ("jobno", jobno),
            ("ec", "4"),
            ("type", ""),
            ("kws", ""),
            ("page", str(page)),
            ("start_date", date_range[0]),
            ("end_date", date_range[1]),
        ]
        try:
            async with guarded_api(ctx, ENDPOINTS["list_applicants"], params=params) as (result, _info):
                return build_applicant_page(
                    result, requested_jobno=jobno, requested_page=page, date_range=date_range
                )
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_applicants: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    _register_tool(mcp, list_applicants, _LIST_APPLICANTS_DESCRIPTION)

    async def list_refreshable_jobs(ctx: Context) -> dict:
        try:
            async with guarded_api(ctx, ENDPOINTS["list_refreshable_jobs"]) as (payload, _info):
                return build_refreshable_jobs(payload)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_refreshable_jobs: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

    _register_tool(mcp, list_refreshable_jobs, _LIST_REFRESHABLE_JOBS_DESCRIPTION)

    async def list_jobs(ctx: Context, page: int = 1) -> dict:
        params = [("page", str(page))]
        try:
            async with guarded_api(ctx, ENDPOINTS["list_jobs"], params=params) as (envelope, _info):
                job_page = build_job_page(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_jobs: 回應結構異常: %s", exc.detail)
            return _malformed_response_payload(exc)

        batches = match_number_batches([row["jobno"] for row in job_page["results"]])
        if not batches:
            return job_page

        # A second guard entry: the throttle gate needs the request count on entry, and
        # the batch count is only known once the job list has been read.
        try:
            async with guarded_sequence(ctx, slots_needed=len(batches)) as (request, _info):
                payloads = []
                for batch in batches:
                    payloads.append(
                        await request(
                            ENDPOINTS["match_number"], params=[("jobNos[]", jobno) for jobno in batch]
                        )
                    )
        except SessionUnavailable as e:
            # Expired, blocked, challenged or logged out: the caller must stop or
            # log in again, so a normal-looking job list would be misleading.
            return e.payload
        except GuardAbort as e:
            log.warning("list_jobs: 配對數查詢失敗，回傳不含配對數的職缺清單: %s", e.payload.get("error"))
            return mark_match_counts_unavailable(job_page, _match_counts_failure_warning(e.payload))

        try:
            return merge_match_counts(job_page, payloads)
        except MalformedResponseError as exc:
            log.error("list_jobs: 配對數回應結構異常: %s", exc.detail)
            return mark_match_counts_unavailable(
                job_page,
                f"配對數回應結構異常（{exc.detail}），today_match_count / total_match_count 為 null",
            )

    _register_tool(mcp, list_jobs, _LIST_JOBS_DESCRIPTION)
