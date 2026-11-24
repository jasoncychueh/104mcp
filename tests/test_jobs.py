"""Tests for the job tools (list_applicants, list_jobs, get_job_detail, list_refreshable_jobs,
refresh_jobs), their pure builders, and the job-number helpers in tools/helpers.py --
the applicant list and job reads, plus dated list_jobs rows, get_job_detail and
refresh_jobs. The bodies of the job-dates routes (jobList, jobUpdate, the refresh
PUT) are synthesised in this file; no local fixture is read for them.

Cases in this file:
  job-number helpers:  T-45, T-46
  job tools:           T-1 .. T-26, T-30 .. T-44, T-47, T-48, T-50 .. T-55
  (T-26 lives in tests/test_discovery.py, which imports this file's harness.)

Assertions come from the public interfaces only, not from the implementation:
the three tools are reached through their registration entry (register_job_tools), the
pure builders through their module attributes, and every 104 request goes through the
real guard + classifier with only the transport (`fetch`) replaced by a scripted spy.
Nothing is mocked across the classify / guard seam.

Import discipline: tools/jobs.py is imported lazily (see _jobs()), so a missing module fails
the job-tool tests with ImportError but does not stop collection of the job-number helper tests above.

Fixture use: every body comes from tests/fixtures/. Two things are built in the
test body, not read from a fixture, and each says so where it happens:
  * a non-empty resumeGroups.today (no capture has one; T-8 moves a row into it);
  * match-number responses for 21 / 81 / 80 jobs (the only measured match-number fixture
    covers 5 jobnos; the rest are synthesised from the measured row shape).
"""
from __future__ import annotations

import copy
import importlib
import json
import re
import sys
import time
from datetime import date, datetime, timedelta, timezone
from uuid import uuid4

import pytest
import pytest_asyncio

import mcp104.tools.discovery as discovery_mod
import mcp104.tools.helpers as helpers
import mcp104.tools.search as search_mod
from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.helpers import get_session_id
from mcp104.tools.jobs import MATCH_NUMBER_MAX_JOBS
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from tests.conftest import _SeqFetchSpy, load_fixture

TAIPEI = timezone(timedelta(hours=8))
UTC = timezone.utc
MATCH_BATCH = 20
MATCH_CAP = MATCH_NUMBER_MAX_JOBS
CHALLENGE_TEXT = "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。"
NOT_LOGGED_IN = {"error": "請先呼叫 login()"}


# ── Fixture / fake-transport plumbing (mirrors tests/test_search.py and test_events.py) ─


def _raw(status, content_type, body, parsed_json, location=None) -> RawResponse:
    return RawResponse(status=status, location=location, content_type=content_type,
                       body=body, body_bytes=body.encode("utf-8"), parsed_json=parsed_json)


def _raw_from_wrapper(name: str) -> RawResponse:
    d = load_fixture(name)
    return _raw(d["http_status"], d["content_type"], d["body_text"], d["body_json"])


def _raw_from_body(body, status: int = 200) -> RawResponse:
    return _raw(status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)


def _challenge_raw() -> RawResponse:
    return _raw(200, "text/html; charset=utf-8", CHALLENGE_TEXT, None)


def _install(monkeypatch, scripted) -> _SeqFetchSpy:
    spy = _SeqFetchSpy(list(scripted))
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


class _FakeSessionObj:
    pass


class _FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class _FakeApp:
    def __init__(self, session_pool, database):
        self.session_pool = session_pool
        self.config = get_config()
        self.db = database


class _FakeCtx:
    def __init__(self, session_pool, database):
        self.session = _FakeSessionObj()
        self.request_context = _FakeRequestContext(_FakeApp(session_pool, database))


_OPENED_DATABASES: list[Database] = []


async def _new_session(tmp_path, *, logged_in: bool = True):
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = _FakeCtx(pool, database)
    info = None
    if logged_in:
        info = SessionInfo(cookies=[], account_label="test@104.com")
        pool.activate_direct(get_session_id(ctx), info)
    return ctx, info


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED_DATABASES:
        await _OPENED_DATABASES.pop().close()


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """Pacing floor's inline sleep is neutralised; pacing itself is tests/test_throttle.py's job."""
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


class _FakeMCP:
    """Records each tool under its name and its effective description, as FastMCP's
    `.tool()` decorator would (register-and-return-unmodified)."""

    def __init__(self):
        self.tools: dict[str, object] = {}
        self.counts: dict[str, int] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self.counts[name] = self.counts.get(name, 0) + 1
            self.tools[name] = fn
            self.descriptions[name] = kwargs.get("description") or fn.__doc__ or ""
            return fn
        return decorator


def _jobs():
    return importlib.import_module("mcp104.tools.jobs")


def _registered_tool(register, name: str):
    mcp = _FakeMCP()
    register(mcp)
    return mcp.tools[name]


def _tool(name: str):
    return _registered_tool(_jobs().register_job_tools, name)


def _search_tool(name: str):
    return _registered_tool(register_search_tools, name)


def _messaging_tool(name: str):
    return _registered_tool(register_messaging_tools, name)


def _jobno() -> str:
    """The job number rows_applicants.json was captured for. list_applicants compares the
    response's jobNo with the request, so the request must carry this machine's value."""
    return str(load_fixture("rows_applicants.json")["result"]["jobNo"])


async def _call_tool(name: str, ctx, jobno: str | None = None):
    if name == "list_applicants":
        return await _tool(name)(jobno if jobno is not None else _jobno(), ctx=ctx)
    return await _tool(name)(ctx=ctx)


# ── Request inspection (params are whatever the tool passes; both dict and pair-list accepted) ─

def _pairs(params):
    if params is None:
        return []
    if isinstance(params, dict):
        return list(params.items())
    return [tuple(p) for p in params]


def _pdict(params) -> dict:
    return dict(_pairs(params))


def _jobnos_sent(params) -> list[str]:
    """The jobNos[] values a match-number request carries, in order. The design does not pin
    how a repeated list parameter is encoded, so both a dict entry holding a list and one
    pair per value are accepted. Anything else fails loudly rather than guessing."""
    found, out = False, []
    for key, value in _pairs(params):
        if str(key).startswith("jobNos"):
            found = True
            if isinstance(value, (list, tuple)):
                out.extend(str(v) for v in value)
            else:
                out.append(str(value))
    if not found:
        raise AssertionError(f"no jobNos[] entry in the match-number request params: {params!r}")
    return out


def _request_text(endpoint, params, body) -> str:
    return f"{getattr(endpoint, 'path', '')}|{params!r}|{body!r}"


def _walk_keys(node):
    if isinstance(node, dict):
        for key, value in node.items():
            yield key
            yield from _walk_keys(value)
    elif isinstance(node, list):
        for value in node:
            yield from _walk_keys(value)


def _text(value):
    return search_mod.html_to_text(value) if isinstance(value, str) else value


def _fixture_rows(body) -> list:
    """Applicant rows in the order the tool should emit them: today first, then each day group."""
    groups = body["result"]["resumeGroups"]
    rows = list(groups["today"])
    for group in groups["others"]:
        rows.extend(group["resumes"])
    return rows


# ── Synthetic job / match bodies ──────────────────────────────────────────────────────────

def _fake_jobnos(n: int) -> list[str]:
    return [f"117{i:05d}" for i in range(n)]


def _today_of(jobno: str) -> str:
    return str(int(jobno) % 7)


def _total_of(jobno: str) -> str:
    return str(int(jobno) % 1000)


def _expected_counts(jobno: str) -> tuple[int, int]:
    return int(_today_of(jobno)), int(_total_of(jobno))


def _match_body(jobnos, *, omit=(), total_overrides=None) -> dict:
    """A match-number envelope with one row per requested jobno, in the measured row shape
    (`jobNo`, `todayMatchCount`, `totalMatchCount`, string counts). Synthesised for the
    21 / 81 / 80-job cases, where no measured body covers the batch."""
    overrides = total_overrides or {}
    rows = [
        {"jobNo": jn, "todayMatchCount": _today_of(jn), "totalMatchCount": overrides.get(jn, _total_of(jn))}
        for jn in jobnos if jn not in omit
    ]
    return {"data": {"list": rows}, "metadata": {}}


def _batches(jobnos) -> list[list[str]]:
    capped = list(jobnos)[:MATCH_CAP]
    return [capped[i:i + MATCH_BATCH] for i in range(0, len(capped), MATCH_BATCH)]


def _jobs_body(jobnos) -> dict:
    template = load_fixture("jobs_apply.json")["data"][0]
    rows = []
    for i, jn in enumerate(jobnos):
        row = copy.deepcopy(template)
        row["jobno"] = jn
        row["job"] = f"職缺{i:03d}"
        rows.append(row)
    return {"data": rows, "metadata": {"total": len(rows), "page": 1, "hasSubDepartment": True}}


def _require_two_count_batches(jobs) -> None:
    """Precondition for tests that look at the first and second count batch: jobs_apply.json
    must hold 21-40 jobs (20 per count request)."""
    assert 20 < len(jobs) <= 40, (
        "jobs_apply.json needs 21-40 jobs so the count lookup takes two batches (tests/fixtures/README.md)"
    )


def _jobnos_of(body) -> list[str]:
    return [str(row["jobno"]) for row in body["data"]]


# Synthetic operator identity carried in jobList's `result`: none of it may ever reach a
# tool response.
_OPERATOR_EMAIL = "operator-sentinel@example.invalid"
_OPERATOR_LOGIN_NO = "login-no-sentinel-0001"
_OPERATOR_BUSER_ID = "buserid-sentinel-0001"


def _dates_of(jobno: str) -> dict:
    n = int(jobno) % 28 + 1
    return {
        "appear_date": f"2026-11-{n:02d} 09:56:15",
        "modifydate": f"2026-10-{n:02d} 10:00:00",
        "inputdate": f"2021-08-{n:02d} 15:31:52",
        "closedate": None,
        "autoclose": "0",
        "d5": "",
    }


def _job_list_body(jobnos, *, omit=()) -> dict:
    """A jobList envelope in the measured shape (old envelope; `result.jobDataAry` rows;
    the operator's identity beside them), one row per jobno not in `omit`."""
    rows = [
        {"jobno": jn, "job": f"jobList 的職缺 {jn}", "switch": "on", "custno": "1", **_dates_of(jn)}
        for jn in jobnos if jn not in omit
    ]
    return {
        "status": "SUCCESS", "message": "", "url": "", "data": None,
        "result": {
            "jobDataAry": rows,
            "pageInfo": {"curPage": "1", "total_page": 1, "total": len(rows)},
            "applyCount": {},
            "email": _OPERATOR_EMAIL,
            "loginNo": _OPERATOR_LOGIN_NO,
            "nccPayload": {"web": "vip_new", "ext": {"Buserid": _OPERATOR_BUSER_ID, "custno": "1"}},
        },
    }


def _list_jobs_script(jobs_body, *, match_bodies=None, job_list_raw=None) -> list[RawResponse]:
    """jobs/apply, then jobList, then one response per match-number batch."""
    if match_bodies is None:
        match_bodies = [_match_body(batch) for batch in _batches(_jobnos_of(jobs_body))]
    if job_list_raw is None:
        job_list_raw = _raw_from_body(_job_list_body(_jobnos_of(jobs_body)))
    return [_raw_from_body(jobs_body), job_list_raw] + [_raw_from_body(body) for body in match_bodies]


async def _run_list_jobs(tmp_path, monkeypatch, jobs_body, *, match_bodies=None, job_list_raw=None):
    ctx, info = await _new_session(tmp_path)
    spy = _install(
        monkeypatch, _list_jobs_script(jobs_body, match_bodies=match_bodies, job_list_raw=job_list_raw)
    )
    result = await _tool("list_jobs")(ctx=ctx)
    return result, spy, info


def _applicant_raw(mutate) -> RawResponse:
    body = load_fixture("rows_applicants.json")
    mutate(body)
    return _raw_from_body(body)


# ══ Job-number helpers — T-45, T-46 ═══════════════════════════════════════════════════════

# -- T-45: normalise_jobno --------------------------------------------------------

@pytest.mark.parametrize("value", [11706763, "11706763"], ids=["int", "digit-string"])
def test_normalise_jobno_returns_the_digit_string_for_an_int_or_a_digit_string(value):
    assert helpers.normalise_jobno(value) == "11706763"


@pytest.mark.parametrize("value", [None, "", "12a", 1.5], ids=["none", "empty", "letters", "float"])
def test_normalise_jobno_refuses_anything_that_is_not_a_job_number(value):
    with pytest.raises(helpers.MalformedResponseError):
        helpers.normalise_jobno(value)


# -- T-46: jobno_error ------------------------------------------------------------

def test_jobno_error_passes_a_non_empty_digit_string_without_an_error():
    assert helpers.jobno_error("11706763") is None


@pytest.mark.parametrize(
    "value", [11706763, None, "", " 11706763"], ids=["int", "none", "empty", "leading-space"]
)
def test_jobno_error_refuses_non_string_empty_or_padded_input_and_names_list_jobs(value):
    result = helpers.jobno_error(value)
    assert isinstance(result, dict), f"expected an error dict for {value!r}, got {result!r}"
    assert isinstance(result.get("error"), str) and result["error"], (
        f"the error for {value!r} must carry a non-empty message"
    )
    assert "list_jobs" in result["error"], (
        f"the message for {value!r} must say the jobno comes from list_jobs: {result['error']!r}"
    )


# ══ Job tools ══════════════════════════════════════════════════════════════════════

# -- T-1 / T-2: the window is 60 days ending on Taipei's today -------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "now",
    [datetime(2026, 11, 12, 10, 0, tzinfo=TAIPEI), datetime(2026, 11, 11, 17, 0, tzinfo=UTC)],
    ids=["T-1-taipei-10h", "T-2-utc-prev-day-17h"],
)
async def test_applicant_window_is_sixty_days_ending_on_taipei_today(tmp_path, monkeypatch, now):
    jobs = _jobs()
    real_taipei_today = jobs.taipei_today
    monkeypatch.setattr(jobs, "taipei_today", lambda *args, **kwargs: real_taipei_today(now=now))
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert len(spy.calls) == 1
    params = _pdict(spy.calls[0][1])
    assert params["start_date"] == "2026/09/13"
    assert params["end_date"] == "2026/11/12"
    assert result["date_range"] == {"start_date": "2026/09/13", "end_date": "2026/11/12"}


# -- T-3: candidate_id is idNo, p_id is pId; no raw idNo on the row -------------

@pytest.mark.asyncio
async def test_each_applicant_row_carries_candidate_id_from_id_no_and_p_id_from_p_id(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    rows = (await _tool("list_applicants")(_jobno(), ctx=ctx))["results"]

    source = _fixture_rows(load_fixture("rows_applicants.json"))
    assert [r["candidate_id"] for r in rows] == [str(s["idNo"]) for s in source]
    assert [r["p_id"] for r in rows] == [str(s["pId"]) for s in source]
    for row in rows:
        assert "id_no" not in row and "idNo" not in row


# -- T-4: the two identifiers are accepted by the tools they are meant for --

@pytest.mark.asyncio
async def test_p_id_reaches_get_conversation_and_candidate_id_reaches_get_resume_detail(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])
    row = (await _tool("list_applicants")(_jobno(), ctx=ctx))["results"][0]

    conversation_spy = _install(monkeypatch, [_raw_from_body({"data": [], "metadata": {}})])
    await _messaging_tool("get_conversation")(job_id=_jobno(), candidate_id=row["p_id"], ctx=ctx)
    assert conversation_spy.calls, "get_conversation must reach the transport: the id guard may not reject a p_id"
    assert row["p_id"] in _request_text(*conversation_spy.calls[0])

    detail_spy = _install(monkeypatch, [_raw_from_body({"data": {}, "metadata": {}})])
    await _search_tool("get_resume_detail")(candidate_id=row["candidate_id"], ctx=ctx)
    assert detail_spy.calls, "get_resume_detail must reach the transport"
    assert row["candidate_id"] in _request_text(*detail_spy.calls[0])


# -- T-5: the row carries only allowlisted fields (plus input_date) -------------

def _converted_applicant_name(raw: str) -> str:
    renames = {"idNo": "candidate_id", "pId": "p_id"}
    if raw in renames:
        return renames[raw]
    (converted,) = helpers.convert_keys({raw: None}).keys()
    return converted


@pytest.mark.asyncio
async def test_applicant_row_keys_are_within_the_allowlist_and_every_present_allowed_field_appears(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    rows = (await _tool("list_applicants")(_jobno(), ctx=ctx))["results"]

    allowed = {_converted_applicant_name(raw): raw for raw in discovery_mod.APPLICANT_ROW_FIELD_GLOSS}
    for row in rows:
        assert set(row) <= set(allowed) | {"input_date"}, f"unexpected keys: {set(row) - set(allowed) - {'input_date'}}"
    for source, row in zip(_fixture_rows(load_fixture("rows_applicants.json")), rows):
        for name, raw in allowed.items():
            if raw in source:
                assert name in row, f"allowed field {raw!r} is in the fixture row but missing as {name!r}"


# -- T-6: exp_job_arr keeps four fields per entry; no note -----------------------

@pytest.mark.asyncio
async def test_exp_job_arr_entries_keep_only_four_fields_with_fixture_values(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    rows = (await _tool("list_applicants")(_jobno(), ctx=ctx))["results"]

    four = {"exp_firm", "exp_title", "exp_start", "exp_end"}
    pairs = [("expFirm", "exp_firm"), ("expTitle", "exp_title"), ("expStart", "exp_start"), ("expEnd", "exp_end")]
    for source, row in zip(_fixture_rows(load_fixture("rows_applicants.json")), rows):
        entries = row["exp_job_arr"]
        source_entries = source.get("expJobArr") or []
        assert len(entries) == len(source_entries)
        for raw_entry, entry in zip(source_entries, entries):
            assert set(entry) <= four
            assert "exp_job_note" not in entry
            for camel, snake in pairs:
                if camel in raw_entry:
                    assert entry[snake] == _text(raw_entry[camel]), f"{snake} differs from the fixture"


# -- T-7: excluded fields never appear, at any level of the response ------------

_FORBIDDEN_ROW_KEYS = {
    "personal_pic", "personalPic", "remark", "remark_without_html", "remarkWithoutHtml",
    "other_course", "otherCourse", "fitness", "auto_no", "autoNo", "snapshot_id", "snapshotId",
    "document_snapshot_id", "documentSnapshotId", "job_no", "jobNo", "job_name", "jobName",
}


@pytest.mark.asyncio
async def test_excluded_applicant_fields_never_reach_the_caller(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    for row in result["results"]:
        assert not (set(row) & _FORBIDDEN_ROW_KEYS), f"excluded fields leaked: {set(row) & _FORBIDDEN_ROW_KEYS}"
    assert not ({"ncc_log_base_data", "nccLogBaseData"} & set(_walk_keys(result)))


# -- T-8: the today group ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_today_group_row_has_null_input_date_and_leads_the_list(tmp_path, monkeypatch):
    # No capture has a non-empty resumeGroups.today, so the today group is built here by
    # moving one row out of a two-row day group into `today`. `today` is a
    # plain row list (its rows carry no group date), which is also unmeasured.
    body = load_fixture("rows_applicants.json")
    groups = body["result"]["resumeGroups"]
    source_group = next((g for g in groups["others"] if len(g["resumes"]) >= 2), None)
    assert source_group is not None, (
        "rows_applicants.json needs a date group with at least 2 applicants (tests/fixtures/README.md)"
    )
    moved = source_group["resumes"].pop(0)
    groups["today"].append(moved)
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(body)])

    rows = (await _tool("list_applicants")(_jobno(), ctx=ctx))["results"]

    assert rows[0]["p_id"] == str(moved["pId"])
    assert rows[0]["input_date"] is None
    expected_dates = [g["inputDate"] for g in groups["others"] for _ in g["resumes"]]
    assert [r["input_date"] for r in rows[1:]] == expected_dates
    assert all(r["input_date"] is not None for r in rows[1:])


# -- T-9: the job header -----------------------------------------------------------

@pytest.mark.asyncio
async def test_job_header_carries_jobno_as_a_string_plus_name_switch_and_role(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    source = load_fixture("rows_applicants.json")["result"]
    assert result["job"]["jobno"] == _jobno() and isinstance(result["job"]["jobno"], str)
    assert result["job"]["job"] == source["jobName"]
    assert result["job"]["switch"] == source["jobSwitch"]
    assert result["job"]["role"] == source["jobRole"]


# -- T-10: resume_number is four ints ----------------------------------------------

@pytest.mark.asyncio
async def test_resume_number_buckets_are_ints_equal_to_the_fixture_strings(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    src = load_fixture("rows_applicants.json")["result"]["resumeNumber"]
    assert result["resume_number"] == {
        "match": int(src["match"]),
        "not_match": int(src["notMatch"]),
        "is_black": int(src["isBlack"]),
        "recycle": int(src["recycle"]),
    }
    assert all(isinstance(v, int) for v in result["resume_number"].values())


# -- T-11: a populated other bucket is named; zero buckets are not ------------------

@pytest.mark.asyncio
async def test_a_nonzero_other_bucket_adds_a_warning_naming_the_count_and_unmeasured_scope(tmp_path, monkeypatch):
    body = load_fixture("rows_applicants.json")
    body["result"]["resumeNumber"]["notMatch"] = "2"
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert any("2" in w and "未量測" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_all_three_other_buckets_at_zero_add_no_such_warning(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    body = load_fixture("rows_applicants.json")
    numbers = body["result"]["resumeNumber"]
    assert all(int(numbers[k]) == 0 for k in ("notMatch", "isBlack", "recycle")), (
        "rows_applicants.json: notMatch, isBlack and recycle must all be 0 (tests/fixtures/README.md)"
    )
    assert body["result"]["pageInfo"]["total_page"] == 1, (
        "rows_applicants.json: pageInfo.total_page must be 1 (tests/fixtures/README.md)"
    )
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert not any("未量測" in w for w in result["warnings"]), result["warnings"]


# -- T-12: zero applicants is a legal empty result --------------------------------

@pytest.mark.asyncio
async def test_zero_applicant_job_is_an_empty_success_not_an_error(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    body = load_fixture("zero_row_applicants.json")
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_applicants")(str(body["result"]["jobNo"]), ctx=ctx)

    assert "error" not in result
    assert result["results"] == []
    assert result["pagination"]["total"] == 0


# -- T-13: pagination mirrors pageInfo; more pages are named --------------------

@pytest.mark.asyncio
async def test_pagination_mirrors_the_page_info_of_the_response(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    page_info = load_fixture("rows_applicants.json")["result"]["pageInfo"]
    assert result["pagination"] == {
        "page": page_info["thisPage"],
        "total_pages": page_info["total_page"],
        "total": page_info["total"],
    }


@pytest.mark.asyncio
async def test_a_second_page_is_named_as_unmeasured_in_a_warning(tmp_path, monkeypatch):
    body = load_fixture("rows_applicants.json")
    body["result"]["pageInfo"]["total_page"] = 3
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert result["pagination"]["total_pages"] == 3
    assert any("未量測" in w and "2" in w for w in result["warnings"]), result["warnings"]


# -- T-14: no browse quota on the applicant list ----------------------------------

@pytest.mark.asyncio
async def test_applicant_list_reports_no_browse_limit(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert result["browse_limit"] is None


# -- T-15: a bad jobno is refused before any request --------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abc", "117 06763", "11706763a"])
async def test_non_digit_jobno_is_refused_naming_list_jobs_and_sends_nothing(tmp_path, monkeypatch, bad):
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])

    result = await _tool("list_applicants")(bad, ctx=ctx)

    assert "error" in result and "list_jobs" in result["error"]
    assert "results" not in result
    assert spy.calls == []


# -- T-16: apply_count60 per job, as int ---------------------------------------------

@pytest.mark.asyncio
async def test_list_jobs_carries_apply_count60_as_an_int_per_job(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    assert len(result["results"]) == len(body["data"])
    for row, source in zip(result["results"], body["data"]):
        assert row["apply_count60"] == source["applyCount60"]
        assert type(row["apply_count60"]) is int


# -- T-17: no all-time apply count, even when the fixture carries one ----------------

@pytest.mark.asyncio
async def test_list_jobs_rows_never_carry_apply_count(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    assert any("applyCount" in row for row in body["data"])  # the fixture does carry it
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    for row in result["results"]:
        assert "apply_count" not in row and "applyCount" not in row


# -- T-18: same order as jobs/apply, jobno as string, list fields present ------------

@pytest.mark.asyncio
async def test_list_jobs_keeps_jobs_apply_order_with_string_jobnos_and_the_listed_fields(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    assert [r["jobno"] for r in result["results"]] == [s["jobno"] for s in body["data"]]
    for row, source in zip(result["results"], body["data"]):
        assert isinstance(row["jobno"], str)
        assert row["job"] == source["job"]
        assert row["switch"] == source["switch"]
        assert row["role"] == source["role"]
        assert set(row["department"]) == set(source["department"])


# -- T-19: the allowlist, not the upstream body, decides the row ---------------------

@pytest.mark.asyncio
async def test_an_unlisted_upstream_field_is_not_on_the_row(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    body["data"][0]["zzNewField"] = "zz-sentinel"
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    row = result["results"][0]
    assert "zz_new_field" not in row and "zzNewField" not in row
    assert "zz-sentinel" not in json.dumps(result, ensure_ascii=False)


# -- T-20: 21-40 jobs -> jobs list, then a 20-job batch and a remainder batch ----------

@pytest.mark.asyncio
async def test_more_than_twenty_jobs_send_a_list_then_two_count_batches_and_each_row_gets_its_own_counts(
    tmp_path, monkeypatch
):
    body = load_fixture("jobs_apply.json")
    jobnos = _jobnos_of(body)
    _require_two_count_batches(jobnos)
    result, spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    assert [endpoint.key for endpoint, _p, _b in spy.calls] == ["list_jobs", "job_list", "match_number", "match_number"]
    assert _jobnos_sent(spy.calls[2][1]) == jobnos[:20]
    assert _jobnos_sent(spy.calls[3][1]) == jobnos[20:]
    for row in result["results"]:
        assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(row["jobno"])
        assert type(row["today_match_count"]) is int and type(row["total_match_count"]) is int


# -- T-21: a missing or unreadable count nulls that row and names its jobno ----------

@pytest.mark.asyncio
async def test_a_job_missing_from_the_match_response_is_null_and_named(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    jobnos = _jobnos_of(body)
    _require_two_count_batches(jobnos)
    batches = _batches(jobnos)
    missing = jobnos[2]
    match_bodies = [_match_body(batches[0], omit={missing})] + [_match_body(b) for b in batches[1:]]

    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body, match_bodies=match_bodies)

    rows = {r["jobno"]: r for r in result["results"]}
    assert rows[missing]["today_match_count"] is None and rows[missing]["total_match_count"] is None
    for jn, row in rows.items():
        if jn != missing:
            assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(jn)
    assert any(missing in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_a_non_numeric_total_count_is_null_and_named(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    jobnos = _jobnos_of(body)
    _require_two_count_batches(jobnos)
    batches = _batches(jobnos)
    bad = jobnos[5]
    match_bodies = [_match_body(batches[0], total_overrides={bad: "abc"})] + [_match_body(b) for b in batches[1:]]

    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body, match_bodies=match_bodies)

    rows = {r["jobno"]: r for r in result["results"]}
    assert rows[bad]["today_match_count"] is None and rows[bad]["total_match_count"] is None
    for jn, row in rows.items():
        if jn != bad:
            assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(jn)
    assert any(bad in w for w in result["warnings"]), result["warnings"]


# -- T-22: only the first MATCH_CAP (60) jobs get counts ----------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("count", [MATCH_CAP + 1, MATCH_CAP], ids=["61-jobs", "60-jobs"])
async def test_only_the_first_sixty_jobs_are_matched_and_the_sixty_first_is_named(tmp_path, monkeypatch, count):
    jobnos = _fake_jobnos(count)
    body = _jobs_body(jobnos)
    ctx, info = await _new_session(tmp_path)
    before = len(info.throttle.request_timestamps)
    spy = _install(monkeypatch, _list_jobs_script(body))

    result = await _tool("list_jobs")(ctx=ctx)

    assert [endpoint.key for endpoint, _p, _b in spy.calls] == ["list_jobs", "job_list"] + ["match_number"] * 3
    for index, call in enumerate(spy.calls[2:]):
        assert _jobnos_sent(call[1]) == jobnos[MATCH_BATCH * index: MATCH_BATCH * (index + 1)]
    rows = result["results"]
    assert len(rows) == count
    for row in rows[:MATCH_CAP]:
        assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(row["jobno"])
    cap_warning = f"前 {MATCH_CAP} 個職缺"
    if count > MATCH_CAP:
        assert rows[MATCH_CAP]["today_match_count"] is None and rows[MATCH_CAP]["total_match_count"] is None
        assert any(cap_warning in w for w in result["warnings"]), result["warnings"]
    else:
        assert not any(cap_warning in w for w in result["warnings"]), result["warnings"]
    # Two list requests plus three count batches, each its own entry in the rolling window.
    assert len(info.throttle.request_timestamps) - before == 5


# -- T-23: a session failure on the second segment is the same error as elsewhere --

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_raw",
    [_challenge_raw, lambda: _raw_from_wrapper("failure_family_a_expired.json")],
    ids=["challenge", "expired"],
)
async def test_second_segment_session_failure_returns_the_error_list_matched_resumes_returns(tmp_path, monkeypatch, make_raw):
    ref_ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])
    reference = await _search_tool("list_matched_resumes")(jobno=_jobno(), ctx=ref_ctx)

    ctx, _ = await _new_session(tmp_path)
    jobs_apply = load_fixture("jobs_apply.json")
    _install(monkeypatch, [
        _raw_from_body(jobs_apply), _raw_from_body(_job_list_body(_jobnos_of(jobs_apply))), make_raw(),
    ])
    result = await _tool("list_jobs")(ctx=ctx)

    assert result == reference
    assert "error" in result and "results" not in result


# -- T-50: a count batch the window cannot take is skipped and warned; other count
#    failures keep the job list too ----------------------------------------------------------

@pytest.mark.asyncio
async def test_count_batch_that_does_not_fit_the_window_is_skipped_and_warned(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    jobs_apply = load_fixture("jobs_apply.json")
    _require_two_count_batches(jobs_apply["data"])
    # One slot is left after the two list requests, and the two count batches need two.
    for _ in range(app.config.max_requests_per_hour - 3):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install(monkeypatch, [
        _raw_from_body(jobs_apply), _raw_from_body(_job_list_body(_jobnos_of(jobs_apply))),
    ])

    result = await _tool("list_jobs")(ctx=ctx)

    assert len(spy.calls) == 2, "no count request may be sent when the gate refuses the second segment"
    assert "error" not in result and len(result["results"]) == len(jobs_apply["data"])
    assert all(r["today_match_count"] is None and r["total_match_count"] is None for r in result["results"])
    assert result["warnings"] and any(re.search(r"\d", w) for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "match_raw",
    [
        lambda: _raw_from_body({"error": {"code": "00005", "message": "jobNos 數量超過上限"}}, status=400),
        lambda: _raw_from_body({"data": {"list": "not-an-array"}, "metadata": {}}),
    ],
    ids=["http-400-validation", "data-list-not-array"],
)
async def test_count_failures_keep_the_job_list_and_null_the_counts(tmp_path, monkeypatch, match_raw):
    ctx, _info = await _new_session(tmp_path)
    jobs_apply = load_fixture("jobs_apply.json")
    _install(monkeypatch, [
        _raw_from_body(jobs_apply), _raw_from_body(_job_list_body(_jobnos_of(jobs_apply))), match_raw(),
    ])

    result = await _tool("list_jobs")(ctx=ctx)

    assert "error" not in result and "results" in result
    assert len(result["results"]) == len(jobs_apply["data"])
    assert all(r["today_match_count"] is None and r["total_match_count"] is None for r in result["results"])
    assert result["warnings"], "the reason the counts are missing must be stated"


# -- T-24: refreshable rows are {jobno, job, appear_date} --------------------------

@pytest.mark.asyncio
async def test_refreshable_rows_are_jobno_job_and_appear_date(tmp_path, monkeypatch):
    body = load_fixture("refreshable_jobs.json")
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_refreshable_jobs")(ctx=ctx)

    source = body["data"]["jobs"]
    assert len(result["results"]) == len(source)
    for row, src in zip(result["results"], source):
        assert set(row) == {"jobno", "job", "appear_date"}
        assert isinstance(row["jobno"], str) and row["jobno"] == str(src["jobNo"])
        assert row["job"] == src["jobName"]
        assert row["appear_date"] == src["appearDate"]


# -- T-25: no update permission is named; permission is silent --------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "name, expect_warning",
    [("refreshable_jobs_no_permit.json", True), ("refreshable_jobs.json", False)],
    ids=["no-permit-derived", "permit-measured"],
)
async def test_missing_update_permission_is_named_in_a_warning(tmp_path, monkeypatch, name, expect_warning):
    raw_doc = load_fixture(name)
    body = raw_doc["body_json"] if "body_json" in raw_doc else raw_doc
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_refreshable_jobs")(ctx=ctx)

    mentions_permission = any("權限" in w for w in result["warnings"])
    assert mentions_permission is expect_warning, result["warnings"]


# -- T-30: auth failures on the first request match list_matched_resumes ----------

_FAILURE_SCENARIOS = {
    "expired-html-200": lambda: _raw_from_wrapper("failure_family_a_expired.json"),
    "company-switch-html-200": lambda: _raw_from_wrapper("failure_applicants_company_switch.json"),
    "forbidden-403": lambda: _raw(403, "text/html; charset=utf-8", "Forbidden", None),
    "challenge-200": _challenge_raw,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(_FAILURE_SCENARIOS))
@pytest.mark.parametrize("tool", ["list_applicants", "list_jobs", "list_refreshable_jobs"])
async def test_first_request_auth_failure_is_the_same_error_as_list_matched_resumes(tmp_path, monkeypatch, scenario, tool):
    make_raw = _FAILURE_SCENARIOS[scenario]
    ref_ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])
    reference = await _search_tool("list_matched_resumes")(jobno=_jobno(), ctx=ref_ctx)

    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])
    result = await _call_tool(tool, ctx)

    assert result == reference
    assert "error" in result and "results" not in result


@pytest.mark.asyncio
async def test_company_switch_html_on_applicants_reads_as_expired_not_unrecognised(tmp_path, monkeypatch):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_wrapper("failure_applicants_company_switch.json")])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert "已過期" in result["error"]
    assert "無法辨識" not in result["error"]


# -- T-31: a non-SUCCESS applyDetail is an error, never an empty list ---------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_raw",
    [
        lambda: _raw_from_body({"status": "FAIL", "message": "x", "result": None}),
        lambda: _raw_from_body({"status": "FAIL", "message": "x", "result": None}, status=400),
        lambda: _raw_from_body({"error": "unauthenticated"}),
        lambda: _raw_from_wrapper("failure_family_b_unauthenticated.json"),
    ],
    ids=["fail-200", "fail-400", "family-b-error-body", "family-b-unauthenticated-fixture"],
)
async def test_non_success_applicant_envelopes_are_errors_not_empty_lists(tmp_path, monkeypatch, make_raw):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert "error" in result and "results" not in result


@pytest.mark.asyncio
async def test_missing_job_404_is_an_error_saying_the_job_does_not_exist(tmp_path, monkeypatch):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_wrapper("failure_applicants_not_found.json")])

    result = await _tool("list_applicants")("99999999", ctx=ctx)

    assert "error" in result and "results" not in result
    assert "此職務不存在" in result["error"]


# -- T-32: structural mismatches are structural errors ------------------------------

def _drop_resume_groups(body):
    del body["result"]["resumeGroups"]


def _drop_match_count(body):
    del body["result"]["resumeNumber"]["match"]


def _null_page_info_with_rows(body):
    body["result"]["pageInfo"] = None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tool, make_raw",
    [
        ("list_applicants", lambda: _applicant_raw(_drop_resume_groups)),
        ("list_applicants", lambda: _applicant_raw(_drop_match_count)),
        ("list_applicants", lambda: _applicant_raw(_null_page_info_with_rows)),
        ("list_jobs", lambda: _raw_from_body({"data": {"list": []}, "metadata": {}})),
        ("list_refreshable_jobs", lambda: _raw_from_body({"data": {"jobs": [], "userPermit": "true"}, "metadata": {}})),
    ],
    ids=["no-resumeGroups", "no-resumeNumber-match", "pageInfo-null-with-rows", "jobs-apply-data-object", "userPermit-string"],
)
async def test_structural_mismatch_is_a_structural_error_not_an_empty_result(tmp_path, monkeypatch, tool, make_raw):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])

    result = await _call_tool(tool, ctx)

    assert "error" in result and "results" not in result
    assert "104 回應結構異常" in result["error"]


# -- T-60: an applicant row without its identifiers is structural --------------

def _first_other_group(body):
    return next(g for g in body["result"]["resumeGroups"]["others"] if g["resumes"])


def _first_other_row(body):
    return _first_other_group(body)["resumes"][0]


def _today_group_object(body):
    group = _first_other_group(body)
    body["result"]["resumeGroups"]["today"] = [
        {"inputDate": group["inputDate"], "resumes": [copy.deepcopy(group["resumes"][0])]}
    ]


def _today_row_without_id_no(body):
    row = copy.deepcopy(_first_other_row(body))
    del row["idNo"]
    body["result"]["resumeGroups"]["today"] = [row]


def _today_row_with_empty_p_id(body):
    row = copy.deepcopy(_first_other_row(body))
    row["pId"] = ""
    body["result"]["resumeGroups"]["today"] = [row]


def _others_row_without_id_no(body):
    del _first_other_row(body)["idNo"]


def _others_row_without_p_id(body):
    del _first_other_row(body)["pId"]


def _others_row_with_empty_id_no(body):
    _first_other_row(body)["idNo"] = ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        _today_group_object,
        _today_row_without_id_no,
        _today_row_with_empty_p_id,
        _others_row_without_id_no,
        _others_row_without_p_id,
        _others_row_with_empty_id_no,
    ],
    ids=[
        "today-is-a-group-object",
        "today-row-without-idNo",
        "today-row-empty-pId",
        "others-row-without-idNo",
        "others-row-without-pId",
        "others-row-empty-idNo",
    ],
)
async def test_an_applicant_row_without_its_identifiers_is_a_structural_error(tmp_path, monkeypatch, mutate):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_applicant_raw(mutate)])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert "error" in result and "results" not in result
    assert "104 回應結構異常" in result["error"]


# -- T-33: no session and no credential file ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_applicants", "list_jobs", "list_refreshable_jobs"])
async def test_no_session_and_no_credential_file_answers_please_login_without_a_request(tmp_path, monkeypatch, tool):
    ctx, _ = await _new_session(tmp_path, logged_in=False)
    spy = _install(monkeypatch, [])

    result = await _call_tool(tool, ctx)

    assert result == NOT_LOGGED_IN
    assert spy.calls == []


# -- T-34: every request is a GET with no body --------------------------------------

_SCRIPTS = {
    "list_applicants": lambda: [_raw_from_body(load_fixture("rows_applicants.json"))],
    "list_jobs": lambda: _list_jobs_script(load_fixture("jobs_apply.json")),
    "list_refreshable_jobs": lambda: [_raw_from_body(load_fixture("refreshable_jobs.json"))],
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(_SCRIPTS))
async def test_every_request_is_a_get_with_no_body(tmp_path, monkeypatch, tool):
    ctx, _ = await _new_session(tmp_path)
    spy = _install(monkeypatch, _SCRIPTS[tool]())

    await _call_tool(tool, ctx)

    assert spy.calls, "the tool must send at least one request"
    for endpoint, _params, body in spy.calls:
        assert endpoint.method == "GET"
        assert body is None


# -- T-35: one request for the two single-request tools ------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_applicants", "list_refreshable_jobs"])
async def test_a_successful_call_sends_exactly_one_request(tmp_path, monkeypatch, tool):
    ctx, _ = await _new_session(tmp_path)
    spy = _install(monkeypatch, _SCRIPTS[tool]())

    await _call_tool(tool, ctx)

    assert len(spy.calls) == 1


# -- T-36: the rolling window counts every request of the multi-request call ---------

@pytest.mark.asyncio
async def test_list_jobs_with_more_than_twenty_jobs_adds_four_entries_to_the_rolling_window(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    before = len(info.throttle.request_timestamps)
    jobs_apply = load_fixture("jobs_apply.json")
    _require_two_count_batches(jobs_apply["data"])
    _install(monkeypatch, _list_jobs_script(jobs_apply))

    await _tool("list_jobs")(ctx=ctx)

    # The job list and the dated list, then two count batches.
    assert len(info.throttle.request_timestamps) - before == 4


# -- T-37: the cold-path worst case of list_jobs, computed not waited ---------------

def test_list_jobs_cold_path_worst_case_leaves_fifteen_seconds_under_the_120_second_timeout():
    from mcp104.browser.api_client import FETCH_TIMEOUT_SECONDS

    interval = get_config().min_call_interval_seconds
    batches = MATCH_CAP // MATCH_BATCH
    identity_lookup = interval + FETCH_TIMEOUT_SECONDS
    jobs_segment = interval + 2 * FETCH_TIMEOUT_SECONDS  # the job list and the dated list
    counts_segment = interval + batches * FETCH_TIMEOUT_SECONDS
    worst_cold = identity_lookup + jobs_segment + counts_segment

    assert worst_cold < 120
    assert 120 - worst_cold >= 15


# -- T-38 (interface): build_applicant_page's key set and its refusal ----------------------

def test_build_applicant_page_returns_exactly_the_documented_keys_for_a_legal_result():
    jobs = _jobs()
    result = copy.deepcopy(load_fixture("rows_applicants.json")["result"])

    page = jobs.build_applicant_page(
        result, requested_jobno=_jobno(), requested_page=1, date_range=("2026/09/13", "2026/11/12")
    )

    assert set(page) == {"job", "results", "pagination", "resume_number", "date_range", "browse_limit", "warnings"}


@pytest.mark.parametrize("bad", ["x", 5, None, []], ids=["str", "int", "none", "list"])
def test_build_applicant_page_refuses_a_result_that_is_not_an_object(bad):
    with pytest.raises(helpers.MalformedResponseError):
        _jobs().build_applicant_page(bad, requested_jobno=_jobno(), requested_page=1, date_range=("a", "b"))


# -- T-39 (interface): the 60-day window, including leap-year edges -------------------------

@pytest.mark.parametrize(
    "today, expected",
    [(date(2026, 3, 1), ("2025/12/31", "2026/03/01")), (date(2024, 4, 29), ("2024/02/29", "2024/04/29"))],
    ids=["non-leap-year", "leap-year"],
)
def test_applicant_date_range_is_the_sixty_days_ending_today(today, expected):
    assert _jobs().applicant_date_range(today) == expected


# -- T-40 (interface): Taipei's date at the UTC+8 boundary ----------------------------------

@pytest.mark.parametrize(
    "now, expected",
    [
        (datetime(2026, 11, 11, 15, 59, tzinfo=UTC), date(2026, 11, 11)),
        (datetime(2026, 11, 11, 16, 0, tzinfo=UTC), date(2026, 11, 12)),
    ],
    ids=["one-minute-before-taipei-midnight", "at-taipei-midnight"],
)
def test_taipei_today_converts_to_utc_plus_eight(now, expected):
    assert _jobs().taipei_today(now=now) == expected


# -- T-41 (interface): build_job_page leaves counts null and handles an empty list ----------

def test_build_job_page_leaves_every_count_null():
    page = _jobs().build_job_page(_jobs_body(["11706763", "11706764"]))

    assert len(page["results"]) == 2
    for row in page["results"]:
        assert row["today_match_count"] is None and row["total_match_count"] is None


def test_build_job_page_leaves_every_date_field_null():
    page = _jobs().build_job_page(_jobs_body(["11706763", "11706764"]))

    for row in page["results"]:
        for name in discovery_mod.JOB_DATE_FIELDS:
            assert row[name] is None, name


def test_build_job_page_on_an_empty_list_is_empty_with_no_reminder():
    page = _jobs().build_job_page({"data": [], "metadata": {"total": 0, "page": 1}})

    assert page["results"] == []
    assert page["warnings"] == []


# -- T-42 (interface): batching is 20 per request and stops at the cap (60 jobs) -----------

@pytest.mark.parametrize(
    "count, batches",
    [(0, 0), (1, 1), (20, 1), (21, 2), (41, 3)],
    ids=["none", "one", "exactly-one-batch", "one-over", "three-batches"],
)
def test_match_number_batches_split_twenty_at_a_time_in_order(count, batches):
    jobnos = _fake_jobnos(count)

    split = _jobs().match_number_batches(jobnos)

    assert len(split) == batches
    assert [jn for batch in split for jn in batch] == jobnos


def test_match_number_batches_cover_no_more_than_the_cap():
    jobnos = _fake_jobnos(MATCH_CAP + 25)

    split = _jobs().match_number_batches(jobnos)

    assert [jn for batch in split for jn in batch] == jobnos[:MATCH_CAP]
    assert len(split) == MATCH_CAP // MATCH_BATCH


# -- T-43 (interface): merge is by jobno, returns a new dict, leaves the input alone --------

def test_merge_match_counts_returns_a_new_page_and_matches_an_int_jobno_to_a_string_row():
    jobs = _jobs()
    page = jobs.build_job_page(_jobs_body(["11706763", "11706764"]))
    before = copy.deepcopy(page)
    payloads = [{"data": {"list": [{"jobNo": 11706763, "todayMatchCount": "1", "totalMatchCount": "5"}]}, "metadata": {}}]

    merged = jobs.merge_match_counts(page, payloads)

    assert merged is not page
    assert page == before
    row = next(r for r in merged["results"] if r["jobno"] == "11706763")
    assert row["today_match_count"] == 1 and row["total_match_count"] == 5


# -- T-44 (interface): an empty refreshable list is a success --------------------------------

def test_build_refreshable_jobs_on_an_empty_permitted_list_is_empty_with_no_reminder():
    payload = {"data": {"jobs": [], "userPermit": True}, "metadata": {}}

    result = _jobs().build_refreshable_jobs(payload)

    assert result["results"] == []
    assert result["pagination"]["total"] == 0
    assert result["warnings"] == []


# -- T-47: HTML in text fields converts exactly like search rows --

@pytest.mark.asyncio
async def test_applicant_text_fields_convert_exactly_like_search_resumes_rows(tmp_path, monkeypatch):
    applicant_body = load_fixture("rows_applicants.json")
    first_applicant = _fixture_rows(applicant_body)[0]
    first_applicant["userName"] = "<p>王</p>"
    first_applicant["latestMemo"] = "a&amp;b"
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(applicant_body)])
    applicant_row = (await _tool("list_applicants")(_jobno(), ctx=ctx))["results"][0]

    search_body = load_fixture("rows_search.json")
    keyword = search_body["result"]["searchForm"]["kws"]
    search_rows = search_body["result"].get("data") or search_body["result"].get("resumes")
    search_rows[0]["userName"] = "<p>王</p>"
    search_rows[0]["latestMemo"] = "a&amp;b"
    ctx2, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(search_body)])
    search_row = (await _search_tool("search_resumes")(keyword=keyword, ctx=ctx2))["results"][0]

    assert applicant_row["user_name"] == search_row["user_name"] == search_mod.html_to_text("<p>王</p>")
    assert applicant_row["latest_memo"] == search_row["latest_memo"] == search_mod.html_to_text("a&amp;b")


def test_job_name_converts_html_the_same_way_list_jobs_always_has():
    body = _jobs_body(["11706763"])
    body["data"][0]["job"] = "<b>職缺</b>&amp;x"

    page = _jobs().build_job_page(body)

    assert page["results"][0]["job"] == search_mod.html_to_text("<b>職缺</b>&amp;x")


# -- T-48 (interface): registration surface -------------------------------------------------

@pytest.mark.asyncio
async def test_register_all_tools_registers_the_job_tools_once_within_budget_and_without_ctx():
    from mcp.server.fastmcp import FastMCP

    from mcp104.main import register_all_tools

    counting = _FakeMCP()
    register_all_tools(counting)
    job_tool_names = ("list_applicants", "list_jobs", "list_refreshable_jobs", "get_job_detail", "refresh_jobs")
    for name in job_tool_names:
        assert counting.counts.get(name) == 1, f"{name} must be registered exactly once: {counting.counts.get(name)}"

    served_mcp = FastMCP("applicants-and-job-reads-test")
    register_all_tools(served_mcp)
    served = {tool.name: tool for tool in await served_mcp.list_tools()}
    for name in job_tool_names:
        assert name in served, f"{name} is not published"
        assert len(served[name].description or "") <= 1948, f"{name} description exceeds the budget"
        assert "ctx" not in served[name].inputSchema.get("properties", {}), f"{name} exposes ctx to the caller"


# -- T-51: object-shaped jobs/apply data is structural and sends no count request ---

@pytest.mark.asyncio
async def test_jobs_apply_object_data_sends_only_the_jobs_request(tmp_path, monkeypatch):
    ctx, _ = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_raw_from_body({"data": {"list": []}, "metadata": {}})])

    result = await _tool("list_jobs")(ctx=ctx)

    assert len(spy.calls) == 1
    assert "104 回應結構異常" in result["error"]
    assert "results" not in result


# -- T-52: a full window refuses the first request of each tool before any send -----

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_applicants", "list_jobs", "list_refreshable_jobs"])
async def test_a_full_rolling_window_refuses_the_first_request_without_sending(tmp_path, monkeypatch, tool):
    ctx, info = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    for _ in range(app.config.max_requests_per_hour):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install(monkeypatch, [])

    result = await _call_tool(tool, ctx)

    assert "error" in result and "retry_after_seconds" in result
    assert "results" not in result
    assert spy.calls == []


# -- T-53 (interface): an empty permitted refreshable list through the real guard ---------

@pytest.mark.asyncio
async def test_empty_refreshable_list_through_the_guard_is_success_with_no_error(tmp_path, monkeypatch):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body({"data": {"jobs": [], "userPermit": True}, "metadata": {}})])

    result = await _tool("list_refreshable_jobs")(ctx=ctx)

    assert "error" not in result
    assert result["results"] == []


# -- T-54 (interface): degrading counts is a new dict with a trailing reminder --------------

def test_mark_match_counts_unavailable_nulls_every_row_and_appends_the_reminder():
    jobs = _jobs()
    page = jobs.build_job_page(_jobs_body(["11706763", "11706764"]))
    before = copy.deepcopy(page)
    reminder = "配對數暫時無法取得（測試用提醒）"

    degraded = jobs.mark_match_counts_unavailable(page, reminder)

    assert degraded is not page
    assert page == before
    for row in degraded["results"]:
        assert row["today_match_count"] is None and row["total_match_count"] is None
    assert degraded["warnings"][-1] == reminder


# -- T-55: an applyDetail that does not match the request is structural -------------

def _other_job(body):
    body["result"]["jobNo"] = "99999999"


def _empty_job_name(body):
    body["result"]["jobName"] = ""


@pytest.mark.asyncio
@pytest.mark.parametrize("mutate", [_other_job, _empty_job_name], ids=["different-jobno", "empty-jobName"])
async def test_applydetail_for_another_job_or_with_no_name_is_a_structural_error(tmp_path, monkeypatch, mutate):
    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_applicant_raw(mutate)])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert "error" in result and "results" not in result
    assert "104 回應結構異常" in result["error"]


# -- Moved from tests/test_search.py: the list_jobs cases ----------
# Expectations follow the new route and the new fields (switch is "on"/"off" as jobs/apply
# returns it; counts come from the match-number follow-up). The old docstring check that
# lived beside them is not carried here.

_SYNTHETIC_JOB_LIST = {
    "data": [
        {"jobno": "12355016", "job": "應用工程師", "switch": "on", "role": "1", "applyCount60": 0,
         "department": {"id": "1001", "code": "D001", "name": "應用工程部"}},
        {"jobno": "99999999", "job": "已關閉的測試職缺", "switch": "off", "role": "1", "applyCount60": 0,
         "department": {"id": "1002", "code": "D002", "name": "測試部"}},
    ],
    "metadata": {"total": 2, "page": 1, "hasSubDepartment": True},
}


@pytest.mark.asyncio
async def test_list_jobs_returns_every_job_including_closed(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, _list_jobs_script(copy.deepcopy(_SYNTHETIC_JOB_LIST)))

    result = await _tool("list_jobs")(ctx=ctx)

    assert "error" not in result
    assert len(result["results"]) == 2


@pytest.mark.asyncio
async def test_list_jobs_switch_passed_through_unfiltered(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, _list_jobs_script(copy.deepcopy(_SYNTHETIC_JOB_LIST)))

    result = await _tool("list_jobs")(ctx=ctx)

    assert [row["switch"] for row in result["results"]] == ["on", "off"]


@pytest.mark.asyncio
async def test_list_jobs_missing_data_is_malformed_not_empty_result(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body({"metadata": {"page": 1, "total": 0}})])

    result = await _tool("list_jobs")(ctx=ctx)

    assert "error" in result and "results" not in result
    assert "104 回應結構異常" in result["error"]


# ══ Job dates: list_jobs folds jobList's six date fields into each row ═══════════════════════

_FAKE_THREE = _fake_jobnos(3)


@pytest.mark.asyncio
async def test_list_jobs_puts_each_jobs_dates_on_its_own_row_whatever_order_job_list_uses(tmp_path, monkeypatch):
    body = _jobs_body(_FAKE_THREE)
    job_list = _job_list_body(list(reversed(_FAKE_THREE)))
    result, _spy, _info = await _run_list_jobs(
        tmp_path, monkeypatch, body, job_list_raw=_raw_from_body(job_list)
    )

    assert [row["jobno"] for row in result["results"]] == _FAKE_THREE
    for row in result["results"]:
        for name, value in _dates_of(row["jobno"]).items():
            assert row[name] == value, (row["jobno"], name)
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_list_jobs_carries_104s_values_as_given_including_null_empty_and_zero_string(tmp_path, monkeypatch):
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, _jobs_body(_FAKE_THREE))

    for row in result["results"]:
        assert "closedate" in row and row["closedate"] is None
        assert row["d5"] == ""
        assert row["autoclose"] == "0"


@pytest.mark.asyncio
async def test_list_jobs_never_returns_the_operators_identity_from_job_list(tmp_path, monkeypatch):
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, _jobs_body(_FAKE_THREE))

    text = json.dumps(result, ensure_ascii=False)
    for sentinel in (_OPERATOR_EMAIL, _OPERATOR_LOGIN_NO, _OPERATOR_BUSER_ID, "nccPayload", "ncc_payload", "login_no"):
        assert sentinel not in text, sentinel


@pytest.mark.asyncio
async def test_a_job_on_jobs_apply_but_not_on_job_list_has_null_dates_and_is_named(tmp_path, monkeypatch):
    body = _jobs_body(_FAKE_THREE)
    absent = _FAKE_THREE[1]
    job_list = _job_list_body(_FAKE_THREE, omit={absent})
    result, _spy, _info = await _run_list_jobs(
        tmp_path, monkeypatch, body, job_list_raw=_raw_from_body(job_list)
    )

    rows = {row["jobno"]: row for row in result["results"]}
    for name in discovery_mod.JOB_DATE_FIELDS:
        assert rows[absent][name] is None
    for jobno in (_FAKE_THREE[0], _FAKE_THREE[2]):
        assert rows[jobno]["appear_date"] == _dates_of(jobno)["appear_date"]
    assert any(absent in w and "jobList" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "job_list_raw",
    [
        lambda: RuntimeError("connection reset"),
        lambda: _raw_from_body({"status": "FAIL", "message": "x", "result": None}),
        lambda: _raw_from_body({"status": "SUCCESS", "message": "", "url": "", "data": None,
                                "result": {"jobDataAry": "not-an-array"}}),
    ],
    ids=["transport-failure", "non-success-status", "job-data-ary-not-an-array"],
)
async def test_a_failed_job_list_still_returns_the_jobs_with_null_dates_and_checks_counts(
    tmp_path, monkeypatch, job_list_raw
):
    body = _jobs_body(_FAKE_THREE)
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _raw_from_body(body), job_list_raw(), _raw_from_body(_match_body(_FAKE_THREE)),
    ])

    result = await _tool("list_jobs")(ctx=ctx)

    assert "error" not in result
    assert [endpoint.key for endpoint, _p, _b in spy.calls] == ["list_jobs", "job_list", "match_number"]
    for row in result["results"]:
        for name in discovery_mod.JOB_DATE_FIELDS:
            assert row[name] is None, name
        assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(row["jobno"])
    assert any("日期" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_raw",
    [_challenge_raw, lambda: _raw_from_wrapper("failure_family_a_expired.json")],
    ids=["challenge", "expired"],
)
async def test_a_session_failure_on_the_job_list_request_is_an_error_not_a_dateless_list(tmp_path, monkeypatch, make_raw):
    ref_ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])
    reference = await _search_tool("list_matched_resumes")(jobno=_jobno(), ctx=ref_ctx)

    ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(_jobs_body(_FAKE_THREE)), make_raw()])
    result = await _tool("list_jobs")(ctx=ctx)

    assert result == reference
    assert "error" in result and "results" not in result


@pytest.mark.asyncio
async def test_list_jobs_sends_the_requested_page_to_both_list_routes(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, _list_jobs_script(_jobs_body(_FAKE_THREE)))

    await _tool("list_jobs")(ctx=ctx, page=2)

    assert _pdict(spy.calls[0][1]) == {"page": "2"}
    assert _pdict(spy.calls[1][1]) == {"page": "2"}


def test_parse_job_dates_reads_only_job_data_ary_and_keeps_104s_values():
    result = _job_list_body(_FAKE_THREE)["result"]
    result["jobDataAry"][0].pop("d5")

    dates = _jobs().parse_job_dates(result)

    assert set(dates) == set(_FAKE_THREE)
    assert set(dates[_FAKE_THREE[1]]) == set(discovery_mod.JOB_DATE_FIELDS)
    assert dates[_FAKE_THREE[0]]["d5"] is None
    assert dates[_FAKE_THREE[1]]["appear_date"] == _dates_of(_FAKE_THREE[1])["appear_date"]
    assert _OPERATOR_EMAIL not in json.dumps(dates)


@pytest.mark.parametrize(
    "bad",
    [None, [], {"jobDataAry": None}, {"jobDataAry": {}}, {"jobDataAry": ["x"]}],
    ids=["none", "list", "no-array", "object-array", "non-object-row"],
)
def test_parse_job_dates_refuses_a_result_that_departs_from_the_measured_shape(bad):
    with pytest.raises(helpers.MalformedResponseError):
        _jobs().parse_job_dates(bad)


def test_merge_job_dates_returns_a_new_page_and_leaves_the_input_alone():
    jobs = _jobs()
    page = jobs.build_job_page(_jobs_body(_FAKE_THREE))
    before = copy.deepcopy(page)

    merged = jobs.merge_job_dates(page, jobs.parse_job_dates(_job_list_body(_FAKE_THREE)["result"]))

    assert merged is not page
    assert page == before
    assert merged["results"][0]["appear_date"] == _dates_of(_FAKE_THREE[0])["appear_date"]


def test_mark_job_dates_unavailable_nulls_every_date_and_appends_the_reminder():
    jobs = _jobs()
    page = jobs.merge_job_dates(
        jobs.build_job_page(_jobs_body(_FAKE_THREE)),
        jobs.parse_job_dates(_job_list_body(_FAKE_THREE)["result"]),
    )
    before = copy.deepcopy(page)

    degraded = jobs.mark_job_dates_unavailable(page, "日期暫時無法取得（測試用提醒）")

    assert degraded is not page
    assert page == before
    for row in degraded["results"]:
        for name in discovery_mod.JOB_DATE_FIELDS:
            assert row[name] is None
    assert degraded["warnings"][-1] == "日期暫時無法取得（測試用提醒）"


def test_describe_result_fields_for_a_job_row_lists_the_six_dates_with_value_and_display_apart():
    fields = discovery_mod._describe_result_fields("job")["fields"]

    for name in discovery_mod.JOB_DATE_FIELDS:
        assert name in fields, name
        assert "值：" in fields[name] and "前端怎麼顯示" in fields[name], name
        assert "warnings" in fields[name], name
    for name in ("appear_date", "inputdate", "closedate", "autoclose", "d5"):
        assert "前端碼" in fields[name], name
    assert "未量測" in fields["modifydate"]
    assert f"前 {MATCH_CAP} 個" in fields["today_match_count"]


# ══ get_job_detail ══════════════════════════════════════════════════════════════════════════

_DETAIL_JOBNO = "12355016"
_DETAIL_NOT_FOUND_BODY = {
    "status": "DATA_NOT_FOUND", "message": "此職務已不存在", "result": None,
    "url": "/job/allJobList", "code": "003001", "data": None,
}
# Values that must never come back from get_job_detail: the staff contact block, the
# account's and editor's internals, and the option dictionaries.
_DETAIL_WITHHELD = (
    "contacter-sentinel", "contact-email-sentinel@example.invalid", "apply-addr-sentinel",
    "phone-sentinel", "cellphone-sentinel", "contact-note-sentinel",
    "competitor-email-sentinel@example.invalid", "competitors-sentinel", "reg-id-sentinel",
    "options-label-sentinel", "reply-sentinel",
)


def _job_detail_result() -> dict:
    return {
        "jobName": "測試職缺（合成）",
        "switch": "on",
        "appearDate": "2026-11-12 09:56:15",
        "openDate": "2021-08-16 15:31:52",
        "canDelete": True, "isHunter": False, "isDispatch": False, "isNewModel": True,
        "isTheNetwork": False, "isGeneral": True,
        "shareLink": [
            {"linkName": "copy", "linkUrl": "https://www.104.com.tw/job/abc?jobsource=vipshare&utm_medium=share_copy"},
            {"linkName": "fb", "linkUrl": "https://www.104.com.tw/job/abc?jobsource=vipshare&utm_medium=share_fb"},
        ],
        "department": {"id": "1001", "code": "D001", "name": "測試部"},
        "detail": {
            "roleId": 1,
            "needEmp": {"low": 1, "high": 2},
            "jobcat": [{"no": "2007001000", "des": "軟體工程師"}],
            "description": "<p>職責一</p><p>職責二</p>",
            "salary": {"salaryTypeId": 4, "salaryLow": 40000, "salaryHigh": 60000},
            "jobAddress": {"addrNo": {"no": "6001006002", "des": "新竹縣竹北市"}, "address": "博愛街1號",
                           "mapAddress": "新竹縣竹北市博愛街1號", "latitude": 24.8, "longitude": 121.0},
            "holiday": "週休二日",
            "industoryArea": None,
        },
        "needCondition": {
            "experience": 3, "education": [4, 8], "otherCondition": "<p>需具備溝通能力</p>",
            "majors": [{"no": "1001", "des": "資訊工程"}], "language": [],
        },
        "contaction": {
            "contacter": "contacter-sentinel",
            "emails": ["contact-email-sentinel@example.invalid"],
            "usingTypes": [1, 2],
            "contactInfo": {"applyAddrNo": None, "applyAddr": "apply-addr-sentinel", "ext": "",
                            "phoneArea": "02", "phone": "phone-sentinel",
                            "cellphone": "cellphone-sentinel", "note": "contact-note-sentinel"},
        },
        "otherSetting": {"closeDate": "", "reply": {"noReply": False, "days": 7, "text": "reply-sentinel"}, "subscript": 0},
        "additionalInfo": {
            "loadTime": "2026-11-12 18:37:34", "poEndDate": "2027-06-28 00:00:00",
            "minimumSalary": {"month": 1}, "competitors": "competitors-sentinel",
            "competitorsEmail": {"blackList": ["competitor-email-sentinel@example.invalid"], "whiteList": []},
            "regId": "reg-id-sentinel",
            "options": {"role": {"1": "options-label-sentinel"}},
        },
    }


def _job_detail_raw(result=None) -> RawResponse:
    return _raw_from_body({
        "status": "SUCCESS", "message": _DETAIL_JOBNO,
        "result": _job_detail_result() if result is None else result, "url": "", "data": None,
    })


async def _get_job_detail(tmp_path, monkeypatch, script, jobno=_DETAIL_JOBNO):
    ctx, info = await _new_session(tmp_path)
    spy = _install(monkeypatch, script)
    result = await _tool("get_job_detail")(jobno, ctx=ctx)
    return result, spy, info


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "12a", " 123", "1.5"])
async def test_get_job_detail_refuses_a_bad_jobno_before_any_request(tmp_path, monkeypatch, bad):
    result, spy, _info = await _get_job_detail(tmp_path, monkeypatch, [], jobno=bad)

    assert "error" in result and "job" not in result
    assert spy.calls == []


@pytest.mark.asyncio
async def test_get_job_detail_is_one_get_to_job_update_with_act_edit_and_the_jobno(tmp_path, monkeypatch):
    _result, spy, _info = await _get_job_detail(tmp_path, monkeypatch, [_job_detail_raw()])

    assert len(spy.calls) == 1
    endpoint, params, body = spy.calls[0]
    assert (endpoint.method, endpoint.path) == ("GET", "/api/job/jobUpdate")
    assert _pdict(params) == {"act": "edit", "jobno": _DETAIL_JOBNO}
    assert body is None


@pytest.mark.asyncio
async def test_get_job_detail_publishes_exactly_the_posting_content_keys(tmp_path, monkeypatch):
    result, _spy, _info = await _get_job_detail(tmp_path, monkeypatch, [_job_detail_raw()])

    assert set(result) == {"job", "browse_limit", "warnings"}
    assert result["browse_limit"] is None and result["warnings"] == []
    job = result["job"]
    assert set(job) == {
        "jobno", "job_name", "switch", "appear_date", "open_date", "share_link", "department",
        "detail", "need_condition", "other_setting",
    }
    assert job["jobno"] == _DETAIL_JOBNO
    assert job["job_name"] == "測試職缺（合成）"
    assert job["other_setting"] == {"close_date": ""}
    assert job["department"] == {"id": "1001", "code": "D001", "name": "測試部"}
    assert [entry["link_name"] for entry in job["share_link"]] == ["copy", "fb"]
    assert job["detail"]["job_address"]["address"] == "博愛街1號"
    assert job["detail"]["job_address"]["latitude"] == 24.8
    assert job["need_condition"]["education"] == [4, 8]


@pytest.mark.asyncio
async def test_get_job_detail_never_returns_staff_contacts_account_internals_or_flags(tmp_path, monkeypatch):
    result, _spy, _info = await _get_job_detail(tmp_path, monkeypatch, [_job_detail_raw()])

    text = json.dumps(result, ensure_ascii=False)
    for sentinel in _DETAIL_WITHHELD:
        assert sentinel not in text, sentinel
    walked = set(_walk_keys(result))
    for key in ("contaction", "additional_info", "options", "competitors_email", "is_hunter", "is_dispatch",
                "is_new_model", "is_the_network", "is_general", "can_delete", "reply", "subscript", "load_time"):
        assert key not in walked, key


@pytest.mark.asyncio
async def test_get_job_detail_converts_rich_text_html_and_leaves_plain_strings_untouched(tmp_path, monkeypatch):
    result, _spy, _info = await _get_job_detail(tmp_path, monkeypatch, [_job_detail_raw()])

    job = result["job"]
    assert job["detail"]["description"] == search_mod.html_to_text("<p>職責一</p><p>職責二</p>")
    assert "<p>" not in job["need_condition"]["other_condition"]
    assert job["detail"]["holiday"] == "週休二日"
    assert job["share_link"][0]["link_url"] == (
        "https://www.104.com.tw/job/abc?jobsource=vipshare&utm_medium=share_copy"
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mutate",
    [
        lambda r: r.pop("jobName"),
        lambda r: r.update(jobName=""),
        lambda r: r.update(detail=[]),
        lambda r: r.pop("detail"),
    ],
    ids=["no-job-name", "empty-job-name", "detail-not-an-object", "no-detail"],
)
async def test_get_job_detail_structural_departures_are_errors_with_no_job(tmp_path, monkeypatch, mutate):
    broken = _job_detail_result()
    mutate(broken)

    result, _spy, _info = await _get_job_detail(tmp_path, monkeypatch, [_job_detail_raw(broken)])

    assert "104 回應結構異常" in result["error"]
    assert "job" not in result


@pytest.mark.asyncio
async def test_get_job_detail_on_a_job_that_does_not_exist_is_a_not_found_error(tmp_path, monkeypatch):
    raw = _raw_from_body(_DETAIL_NOT_FOUND_BODY, status=404)

    result, spy, _info = await _get_job_detail(tmp_path, monkeypatch, [raw], jobno="1")

    assert len(spy.calls) == 1
    assert "找不到資料" in result["error"] and "此職務已不存在" in result["error"]
    assert "job" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "make_raw",
    [_challenge_raw, lambda: _raw_from_wrapper("failure_family_a_expired.json")],
    ids=["challenge", "expired"],
)
async def test_get_job_detail_session_failures_are_the_same_errors_as_elsewhere(tmp_path, monkeypatch, make_raw):
    ref_ctx, _ = await _new_session(tmp_path)
    _install(monkeypatch, [make_raw()])
    reference = await _search_tool("list_matched_resumes")(jobno=_jobno(), ctx=ref_ctx)

    result, _spy, _info = await _get_job_detail(tmp_path, monkeypatch, [make_raw()])

    assert result == reference


# ══ refresh_jobs ════════════════════════════════════════════════════════════════════════════

_EMPTY_REFRESHABLE = {"data": {"jobs": [], "userPermit": True}, "metadata": {}}
_PUT_ACCEPTED = {"data": {}, "metadata": {}}
_PUT_REFUSED = {"code": "00005", "message": "帶入內容驗證失敗", "detail": {"jobs": ["jobs-sentinel-reason"]}}
_JOBNO_A, _JOBNO_B, _JOBNO_C = "11111111", "22222222", "33333333"


def _refreshable_raw(jobnos=(), *, permit=True, raw_jobno=str) -> RawResponse:
    jobs = [
        {"jobNo": raw_jobno(jn), "jobName": f"職缺{jn}", "appearDate": "2026-11-01 09:00:00"}
        for jn in jobnos
    ]
    return _raw_from_body({"data": {"jobs": jobs, "userPermit": permit}, "metadata": {}})


def _put_accepted() -> RawResponse:
    return _raw_from_body(_PUT_ACCEPTED)


async def _refresh(tmp_path, monkeypatch, jobnos, script):
    ctx, info = await _new_session(tmp_path)
    spy = _install(monkeypatch, script)
    result = await _tool("refresh_jobs")(jobnos, ctx=ctx)
    return result, spy, info


def _call_keys(spy) -> list[str]:
    return [endpoint.key for endpoint, _p, _b in spy.calls]


# -- before anything is sent ---------------------------------------------------------------

@pytest.mark.asyncio
async def test_refresh_jobs_refuses_an_empty_list_and_says_there_is_no_refresh_all(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(tmp_path, monkeypatch, [], [])

    assert "全部更新" in result["error"] and "results" not in result
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [["12a"], [""], [_JOBNO_A, "x1"]], ids=["letters", "empty", "second-bad"])
async def test_refresh_jobs_refuses_a_jobno_that_is_not_all_digits_before_any_request(tmp_path, monkeypatch, bad):
    result, spy, _info = await _refresh(tmp_path, monkeypatch, bad, [])

    assert "error" in result and "results" not in result
    assert spy.calls == []


@pytest.mark.asyncio
async def test_refresh_jobs_refuses_a_repeated_jobno_naming_it_before_any_request(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(tmp_path, monkeypatch, [_JOBNO_A, _JOBNO_B, _JOBNO_A], [])

    assert _JOBNO_A in result["error"] and "results" not in result
    assert spy.calls == []


# -- the pre-check ------------------------------------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize(
    "listing",
    [lambda: _refreshable_raw([_JOBNO_A]), lambda: _raw_from_body(_EMPTY_REFRESHABLE)],
    ids=["other-jobs-listed", "list-is-empty"],
)
async def test_a_jobno_not_on_the_refreshable_list_sends_only_the_list_read(tmp_path, monkeypatch, listing):
    result, spy, _info = await _refresh(tmp_path, monkeypatch, [_JOBNO_A, _JOBNO_B], [listing()])

    assert _call_keys(spy) == ["list_refreshable_jobs"]
    assert spy.calls[0][0].method == "GET"
    assert "results" not in result
    assert _JOBNO_B in result["error"] and "list_refreshable_jobs" in result["error"]
    assert "7 天" in result["error"] and "未實測" in result["error"]


@pytest.mark.asyncio
async def test_missing_jobnos_are_named_in_the_callers_order(tmp_path, monkeypatch):
    result, _spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_C, _JOBNO_A, _JOBNO_B], [_refreshable_raw([_JOBNO_A])]
    )

    error = result["error"]
    assert error.index(_JOBNO_C) < error.index(_JOBNO_B)
    assert _JOBNO_A not in error


@pytest.mark.asyncio
async def test_no_update_permission_sends_only_the_list_read(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A], [_refreshable_raw([_JOBNO_A], permit=False)]
    )

    assert _call_keys(spy) == ["list_refreshable_jobs"]
    assert "權限" in result["error"] and "results" not in result


@pytest.mark.asyncio
async def test_an_unparseable_refreshable_list_is_a_structural_error_and_nothing_is_written(tmp_path, monkeypatch):
    raw = _raw_from_body({"data": {"jobs": [], "userPermit": "true"}, "metadata": {}})

    result, spy, _info = await _refresh(tmp_path, monkeypatch, [_JOBNO_A], [raw])

    assert _call_keys(spy) == ["list_refreshable_jobs"]
    assert "104 回應結構異常" in result["error"] and "results" not in result


@pytest.mark.asyncio
async def test_a_failed_first_request_is_an_error_with_no_put_and_no_results(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(tmp_path, monkeypatch, [_JOBNO_A], [RuntimeError("timed out")])

    assert len(spy.calls) == 1
    assert "error" in result and "results" not in result


def _refreshable_raw_with_unprintable_jobno() -> RawResponse:
    """A list whose one jobNo is an int too long for Python to turn into a string
    (int-to-str conversion has a digit limit): 104's list parse then raises an exception that
    is not a MalformedResponseError. Built by hand because json.dumps hits the same limit."""
    body = {"data": {"jobs": [{"jobNo": 10 ** 5000, "jobName": "x", "appearDate": "d"}], "userPermit": True},
            "metadata": {}}
    return _raw(200, "application/json; charset=utf-8", "{}", body)


@pytest.fixture
def _int_str_limit_enforced():
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    yield
    sys.set_int_max_str_digits(previous)


@pytest.mark.asyncio
async def test_an_unexpected_error_after_the_put_is_every_job_unconfirmed_with_the_reason_in_warnings(
    tmp_path, monkeypatch, _int_str_limit_enforced
):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw_with_unprintable_jobno()],
    )

    assert _call_keys(spy) == ["list_refreshable_jobs", "refresh_jobs", "list_refreshable_jobs"]
    assert "error" not in result
    assert result["results"] == [{"jobno": _JOBNO_A, "refreshed": "unconfirmed"}]
    assert any("內部錯誤" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_an_unexpected_error_before_the_put_says_nothing_was_sent(
    tmp_path, monkeypatch, _int_str_limit_enforced
):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A], [_refreshable_raw_with_unprintable_jobno()]
    )

    assert _call_keys(spy) == ["list_refreshable_jobs"]
    assert "沒有送出職缺更新" in result["error"] and "results" not in result


@pytest.mark.asyncio
async def test_a_put_that_times_out_says_timeout_and_is_read_back(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A]), TimeoutError(), _refreshable_raw([_JOBNO_A])],
    )

    assert _call_keys(spy) == ["list_refreshable_jobs", "refresh_jobs", "list_refreshable_jobs"]
    note = next(w for w in result["warnings"] if "更新請求（PUT）" in w)
    assert "逾時（TimeoutError，等了 " in note
    assert "結果以重讀可更新清單為準" in note
    assert "無法辨識" not in note


@pytest.mark.asyncio
async def test_a_put_answered_with_an_html_error_page_says_what_came_back_without_contact_text(tmp_path, monkeypatch):
    page = _raw(502, "text/html", "<h1>Bad Gateway</h1> mail SYNTH@example.invalid  call 02-2345-6789 ref SYNTH-REF", None)
    result, _spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A]), page, _refreshable_raw([_JOBNO_A])],
    )

    note = next(w for w in result["warnings"] if "更新請求（PUT）" in w)
    assert "逾時" not in note
    assert "無法辨識的回應（kind=unrecognised_status，HTTP 502，content-type text/html，Location 無，耗時 " in note
    assert "內容開頭「<h1>Bad Gateway</h1> mail ［已遮蔽］ call ［已遮蔽］ ref SYNTH-REF」" in note


# -- the PUT and the re-read ----------------------------------------------------------------

@pytest.mark.asyncio
async def test_the_put_body_is_the_callers_jobnos_in_order_as_104_gave_them(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_B, _JOBNO_A],
        [_refreshable_raw([_JOBNO_A, _JOBNO_B]), _put_accepted(), _refreshable_raw()],
    )

    assert _call_keys(spy) == ["list_refreshable_jobs", "refresh_jobs", "list_refreshable_jobs"]
    endpoint, _params, body = spy.calls[1]
    assert endpoint.method == "PUT" and endpoint.path == "/vipapi/job/over-seven-day-job"
    assert body == {"jobs": [{"jobNo": _JOBNO_B}, {"jobNo": _JOBNO_A}]}
    assert "results" in result


@pytest.mark.asyncio
async def test_the_put_body_keeps_an_integer_jobno_an_integer(tmp_path, monkeypatch):
    _result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A], raw_jobno=int), _put_accepted(), _refreshable_raw()],
    )

    assert spy.calls[1][2] == {"jobs": [{"jobNo": int(_JOBNO_A)}]}


@pytest.mark.asyncio
async def test_a_job_gone_from_the_re_read_list_is_confirmed_in_the_closed_result_shape(tmp_path, monkeypatch):
    result, _spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw()],
    )

    assert set(result) == {"results", "message", "warnings"}
    assert result["results"] == [{"jobno": _JOBNO_A, "refreshed": "confirmed"}]
    assert all(set(row) == {"jobno", "refreshed"} for row in result["results"])
    assert result["warnings"] == []
    assert "error" not in result


@pytest.mark.asyncio
async def test_a_job_still_on_the_re_read_list_is_unconfirmed_and_not_called_a_failure(tmp_path, monkeypatch):
    result, _spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw([_JOBNO_A])],
    )

    assert result["results"] == [{"jobno": _JOBNO_A, "refreshed": "unconfirmed"}]
    assert "error" not in result
    message = result["message"]
    assert "不代表失敗" in message and "104 後台" in message and "不要重送" in message


@pytest.mark.asyncio
async def test_several_jobs_get_a_per_job_outcome_in_the_callers_order(tmp_path, monkeypatch):
    result, _spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_C, _JOBNO_A, _JOBNO_B],
        [
            _refreshable_raw([_JOBNO_A, _JOBNO_B, _JOBNO_C]),
            _put_accepted(),
            _refreshable_raw([_JOBNO_A]),
        ],
    )

    assert result["results"] == [
        {"jobno": _JOBNO_C, "refreshed": "confirmed"},
        {"jobno": _JOBNO_A, "refreshed": "unconfirmed"},
        {"jobno": _JOBNO_B, "refreshed": "confirmed"},
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "reread",
    [
        lambda: RuntimeError("timed out"),
        lambda: _raw_from_body({"data": {"jobs": [], "userPermit": "yes"}, "metadata": {}}),
        _challenge_raw,
        lambda: _raw_from_wrapper("failure_family_b_unauthenticated.json"),
    ],
    ids=["timeout", "malformed", "cloudflare-challenge", "session-expired"],
)
async def test_a_failed_re_read_leaves_every_job_unconfirmed_with_the_reason_in_warnings(tmp_path, monkeypatch, reread):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A, _JOBNO_B],
        [_refreshable_raw([_JOBNO_A, _JOBNO_B]), _put_accepted(), reread()],
    )

    assert len(spy.calls) == 3
    assert "error" not in result
    assert [row["refreshed"] for row in result["results"]] == ["unconfirmed", "unconfirmed"]
    assert result["warnings"], "the reason the re-read failed must be visible"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "put_raw",
    [
        lambda: RuntimeError("timed out"),
        lambda: _raw_from_body({"error": "method not allowed"}, status=405),
        lambda: _raw_from_body({"error": "boom"}, status=500),
        lambda: _raw_from_body({"data": [], "metadata": {}}),
        lambda: _raw(200, "text/html; charset=utf-8", "<html>ok</html>", None),
    ],
    ids=["timeout", "http-405", "http-500", "data-is-an-array", "not-json"],
)
@pytest.mark.parametrize("still_listed, expected", [(False, "confirmed"), (True, "unconfirmed")], ids=["gone", "still-listed"])
async def test_an_unrecognised_put_response_is_re_read_and_judged_by_the_list(
    tmp_path, monkeypatch, put_raw, still_listed, expected
):
    after = _refreshable_raw([_JOBNO_A] if still_listed else [])
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A], [_refreshable_raw([_JOBNO_A]), put_raw(), after],
    )

    assert _call_keys(spy) == ["list_refreshable_jobs", "refresh_jobs", "list_refreshable_jobs"]
    assert "error" not in result
    assert result["results"] == [{"jobno": _JOBNO_A, "refreshed": expected}]
    assert any("PUT" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_a_refused_batch_of_several_jobs_is_re_read_with_104s_words_in_warnings(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A, _JOBNO_B],
        [
            _refreshable_raw([_JOBNO_A, _JOBNO_B]),
            _raw_from_body(_PUT_REFUSED, status=400),
            _refreshable_raw([_JOBNO_B]),
        ],
    )

    assert len(spy.calls) == 3
    assert "error" not in result
    assert result["results"] == [
        {"jobno": _JOBNO_A, "refreshed": "confirmed"},
        {"jobno": _JOBNO_B, "refreshed": "unconfirmed"},
    ]
    assert any("jobs-sentinel-reason" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_a_refused_single_job_is_an_error_with_no_re_read(tmp_path, monkeypatch):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, [_JOBNO_A],
        [_refreshable_raw([_JOBNO_A]), _raw_from_body(_PUT_REFUSED, status=400)],
    )

    assert len(spy.calls) == 2
    assert "error" in result and "results" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("jobnos", [[_JOBNO_A], [_JOBNO_A, _JOBNO_B]], ids=["single", "several"])
@pytest.mark.parametrize(
    "put_raw",
    [
        lambda: _raw(401, "application/json", "{}", {}),
        _challenge_raw,
        lambda: _raw(403, "text/html; charset=utf-8", "Forbidden", None),
    ],
    ids=["session-expired", "cloudflare-challenge", "blocked-403"],
)
async def test_a_session_problem_on_the_put_is_an_error_and_is_never_re_read(tmp_path, monkeypatch, jobnos, put_raw):
    result, spy, _info = await _refresh(
        tmp_path, monkeypatch, jobnos, [_refreshable_raw(jobnos), put_raw()],
    )

    assert len(spy.calls) == 2
    assert "error" in result and "results" not in result


# -- wording --------------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_nothing_handed_out_names_the_account_holder(tmp_path, monkeypatch):
    mcp = _FakeMCP()
    _jobs().register_job_tools(mcp)
    texts = [mcp.descriptions[name] for name in mcp.descriptions]

    scripts = [
        ([_JOBNO_A], [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw()]),
        ([_JOBNO_A], [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw([_JOBNO_A])]),
        ([_JOBNO_A, _JOBNO_B], [_refreshable_raw([_JOBNO_A, _JOBNO_B]), _raw_from_body(_PUT_REFUSED, status=400),
                                 _refreshable_raw([_JOBNO_A, _JOBNO_B])]),
        ([_JOBNO_B], [_refreshable_raw([_JOBNO_A])]),
        ([_JOBNO_A], [_refreshable_raw([_JOBNO_A], permit=False)]),
        ([], []),
        # the PUT's response is unrecognised (timeout), then the re-read succeeds
        ([_JOBNO_A], [_refreshable_raw([_JOBNO_A]), RuntimeError("timed out"), _refreshable_raw([_JOBNO_A])]),
        # the re-read fails
        ([_JOBNO_A], [_refreshable_raw([_JOBNO_A]), _put_accepted(), RuntimeError("timed out")]),
        # an unexpected internal error after the PUT was sent: the re-read list cannot be parsed
        ([_JOBNO_A], [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw_with_unprintable_jobno()]),
        # an unexpected internal error before the PUT
        ([_JOBNO_A], [_refreshable_raw_with_unprintable_jobno()]),
    ]
    previous_limit = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    try:
        for jobnos, script in scripts:
            result, _spy, _info = await _refresh(tmp_path, monkeypatch, jobnos, script)
            texts.extend(_every_string(result))
        # list_jobs: the jobList request fails (transport) and its body is malformed
        for job_list_raw in (
            RuntimeError("connection reset"),
            _raw_from_body({"status": "SUCCESS", "message": "", "url": "", "data": None,
                            "result": {"jobDataAry": "not-an-array"}}),
        ):
            result, _spy, _info = await _run_list_jobs(
                tmp_path, monkeypatch, _jobs_body(_FAKE_THREE), job_list_raw=job_list_raw
            )
            assert any("日期" in w for w in result["warnings"]), result["warnings"]
            texts.extend(_every_string(result))
    finally:
        sys.set_int_max_str_digits(previous_limit)

    for text in texts:
        assert "帳號持有人" not in text


def _every_string(value) -> list[str]:
    """Every string anywhere in a response: keys and values, at any depth."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        return [s for k, v in value.items() for s in (_every_string(k) + _every_string(v))]
    if isinstance(value, (list, tuple)):
        return [s for item in value for s in _every_string(item)]
    return []


def test_the_refresh_description_states_the_closed_result_values_and_the_unmeasured_basis():
    mcp = _FakeMCP()
    _jobs().register_job_tools(mcp)
    description = mcp.descriptions["refresh_jobs"]

    for needle in ("無法撤回", "全部更新", "list_refreshable_jobs", "三個請求", "confirmed", "unconfirmed",
                   "尚未實測", "不要重送", "後台"):
        assert needle in description, needle
    assert "不代表失敗" in description


def test_the_job_detail_description_says_reading_twice_showed_no_lock_and_other_effects_are_unmeasured():
    mcp = _FakeMCP()
    _jobs().register_job_tools(mcp)
    description = mcp.descriptions["get_job_detail"]

    assert "讀取兩次未觀察到鎖定，其他副作用未量測" in description
    assert "分享連結清單" in description


def test_the_refreshable_list_description_points_at_refresh_jobs():
    mcp = _FakeMCP()
    _jobs().register_job_tools(mcp)

    assert "refresh_jobs" in mcp.descriptions["list_refreshable_jobs"]


# -- the throttle ledger ------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_a_successful_refresh_records_three_requests_in_the_rolling_window(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    before = len(info.throttle.request_timestamps)
    _install(monkeypatch, [_refreshable_raw([_JOBNO_A]), _put_accepted(), _refreshable_raw()])

    await _tool("refresh_jobs")([_JOBNO_A], ctx=ctx)

    assert len(info.throttle.request_timestamps) - before == 3


@pytest.mark.asyncio
async def test_with_only_two_slots_left_the_whole_refresh_is_refused_before_any_request(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    for _ in range(app.config.max_requests_per_hour - 2):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install(monkeypatch, [])

    result = await _tool("refresh_jobs")([_JOBNO_A], ctx=ctx)

    assert "error" in result and "retry_after_seconds" in result
    assert "results" not in result
    assert spy.calls == []


# ── job-switch-delete (T-59, T-60, T-61) ───────
# The jobList result is synthesised here in the measured key set
# (docs/104-site-facts.md §8.31): rows carry jobno, switch, canDelete, isInVerify,
# job_setting; the account level carries permit, coStatus, isHunter, isDispatch,
# isNewModel, poNum, isWeb; the operator identity fields are present so the
# tests can prove they never reach the parsed value.

_SWITCH_OPERATOR_EMAIL = "SYNTH-SWITCH-OPERATOR@example.invalid"
_SWITCH_OPERATOR_LOGIN = "SYNTH-SWITCH-LOGIN"


def _switch_result(rows, *, omit=(), overrides=None, page_info=True, total_page=1):
    result = {
        "jobDataAry": rows,
        "permit": {"job__5": {"result": True, "message": ""},
                   "job__10": {"result": True, "message": ""},
                   "job__11": {"result": True, "message": ""}},
        "poNum": 0,
        "onJobCnt": 1,
        "coStatus": "on",
        "isHunter": False,
        "isDispatch": False,
        "isNewModel": False,
        "isWeb": False,
        "email": _SWITCH_OPERATOR_EMAIL,
        "loginNo": _SWITCH_OPERATOR_LOGIN,
        "nccPayload": {"web": "vip_new", "ext": {"Buserid": "SYNTH-SWITCH-BUSERID", "custno": "1"}},
    }
    if page_info:
        result["pageInfo"] = {"curPage": "1", "total_page": total_page, "total": len(rows)}
    for key in omit:
        result.pop(key, None)
    if overrides:
        result.update(overrides)
    return result


def _switch_row(jobno, *, switch="on", can_delete=True, in_verify=0, job_setting="SYNTH-SETTING"):
    return {"jobno": jobno, "job": f"SYNTH-JOB-{jobno}", "switch": switch,
            "canDelete": can_delete, "isInVerify": in_verify, "job_setting": job_setting}


# T-59 (interface): parse_job_list reads the measured list and refuses departures from it.

def test_parse_job_list_reads_each_row_with_its_normalised_and_raw_fields_in_list_order():
    rows = [_switch_row("1000002", switch="off", can_delete=True, in_verify=0, job_setting="SYNTH-A"),
            _switch_row("1000001", switch="on", can_delete=False, in_verify=1, job_setting="SYNTH-B")]
    parsed = _jobs().parse_job_list(_switch_result(rows))

    assert [row.jobno for row in parsed.rows] == ["1000002", "1000001"]
    first = parsed.rows[0]
    assert first.raw_jobno == "1000002"
    assert first.switch == "off"
    assert first.can_delete is True
    assert first.is_in_verify == 0
    assert first.job_setting == "SYNTH-A"
    second = parsed.rows[1]
    assert second.can_delete is False
    assert second.is_in_verify == 1
    assert second.job_setting == "SYNTH-B"


def test_parse_job_list_carries_the_account_level_values_as_given():
    parsed = _jobs().parse_job_list(_switch_result([_switch_row("1000001")], overrides={"poNum": "0"}))

    account = parsed.account
    assert account.permit == {"job__5": {"result": True, "message": ""},
                              "job__10": {"result": True, "message": ""},
                              "job__11": {"result": True, "message": ""}}
    assert account.co_status == "on"
    assert account.is_hunter is False
    assert account.is_dispatch is False
    assert account.is_new_model is False
    assert account.po_num == "0"
    assert account.is_web is False


def test_parse_job_list_leaves_absent_account_keys_as_none():
    parsed = _jobs().parse_job_list(_switch_result([_switch_row("1000001")],
                                                   omit=("isWeb", "isOutEnsure")))
    assert parsed.account.is_web is None
    assert parsed.account.is_out_ensure is None


def test_parse_job_list_reads_is_out_ensure_when_present():
    parsed = _jobs().parse_job_list(_switch_result([_switch_row("1000001")],
                                                   overrides={"isOutEnsure": True}))
    assert parsed.account.is_out_ensure is True


def test_job_list_total_page_is_the_integer_page_info_reports():
    parsed = _jobs().parse_job_list(_switch_result([_switch_row("1000001")], total_page=2))
    assert parsed.total_page() == 2


@pytest.mark.parametrize("bad", ["SYNTH-NOT-AN-OBJECT", ["SYNTH"], None],
                         ids=["string", "list", "none"])
def test_parse_job_list_refuses_a_result_that_is_not_an_object(bad):
    with pytest.raises(helpers.MalformedResponseError):
        _jobs().parse_job_list(bad)


def test_parse_job_list_refuses_a_job_data_ary_that_is_not_a_list():
    with pytest.raises(helpers.MalformedResponseError):
        _jobs().parse_job_list(_switch_result([], overrides={"jobDataAry": {"jobno": "1000001"}}))


def test_parse_job_list_refuses_a_row_that_is_not_an_object():
    with pytest.raises(helpers.MalformedResponseError):
        _jobs().parse_job_list(_switch_result(["SYNTH-ROW"]))


def test_parse_job_list_with_no_page_info_parses_and_total_page_refuses():
    parsed = _jobs().parse_job_list(_switch_result([_switch_row("1000001")], page_info=False))
    assert [row.jobno for row in parsed.rows] == ["1000001"]
    with pytest.raises(helpers.MalformedResponseError):
        parsed.total_page()


def test_parsed_job_list_never_keeps_the_operator_identity():
    parsed = _jobs().parse_job_list(_switch_result([_switch_row("1000001")]))
    rendered = repr(parsed)
    assert _SWITCH_OPERATOR_EMAIL not in rendered
    assert _SWITCH_OPERATOR_LOGIN not in rendered
    assert "SYNTH-SWITCH-BUSERID" not in rendered


# T-60 (interface): parse_job_dates, rebuilt on parse_job_list, keeps its output and
# does not require pageInfo or poNum.

def test_parse_job_dates_still_returns_each_jobs_dates_when_page_info_and_po_num_are_absent():
    jobnos = ["1000001", "1000002"]
    rows = []
    for jn in jobnos:
        row = _switch_row(jn)
        for field in discovery_mod.JOB_DATE_FIELDS:
            row[field] = f"SYNTH-{field}-{jn}"
        rows.append(row)
    result = _switch_result(rows, omit=("poNum",), page_info=False)

    dates = _jobs().parse_job_dates(result)

    assert set(dates) == set(jobnos)
    for jn in jobnos:
        assert set(dates[jn]) == set(discovery_mod.JOB_DATE_FIELDS)
        for field in discovery_mod.JOB_DATE_FIELDS:
            assert dates[jn][field] == f"SYNTH-{field}-{jn}"


# T-61 (interface): one decision function for "does a failed write need a reread".

def _failure(kind: str):
    return helpers.GuardAbort({"error": f"SYNTH-{kind}"}, kind=kind)


@pytest.mark.parametrize("kind", ["transport", "malformed", "non_json", "unrecognised_status",
                                  "a_kind_added_later"])
def test_ambiguous_write_failures_are_reread_whatever_the_batch_size(kind):
    assert _jobs().write_failure_disposition(_failure(kind), batch_size=1) == "reread_ambiguous"
    assert _jobs().write_failure_disposition(_failure(kind), batch_size=2) == "reread_ambiguous"


@pytest.mark.parametrize("kind", ["validation", "not_found"])
def test_a_refused_batch_is_reread_only_when_it_holds_more_than_one_job(kind):
    assert _jobs().write_failure_disposition(_failure(kind), batch_size=2) == "reread_refused"
    assert _jobs().write_failure_disposition(_failure(kind), batch_size=1) == "raise"


@pytest.mark.parametrize("kind", ["expired", "blocked", "challenge", "header_fault", "throttled"])
def test_session_and_gate_failures_are_raised_not_reread(kind):
    assert _jobs().write_failure_disposition(_failure(kind), batch_size=2) == "raise"


# ── job-edit-create (T-59) ─────────────────────
# permit_state, JobList.add_on_tables and JobList.add_ons. The jobList result is the
# job-switch synthesiser above (_switch_result / _switch_row); the four add-on tables
# arrive as overrides, never from a fixture. Cases named here are job-edit-create's T-59;
# the T-59 comments above belong to a different spec.

_ADD_ON_TABLES = ("hotOrder", "selectJob", "priority", "jobSort")


def test_permit_state_is_allowed_denied_or_unknown_and_never_guesses():
    jobs = _jobs()
    allowed = jobs.permit_state({"job__4": {"result": True, "message": ""}}, "job__4")
    assert allowed.state == "allowed"

    denied = jobs.permit_state({"job__4": {"result": False, "message": "SYNTH-DENIED-REASON"}}, "job__4")
    assert denied.state == "denied"
    assert denied.message == "SYNTH-DENIED-REASON"

    denied_without_text = jobs.permit_state({"job__4": {"result": False}}, "job__4")
    assert denied_without_text.state == "denied"
    assert denied_without_text.message is None

    unknown = [
        ({"job__5": {"result": True}}, "job__4"),
        ({"job__4": {"result": "true", "message": ""}}, "job__4"),
        ({"job__4": {"result": 1, "message": ""}}, "job__4"),
        ({"job__4": "SYNTH-NOT-AN-OBJECT"}, "job__4"),
        (["SYNTH-PERMIT-LIST"], "job__4"),
        (None, "job__4"),
    ]
    for permit, name in unknown:
        assert jobs.permit_state(permit, name).state == "unknown", (permit, name)


def test_parse_job_list_keeps_the_four_add_on_tables_as_given_and_accepts_a_string_table():
    jobs = _jobs()
    rows = [_switch_row("1000001"), _switch_row("1000002")]
    parsed = jobs.parse_job_list(_switch_result(rows, overrides={
        "hotOrder": {"1000001": 2},
        "selectJob": {"1000002": 1},
        "priority": [],
        "jobSort": {"1000001": 1},
    }))
    tables = parsed.add_on_tables
    assert tables.hot_order == {"1000001": 2}
    assert tables.select_job == {"1000002": 1}
    assert tables.priority == []
    assert tables.job_sort == {"1000001": 1}

    parsed = jobs.parse_job_list(_switch_result(
        [_switch_row("1000001")],
        overrides={"hotOrder": "SYNTH-STRING-TABLE"},
        omit=("selectJob", "priority", "jobSort"),
    ))
    assert parsed.add_on_tables.hot_order == "SYNTH-STRING-TABLE"
    assert parsed.add_on_tables.select_job is None
    assert parsed.add_on_tables.priority is None
    assert parsed.add_on_tables.job_sort is None


def test_add_ons_returns_this_jobnos_entries_as_kind_and_value_and_refuses_string_tables():
    jobs = _jobs()
    parsed = jobs.parse_job_list(_switch_result(
        [_switch_row("1000001"), _switch_row("1000002")],
        overrides={
            "hotOrder": {"1000001": 2, "1000003": 9},
            "selectJob": {"1000002": 1},
            "priority": {"1000001": 3},
            "jobSort": {},
        },
    ))
    assert parsed.add_ons("1000001") == {"focus": 2, "priority": 3}
    assert parsed.add_ons("1000002") == {"pick_up": 1}

    empty = jobs.parse_job_list(_switch_result(
        [_switch_row("1000001")], overrides={table: [] for table in _ADD_ON_TABLES}))
    assert empty.add_ons("1000001") == {}

    absent = jobs.parse_job_list(_switch_result([_switch_row("1000001")], omit=_ADD_ON_TABLES))
    assert absent.add_ons("1000001") == {}

    for table in _ADD_ON_TABLES:
        string_table = jobs.parse_job_list(_switch_result(
            [_switch_row("1000001")], overrides={table: "SYNTH-STRING-TABLE"}))
        with pytest.raises(helpers.MalformedResponseError):
            string_table.add_ons("1000001")
