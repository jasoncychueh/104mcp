"""Tests for the candidate-status tools and the id-link recording call sites.

Covers the two read tools (get_candidate_status, list_tracked_candidates), the
contact check and status write that now read through the same record lookup,
the pair recording done in the search, applicant, resume-detail and event
tools, the send-time status write, and the tool registration surface.

Same transport seam as tests/test_events.py: api_client.fetch and helpers.fetch
are replaced; guarded_api, the session pool, and the SQLite file are real. The
account identity is cached on the session, so no identity request is made.
Synthetic ids and names only; fixture values are read at run time and never
copied into this file.
"""

import asyncio
import copy
import json
import logging
import re
import sqlite3
from datetime import datetime
from uuid import uuid4
from zoneinfo import ZoneInfo

import aiosqlite
import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

import mcp104.db.database as dbmod
from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database, ID_SOURCE_MESSAGE, ID_SOURCE_RESUME, ID_SOURCE_UNKNOWN
from mcp104.tools.events import register_event_tools
from mcp104.tools.helpers import get_session_id
from mcp104.tools.jobs import register_job_tools
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from mcp104.tools.status import register_status_tools
from tests.conftest import _SeqFetchSpy, load_fixture

TAIPEI = ZoneInfo("Asia/Taipei")
UTC = ZoneInfo("UTC")
TAIPEI_ISO = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+08:00")

ACCOUNT = "test@104.com"
OTHER_ACCOUNT = "other@104.com"
RESUME = ID_SOURCE_RESUME
MESSAGE = ID_SOURCE_MESSAGE
UNKNOWN = ID_SOURCE_UNKNOWN
KNOWN_OLD = "2020-01-01 00:00:00"
SEARCH_KEYWORD = "應用工程師"
JOBNO = "12355016"
PID = "073748"
BRIDGED = "0900000000001"
LETTER = "測試信件本文-合成"
LOCATION = "測試地點-合成"
CONTACT = "測試聯絡人"
CONTACT_PHONE = "0900-000-000"
FUTURE_MEETING = "2099-12-31 10:00"
NO_LOGIN = {"error": "請先呼叫 login()"}
ID_SOURCE_ERROR = {"error": "id_source 必須為 ['message', 'resume'] 之一"}
VALID_STATUSES = ("contacted", "replied", "interested", "not_interested", "scheduled")

# The tool names registered before this change: the 0.6.0 surface.
PRE_ROUND_TOOL_NAMES = frozenset({
    "browse_filter_values", "check_already_contacted", "check_login", "describe_result_fields",
    "get_candidate_photo", "get_conversation", "get_job_detail", "get_resume_attachment",
    "get_resume_detail", "list_applicants", "list_jobs", "list_matched_resumes",
    "list_recommended_resumes", "list_refreshable_jobs", "list_templates", "login", "logout",
    "read_messages", "refresh_jobs", "search_resumes", "send_inquiry", "send_interview_invite",
    "send_message", "send_personality_test_invite", "send_start_date_reminder",
    "send_thank_you_letter", "update_candidate_status",
})


# ── Registration plumbing (mirrors tests/test_events.py) ────────────────────────

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


class _FakeMCP:
    def __init__(self):
        self.tools = {}
        self.counts = {}
        self.descriptions = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self.counts[name] = self.counts.get(name, 0) + 1
            self.tools[name] = fn
            self.descriptions[name] = kwargs.get("description") or fn.__doc__ or ""
            return fn
        return decorator


_REGISTRY = None


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        mcp = _FakeMCP()
        register_messaging_tools(mcp)
        register_status_tools(mcp)
        register_event_tools(mcp)
        register_search_tools(mcp)
        register_job_tools(mcp)
        _REGISTRY = mcp
    return _REGISTRY


def _tool(name):
    return _registry().tools[name]


_OPENED = []


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED:
        await _OPENED.pop().close()


async def _open_database(path):
    database = Database(str(path))
    _OPENED.append(database)
    await database.init()
    return database


async def _session(tmp_path, account=ACCOUNT, database=None):
    if database is None:
        database = await _open_database(tmp_path / f"db_{uuid4().hex}.sqlite")
    pool = SessionPool()
    ctx = _FakeCtx(pool, database)
    info = SessionInfo(cookies=[], account_label=account)
    pool.activate_direct(get_session_id(ctx), info)
    return ctx, info, database


async def _anonymous(tmp_path):
    database = await _open_database(tmp_path / f"db_{uuid4().hex}.sqlite")
    return _FakeCtx(SessionPool(), database), database


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


# ── Fake transport ──────────────────────────────────────────────────────────────

def _raw_from_body(body, status=200):
    text = json.dumps(body, ensure_ascii=False)
    return RawResponse(status=status, location=None, content_type="application/json; charset=utf-8",
                       body=text, body_bytes=text.encode("utf-8"), parsed_json=body)


def _raw_from_wrapper(fixture_name):
    d = load_fixture(fixture_name)
    return RawResponse(status=d["http_status"], location=None, content_type=d["content_type"],
                       body=d["body_text"], body_bytes=d["body_text"].encode("utf-8"),
                       parsed_json=d["body_json"])


class _RepeatFetch:
    def __init__(self, response):
        self._response = response
        self.calls = []

    async def __call__(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append((endpoint, params, body))
        return self._response


class _NoRequestFetch:
    def __init__(self):
        self.calls = []

    async def __call__(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append((endpoint, params, body))
        raise AssertionError("this tool must not send a 104 request")


def _patch_fetch(monkeypatch, spy):
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


def _repeat_fetch(monkeypatch, raw):
    return _patch_fetch(monkeypatch, _RepeatFetch(raw))


def _scripted_fetch(monkeypatch, scripted):
    return _patch_fetch(monkeypatch, _SeqFetchSpy(scripted))


def _resolve_ok(id_no):
    return _raw_from_body({"data": {"idNo": id_no}, "metadata": {}})


def _last_info(emailcc=None):
    return _raw_from_body({"data": {"emailCC": emailcc or []}, "metadata": {}})


def _envelope(p_id, id_no, event_id):
    return {
        "data": [{
            "pId": p_id,
            "idNo": id_no,
            "streamId": f"{p_id}_{JOBNO}",
            "messageId": ["2095355457"],
            "eventId": event_id,
            "isSynchronized": True,
        }],
        "failed": [],
        "metadata": {},
    }


def _inquiry_script(p_id, id_no=BRIDGED, event_id="evt-syn-0001"):
    return [_resolve_ok(id_no), _last_info(), _raw_from_body(_envelope(p_id, id_no, event_id))]


# ── SQL helpers (raw reads of persisted state) ──────────────────────────────────

async def _select(path, sql, params=()):
    async with aiosqlite.connect(str(path)) as conn:
        cursor = await conn.execute(sql, params)
        names = [column[0] for column in cursor.description]
        rows = await cursor.fetchall()
    return [dict(zip(names, row)) for row in rows]


async def _execute(path, sql, params=()):
    async with aiosqlite.connect(str(path)) as conn:
        await conn.execute(sql, params)
        await conn.commit()


async def _snapshot_all(path):
    tables = await _select(
        path, "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
    )
    return {table["name"]: await _select(path, f"SELECT * FROM {table['name']} ORDER BY rowid")
            for table in tables}


async def _row(path, candidate_id, id_source, account=ACCOUNT):
    rows = await _select(
        path,
        "SELECT * FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (candidate_id, id_source, account),
    )
    return rows[0] if rows else None


async def _count_rows(path, candidate_id, id_source, account=ACCOUNT):
    rows = await _select(
        path,
        "SELECT COUNT(*) AS n FROM candidates WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (candidate_id, id_source, account),
    )
    return rows[0]["n"]


async def _insert_raw(path, candidate_id, id_source, *, account=ACCOUNT, name=None, status=None,
                      created_at="2026-01-01 00:00:00", status_updated_at=None):
    await _execute(
        path,
        "INSERT INTO candidates (candidate_id, id_source, account_email, name, status, created_at, "
        "status_updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (candidate_id, id_source, account, name, status, created_at, status_updated_at),
    )


async def _set_status_time(path, candidate_id, id_source, stamp, account=ACCOUNT):
    await _execute(
        path,
        "UPDATE candidates SET status_updated_at = ? WHERE candidate_id = ? AND id_source = ? AND account_email = ?",
        (stamp, candidate_id, id_source, account),
    )


# ── Tool-call helpers ───────────────────────────────────────────────────────────

async def _update(ctx, candidate_id, id_source, status):
    return await _tool("update_candidate_status")(
        candidate_id=candidate_id, id_source=id_source, status=status, ctx=ctx)


async def _status(ctx, candidate_id, id_source):
    return await _tool("get_candidate_status")(candidate_id=candidate_id, id_source=id_source, ctx=ctx)


async def _listed(ctx, **kwargs):
    return await _tool("list_tracked_candidates")(ctx=ctx, **kwargs)


async def _contacted(ctx, candidate_id, id_source):
    return await _tool("check_already_contacted")(candidate_id=candidate_id, id_source=id_source, ctx=ctx)


async def _search(ctx):
    return await _tool("search_resumes")(keyword=SEARCH_KEYWORD, ctx=ctx)


async def _search_fixture_pair(ctx, monkeypatch):
    """Run the real search tool on the fixture and return the first row's (resume id, message id)."""
    _repeat_fetch(monkeypatch, _raw_from_body(load_fixture("rows_search.json")))
    result = await _search(ctx)
    assert "error" not in result
    row = result["results"][0]
    return row["candidate_id"], row["p_id"]


def _to_taipei(utc_text):
    if utc_text is None:
        return None
    parsed = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    return parsed.astimezone(TAIPEI).isoformat(timespec="seconds")


def _records_for(result, candidate_id, id_source):
    return [r for r in result["records"] if r["candidate_id"] == candidate_id and r["id_source"] == id_source]


def _linked_items(result):
    return [item for record in result["records"] for item in record["linked_ids"]]


async def _assert_every_linked_pair_is_found(ctx, rows):
    pairs = [(row["candidate_id"], row["p_id"]) for row in rows if row.get("candidate_id") and row.get("p_id")]
    assert pairs, "the fixture rows must carry both ids on at least one row"
    for resume_id, message_id in pairs:
        await _update(ctx, message_id, MESSAGE, "contacted")
        result = await _status(ctx, message_id, MESSAGE)
        assert any(item["candidate_id"] == resume_id and item["id_source"] == RESUME
                   for item in _linked_items(result)), (resume_id, message_id)


# ═════════════════════════════════════════════════════════════════════════════════
# get_candidate_status — reading one candidate back
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_status_written_by_update_reads_back_as_one_record(tmp_path):
    ctx, _info, _db = await _session(tmp_path)
    await _update(ctx, "123", MESSAGE, "interested")

    result = await _status(ctx, "123", MESSAGE)

    assert "error" not in result
    assert len(result["records"]) == 1
    record = result["records"][0]
    assert record["candidate_id"] == "123"
    assert record["id_source"] == MESSAGE
    assert record["status"] == "interested"
    assert isinstance(record["created_at"], str) and TAIPEI_ISO.fullmatch(record["created_at"])
    assert isinstance(record["status_updated_at"], str) and TAIPEI_ISO.fullmatch(record["status_updated_at"])
    assert record["name"] is None


@pytest.mark.asyncio
async def test_contact_recorded_on_message_id_is_read_back_from_the_resume_id_of_the_same_person(
        tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    resume_id, message_id = await _search_fixture_pair(ctx, monkeypatch)
    await _update(ctx, message_id, MESSAGE, "contacted")

    result = await _status(ctx, resume_id, RESUME)

    matches = [r for r in result["records"] if r["candidate_id"] == message_id]
    assert len(matches) == 1
    assert matches[0]["id_source"] == MESSAGE
    assert matches[0]["status"] == "contacted"
    assert matches[0]["matched_by"] == "linked"


@pytest.mark.asyncio
async def test_records_keep_database_order_and_match_labels_for_the_same_person(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    resume_id, message_id = await _search_fixture_pair(ctx, monkeypatch)
    await _update(ctx, resume_id, RESUME, "interested")
    await _update(ctx, message_id, MESSAGE, "contacted")
    await _insert_raw(db.path, resume_id, UNKNOWN, status="contacted")

    result = await _status(ctx, resume_id, RESUME)
    expected = await db.find_candidate_records(resume_id, RESUME, ACCOUNT)

    got = [(r["candidate_id"], r["id_source"], r["matched_by"]) for r in result["records"]]
    want = [(r["candidate_id"], r["id_source"], r["matched_by"]) for r in expected]
    assert got == want
    assert {matched for _cid, _src, matched in got} == {"same_id", "same_id_unknown_source", "linked"}


@pytest.mark.asyncio
async def test_contacted_flag_matches_check_already_contacted_in_each_data_state(tmp_path, monkeypatch):
    # State 1: one message record with a status.
    ctx1, _i1, _d1 = await _session(tmp_path)
    await _update(ctx1, "123", MESSAGE, "interested")
    assert (await _status(ctx1, "123", MESSAGE))["contacted"] == await _contacted(ctx1, "123", MESSAGE)

    # State 2: a resume id paired with a contacted message id.
    ctx2, _i2, _d2 = await _session(tmp_path)
    resume_id, message_id = await _search_fixture_pair(ctx2, monkeypatch)
    await _update(ctx2, message_id, MESSAGE, "contacted")
    assert (await _status(ctx2, resume_id, RESUME))["contacted"] == await _contacted(ctx2, resume_id, RESUME)

    # State 3: nothing recorded.
    ctx3, _i3, _d3 = await _session(tmp_path)
    assert (await _status(ctx3, "999", RESUME))["contacted"] == await _contacted(ctx3, "999", RESUME)


@pytest.mark.asyncio
async def test_empty_store_reads_as_not_contacted_without_error(tmp_path):
    ctx, _info, _db = await _session(tmp_path)

    assert await _status(ctx, "999", RESUME) == {"contacted": False, "records": [], "warnings": []}


@pytest.mark.asyncio
async def test_linked_ids_show_the_other_side_status_and_time_converted_to_taipei(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    a_id, p_id, p2_id = "1101000000001", "110101", "110102"
    await db.record_id_links(ACCOUNT, [(a_id, p_id), (a_id, p2_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await _update(ctx, a_id, RESUME, "interested")
    await _update(ctx, p_id, MESSAGE, "scheduled")
    await _set_status_time(db.path, a_id, RESUME, "2026-11-11 07:00:00")
    await _set_status_time(db.path, p_id, MESSAGE, "2026-11-12 07:00:00")

    result = await _status(ctx, a_id, RESUME)
    db_rows = await db.find_candidate_records(a_id, RESUME, ACCOUNT)

    tool_by_key = {(r["candidate_id"], r["id_source"]): r for r in result["records"]}
    db_by_key = {(r["candidate_id"], r["id_source"]): r for r in db_rows}
    assert tool_by_key.keys() == db_by_key.keys()
    for key, tool_record in tool_by_key.items():
        expected_linked = [
            {
                "candidate_id": item["candidate_id"],
                "id_source": item["id_source"],
                "status": item["status"],
                "status_updated_at": _to_taipei(item["status_updated_at"]),
            }
            for item in db_by_key[key]["linked_ids"]
        ]
        assert tool_record["linked_ids"] == expected_linked, key

    a_record = tool_by_key[(a_id, RESUME)]
    p_item = next(item for item in a_record["linked_ids"] if item["candidate_id"] == p_id)
    assert p_item["status_updated_at"] == tool_by_key[(p_id, MESSAGE)]["status_updated_at"]
    p2_item = next(item for item in a_record["linked_ids"] if item["candidate_id"] == p2_id)
    assert p2_item["status"] is None
    assert p2_item["status_updated_at"] is None


@pytest.mark.asyncio
async def test_records_with_different_statuses_get_a_reminder_that_names_each_one(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    a_id, p_id = "1201000000001", "120101"
    await db.record_id_links(ACCOUNT, [(a_id, p_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await _update(ctx, a_id, RESUME, "interested")
    await _update(ctx, p_id, MESSAGE, "contacted")
    await _set_status_time(db.path, a_id, RESUME, "2026-11-11 07:00:00")
    await _set_status_time(db.path, p_id, MESSAGE, "2026-11-12 07:00:00")

    result = await _status(ctx, a_id, RESUME)

    reminders = [w for w in result["warnings"] if "狀態不同" in w]
    assert len(reminders) == 1
    a_record = _records_for(result, a_id, RESUME)[0]
    p_record = _records_for(result, p_id, MESSAGE)[0]
    for needle in (a_id, p_id, "interested", "contacted",
                   a_record["status_updated_at"], p_record["status_updated_at"]):
        assert needle in reminders[0], needle

    # Same status on both sides: no such reminder.
    await _update(ctx, p_id, MESSAGE, "interested")
    same = await _status(ctx, a_id, RESUME)
    assert not [w for w in same["warnings"] if "狀態不同" in w]


@pytest.mark.asyncio
async def test_status_time_reads_back_as_taipei_iso_and_missing_time_as_none(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _insert_raw(db.path, "8801", MESSAGE, status="interested", status_updated_at="2026-11-13 07:04:05")
    await _insert_raw(db.path, "8802", MESSAGE, status="contacted", status_updated_at=None)

    first = await _status(ctx, "8801", MESSAGE)
    assert first["records"][0]["status_updated_at"] == "2026-11-13T15:04:05+08:00"
    assert first["records"][0]["created_at"] == "2026-01-01T08:00:00+08:00"

    second = await _status(ctx, "8802", MESSAGE)
    assert second["records"][0]["status_updated_at"] is None


@pytest.mark.asyncio
async def test_reading_status_changes_no_row_in_any_table(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _update(ctx, "9001", MESSAGE, "interested")
    await _insert_raw(db.path, "9002", RESUME, name="Synthetic")
    before = await _snapshot_all(db.path)

    await _status(ctx, "9001", MESSAGE)
    await _status(ctx, "9999", RESUME)  # no record at all

    assert await _snapshot_all(db.path) == before


@pytest.mark.asyncio
async def test_status_reader_rejects_bad_source_and_blank_id_before_querying(tmp_path):
    ctx, _info, _db = await _session(tmp_path)

    assert set(await _status(ctx, "", RESUME)) == {"error"}
    assert set(await _status(ctx, "  ", RESUME)) == {"error"}
    unknown_source = await _status(ctx, "1", "unknown")
    assert set(unknown_source) == {"error"}
    assert RESUME in unknown_source["error"]
    assert MESSAGE in unknown_source["error"]


@pytest.mark.asyncio
async def test_status_reader_has_no_session_and_answers_like_the_contact_check(tmp_path, monkeypatch):
    ctx, _db = await _anonymous(tmp_path)
    spy = _patch_fetch(monkeypatch, _NoRequestFetch())

    assert await _status(ctx, "1", RESUME) == NO_LOGIN
    assert await _listed(ctx) == NO_LOGIN
    assert await _contacted(ctx, "1", RESUME) == NO_LOGIN
    assert spy.calls == []


@pytest.mark.asyncio
async def test_new_tools_send_no_request_spend_no_throttle_slot_and_leave_the_daily_count(tmp_path, monkeypatch):
    ctx, info, db = await _session(tmp_path)
    await _insert_raw(db.path, "4201", MESSAGE, status="contacted")
    spy = _patch_fetch(monkeypatch, _NoRequestFetch())
    window_before = len(info.throttle.request_timestamps)
    daily_before = await db.get_daily_sent_count(ACCOUNT)

    await _status(ctx, "4201", MESSAGE)
    await _listed(ctx)

    assert spy.calls == []
    assert len(info.throttle.request_timestamps) == window_before
    assert await db.get_daily_sent_count(ACCOUNT) == daily_before


@pytest.mark.asyncio
async def test_each_record_and_listed_row_has_a_fixed_key_set_without_the_account(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    a_id, p_id = "5101000000001", "510101"
    await db.record_id_links(ACCOUNT, [(a_id, p_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await _update(ctx, a_id, RESUME, "interested")
    await _update(ctx, p_id, MESSAGE, "contacted")
    record_keys = {"candidate_id", "id_source", "name", "status", "created_at", "status_updated_at",
                   "matched_by", "linked_ids"}

    status = await _status(ctx, a_id, RESUME)
    assert status["records"]
    for record in status["records"]:
        assert set(record) == record_keys
        assert "account_email" not in record

    listed = await _listed(ctx)
    assert listed["results"]
    for row in listed["results"]:
        assert set(row) == record_keys - {"matched_by"}
        assert "account_email" not in row


# ═════════════════════════════════════════════════════════════════════════════════
# list_tracked_candidates — listing by status
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_status_filter_lists_only_that_status_in_database_order(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    for candidate_id in ["1101", "1102", "1103"]:
        await _update(ctx, candidate_id, RESUME, "interested")
    for candidate_id in ["1104", "1105"]:
        await _update(ctx, candidate_id, MESSAGE, "contacted")
    await db.upsert_candidate("1106", RESUME, ACCOUNT, name="Synthetic name only")

    result = await _listed(ctx, status="interested")

    rows, total = await db.list_candidates(ACCOUNT, "interested", limit=50, offset=0)
    assert [(r["candidate_id"], r["id_source"], r["status"]) for r in result["results"]] == \
        [(r["candidate_id"], r["id_source"], r["status"]) for r in rows]
    assert result["pagination"]["total"] == total == 3


@pytest.mark.asyncio
async def test_no_status_filter_lists_every_status_bearing_record_in_database_order(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    for candidate_id in ["1101", "1102", "1103"]:
        await _update(ctx, candidate_id, RESUME, "interested")
    for candidate_id in ["1104", "1105"]:
        await _update(ctx, candidate_id, MESSAGE, "contacted")
    await db.upsert_candidate("1106", RESUME, ACCOUNT, name="Synthetic name only")

    result = await _listed(ctx)

    rows, total = await db.list_candidates(ACCOUNT, None, limit=50, offset=0)
    assert [(r["candidate_id"], r["id_source"], r["status"]) for r in result["results"]] == \
        [(r["candidate_id"], r["id_source"], r["status"]) for r in rows]
    assert result["pagination"]["total"] == total == 5


@pytest.mark.asyncio
async def test_listing_order_is_the_database_order_and_stable_across_calls(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _insert_raw(db.path, "5101", RESUME, status="interested", status_updated_at="2026-11-10 01:00:00")
    await _insert_raw(db.path, "5102", RESUME, status="interested", status_updated_at="2026-11-11 01:00:00")
    await _insert_raw(db.path, "5103", RESUME, status="interested", status_updated_at="2026-11-12 01:00:00")
    await _insert_raw(db.path, "5104", RESUME, status="interested", status_updated_at=None)
    await _insert_raw(db.path, "5105", MESSAGE, status="interested", status_updated_at="2026-11-11 01:00:00")
    await _insert_raw(db.path, "5106", MESSAGE, status="interested", status_updated_at="2026-11-11 01:00:00")

    first = await _listed(ctx)

    ids = [(r["candidate_id"], r["id_source"]) for r in first["results"]]
    rows, _total = await db.list_candidates(ACCOUNT, None, limit=50, offset=0)
    assert ids == [(r["candidate_id"], r["id_source"]) for r in rows]
    assert first["results"][-1]["status_updated_at"] is None
    second = await _listed(ctx)
    assert [(r["candidate_id"], r["id_source"]) for r in second["results"]] == ids


@pytest.mark.asyncio
async def test_list_pages_hold_fifty_rows_and_out_of_range_pages_are_empty_not_errors(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    for i in range(51):
        await _insert_raw(db.path, f"{5200 + i}", RESUME, status="scheduled",
                          status_updated_at="2026-11-13 07:00:00")

    page1 = await _listed(ctx, page=1)
    assert len(page1["results"]) == 50
    assert page1["pagination"] == {"page": 1, "total_pages": 2, "total": 51}
    page2 = await _listed(ctx, page=2)
    assert len(page2["results"]) == 1
    page3 = await _listed(ctx, page=3)
    assert "error" not in page3
    assert page3["results"] == []
    assert page3["pagination"]["total"] == 51

    assert set(page1) == {"results", "pagination", "browse_limit", "warnings"}
    assert page1["browse_limit"] is None
    assert set(page1["pagination"]) == {"page", "total_pages", "total"}

    empty_ctx, _info2, _db2 = await _session(tmp_path)
    empty = await _listed(empty_ctx)
    assert empty["pagination"]["total_pages"] == 0
    assert empty["results"] == []


@pytest.mark.asyncio
async def test_each_candidate_row_shows_its_other_space_id_when_both_ids_are_recorded(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    resume_id, message_id = await _search_fixture_pair(ctx, monkeypatch)
    await _update(ctx, resume_id, RESUME, "interested")
    await _update(ctx, message_id, MESSAGE, "contacted")

    result = await _listed(ctx)

    assert len(result["results"]) == 2
    by_key = {(r["candidate_id"], r["id_source"]): r for r in result["results"]}
    assert any(item["candidate_id"] == message_id for item in by_key[(resume_id, RESUME)]["linked_ids"])
    assert any(item["candidate_id"] == resume_id for item in by_key[(message_id, MESSAGE)]["linked_ids"])


@pytest.mark.asyncio
async def test_unknown_source_row_is_listed_with_a_source_reminder(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _insert_raw(db.path, "8701", UNKNOWN, status="contacted")

    result = await _listed(ctx)

    entries = [r for r in result["results"] if r["candidate_id"] == "8701"]
    assert len(entries) == 1
    assert entries[0]["id_source"] == UNKNOWN
    assert len([w for w in result["warnings"] if "來源已不可考" in w]) == 1


@pytest.mark.asyncio
async def test_list_rejects_bad_status_and_page_values_before_querying(tmp_path):
    ctx, _info, _db = await _session(tmp_path)

    for kwargs in ({"status": "maybe"}, {"page": 0}, {"page": -1}, {"page": True}, {"page": "1"},
                   {"page": 1.0}):
        result = await _listed(ctx, **kwargs)
        assert set(result) == {"error"}, kwargs
        assert isinstance(result["error"], str)

    bad_status = await _listed(ctx, status="maybe")
    for valid in VALID_STATUSES:
        assert valid in bad_status["error"]

    assert "error" not in await _listed(ctx, status="scheduled", page=3)


@pytest.mark.asyncio
async def test_list_entry_linked_ids_show_the_other_side_status_converted_and_order_intact(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    a_id, p_id = "5701000000001", "570101"
    await db.record_id_links(ACCOUNT, [(a_id, p_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await _update(ctx, a_id, RESUME, "interested")
    await _update(ctx, p_id, MESSAGE, "scheduled")
    await _set_status_time(db.path, p_id, MESSAGE, "2026-11-12 07:00:00")

    listed = await _listed(ctx, status="interested")
    a_entry = next(r for r in listed["results"] if r["candidate_id"] == a_id)

    rows, _total = await db.list_candidates(ACCOUNT, "interested", limit=50, offset=0)
    db_entry = next(r for r in rows if r["candidate_id"] == a_id)
    expected = [
        {
            "candidate_id": item["candidate_id"],
            "id_source": item["id_source"],
            "status": item["status"],
            "status_updated_at": _to_taipei(item["status_updated_at"]),
        }
        for item in db_entry["linked_ids"]
    ]
    assert a_entry["linked_ids"] == expected
    assert a_entry["linked_ids"][0]["status"] == "scheduled"


# ═════════════════════════════════════════════════════════════════════════════════
# Pair recording in the list, applicant, resume-detail and event tools
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_list_and_detail_responses_are_identical_on_first_and_second_call(tmp_path, monkeypatch):
    applicants_fixture = load_fixture("rows_applicants.json")
    detail_id = str(load_fixture("resume_unrestricted.json")["data"]["resume"]["idNo"])
    cases = [
        ("search_resumes", {"keyword": SEARCH_KEYWORD}, "rows_search.json",
         {"results", "pagination", "browse_limit", "warnings", "applied_filters"}),
        ("list_recommended_resumes", {"jobno": JOBNO}, "rows_recommend.json",
         {"results", "pagination", "browse_limit", "warnings"}),
        ("list_matched_resumes", {"jobno": JOBNO}, "rows_match.json",
         {"results", "pagination", "browse_limit", "warnings"}),
        ("list_applicants", {"jobno": str(applicants_fixture["result"]["jobNo"])}, "rows_applicants.json",
         {"job", "results", "pagination", "resume_number", "date_range", "browse_limit", "warnings"}),
        ("get_resume_detail", {"candidate_id": detail_id}, "resume_unrestricted.json",
         {"resume", "browse_limit", "warnings"}),
    ]
    for name, kwargs, fixture_name, expected_keys in cases:
        # A fresh store per tool: the first call has no recorded pairs yet.
        ctx, _info, _db = await _session(tmp_path)
        _repeat_fetch(monkeypatch, _raw_from_body(load_fixture(fixture_name)))
        first = await _tool(name)(ctx=ctx, **kwargs)
        second = await _tool(name)(ctx=ctx, **kwargs)
        assert "error" not in first, name
        assert first == second, name
        assert set(first) == expected_keys, name


@pytest.mark.asyncio
async def test_pairs_from_the_three_resume_lists_are_found_from_the_message_id(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)

    _repeat_fetch(monkeypatch, _raw_from_body(load_fixture("rows_search.json")))
    search = await _search(ctx)
    await _assert_every_linked_pair_is_found(ctx, search["results"])

    _repeat_fetch(monkeypatch, _raw_from_body(load_fixture("rows_recommend.json")))
    recommended = await _tool("list_recommended_resumes")(jobno=JOBNO, ctx=ctx)
    await _assert_every_linked_pair_is_found(ctx, recommended["results"])

    _repeat_fetch(monkeypatch, _raw_from_body(load_fixture("rows_match.json")))
    matched = await _tool("list_matched_resumes")(jobno=JOBNO, ctx=ctx)
    await _assert_every_linked_pair_is_found(ctx, matched["results"])


@pytest.mark.asyncio
async def test_pairs_from_the_applicant_list_are_found_from_the_message_id(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    fixture = load_fixture("rows_applicants.json")
    _repeat_fetch(monkeypatch, _raw_from_body(fixture))

    applicants = await _tool("list_applicants")(jobno=str(fixture["result"]["jobNo"]), ctx=ctx)

    assert "error" not in applicants
    await _assert_every_linked_pair_is_found(ctx, applicants["results"])


@pytest.mark.asyncio
async def test_resume_detail_records_its_own_id_pair_and_both_sides_read_back(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    body = copy.deepcopy(load_fixture("resume_unrestricted.json"))
    resume = body["data"]["resume"]
    id_no, p_id = str(resume["idNo"]), str(resume["pId"])
    _repeat_fetch(monkeypatch, _raw_from_body(body))
    detail = await _tool("get_resume_detail")(candidate_id=id_no, ctx=ctx)
    assert "error" not in detail
    await _update(ctx, id_no, RESUME, "interested")
    await _update(ctx, p_id, MESSAGE, "contacted")

    from_resume = await _status(ctx, id_no, RESUME)
    assert any(item["candidate_id"] == p_id and item["id_source"] == MESSAGE for item in _linked_items(from_resume))
    from_message = await _status(ctx, p_id, MESSAGE)
    assert any(item["candidate_id"] == id_no and item["id_source"] == RESUME for item in _linked_items(from_message))


@pytest.mark.asyncio
async def test_event_bridge_pair_is_recorded_even_when_104_rejects_the_send(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    _scripted_fetch(monkeypatch, [
        _resolve_ok(BRIDGED),
        _last_info(),
        _raw_from_wrapper("failure_send_validation.json"),
    ])
    rejected = await _tool("send_inquiry")(job_id=JOBNO, candidate_id=PID, message=LETTER, ctx=ctx)
    assert "error" in rejected
    await _update(ctx, PID, MESSAGE, "contacted")
    await _update(ctx, BRIDGED, RESUME, "interested")

    found = await _status(ctx, PID, MESSAGE)
    assert any(record["candidate_id"] == BRIDGED and record["id_source"] == RESUME
               for record in found["records"])

    # The reverse bridge itself failing: nothing is recorded, so nothing is found.
    failed_ctx, _info2, _db2 = await _session(tmp_path)
    _scripted_fetch(monkeypatch, [_raw_from_body({"code": "00004", "message": "找不到對應資源", "detail": []},
                                                 status=404)])
    failed = await _tool("send_inquiry")(job_id=JOBNO, candidate_id=PID, message=LETTER, ctx=failed_ctx)
    assert "error" in failed
    await _update(failed_ctx, PID, MESSAGE, "contacted")
    await _update(failed_ctx, BRIDGED, RESUME, "interested")

    not_found = await _status(failed_ctx, PID, MESSAGE)
    assert not any(item["candidate_id"] == BRIDGED for item in _linked_items(not_found))
    assert not any(record["candidate_id"] == BRIDGED for record in not_found["records"])


@pytest.mark.asyncio
async def test_repeated_observations_keep_one_pair_with_its_first_source_and_time(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    rows_body = load_fixture("rows_search.json")
    _repeat_fetch(monkeypatch, _raw_from_body(rows_body))
    first = await _search(ctx)
    row = first["results"][0]
    id_no, p_id = row["candidate_id"], row["p_id"]
    await _execute(db.path, "UPDATE candidate_id_links SET first_seen_at = ?", (KNOWN_OLD,))
    link_query = "SELECT id_no, p_id, source, first_seen_at FROM candidate_id_links ORDER BY id_no, p_id"
    before = await _select(db.path, link_query)

    await _search(ctx)
    detail_body = copy.deepcopy(load_fixture("resume_unrestricted.json"))
    detail_body["data"]["resume"]["idNo"] = id_no
    detail_body["data"]["resume"]["pId"] = p_id
    _repeat_fetch(monkeypatch, _raw_from_body(detail_body))
    await _tool("get_resume_detail")(candidate_id=id_no, ctx=ctx)

    after = await _select(db.path, link_query)
    assert after == before
    pair_rows = [r for r in after if r["id_no"] == id_no and r["p_id"] == p_id]
    assert len(pair_rows) == 1
    assert pair_rows[0]["source"] == "resume_row"
    assert pair_rows[0]["first_seen_at"] == KNOWN_OLD


@pytest.mark.asyncio
async def test_records_and_contact_checks_stay_within_one_account(tmp_path, monkeypatch):
    ctx_x, _info_x, db = await _session(tmp_path, account=ACCOUNT)
    resume_id, message_id = await _search_fixture_pair(ctx_x, monkeypatch)
    await _update(ctx_x, message_id, MESSAGE, "contacted")
    assert await _contacted(ctx_x, resume_id, RESUME) is True

    ctx_y, _info_y, _db_y = await _session(tmp_path, account=OTHER_ACCOUNT, database=db)
    assert await _contacted(ctx_y, resume_id, RESUME) is False
    assert (await _status(ctx_y, resume_id, RESUME))["records"] == []


@pytest.mark.asyncio
async def test_rows_missing_or_malformed_ids_are_skipped_and_other_rows_are_unchanged(tmp_path, monkeypatch):
    original = load_fixture("rows_search.json")
    original_rows = original["result"]["data"]
    assert len(original_rows) >= 4, "rows_search.json needs at least 4 rows for this case"
    orig_ids = [str(row["idNo"]) for row in original_rows[:3]]
    orig_pids = [str(row["pId"]) for row in original_rows[:3]]

    baseline_ctx, _info_b, _db_b = await _session(tmp_path)
    _repeat_fetch(monkeypatch, _raw_from_body(copy.deepcopy(original)))
    baseline = await _search(baseline_ctx)

    modified = copy.deepcopy(original)
    rows = modified["result"]["data"]
    rows[0]["pId"] = ""
    del rows[1]["pId"]
    rows[2]["idNo"] = "12a"
    ctx, _info, _db = await _session(tmp_path)
    _repeat_fetch(monkeypatch, _raw_from_body(modified))
    changed = await _search(ctx)

    assert "error" not in changed
    assert len(changed["results"]) == len(baseline["results"])
    assert changed["results"][3:] == baseline["results"][3:]

    for row in original_rows[3:]:
        await _update(ctx, str(row["pId"]), MESSAGE, "contacted")
        result = await _status(ctx, str(row["pId"]), MESSAGE)
        assert any(item["candidate_id"] == str(row["idNo"]) and item["id_source"] == RESUME
                   for item in _linked_items(result))

    for index, dropped_pid in ((0, orig_pids[0]), (1, orig_pids[1]), (2, orig_pids[2])):
        await _update(ctx, dropped_pid, MESSAGE, "contacted")
        result = await _status(ctx, dropped_pid, MESSAGE)
        assert not any(item["candidate_id"] in (orig_ids[index], "12a") and item["id_source"] == RESUME
                       for item in _linked_items(result)), index


@pytest.mark.asyncio
async def test_message_only_candidate_with_no_pairing_is_not_contacted_through_the_resume_side(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _update(ctx, "1601000000001", RESUME, "contacted")
    await db.upsert_candidate("170001", MESSAGE, ACCOUNT, name="Synthetic")

    assert await _contacted(ctx, "170001", MESSAGE) is False
    result = await _status(ctx, "170001", MESSAGE)
    assert not any(record["candidate_id"] == "1601000000001" for record in result["records"])


@pytest.mark.asyncio
async def test_failed_pair_recording_leaves_the_response_and_logs_without_identifiers(
        tmp_path, monkeypatch, caplog):
    original = load_fixture("rows_search.json")
    identifiers = set()
    for row in original["result"]["data"]:
        identifiers.add(str(row["idNo"]))
        identifiers.add(str(row["pId"]))
    identifiers = {ident for ident in identifiers if len(ident) >= 6}

    healthy_ctx, _info_h, _db_h = await _session(tmp_path)
    _repeat_fetch(monkeypatch, _raw_from_body(copy.deepcopy(original)))
    healthy = await _search(healthy_ctx)

    broken_ctx, _info_b, broken_db = await _session(tmp_path)
    await _execute(broken_db.path, "DROP TABLE candidate_id_links")
    caplog.set_level(logging.ERROR)
    caplog.clear()
    broken = await _search(broken_ctx)

    assert "error" not in broken
    assert broken == healthy
    errors = [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert errors
    for ident in identifiers:
        assert ident not in caplog.text


@pytest.mark.asyncio
async def test_send_writes_scheduled_with_a_time_when_an_interview_invite_is_accepted(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    _scripted_fetch(monkeypatch, _inquiry_script(PID, event_id="evt-syn-0004"))

    result = await _tool("send_interview_invite")(
        job_id=JOBNO, candidate_id=PID, message=LETTER, meeting_time=FUTURE_MEETING,
        location=LOCATION, phase=1, contact_name=CONTACT, contact_phone=CONTACT_PHONE, ctx=ctx,
    )
    assert "error" not in result

    found = _records_for(await _status(ctx, PID, MESSAGE), PID, MESSAGE)
    assert len(found) == 1
    assert found[0]["status"] == "scheduled"
    assert found[0]["status_updated_at"] is not None


@pytest.mark.asyncio
async def test_resume_detail_name_update_keeps_the_status_and_its_time(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    a_id = "1600000000001"
    await _update(ctx, a_id, RESUME, "interested")
    await _set_status_time(db.path, a_id, RESUME, KNOWN_OLD)
    body = copy.deepcopy(load_fixture("resume_unrestricted.json"))
    body["data"]["resume"]["idNo"] = a_id
    _repeat_fetch(monkeypatch, _raw_from_body(body))

    detail = await _tool("get_resume_detail")(candidate_id=a_id, ctx=ctx)

    assert "error" not in detail
    row = await _row(db.path, a_id, RESUME)
    assert row["name"] == body["data"]["resume"]["userName"]
    assert row["status"] == "interested"
    assert row["status_updated_at"] == KNOWN_OLD


@pytest.mark.asyncio
async def test_accepted_inquiry_keeps_an_existing_message_status_and_its_time(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    await _update(ctx, PID, MESSAGE, "interested")
    await _set_status_time(db.path, PID, MESSAGE, KNOWN_OLD)
    _scripted_fetch(monkeypatch, _inquiry_script(PID, event_id="evt-syn-0005"))

    result = await _tool("send_inquiry")(job_id=JOBNO, candidate_id=PID, message=LETTER, ctx=ctx)

    assert "error" not in result
    row = await _row(db.path, PID, MESSAGE)
    assert row["status"] == "interested"
    assert row["status_updated_at"] == KNOWN_OLD


@pytest.mark.asyncio
async def test_accepted_inquiry_and_contact_check_see_each_other_across_the_pair(tmp_path, monkeypatch):
    ctx, _info, _db = await _session(tmp_path)
    _scripted_fetch(monkeypatch, _inquiry_script(PID, event_id="evt-syn-0006"))

    result = await _tool("send_inquiry")(job_id=JOBNO, candidate_id=PID, message=LETTER, ctx=ctx)

    assert "error" not in result
    assert await _contacted(ctx, BRIDGED, RESUME) is True
    assert await _contacted(ctx, PID, MESSAGE) is True


@pytest.mark.asyncio
async def test_status_set_on_one_side_of_a_pair_marks_the_other_side_contacted(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    resume_id, message_id = await _search_fixture_pair(ctx, monkeypatch)
    await _update(ctx, resume_id, RESUME, "interested")

    assert await _contacted(ctx, message_id, MESSAGE) is True


@pytest.mark.asyncio
async def test_two_resume_ids_paired_to_one_message_id_are_one_contact_group(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    a1, a2, p_id = "1701000000001", "1701000000002", "170101"
    await db.record_id_links(ACCOUNT, [(a1, p_id), (a2, p_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await _update(ctx, a2, RESUME, "interested")

    assert await _contacted(ctx, a1, RESUME) is True
    result = await _status(ctx, a1, RESUME)
    expected = await db.find_candidate_records(a1, RESUME, ACCOUNT)
    got = [(r["candidate_id"], r["id_source"], r["matched_by"]) for r in result["records"]]
    want = [(r["candidate_id"], r["id_source"], r["matched_by"]) for r in expected]
    assert got == want
    assert _records_for(result, a2, RESUME)[0]["matched_by"] == "linked"


@pytest.mark.asyncio
async def test_contact_check_is_false_for_a_name_only_pair_and_true_for_a_not_interested_status(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _update(ctx, "1501", RESUME, "not_interested")
    assert await _contacted(ctx, "1501", RESUME) is True

    await db.record_id_links(ACCOUNT, [("1502", "150301")], dbmod.LINK_SOURCE_RESUME_ROW)
    await db.upsert_candidate("1502", RESUME, ACCOUNT, name="Synthetic A")
    await db.upsert_candidate("150301", MESSAGE, ACCOUNT, name="Synthetic P")
    assert await _contacted(ctx, "1502", RESUME) is False
    assert await _contacted(ctx, "150301", MESSAGE) is False


@pytest.mark.asyncio
async def test_contact_check_returns_plain_bool_in_every_case_and_keeps_the_id_source_error(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _update(ctx, "2901", RESUME, "interested")
    await db.upsert_candidate("2902", RESUME, ACCOUNT, name="Synthetic")

    with_status = await _contacted(ctx, "2901", RESUME)
    name_only = await _contacted(ctx, "2902", RESUME)
    no_record = await _contacted(ctx, "2903", RESUME)

    for value, expected in ((with_status, True), (name_only, False), (no_record, False)):
        assert type(value) is bool
        assert value is expected
    assert await _contacted(ctx, "2901", "x") == ID_SOURCE_ERROR


# ═════════════════════════════════════════════════════════════════════════════════
# Unknown-source records (written before the source was recorded)
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_unknown_source_record_counts_for_both_sources_and_reads_as_same_id_unknown_source(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _insert_raw(db.path, "777", UNKNOWN, status="contacted")

    assert await _contacted(ctx, "777", RESUME) is True
    assert await _contacted(ctx, "777", MESSAGE) is True
    result = await _status(ctx, "777", MESSAGE)
    matches = [r for r in result["records"] if r["candidate_id"] == "777" and r["id_source"] == UNKNOWN]
    assert len(matches) == 1
    assert matches[0]["matched_by"] == "same_id_unknown_source"


@pytest.mark.asyncio
async def test_unknown_source_reminder_appears_only_when_such_a_record_is_hit(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _insert_raw(db.path, "777", UNKNOWN, status="contacted")

    with_unknown = await _status(ctx, "777", MESSAGE)
    assert len([w for w in with_unknown["warnings"] if "來源已不可考" in w]) == 1

    ctx2, _info2, _db2 = await _session(tmp_path)
    without_unknown = await _status(ctx2, "777", MESSAGE)
    assert not [w for w in without_unknown["warnings"] if "來源已不可考" in w]


@pytest.mark.asyncio
async def test_update_on_a_resume_id_never_touches_the_unknown_source_row(tmp_path):
    ctx, _info, db = await _session(tmp_path)
    await _insert_raw(db.path, "777", UNKNOWN, status="contacted")

    await _update(ctx, "777", RESUME, "interested")

    unknown = await _row(db.path, "777", UNKNOWN)
    assert unknown["status"] == "contacted"
    assert unknown["id_source"] == UNKNOWN
    written = await _row(db.path, "777", RESUME)
    assert written["status"] == "interested"


@pytest.mark.asyncio
async def test_warnings_stay_silent_when_statuses_agree_or_one_side_has_no_status(tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    a_id, p_id, p2_id = "5601000000001", "560101", "560102"
    await db.record_id_links(ACCOUNT, [(a_id, p_id), (a_id, p2_id)], dbmod.LINK_SOURCE_RESUME_ROW)
    await _update(ctx, a_id, RESUME, "interested")
    await _update(ctx, p_id, MESSAGE, "interested")
    await db.upsert_candidate(p2_id, MESSAGE, ACCOUNT, name="Synthetic")

    result = await _status(ctx, a_id, RESUME)
    assert not [w for w in result["warnings"] if "狀態不同" in w]
    assert not [w for w in result["warnings"] if "來源已不可考" in w]

    await _update(ctx, p_id, MESSAGE, "contacted")
    different = await _status(ctx, a_id, RESUME)
    reminders = [w for w in different["warnings"] if "狀態不同" in w]
    assert len(reminders) == 1
    for word in ("矛盾", "錯誤", "不一致"):
        assert word not in reminders[0]


# ═════════════════════════════════════════════════════════════════════════════════
# update_candidate_status — shape and the status-time / unknown-row rules
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_update_status_success_and_failure_shapes_are_unchanged(tmp_path):
    ctx, _info, _db = await _session(tmp_path)

    assert await _update(ctx, "4301", RESUME, "interested") == {"success": True}
    bad = await _update(ctx, "4301", RESUME, "maybe")
    assert set(bad) == {"success", "error"}
    assert bad["success"] is False
    assert isinstance(bad["error"], str) and bad["error"]


# ═════════════════════════════════════════════════════════════════════════════════
# Concurrency
# ═════════════════════════════════════════════════════════════════════════════════

def _normalized(result, candidate_id):
    return json.dumps(result, ensure_ascii=False, sort_keys=True).replace(candidate_id, "<CID>")


@pytest.mark.asyncio
async def test_send_and_status_update_racing_on_one_message_id_leave_interested_and_one_row(
        tmp_path, monkeypatch):
    ctx, _info, db = await _session(tmp_path)
    baseline_id = "599999"
    racing_ids = [f"5{i:05d}" for i in range(20)]
    script = []
    for candidate_id in [baseline_id] + racing_ids:
        script += _inquiry_script(candidate_id, event_id="evt-syn-0008")
    _scripted_fetch(monkeypatch, script)

    baseline = await _tool("send_inquiry")(job_id=JOBNO, candidate_id=baseline_id, message=LETTER, ctx=ctx)
    assert "error" not in baseline
    expected = _normalized(baseline, baseline_id)

    # The DB-level set_status_if_unset race test (both gather orders) is the real guard against
    # the read-then-write race; this end-to-end run checks the send path only.
    for candidate_id in racing_ids:
        sent, _ = await asyncio.gather(
            _tool("send_inquiry")(job_id=JOBNO, candidate_id=candidate_id, message=LETTER, ctx=ctx),
            _update(ctx, candidate_id, MESSAGE, "interested"),
        )
        assert _normalized(sent, candidate_id) == expected, candidate_id
        row = await _row(db.path, candidate_id, MESSAGE)
        assert row["status"] == "interested", candidate_id
        assert await _count_rows(db.path, candidate_id, MESSAGE) == 1, candidate_id


# ═════════════════════════════════════════════════════════════════════════════════
# Module-level helpers (no tool call)
# ═════════════════════════════════════════════════════════════════════════════════

def test_id_link_pairs_keeps_only_clean_numeric_pairs_in_first_seen_order():
    from mcp104.tools.status import id_link_pairs

    items = [
        {"a": "1", "b": "2"},
        {"a": "1", "b": "2"},
        {"a": "3"},
        "x",
        {"a": "4", "b": None},
        {"a": "5", "b": "6x"},
        {"a": "7", "b": "８"},  # full-width digit: not ASCII, must be skipped
    ]

    assert id_link_pairs(items, id_no_key="a", p_id_key="b") == [("1", "2")]


@pytest.mark.asyncio
async def test_remember_id_links_swallows_storage_errors_and_logs_without_identifiers(caplog):
    from mcp104.tools.status import remember_id_links

    class _FailingDatabase:
        async def record_id_links(self, account_label, pairs, source):
            raise sqlite3.OperationalError("synthetic storage failure")

    class _FailingApp:
        db = _FailingDatabase()

    caplog.set_level(logging.ERROR)
    await remember_id_links(_FailingApp(), ACCOUNT, [("1234509876543", "5550012")],
                            dbmod.LINK_SOURCE_RESUME_ROW)

    assert [record for record in caplog.records if record.levelno >= logging.ERROR]
    assert "1234509876543" not in caplog.text
    assert "5550012" not in caplog.text


@pytest.mark.asyncio
async def test_remember_id_links_does_nothing_without_an_account_label():
    from mcp104.tools.status import remember_id_links

    class _RecordingDatabase:
        def __init__(self):
            self.calls = []

        async def record_id_links(self, *args):
            self.calls.append(args)

    recorder = _RecordingDatabase()

    class _App:
        db = recorder

    await remember_id_links(_App(), None, [("1234509876543", "5550012")], dbmod.LINK_SOURCE_RESUME_ROW)

    assert recorder.calls == []


# ═════════════════════════════════════════════════════════════════════════════════
# Registration surface and tool descriptions
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_registration_adds_the_two_status_reads_once_and_publishes_no_caller_ctx_field():
    from mcp104.main import register_all_tools

    counting = _FakeMCP()
    register_all_tools(counting)
    for name in ("get_candidate_status", "list_tracked_candidates"):
        assert counting.counts.get(name) == 1, f"{name} must be registered exactly once: {counting.counts.get(name)}"

    mcp = FastMCP("status-completion-registration")
    register_all_tools(mcp)
    published = {tool.name: tool for tool in await mcp.list_tools()}

    assert PRE_ROUND_TOOL_NAMES <= set(published), sorted(PRE_ROUND_TOOL_NAMES - set(published))
    for name in ("get_candidate_status", "list_tracked_candidates", "check_already_contacted",
                 "update_candidate_status"):
        assert len(published[name].description or "") <= 1948, name
        schema = published[name].inputSchema
        assert "ctx" not in schema.get("properties", {}), name
        assert "ctx" not in schema.get("required", []), name

    contacted = published["check_already_contacted"].inputSchema
    assert set(contacted["properties"]) == {"candidate_id", "id_source"}
    assert sorted(contacted["required"]) == ["candidate_id", "id_source"]
    updated = published["update_candidate_status"].inputSchema
    assert set(updated["properties"]) == {"candidate_id", "id_source", "status"}
    assert sorted(updated["required"]) == ["candidate_id", "id_source", "status"]
    for schema in (contacted, updated):
        for prop in schema["properties"].values():
            assert prop.get("type") == "string"


def test_get_candidate_status_description_says_the_applicant_binding_is_unmeasured():
    description = _registry().descriptions["get_candidate_status"]

    assert "應徵者" in description
    assert "未另外量測" in description
