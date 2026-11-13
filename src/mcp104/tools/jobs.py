"""The job tools: the read tools list_applicants, list_jobs, get_job_detail and
list_refreshable_jobs, and the one write tool, refresh_jobs.

Answers: given an already-classified 104 payload (browser/api_client.classify()'s
success `payload`), how does it become the documented tool-facing response — the
applicant rows and their date groups, the job rows with their dates and per-job match
counts, one job's posting content, the refreshable-job list, the per-job outcome of a
refresh. Deliberately does not: issue the HTTP request, decide session/auth failures, or
run the request throttle — tools/helpers.py's `guarded_api` and `guarded_sequence` own
all three. The pure functions below take already-classified content and return the
tool-facing dict (or raise MalformedResponseError when its shape departs from what was
measured); the tools themselves only check their arguments, go through a guard, and hand
the response to a pure function.

Wire facts: docs/104-site-facts.md §8.28–§8.31.
"""

import copy
import logging
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from types import MappingProxyType
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP

import mcp104.tools.discovery as discovery_mod
from mcp104.browser.api_client import ENDPOINTS
from mcp104.db.database import LINK_SOURCE_APPLICANT_ROW
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    SessionUnavailable,
    convert_keys,
    guarded_api,
    guarded_sequence,
    jobno_error,
    malformed_response_payload,
    normalise_jobno,
)
from mcp104.tools.registration import register_tool
from mcp104.tools.messaging import _send_verdict
from mcp104.tools.search import compact_exp_job, html_to_text
from mcp104.tools.status import id_link_pairs, remember_id_links

log = logging.getLogger("104-mcp.jobs")


# ── Applicant list ───────────────────────────────────────────────────────

# 104's web page asks for the last 60 days; the route accepts the window as two dates.
APPLICANT_WINDOW_DAYS = 60
_TAIPEI = timezone(timedelta(hours=8))
_DATE_FORMAT = "%Y/%m/%d"

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
        picked["expJobArr"] = [compact_exp_job(entry) for entry in picked["expJobArr"]]
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
# Match counts are looked up for the first 60 jobs of a page only. Worst case inside the
# session lock, every request timing out (5 s pacing floor + 15 s timeout each): the first
# segment is 5 + 2 x 15, the count segment 5 + 15k for k batches, and a cold
# account.json adds one event_last_info round of 5 + 15 — 60 + 15k seconds in all. k = 3
# is 105 s, 15 s under the 120 s client timeout; k = 4 would be 120 s, equal to it.
MATCH_NUMBER_MAX_JOBS = 60

_JOB_ROW_RAW_FIELDS = (
    frozenset(discovery_mod.JOB_ROW_FIELD_GLOSS)
    - {"todayMatchCount", "totalMatchCount"}
    - frozenset(discovery_mod.JOB_DATE_FIELDS)
)


def build_job_page(envelope: object) -> dict:
    """list_jobs' family-B envelope ({data, metadata}) -> the tool-facing page, every
    row's date fields and match counts None until `merge_job_dates` and
    `merge_match_counts` fill them. `pagination.page` /
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
        for name in discovery_mod.JOB_DATE_FIELDS:
            row[name] = None
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


@dataclass(frozen=True)
class JobListRow:
    """One row of GET /api/job/jobList's `jobDataAry`. `jobno` is the normalised string;
    `raw_jobno` is 104's own value, kept so a write sends it back exactly as 104 gave it.
    Every other field is 104's value as given, None when 104 left the key out. `fields`
    is the whole row as 104 sent it (read-only)."""

    jobno: str
    raw_jobno: object
    switch: object
    can_delete: object
    is_in_verify: object
    job_setting: object
    fields: Mapping[str, object]


@dataclass(frozen=True)
class JobListAccount:
    """The account-level keys of jobList's `result` that decide whether 104's own front
    end lets an open, close or delete happen — 104's values as given, None when the key
    is absent. Nothing here is type-checked: how each is read belongs to the caller. The
    operator's identity keys (`email`, `loginNo`, `nccPayload`) are never read."""

    permit: object
    co_status: object
    is_hunter: object
    is_dispatch: object
    is_new_model: object
    is_out_ensure: object
    po_num: object
    is_web: object


@dataclass(frozen=True)
class JobListAddOns:
    """The four paid-exposure tables of jobList's `result` (`hotOrder`, `selectJob`,
    `priority`, `jobSort`), each keyed by jobno on 104's side — 104's values as given, None
    when the key is absent. Nothing here is type-checked: `JobList.add_ons` reads them."""

    hot_order: object
    select_job: object
    priority: object
    job_sort: object


# JobListAddOns attribute -> the name `JobList.add_ons` reports it under.
_ADD_ON_KINDS = (
    ("hot_order", "focus"),
    ("select_job", "pick_up"),
    ("priority", "priority"),
    ("job_sort", "job_sort"),
)


@dataclass(frozen=True)
class JobList:
    """GET /api/job/jobList's `result`, parsed once: the rows in list order, the
    account-level keys and the four paid-exposure tables. The whole `result` is not kept."""

    rows: tuple[JobListRow, ...]
    account: JobListAccount
    page_total: object  # pageInfo.total_page as 104 gave it; None when absent
    add_on_tables: JobListAddOns

    def add_ons(self, jobno: str) -> dict[str, object]:
        """The paid-exposure entries of this job: `focus`, `pick_up`, `priority` and
        `job_sort`, each present only when its table has an entry for `jobno` (the entry as
        104 gave it). The tables are keyed by jobno; an empty table arrives as an empty
        array (a PHP empty associative array) and, like an absent one, has no entries. Any
        other type raises MalformedResponseError."""
        found: dict[str, object] = {}
        for attribute, name in _ADD_ON_KINDS:
            table = getattr(self.add_on_tables, attribute)
            if table is None or (isinstance(table, list) and not table):
                continue
            if not isinstance(table, dict):
                raise MalformedResponseError(f"jobList 的加值廣告表 {name} 不是物件")
            if jobno in table:
                found[name] = table[jobno]
        return found

    def total_page(self) -> int:
        """The list's page count. Raises MalformedResponseError unless 104 gave an
        integer — a caller that needs the whole list on one page cannot go on without it."""
        if not _is_int(self.page_total):
            raise MalformedResponseError("jobList 的 pageInfo.total_page 缺失或不是整數")
        return self.page_total


def parse_job_list(result: object) -> JobList:
    """GET /api/job/jobList's `result` -> a JobList. The one structural check of this list,
    shared by list_jobs' date merge and the open/close/delete tools. Raises
    MalformedResponseError when `result` is not an object, `jobDataAry` not an array, a row
    not an object, or a jobno not a job number. A missing `pageInfo` is not an error here
    (only `JobList.total_page()` needs it), and the four paid-exposure tables are kept as
    given, unchecked (only `JobList.add_ons` reads them)."""
    if not isinstance(result, dict):
        raise MalformedResponseError("jobList 的 result 缺失或非物件")
    raw_rows = result.get("jobDataAry")
    if not isinstance(raw_rows, list):
        raise MalformedResponseError("jobList 的 jobDataAry 缺失或非陣列")
    rows = []
    for raw in raw_rows:
        if not isinstance(raw, dict):
            raise MalformedResponseError("jobList 的職缺列不是物件")
        rows.append(
            JobListRow(
                jobno=normalise_jobno(raw.get("jobno")),
                raw_jobno=raw.get("jobno"),
                switch=raw.get("switch"),
                can_delete=raw.get("canDelete"),
                is_in_verify=raw.get("isInVerify"),
                job_setting=raw.get("job_setting"),
                fields=MappingProxyType(dict(raw)),
            )
        )
    page_info = result.get("pageInfo")
    account = JobListAccount(
        permit=result.get("permit"),
        co_status=result.get("coStatus"),
        is_hunter=result.get("isHunter"),
        is_dispatch=result.get("isDispatch"),
        is_new_model=result.get("isNewModel"),
        is_out_ensure=result.get("isOutEnsure"),
        po_num=result.get("poNum"),
        is_web=result.get("isWeb"),
    )
    return JobList(
        rows=tuple(rows),
        account=account,
        page_total=page_info.get("total_page") if isinstance(page_info, dict) else None,
        add_on_tables=JobListAddOns(
            hot_order=result.get("hotOrder"),
            select_job=result.get("selectJob"),
            priority=result.get("priority"),
            job_sort=result.get("jobSort"),
        ),
    )


@dataclass(frozen=True)
class Permit:
    """One entry of a 104 `permit` object, read: `allowed`, `denied` (with 104's own
    `message` when it gave a string) or `unknown` (the entry is absent or not the measured
    shape)."""

    state: Literal["allowed", "denied", "unknown"]
    message: str | None = None


def permit_state(permit: object, name: str) -> Permit:
    """Pure. Read `permit[name]` of a jobList or job/more `permit`: each entry is
    {result: bool, message: str}. `allowed` only when `result is True`, `denied` only when
    `result is False`; a `permit` that is not an object, a missing `name`, an entry that is
    not an object or a `result` that is not a boolean is `unknown` — never guessed into
    either of the other two."""
    if not isinstance(permit, dict):
        return Permit("unknown")
    entry = permit.get(name)
    if not isinstance(entry, dict):
        return Permit("unknown")
    result = entry.get("result")
    if result is True:
        return Permit("allowed")
    if result is False:
        message = entry.get("message")
        return Permit("denied", message if isinstance(message, str) else None)
    return Permit("unknown")


def parse_job_dates(result: object) -> dict[str, dict]:
    """GET /api/job/jobList's `result` -> {jobno: {the six JOB_DATE_FIELDS, 104's values
    as given, None for a key 104 left out}}. Reads `jobDataAry` only (through
    `parse_job_list`): the rest of `result` carries the operator's own identity and is
    never touched. A `result` that is not an object, a `jobDataAry` that is not an array,
    or a row that is not an object raises MalformedResponseError."""
    return {
        row.jobno: {name: row.fields.get(name) for name in discovery_mod.JOB_DATE_FIELDS}
        for row in parse_job_list(result).rows
    }


def merge_job_dates(page: dict, dates_by_jobno: dict[str, dict]) -> dict:
    """Fold jobList's dates into `page`'s rows by jobno (never by position). Returns a
    new dict; `page` is not modified. A row with no entry keeps its six fields None and
    is named in a warning: the two list routes' paging is unmeasured, so a job on this
    page of one may sit on another page of the other."""
    merged = copy.deepcopy(page)
    missing = []
    for row in merged["results"]:
        dates = dates_by_jobno.get(row["jobno"])
        if dates is None:
            missing.append(row["jobno"])
            continue
        row.update(dates)
    if missing:
        merged["warnings"].append(
            f"這些職缺不在 jobList 的同一頁上：{'、'.join(missing)}；它們的日期欄位為 null"
            "（兩條清單路由的分頁是否對齊未量測）"
        )
    return merged


def mark_job_dates_unavailable(page: dict, warning: str) -> dict:
    """Degrade to a job list without dates: a new dict, every row's six date fields None,
    `warning` appended. `page` is not modified."""
    degraded = copy.deepcopy(page)
    for row in degraded["results"]:
        for name in discovery_mod.JOB_DATE_FIELDS:
            row[name] = None
    degraded["warnings"].append(warning)
    return degraded


def match_number_batches(jobnos: Sequence[str]) -> list[list[str]]:
    """The first `MATCH_NUMBER_MAX_JOBS` jobnos in their given order, 20 to a batch."""
    covered = list(jobnos[:MATCH_NUMBER_MAX_JOBS])
    return [covered[i : i + MATCH_NUMBER_BATCH_SIZE] for i in range(0, len(covered), MATCH_NUMBER_BATCH_SIZE)]


def merge_match_counts(page: dict, payloads: Sequence[object]) -> dict:
    """Fold each match-number envelope's `data.list` into `page`'s rows by jobno (never
    by position). Returns a new dict; `page` is not modified. A row inside the first
    `MATCH_NUMBER_MAX_JOBS` with no entry, or with a count that is not a non-negative
    integer, keeps both counts None and is named in a warning; rows after that keep None
    with their own warning. A payload whose `data.list` is not an array raises MalformedResponseError."""
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


def _job_dates_failure_warning(error_payload: dict) -> str:
    return f"日期查詢失敗（{error_payload.get('error')}），六個日期欄位為 null"


def _match_counts_failure_warning(error_payload: dict) -> str:
    retry_after = error_payload.get("retry_after_seconds")
    if retry_after is not None:
        return (
            f"配對數查詢被節流拒絕，today_match_count / total_match_count 為 null；"
            f"請 {retry_after} 秒後再呼叫 list_jobs 取得配對數（retry_after_seconds={retry_after}）"
        )
    return f"配對數查詢失敗（{error_payload.get('error')}），today_match_count / total_match_count 為 null"


# ── Refreshable jobs ─────────────────────────────────────────────────────


@dataclass(frozen=True)
class RefreshableJob:
    """One row of the refreshable list. `raw_jobno` is 104's own value (a string or an
    int), kept so a refresh request sends it back exactly as 104 gave it."""

    jobno: str
    raw_jobno: object
    job: object
    appear_date: object


def parse_refreshable_jobs(payload: object) -> tuple[list[RefreshableJob], bool]:
    """over-seven-day-job's family-B envelope ({data: {jobs, userPermit}, metadata}) ->
    (its rows, userPermit). The one structural check of this list, shared by the list
    tool, the refresh pre-check and the refresh re-read; raises MalformedResponseError
    when `data` is not an object, `jobs` not an array, `userPermit` not a boolean, a row
    not an object or a jobNo not a job number."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MalformedResponseError("data 缺失或非物件")
    jobs = data.get("jobs")
    if not isinstance(jobs, list):
        raise MalformedResponseError("data.jobs 缺失或非陣列")
    permit = data.get("userPermit")
    if not isinstance(permit, bool):
        raise MalformedResponseError("data.userPermit 缺失或非布林")
    entries = []
    for raw in jobs:
        if not isinstance(raw, dict):
            raise MalformedResponseError("可更新職缺列不是物件")
        entries.append(
            RefreshableJob(
                jobno=normalise_jobno(raw.get("jobNo")),
                raw_jobno=raw.get("jobNo"),
                job=raw.get("jobName"),
                appear_date=raw.get("appearDate"),
            )
        )
    return entries, permit


def build_refreshable_jobs(payload: object) -> dict:
    """over-seven-day-job's family-B envelope -> the tool-facing page. `appear_date` is
    104's string as given."""
    entries, permit = parse_refreshable_jobs(payload)
    results = [{"jobno": e.jobno, "job": e.job, "appear_date": e.appear_date} for e in entries]
    warnings = []
    if not permit:
        warnings.append("這個帳號沒有更新職缺的權限（user_permit 為 false）；清單僅供參考")
    return {
        "results": results,
        "pagination": {"page": 1, "total_pages": None, "total": len(results)},
        "browse_limit": None,
        "warnings": warnings,
    }


# ── Refreshing job dates ─────────────────────────────────────────────────


def write_failure_disposition(
    abort: GuardAbort, *, batch_size: int
) -> Literal["reread_ambiguous", "reread_refused", "raise"]:
    """What a job write that aborted should do next — the one rule every job write tool
    (refresh, open, close, delete) shares.

    "reread_ambiguous": the write may have reached 104 and taken effect (a timeout, a
    dropped connection, a response that is not 104's JSON, and any kind the classifier
    learns later that `NOT_SENT` does not list) — read the list back and say the result
    follows it. "reread_refused": 104 refused a batch of more than one job (`validation`,
    `not_found`); whether part of the batch took effect is unmeasured, so read back and
    report per job. "raise": 104 did not take the write (session expired, blocked,
    challenged, a single job refused, a header or parameter fault, the throttle) — return
    the abort's payload."""
    if _send_verdict(abort.kind) == "ambiguous":
        return "reread_ambiguous"
    if abort.kind in ("validation", "not_found") and batch_size > 1:
        return "reread_refused"
    return "raise"


def refresh_preflight(requested: Sequence[str], entries: Sequence[RefreshableJob], permit: bool) -> str | None:
    """The check before anything is written: None when the account may refresh and every
    requested jobno is on the refreshable list, otherwise the error text. A jobno not on
    the list is named in the caller's order."""
    if permit is not True:
        return "這個帳號沒有更新職缺的權限（user_permit 為 false），沒有送出更新"
    listed = {e.jobno for e in entries}
    missing = [jobno for jobno in requested if jobno not in listed]
    if missing:
        return (
            f"這些職缺不在可更新清單上，沒有送出更新：{'、'.join(missing)}。只有 list_refreshable_jobs "
            "列出的職缺能更新。可能的原因（依 104 的規則，本工具尚未實測）：在可更新的間隔內已更新過"
            "（標準每 7 天一次、加購每 3 天一次）、近 14 天沒有求才行為、職缺已關閉；從清單本身分辨不出是哪一個"
        )
    return None


def refresh_body(requested: Sequence[str], entries: Sequence[RefreshableJob]) -> dict:
    """The PUT body: {"jobs": [{"jobNo": <104's own value>}, ...]} in the caller's order.
    Every requested jobno must be among `entries` (refresh_preflight says so first)."""
    by_jobno = {e.jobno: e for e in entries}
    return {"jobs": [{"jobNo": by_jobno[jobno].raw_jobno} for jobno in requested]}


def refresh_results(requested: Sequence[str], entries_after: Sequence[RefreshableJob]) -> list[dict]:
    """One row per requested jobno in the caller's order: "confirmed" when the re-read
    list no longer carries it, "unconfirmed" when it still does."""
    still_listed = {e.jobno for e in entries_after}
    return [
        {"jobno": jobno, "refreshed": "unconfirmed" if jobno in still_listed else "confirmed"}
        for jobno in requested
    ]


def all_unconfirmed(requested: Sequence[str]) -> list[dict]:
    """The result rows when the re-read did not happen or failed: nothing is confirmed."""
    return [{"jobno": jobno, "refreshed": "unconfirmed"} for jobno in requested]


_REFRESH_CONFIRMED_MESSAGE = (
    "已確認更新：這些職缺重讀可更新清單時已不在清單上（依 104 的規則——更新成功的職缺會離開可更新"
    "清單，本工具尚未實測；更新請求本身的回應也未量測）。"
)
_REFRESH_UNCONFIRMED_MESSAGE = (
    "這些職缺未確認已更新：重讀可更新清單時仍在清單上，或重讀失敗。這不代表失敗——職缺從清單移除可能有"
    "延遲。請到 104 後台確認，不要重送；list_jobs 的 appear_date（104 前端顯示為「更新日」）可做參考。"
)


def refresh_message(results: Sequence[dict]) -> str:
    confirmed = [r["jobno"] for r in results if r["refreshed"] == "confirmed"]
    unconfirmed = [r["jobno"] for r in results if r["refreshed"] != "confirmed"]
    parts = []
    if confirmed:
        parts.append(f"{_REFRESH_CONFIRMED_MESSAGE}職缺：{'、'.join(confirmed)}。")
    if unconfirmed:
        parts.append(f"{_REFRESH_UNCONFIRMED_MESSAGE}職缺：{'、'.join(unconfirmed)}。")
    return "\n".join(parts)


# ── One job's posting content ────────────────────────────────────────────

# The posting content get_job_detail publishes, as 104's top-level keys of
# GET /api/job/jobUpdate's `result`. Everything else is not posting content and is left
# out: `contaction` (the company staff's own contact details — treated like the
# operator's identity, no tool needs it), `additionalInfo` (editor and account internals,
# plus the code dictionaries), the account-type and capability flags, `otherSetting`
# apart from `closeDate`.
_JOB_DETAIL_KEPT_KEYS = ("jobName", "switch", "appearDate", "openDate", "shareLink", "department", "detail", "needCondition")


def build_job_detail(result: object, *, requested_jobno: str) -> dict:
    """GET /api/job/jobUpdate's `result` -> the tool-facing job (see
    `_JOB_DETAIL_KEPT_KEYS` for what is published). Keys are converted mechanically to
    snake_case, and only rich-text HTML in string values becomes plain text; codes are
    left exactly as 104 gave them. Raises MalformedResponseError when `result` is not an
    object, `jobName` is not a non-empty string, or `detail` is not an object."""
    if not isinstance(result, dict):
        raise MalformedResponseError("result 缺失或非物件")
    job_name = result.get("jobName")
    if not isinstance(job_name, str) or not job_name:
        raise MalformedResponseError("jobName 缺失或為空")
    if not isinstance(result.get("detail"), dict):
        raise MalformedResponseError("detail 缺失或非物件")
    picked = {key: result[key] for key in _JOB_DETAIL_KEPT_KEYS if key in result}
    other_setting = result.get("otherSetting")
    if isinstance(other_setting, dict) and "closeDate" in other_setting:
        picked["otherSetting"] = {"closeDate": other_setting["closeDate"]}
    job = convert_keys(picked, string_transform=html_to_text)
    job["jobno"] = requested_jobno
    return job


# ── Tool descriptions ────────────────────────────────────────────────────
#
# Each becomes the tool's actual __doc__ (see register_tool). Field meanings live in
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
total_match_count（目前配對人數），以及六個日期欄位 appear_date、modifydate、inputdate、
closedate、autoclose、d5（104 的原值；104 前端把 appear_date 顯示為「更新日」）；欄位意思
見 describe_result_fields(row_type="job")。

⚠ 一次呼叫是 2 個清單請求（職缺清單與日期），加上最多 3 個配對數請求（前 60 個職缺，每 20
個一批）。日期取不到（日期查詢失敗，或職缺不在日期清單的同一頁）時六個日期欄位為 null；
配對數查不到（節流、104 沒回、超過前 60 個）時兩個配對數為 null。原因都寫在 warnings，
null 不是 0。session 失效時整個呼叫回錯誤。

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
不會更新任何職缺；要更新請用 refresh_jobs。

成功時固定回傳 {"results": [...], "pagination": {"page","total_pages","total"},
"browse_limit": null, "warnings": [...]}；這條路由沒有分頁資訊，total_pages 為 null、total 是
這次拿到的筆數。帳號沒有更新權限時 warnings 會說明。沒有可更新的職缺是合法的空結果
（results 為 []）。失敗時回傳 {"error": str}，沒有 results 欄位。
"""

_GET_JOB_DETAIL_DESCRIPTION = """讀一個職缺的完整刊登內容（走 JSON API；唯讀，一次呼叫一個請求）。

Args:
    jobno: 職缺代碼，必填，全數字字串 —— 來自 list_jobs() 的 jobno。

成功時固定回傳 {"job": {...}, "browse_limit": null, "warnings": []}。job 含 jobno、
job_name、switch、appear_date、open_date、share_link（公開職缺頁的分享連結清單）、
department，以及 detail（說明、薪資、工作地點、時段等）、need_condition（條件）、
other_setting（只有 close_date）。鍵由 104 的原名機械式轉成 snake_case，只有富文字的 HTML
會轉成純文字；代碼欄位（role_id、education 等）照 104 的原樣、沒有加標籤。

不發布：公司同仁的聯絡資料（與操作者身分同類，沒有任何工具需要，不因刊登頁上看得到
而例外）、編輯器與帳號的內部資料、帳號類型旗標。

讀取兩次未觀察到鎖定，其他副作用未量測。

失敗時回傳 {"error": str}，沒有 job 欄位；職缺不存在（104 回 404）也是錯誤。
"""

_REFRESH_JOBS_DESCRIPTION = """更新職缺的日期（走 JSON API；寫入）。⚠ 會立刻改變職缺在求職者搜尋中的排序，
無法撤回；呼叫前必須先讓真人確認要更新哪些職缺。

Args:
    jobnos: 必填，要更新的 jobno 清單（全數字字串，不可重複）。沒有「全部更新」，要明確列出。
        只接受 list_refreshable_jobs 列出的職缺，其他的在送出前拒絕。

一次呼叫送三個請求：讀可更新清單、送出更新（PUT）、重讀可更新清單。

成功時固定回傳 {"results": [{"jobno", "refreshed"}], "message", "warnings"}，refreshed 只有
"confirmed" 與 "unconfirmed" 兩個值，照值判斷。confirmed：重讀時該職缺已不在可更新清單上
——依 104 的規則（更新成功的職缺會離開清單；本工具尚未實測），更新請求本身的回應未量測。
unconfirmed：重讀時仍在清單上，或重讀失敗；這不代表失敗，職缺移出清單可能有延遲，請到 104
後台確認，不要重送。更新請求逾時或回應無法辨識時照樣重讀；送多個職缺而 104 拒絕整批時
也照樣重讀並逐筆回報，拒絕文字在 warnings。

其餘情況回傳 {"error": str}（節流拒絕另帶 retry_after_seconds），沒有 results：送出前的
檢查不通過、只送一個職缺而被拒、session 過期、遭封鎖或挑戰頁。是否佔 104 額度未量測。
"""


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
            async with guarded_api(ctx, ENDPOINTS["list_applicants"], params=params) as (result, info):
                response = build_applicant_page(
                    result, requested_jobno=jobno, requested_page=page, date_range=date_range
                )
                await remember_id_links(
                    ctx.request_context.lifespan_context, info.account_label,
                    id_link_pairs(response["results"], id_no_key="candidate_id", p_id_key="p_id"),
                    LINK_SOURCE_APPLICANT_ROW,
                )
                return response
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_applicants: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)

    register_tool(mcp, list_applicants, _LIST_APPLICANTS_DESCRIPTION)

    async def list_refreshable_jobs(ctx: Context) -> dict:
        try:
            async with guarded_api(ctx, ENDPOINTS["list_refreshable_jobs"]) as (payload, _info):
                return build_refreshable_jobs(payload)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_refreshable_jobs: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)

    register_tool(mcp, list_refreshable_jobs, _LIST_REFRESHABLE_JOBS_DESCRIPTION)

    async def refresh_jobs(jobnos: list[str], ctx: Context) -> dict:
        if not jobnos:
            return {
                "error": "jobnos 是空的。本工具沒有「全部更新」，請明確列出要更新的 jobno"
                "（來自 list_refreshable_jobs）。"
            }
        for jobno in jobnos:
            error = jobno_error(jobno)
            if error is not None:
                return error
        duplicated = sorted({jobno for jobno in jobnos if jobnos.count(jobno) > 1})
        if duplicated:
            return {"error": f"jobnos 有重複：{'、'.join(duplicated)}。每個職缺只能列一次。"}

        requested = list(jobnos)
        warnings: list[str] = []
        # Set as the last act before the PUT is issued: the proof that the write may have
        # reached 104, read by the catch-all below.
        put_attempted = False

        async def _before_put(_info) -> None:
            nonlocal put_attempted
            put_attempted = True

        try:
            async with guarded_sequence(ctx, slots_needed=3) as (request, _info):
                entries, permit = parse_refreshable_jobs(await request(ENDPOINTS["list_refreshable_jobs"]))
                problem = refresh_preflight(requested, entries, permit)
                if problem is not None:
                    return {"error": problem}

                try:
                    await request(
                        ENDPOINTS["refresh_jobs"],
                        body=refresh_body(requested, entries),
                        before_request=_before_put,
                    )
                except GuardAbort as e:
                    disposition = write_failure_disposition(e, batch_size=len(requested))
                    if disposition == "reread_ambiguous":
                        # The PUT may have taken effect: only the list says. A timeout is
                        # the likeliest case of exactly that.
                        warnings.append("更新請求（PUT）逾時或回應無法辨識，結果以重讀可更新清單為準")
                    elif disposition == "reread_refused":
                        # Whether part of a refused batch took effect is unmeasured.
                        warnings.append(
                            f"104 拒絕了這批更新（{e.payload.get('error')}）；一批裡是否有職缺已部分生效未量測，"
                            "結果以重讀可更新清單為準"
                        )
                    else:
                        raise

                try:
                    entries_after, _permit_after = parse_refreshable_jobs(
                        await request(ENDPOINTS["list_refreshable_jobs"])
                    )
                    results = refresh_results(requested, entries_after)
                except (GuardAbort, MalformedResponseError) as exc:
                    reason = exc.payload.get("error") if isinstance(exc, GuardAbort) else exc.detail
                    warnings.append(f"重讀可更新清單失敗（{reason}），無法確認，每個職缺都記為未確認")
                    results = all_unconfirmed(requested)
                return {"results": results, "message": refresh_message(results), "warnings": warnings}
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("refresh_jobs: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)
        except Exception:
            log.error("refresh_jobs: 非預期例外 (put_attempted=%s)", put_attempted, exc_info=True)
            if put_attempted:
                results = all_unconfirmed(requested)
                return {
                    "results": results,
                    "message": refresh_message(results),
                    "warnings": warnings + ["發生內部錯誤，無法判斷是否已更新；請到 104 後台確認，不要重送"],
                }
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出職缺更新"}

    register_tool(mcp, refresh_jobs, _REFRESH_JOBS_DESCRIPTION)

    async def get_job_detail(jobno: str, ctx: Context) -> dict:
        error = jobno_error(jobno)
        if error is not None:
            return error
        params = [("act", "edit"), ("jobno", jobno)]
        try:
            async with guarded_api(ctx, ENDPOINTS["get_job_detail"], params=params) as (result, _info):
                return {
                    "job": build_job_detail(result, requested_jobno=jobno),
                    "browse_limit": None,
                    "warnings": [],
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("get_job_detail: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)

    register_tool(mcp, get_job_detail, _GET_JOB_DETAIL_DESCRIPTION)

    async def list_jobs(ctx: Context, page: int = 1) -> dict:
        params = [("page", str(page))]
        try:
            async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
                job_page = build_job_page(await request(ENDPOINTS["list_jobs"], params=params))
                try:
                    job_page = merge_job_dates(job_page, parse_job_dates(await request(ENDPOINTS["job_list"], params=params)))
                except SessionUnavailable:
                    # Expired, blocked, challenged or logged out: the caller must stop or
                    # log in again, so a normal-looking job list would be misleading.
                    raise
                except GuardAbort as e:
                    log.warning("list_jobs: 日期查詢失敗，回傳不含日期的職缺清單: %s", e.payload.get("error"))
                    job_page = mark_job_dates_unavailable(job_page, _job_dates_failure_warning(e.payload))
                except MalformedResponseError as exc:
                    log.error("list_jobs: 日期回應結構異常: %s", exc.detail)
                    job_page = mark_job_dates_unavailable(
                        job_page, f"日期回應結構異常（{exc.detail}），日期欄位為 null"
                    )
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_jobs: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)

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

    register_tool(mcp, list_jobs, _LIST_JOBS_DESCRIPTION)
