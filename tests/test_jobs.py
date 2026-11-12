"""Tests for the job tools (list_applicants, list_jobs, list_refreshable_jobs), their
pure builders, and the job-number helpers in tools/helpers.py -- applicants-and-job-reads.

Cases in this file (design.md Testing Strategy, "Test Cases" table):
  Phase 1 (task 11):  T-45, T-46
  Phase 2 (task 7):   T-1 .. T-26, T-30 .. T-44, T-47, T-48, T-50 .. T-55
  (T-26 lives in tests/test_discovery.py, which imports this file's harness.)

Written against design.md's declared interfaces only (spec-tester Mode 1 blindness rule):
the three tools are reached through their registration entry (register_job_tools), the
pure builders through their module attributes, and every 104 request goes through the
real guard + classifier with only the transport (`fetch`) replaced by a scripted spy.
Nothing is mocked across the classify / guard seam.

Import discipline: tools/jobs.py is imported lazily (see _jobs()), so a missing module fails
the Phase 2 tests with ImportError but does not stop collection of the Phase 1 tests above.

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
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from tests.conftest import _SeqFetchSpy, load_fixture

TAIPEI = timezone(timedelta(hours=8))
UTC = timezone.utc
MATCH_BATCH = 20
MATCH_CAP = 80
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


def _list_jobs_script(jobs_body, *, match_bodies=None) -> list[RawResponse]:
    if match_bodies is None:
        match_bodies = [_match_body(batch) for batch in _batches(_jobnos_of(jobs_body))]
    return [_raw_from_body(jobs_body)] + [_raw_from_body(body) for body in match_bodies]


async def _run_list_jobs(tmp_path, monkeypatch, jobs_body, *, match_bodies=None):
    ctx, info = await _new_session(tmp_path)
    spy = _install(monkeypatch, _list_jobs_script(jobs_body, match_bodies=match_bodies))
    result = await _tool("list_jobs")(ctx=ctx)
    return result, spy, info


def _applicant_raw(mutate) -> RawResponse:
    body = load_fixture("rows_applicants.json")
    mutate(body)
    return _raw_from_body(body)


# ══ Phase 1 (task 11) — T-45, T-46 ═══════════════════════════════════════════════════════

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


# ══ Phase 2 (task 7) ══════════════════════════════════════════════════════════════════════

# -- T-1 / T-2 (R1.1): the window is 60 days ending on Taipei's today -------------------

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


# -- T-3 (R1.2): candidate_id is idNo, p_id is pId; no raw idNo on the row -------------

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


# -- T-4 (R1.2, e2e): the two identifiers are accepted by the tools they are meant for --

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


# -- T-5 (R1.3): the row carries only allowlisted fields (plus input_date) -------------

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


# -- T-6 (R1.4): exp_job_arr keeps four fields per entry; no note -----------------------

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


# -- T-7 (R1.5): excluded fields never appear, at any level of the response ------------

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


# -- T-8 (R1.6): the today group ----------------------------------------------------------

@pytest.mark.asyncio
async def test_a_today_group_row_has_null_input_date_and_leads_the_list(tmp_path, monkeypatch):
    # No capture has a non-empty resumeGroups.today, so the today group is built here by
    # moving one row out of a two-row day group into `today`. Design §C2 gives `today` as a
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


# -- T-9 (R1.7): the job header -----------------------------------------------------------

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


# -- T-10 (R1.8): resume_number is four ints ----------------------------------------------

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


# -- T-11 (R1.9): a populated other bucket is named; zero buckets are not ------------------

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


# -- T-12 (R1.10): zero applicants is a legal empty result --------------------------------

@pytest.mark.asyncio
async def test_zero_applicant_job_is_an_empty_success_not_an_error(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    body = load_fixture("zero_row_applicants.json")
    _install(monkeypatch, [_raw_from_body(body)])

    result = await _tool("list_applicants")(str(body["result"]["jobNo"]), ctx=ctx)

    assert "error" not in result
    assert result["results"] == []
    assert result["pagination"]["total"] == 0


# -- T-13 (R1.11): pagination mirrors pageInfo; more pages are named --------------------

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


# -- T-14 (R1.12): no browse quota on the applicant list ----------------------------------

@pytest.mark.asyncio
async def test_applicant_list_reports_no_browse_limit(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    _install(monkeypatch, [_raw_from_body(load_fixture("rows_applicants.json"))])

    result = await _tool("list_applicants")(_jobno(), ctx=ctx)

    assert result["browse_limit"] is None


# -- T-15 (R2.1): a bad jobno is refused before any request --------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["", "abc", "117 06763", "11706763a"])
async def test_non_digit_jobno_is_refused_naming_list_jobs_and_sends_nothing(tmp_path, monkeypatch, bad):
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])

    result = await _tool("list_applicants")(bad, ctx=ctx)

    assert "error" in result and "list_jobs" in result["error"]
    assert "results" not in result
    assert spy.calls == []


# -- T-16 (R3.1): apply_count60 per job, as int ---------------------------------------------

@pytest.mark.asyncio
async def test_list_jobs_carries_apply_count60_as_an_int_per_job(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    assert len(result["results"]) == len(body["data"])
    for row, source in zip(result["results"], body["data"]):
        assert row["apply_count60"] == source["applyCount60"]
        assert type(row["apply_count60"]) is int


# -- T-17 (R3.2): no all-time apply count, even when the fixture carries one ----------------

@pytest.mark.asyncio
async def test_list_jobs_rows_never_carry_apply_count(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    assert any("applyCount" in row for row in body["data"])  # the fixture does carry it
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    for row in result["results"]:
        assert "apply_count" not in row and "applyCount" not in row


# -- T-18 (R3.3): same order as jobs/apply, jobno as string, list fields present ------------

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


# -- T-19 (R3.4): the allowlist, not the upstream body, decides the row ---------------------

@pytest.mark.asyncio
async def test_an_unlisted_upstream_field_is_not_on_the_row(tmp_path, monkeypatch):
    body = load_fixture("jobs_apply.json")
    body["data"][0]["zzNewField"] = "zz-sentinel"
    result, _spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    row = result["results"][0]
    assert "zz_new_field" not in row and "zzNewField" not in row
    assert "zz-sentinel" not in json.dumps(result, ensure_ascii=False)


# -- T-20 (R4.1): 21-40 jobs -> jobs list, then a 20-job batch and a remainder batch ----------

@pytest.mark.asyncio
async def test_more_than_twenty_jobs_send_a_list_then_two_count_batches_and_each_row_gets_its_own_counts(
    tmp_path, monkeypatch
):
    body = load_fixture("jobs_apply.json")
    jobnos = _jobnos_of(body)
    _require_two_count_batches(jobnos)
    result, spy, _info = await _run_list_jobs(tmp_path, monkeypatch, body)

    assert [endpoint.key for endpoint, _p, _b in spy.calls] == ["list_jobs", "match_number", "match_number"]
    assert _jobnos_sent(spy.calls[1][1]) == jobnos[:20]
    assert _jobnos_sent(spy.calls[2][1]) == jobnos[20:]
    for row in result["results"]:
        assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(row["jobno"])
        assert type(row["today_match_count"]) is int and type(row["total_match_count"]) is int


# -- T-21 (R4.2): a missing or unreadable count nulls that row and names its jobno ----------

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


# -- T-22 (R4.3): only the first 80 jobs get counts ---------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("count", [81, 80], ids=["81-jobs", "80-jobs"])
async def test_only_the_first_eighty_jobs_are_matched_and_the_eighty_first_is_named(tmp_path, monkeypatch, count):
    jobnos = _fake_jobnos(count)
    body = _jobs_body(jobnos)
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, _list_jobs_script(body))

    result = await _tool("list_jobs")(ctx=ctx)

    assert [endpoint.key for endpoint, _p, _b in spy.calls] == ["list_jobs"] + ["match_number"] * 4
    for index, call in enumerate(spy.calls[1:]):
        assert _jobnos_sent(call[1]) == jobnos[MATCH_BATCH * index: MATCH_BATCH * (index + 1)]
    rows = result["results"]
    assert len(rows) == count
    for row in rows[:MATCH_CAP]:
        assert (row["today_match_count"], row["total_match_count"]) == _expected_counts(row["jobno"])
    if count > MATCH_CAP:
        assert rows[MATCH_CAP]["today_match_count"] is None and rows[MATCH_CAP]["total_match_count"] is None
        assert any("80" in w for w in result["warnings"]), result["warnings"]
    else:
        assert not any("80" in w for w in result["warnings"]), result["warnings"]


# -- T-23 (R4.4): a session failure on the second segment is the same error as elsewhere --

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
    _install(monkeypatch, [_raw_from_body(load_fixture("jobs_apply.json")), make_raw()])
    result = await _tool("list_jobs")(ctx=ctx)

    assert result == reference
    assert "error" in result and "results" not in result


# -- T-50 (R4.5): a count batch the window cannot take is skipped and warned; other count
#    failures keep the job list too ----------------------------------------------------------

@pytest.mark.asyncio
async def test_count_batch_that_does_not_fit_the_window_is_skipped_and_warned(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    jobs_apply = load_fixture("jobs_apply.json")
    _require_two_count_batches(jobs_apply["data"])
    # One slot is left after the jobs/apply request, and the two count batches need two.
    for _ in range(app.config.max_requests_per_hour - 2):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install(monkeypatch, [_raw_from_body(jobs_apply)])

    result = await _tool("list_jobs")(ctx=ctx)

    assert len(spy.calls) == 1, "no count request may be sent when the gate refuses the second segment"
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
    _install(monkeypatch, [_raw_from_body(jobs_apply), match_raw()])

    result = await _tool("list_jobs")(ctx=ctx)

    assert "error" not in result and "results" in result
    assert len(result["results"]) == len(jobs_apply["data"])
    assert all(r["today_match_count"] is None and r["total_match_count"] is None for r in result["results"])
    assert result["warnings"], "the reason the counts are missing must be stated"


# -- T-24 (R5.1): refreshable rows are {jobno, job, appear_date} --------------------------

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


# -- T-25 (R5.2): no update permission is named; permission is silent --------------------

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


# -- T-30 (R7.1): auth failures on the first request match list_matched_resumes ----------

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


# -- T-31 (R7.1): a non-SUCCESS applyDetail is an error, never an empty list ---------------

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


# -- T-32 (R7.2): structural mismatches are structural errors ------------------------------

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


# -- T-60 (R7.2, R1.2): an applicant row without its identifiers is structural --------------

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


# -- T-33 (R7.3): no session and no credential file ----------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_applicants", "list_jobs", "list_refreshable_jobs"])
async def test_no_session_and_no_credential_file_answers_please_login_without_a_request(tmp_path, monkeypatch, tool):
    ctx, _ = await _new_session(tmp_path, logged_in=False)
    spy = _install(monkeypatch, [])

    result = await _call_tool(tool, ctx)

    assert result == NOT_LOGGED_IN
    assert spy.calls == []


# -- T-34 (R8.1): every request is a GET with no body --------------------------------------

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


# -- T-35 (R8.2): one request for the two single-request tools ------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", ["list_applicants", "list_refreshable_jobs"])
async def test_a_successful_call_sends_exactly_one_request(tmp_path, monkeypatch, tool):
    ctx, _ = await _new_session(tmp_path)
    spy = _install(monkeypatch, _SCRIPTS[tool]())

    await _call_tool(tool, ctx)

    assert len(spy.calls) == 1


# -- T-36 (R8.3): the rolling window counts every request of the multi-request call ---------

@pytest.mark.asyncio
async def test_list_jobs_with_more_than_twenty_jobs_adds_three_entries_to_the_rolling_window(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    before = len(info.throttle.request_timestamps)
    jobs_apply = load_fixture("jobs_apply.json")
    _require_two_count_batches(jobs_apply["data"])
    _install(monkeypatch, _list_jobs_script(jobs_apply))

    await _tool("list_jobs")(ctx=ctx)

    assert len(info.throttle.request_timestamps) - before == 3


# -- T-37 (R8.4): the cold-path worst case of list_jobs, computed not waited ---------------

def test_list_jobs_cold_path_worst_case_leaves_fifteen_seconds_under_the_120_second_timeout():
    from mcp104.browser.api_client import FETCH_TIMEOUT_SECONDS

    interval = get_config().min_call_interval_seconds
    batches = MATCH_CAP // MATCH_BATCH
    identity_lookup = interval + FETCH_TIMEOUT_SECONDS
    jobs_segment = interval + FETCH_TIMEOUT_SECONDS
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


def test_build_job_page_on_an_empty_list_is_empty_with_no_reminder():
    page = _jobs().build_job_page({"data": [], "metadata": {"total": 0, "page": 1}})

    assert page["results"] == []
    assert page["warnings"] == []


# -- T-42 (interface): batching is 20 per request and stops at 80 jobs ---------------------

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


# -- T-47 (interface, R6.2 values): HTML in text fields converts exactly like search rows --

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


# The pre-cycle tool surface: the 23 names register_all_tools registers on the main tree at
# c6fb0b4 (before applicants-and-job-reads), taken by running that tree's list_tools rather
# than from the design. The cycle adds exactly list_applicants and list_refreshable_jobs;
# list_jobs was already registered and keeps its name.
_PRE_CYCLE_TOOL_NAMES = frozenset({
    "browse_filter_values", "check_already_contacted", "check_login", "describe_result_fields",
    "get_candidate_photo", "get_conversation", "get_resume_attachment", "get_resume_detail",
    "list_jobs", "list_matched_resumes", "list_recommended_resumes", "list_templates", "login",
    "logout", "read_messages", "search_resumes", "send_inquiry", "send_interview_invite",
    "send_message", "send_personality_test_invite", "send_start_date_reminder",
    "send_thank_you_letter", "update_candidate_status",
})

# -- T-48 (interface): registration surface -------------------------------------------------

@pytest.mark.asyncio
async def test_register_all_tools_registers_the_job_tools_once_within_budget_and_without_ctx():
    from mcp.server.fastmcp import FastMCP

    from mcp104.main import register_all_tools

    counting = _FakeMCP()
    register_all_tools(counting)
    for name in ("list_applicants", "list_jobs", "list_refreshable_jobs"):
        assert counting.counts.get(name) == 1, f"{name} must be registered exactly once: {counting.counts.get(name)}"

    served_mcp = FastMCP("applicants-and-job-reads-test")
    register_all_tools(served_mcp)
    served = {tool.name: tool for tool in await served_mcp.list_tools()}
    expected_names = _PRE_CYCLE_TOOL_NAMES | {"list_applicants", "list_refreshable_jobs"}
    assert set(counting.counts) == expected_names, (
        f"registered names differ from pre-cycle + the two new tools: "
        f"{sorted(set(counting.counts) ^ expected_names)}"
    )
    assert set(served) == expected_names, (
        f"published tool set differs from pre-cycle + the two new tools: "
        f"{sorted(set(served) ^ expected_names)}"
    )
    for name in ("list_applicants", "list_jobs", "list_refreshable_jobs"):
        assert name in served
        assert len(served[name].description or "") <= 1948, f"{name} description exceeds the budget"
        assert "ctx" not in served[name].inputSchema.get("properties", {}), f"{name} exposes ctx to the caller"


# -- T-51 (R7.2): object-shaped jobs/apply data is structural and sends no count request ---

@pytest.mark.asyncio
async def test_jobs_apply_object_data_sends_only_the_jobs_request(tmp_path, monkeypatch):
    ctx, _ = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_raw_from_body({"data": {"list": []}, "metadata": {}})])

    result = await _tool("list_jobs")(ctx=ctx)

    assert len(spy.calls) == 1
    assert "104 回應結構異常" in result["error"]
    assert "results" not in result


# -- T-52 (R8.2): a full window refuses the first request of each tool before any send -----

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


# -- T-55 (R7.2): an applyDetail that does not match the request is structural -------------

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


# -- Moved from tests/test_search.py (tasks.md Phase 2 task 7): the list_jobs cases ----------
# Expectations follow the new route and the new fields (switch is "on"/"off" as jobs/apply
# returns it; counts come from the match-number follow-up). The old R5.3 docstring check that
# lived beside them is not carried here; see the report.

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
