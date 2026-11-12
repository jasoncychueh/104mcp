"""Tests for the event-send skeleton (tools/events.py) and the send_inquiry route.

The send_inquiry tests below were moved here verbatim from tests/test_messaging.py
(event-sends task 12): assertions and test names are unchanged; only the tool
acquisition (register_event_tools) and the imports changed. The new cases cite
their design Testing Strategy ID in the docstring.

Same transport seam as tests/test_messaging.py: api_client.fetch and helpers.fetch
are substituted; classify(), guarded_sequence and the SQLite file are real.
"""
from __future__ import annotations

import json
import logging
import re
import sqlite3
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from mcp.server.fastmcp import FastMCP

from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.events import (
    TAIPEI,
    EventRoute,
    check_meeting_time,
    check_message,
    check_phase,
    check_reply_days,
    check_text,
    register_event_tools,
    send_event,
)
from mcp104.tools.helpers import get_session_id
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.status import register_status_tools
from tests.conftest import _SeqFetchSpy, load_fixture




# ── Fixture / fake-transport plumbing (mirrors tests/test_messaging.py) ─────────────

def _raw(status: int, content_type: str, body: str, parsed_json, location: str | None = None) -> RawResponse:
    return RawResponse(status=status, location=location, content_type=content_type,
                        body=body, body_bytes=body.encode("utf-8"), parsed_json=parsed_json)


def _raw_from_body(body: dict, status: int = 200) -> RawResponse:
    return _raw(status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)


def _raw_from_wrapper(fixture_name: str) -> RawResponse:
    d = load_fixture(fixture_name)
    return _raw(d["http_status"], d["content_type"], d["body_text"], d["body_json"])


class _FetchSpy:
    def __init__(self, response: RawResponse):
        self._response = response
        self.calls: list[tuple[object, object, object]] = []

    async def __call__(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append((endpoint, params, body))
        return self._response


class _NeverCalledFetch:
    async def __call__(self, *args, **kwargs):
        raise AssertionError("fetch must not be called for this case")


def _install_fake_fetch(monkeypatch, raw: RawResponse) -> _FetchSpy:
    spy = _FetchSpy(raw)
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


def _install_never_called_fetch(monkeypatch) -> None:
    spy = _NeverCalledFetch()
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)


class FakeSessionObj:
    pass


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeApp:
    def __init__(self, session_pool: SessionPool, database: Database):
        self.session_pool = session_pool
        self.config = get_config()
        self.db = database


class FakeCtx:
    def __init__(self, session_pool: SessionPool, database: Database):
        self.session = FakeSessionObj()
        self.request_context = FakeRequestContext(FakeApp(session_pool, database))


class FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self.tools[name] = fn
            self.descriptions[name] = kwargs.get("description") or fn.__doc__ or ""
            return fn
        return decorator


def _register_tools() -> tuple[dict[str, object], dict[str, str]]:
    mcp = FakeMCP()
    register_messaging_tools(mcp)
    register_status_tools(mcp)
    register_event_tools(mcp)
    return mcp.tools, mcp.descriptions


TOOLS, TOOL_DESCRIPTIONS = _register_tools()
send_message = TOOLS["send_message"]
send_inquiry = TOOLS["send_inquiry"]
get_conversation = TOOLS["get_conversation"]
check_already_contacted = TOOLS["check_already_contacted"]


_OPENED_DATABASES: list[Database] = []


async def _new_session(tmp_path) -> tuple[FakeCtx, SessionInfo, Database]:
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight off SessionInfo.cookies now —
    # there is no BrowserContext left to fake post-login (§C7).
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return ctx, info, database


async def _new_session_with_db_file(tmp_path) -> tuple[FakeCtx, SessionInfo, Database, Path]:
    """Same as _new_session, but also hands back the SQLite file path so a test can
    break one table on disk while the Database object stays open."""
    pool = SessionPool()
    db_path = tmp_path / f"db_{uuid4().hex}.sqlite"
    database = Database(str(db_path))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)
    sid = get_session_id(ctx)
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return ctx, info, database, db_path


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED_DATABASES:
        await _OPENED_DATABASES.pop().close()


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


# ── Synthetic conversation messages — the measured 13 field names [M §6b.7, §6b.8-3,
# §6b.10] — watermark = 9101, so sent id<=9101 is read, sent id>9101 is unread ───────

_MSG_SENT_READ = {
    "id": "9100", "userName": "操作者", "type": 0, "snapshotId": "snap-1",
    "idNo": "0798066676650", "content": "您好，方便聊聊嗎", "unsend": False,
    "ogMeta": None, "source": 0, "createdAt": "2026-08-10 09:00:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_MSG_RECEIVED = {
    "id": "9102", "userName": "測試甲", "type": 0, "snapshotId": "snap-1",
    "idNo": "0798066676650", "content": "有的", "unsend": False,
    "ogMeta": None, "source": 1, "createdAt": "2026-08-10 09:02:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_MSG_TYPE6_INVISIBLE = {
    "id": "9103", "userName": "操作者", "type": 6, "snapshotId": "snap-1",
    "idNo": "0798066676650", "content": "（系統事件，網頁版看不到）", "unsend": False,
    "ogMeta": None, "source": 0, "createdAt": "2026-08-10 09:03:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_MSG_WITH_EVENT = {
    "id": "9104", "userName": "操作者", "type": 1, "snapshotId": "snap-1",
    "idNo": "0798066676650", "content": "已邀請面試", "unsend": False,
    "ogMeta": None, "source": 0, "createdAt": "2026-08-10 09:04:00",
    "event": {
        "eventId": "evt-1", "eventType": 1, "status": 1,
        "content": "合成信件內容（測試用，非真實信件）", "replyDate": "2026-08-12", "replyDay": None,
        "contactInfo": {
            "meetingTime": "2026-08-15 14:00", "meetingTimeOptions": [],
            "location": "台北市信義區某大樓", "contact": "王小明", "contactTel": "0912-345-678",
        },
        "file": [],
    },
    "file": [], "isSynchronized": True,
}
_MSG_SENT_UNREAD = {
    "id": "9105", "userName": "操作者", "type": 0, "snapshotId": "snap-1",
    "idNo": "0798066676650", "content": "有空嗎？", "unsend": False,
    "ogMeta": None, "source": 0, "createdAt": "2026-08-10 09:05:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_CONVERSATION_MESSAGES = [
    _MSG_SENT_READ, _MSG_RECEIVED, _MSG_TYPE6_INVISIBLE, _MSG_WITH_EVENT, _MSG_SENT_UNREAD,
]
_CONVERSATION_ENVELOPE_SINGLE_PAGE = {
    "data": _CONVERSATION_MESSAGES,
    "metadata": {"perPage": 100, "page": 1, "totalPage": 1, "total": 5, "creadAt": 9101},
}


# ═════════════════════════════════════════════════════════════════════════════════
# outbound-contact — T-1..T-16, T-18..T-54
#
# Success envelope, synthetic values only (Test Approach): a candidate never
# named search_resumes/get_resume_detail/read_messages, a letter body that is
# obviously not real, an emailCC that is obviously not a real recruiter
# address. The five-shape response table and the send_inquiry request
# sequence are exercised end to end through the real classify()/guarded_api/
# guarded_sequence — never by hand-building a verdict or a GuardAbort.
# ═════════════════════════════════════════════════════════════════════════════════

_SYNTHETIC_LETTER = "測試信件本文-合成"
_SYNTHETIC_CC = "cc-a@example.invalid"
# Synthetic ids: the same length as the measured 13-14 digit idNo and 6-8 digit pId
# shapes (docs/104-site-facts.md §6b.12/§8.12, §8.17), but they start with 0, which no
# real id does, so they cannot be a real person's id. The idNo-shaped one must be
# rejected by the >=12-digit guard on all three messaging tools; the pId-shaped one must
# PASS the same guard.
_IDNO_SHAPED_ID = "0798066676650"
_PID_SHAPED_ID = "073748"


def _outbound_success_envelope(event_id: str = "") -> dict:
    """§Testing Strategy's "成功信封" — identical on both send_message and
    send_willingness_event; only eventId differs by route (send_message
    measures "" [T-4], send_inquiry measures non-empty [T-36])."""
    return {
        "data": [{
            "pId": "073748",
            "idNo": "30000006675849",
            "streamId": "073748_12355016",
            "messageId": ["2095355457"],
            "eventId": event_id,
            "isSynchronized": True,
        }],
        "failed": [],
        "metadata": {},
    }


def _install_seq_fetch(monkeypatch, scripted) -> _SeqFetchSpy:
    spy = _SeqFetchSpy(scripted)
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


_RESOLVE_IDNO_OK = _raw_from_body({"data": {"idNo": "30000006675849"}, "metadata": {}})
_LAST_INFO_EMPTY_CC = _raw_from_body({"data": {"emailCC": []}, "metadata": {}})


def _inquiry_script(event_id: str = "evt-syn-0001", emailcc=None) -> list:
    """A full, successful 3-response send_inquiry script: resolve -> last-info
    -> success envelope. `emailcc` defaults to [] (the measured "no CC"
    shape, still a present key)."""
    return [
        _RESOLVE_IDNO_OK,
        _raw_from_body({"data": {"emailCC": emailcc if emailcc is not None else []}, "metadata": {}}),
        _raw_from_body(_outbound_success_envelope(event_id=event_id)),
    ]



# ── send_inquiry — T-12..T-41 ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T12_send_inquiry_issues_the_three_subrequests_in_fixed_order(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    keys = [ep.key for ep, _params, _body in spy.calls]
    assert keys == ["resolve_candidate_idno", "event_last_info", "send_willingness_event"]


@pytest.mark.asyncio
async def test_T13_send_inquiry_never_calls_any_template_endpoint(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    keys = [ep.key for ep, _params, _body in spy.calls]
    assert not any("template" in k for k in keys)


@pytest.mark.asyncio
async def test_T14_send_inquiry_counts_all_three_subrequests_in_the_rolling_window(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    before = len(info.throttle.request_timestamps)
    _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    after = len(info.throttle.request_timestamps)
    assert after - before == 3


@pytest.mark.asyncio
async def test_T15_send_inquiry_sleeps_at_most_once_for_the_whole_sequence(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    sleep_calls: list[float] = []

    async def counting_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", counting_sleep)
    _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(sleep_calls) <= 1


@pytest.mark.asyncio
async def test_T16_send_inquiry_blocked_by_the_gate_before_any_subrequest_with_two_slots_left(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    cap = app.config.max_requests_per_hour
    for _ in range(cap - 2):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 0
    assert "error" in result


@pytest.mark.asyncio
async def test_T18_send_inquiry_403_after_prior_success_uses_after_success_wording(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    blocked = _raw(403, "text/html; charset=utf-8", "blocked", None)
    spy = _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, blocked])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 2
    assert "先前已成功過" in result["error"]


@pytest.mark.asyncio
async def test_T19_send_inquiry_message_over_1000_chars_rejected_before_any_request(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())
    long_message = "字" * 1001

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=long_message)

    assert result["success"] is False
    assert "error" in result
    assert len(spy.calls) == 0
    assert await db.get_daily_sent_count(info.account_label) == 0
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row is None


@pytest.mark.asyncio
async def test_T20_send_inquiry_message_exactly_1000_chars_is_accepted(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())
    message = "字" * 1000

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=message)

    assert len(spy.calls) == 3
    assert result["sent"] == "confirmed"


@pytest.mark.asyncio
async def test_T21_send_inquiry_resolve_404_aborts_with_its_own_payload(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    not_found = _raw_from_body({"code": "00004", "message": "找不到對應資源", "detail": []}, status=404)
    spy = _install_seq_fetch(monkeypatch, [not_found])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 1
    assert "error" in result
    assert "找不到對應資源" in result["error"]
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_T22_send_inquiry_last_info_failure_aborts_before_third_request(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    server_error = _raw_from_body({"error": "internal"}, status=500)
    spy = _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, server_error])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 2
    assert "error" in result
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_T23_send_inquiry_resolve_missing_idno_is_malformed_before_last_info(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    missing_idno = _raw_from_body({"data": {}, "metadata": {}})
    spy = _install_seq_fetch(monkeypatch, [missing_idno])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 1
    assert "error" in result
    assert "sent" not in result


@pytest.mark.asyncio
async def test_send_inquiry_exception_after_send_attempted_never_logs_body_or_cookie(tmp_path, monkeypatch, caplog):
    """T-81 (R7.3) — send tool catch-all: a non-GuardAbort exception escaping
    AFTER send_attempted is set (the third sub-request -- the send itself --
    raises once _before_third's last statement has already run) is caught by
    send_inquiry's own except-Exception block and reaches _conclude_send's
    log.error(..., exc_info=True). That log call must never leak the letter
    body, the emailCC value, the resolved idNo, or a cookie value."""
    ctx, info, db = await _new_session(tmp_path)
    info.cookies = [{"name": "its", "value": "SYN-COOKIE-VALUE-4d2e", "domain": ".104.com.tw"}]
    _install_seq_fetch(monkeypatch, [
        _RESOLVE_IDNO_OK,
        _raw_from_body({"data": {"emailCC": ["cc-a@example.invalid"]}, "metadata": {}}),
        RuntimeError("boom"),
    ])

    with caplog.at_level(logging.DEBUG):
        result = await send_inquiry(
            ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID,
            message="SYN-LETTER-body-9f1c",
        )

    assert result["sent"] == "unconfirmed"

    log_text = caplog.text
    for forbidden in (
        "SYN-LETTER-body-9f1c", "cc-a@example.invalid", "SYN-COOKIE-VALUE-4d2e",
        _RESOLVE_IDNO_OK.parsed_json["data"]["idNo"],
    ):
        assert forbidden not in log_text, f"{forbidden!r} leaked into logs"


@pytest.mark.asyncio
async def test_T24_send_inquiry_transport_exception_on_first_subrequest_is_not_unconfirmed(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, [RuntimeError("simulated transport failure")])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert result.get("sent") != "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 0
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row is None


@pytest.mark.asyncio
async def test_T25_send_inquiry_transport_exception_on_second_subrequest_is_not_unconfirmed(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, RuntimeError("simulated transport failure")])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 2
    assert result.get("sent") != "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_T26_send_inquiry_third_request_body_matches_the_measured_key_set(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    script = [
        _raw_from_body({"data": {"idNo": "30000006675849"}, "metadata": {}}),
        _raw_from_body({"data": {"emailCC": [_SYNTHETIC_CC]}, "metadata": {}}),
        _raw_from_body(_outbound_success_envelope()),
    ]
    spy = _install_seq_fetch(monkeypatch, script)

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 3
    _endpoint, _params, body = spy.calls[2]
    expected_keys = {
        "candidate", "contactJobNo", "content", "templateId",
        "isRequiredReplyDay", "replyDay", "contact", "contactTel",
        "isWithDetail", "file", "ec", "rc", "emailCC",
    }
    assert set(body.keys()) == expected_keys
    assert body["candidate"] == [{"idNo": "30000006675849"}]
    assert body["contactJobNo"] == "12355016"
    assert body["content"] == _SYNTHETIC_LETTER
    assert body["isWithDetail"] is True
    assert body["emailCC"] == [_SYNTHETIC_CC]


@pytest.mark.asyncio
async def test_T27_send_inquiry_empty_emailcc_list_is_sent_as_is(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script(emailcc=[]))

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    _endpoint, _params, body = spy.calls[2]
    assert body["emailCC"] == []


@pytest.mark.asyncio
async def test_T28_send_inquiry_emailcc_missing_or_wrong_type_aborts_before_third_request(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    missing_cc = _raw_from_body({"data": {}, "metadata": {}})
    spy = _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, missing_cc])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 2
    assert "error" in result
    assert await db.get_daily_sent_count(info.account_label) == 0

    ctx2, info2, db2 = await _new_session(tmp_path)
    wrong_type_cc = _raw_from_body({"data": {"emailCC": "not-a-list"}, "metadata": {}})
    spy2 = _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, wrong_type_cc])

    result2 = await send_inquiry(ctx=ctx2, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy2.calls) == 2
    assert "error" in result2
    assert await db2.get_daily_sent_count(info2.account_label) == 0


@pytest.mark.asyncio
async def test_T29_send_inquiry_template_id_passed_through_verbatim(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER,
                        template_id="966243507479143185")

    _endpoint, _params, body = spy.calls[2]
    assert body["templateId"] == "966243507479143185"


@pytest.mark.asyncio
async def test_T30_send_inquiry_omitted_template_id_sends_present_empty_string(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    _endpoint, _params, body = spy.calls[2]
    assert "templateId" in body
    assert body["templateId"] == ""


@pytest.mark.asyncio
async def test_T31_warnings_key_is_unconditional_across_all_three_sent_shapes(tmp_path, monkeypatch):
    variants = {
        "confirmed": _outbound_success_envelope(),
        "failed_nonempty": {"data": [], "failed": [{"x": 1}], "metadata": {}},
        "failed_absent": {"data": [], "metadata": {}},
    }
    for label, third_body in variants.items():
        ctx, _info, _db = await _new_session(tmp_path)
        _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, _LAST_INFO_EMPTY_CC, _raw_from_body(third_body)])

        result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

        assert "warnings" in result, label
        assert result["warnings"] == [], label


@pytest.mark.asyncio
async def test_T32_no_sent_shape_ever_exposes_quota_or_operator_email(tmp_path, monkeypatch):
    variants = {
        "confirmed": _outbound_success_envelope(),
        "failed_nonempty": {"data": [], "failed": [{"x": 1}], "metadata": {}},
        "failed_absent": {"data": [], "metadata": {}},
    }
    for label, third_body in variants.items():
        ctx, _info, _db = await _new_session(tmp_path)
        script = [
            _RESOLVE_IDNO_OK,
            _raw_from_body({"data": {"emailCC": []}, "metadata": {"quota": 299, "userEmail": "op@example.invalid"}}),
            _raw_from_body(third_body),
        ]
        _install_seq_fetch(monkeypatch, script)

        result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

        text = json.dumps(result, ensure_ascii=False)
        assert "quota" not in text, label
        assert "299" not in text, label
        assert "op@example.invalid" not in text, label


@pytest.mark.asyncio
async def test_T33_send_inquiry_daily_cap_reached_returns_plain_error_shape_without_warnings(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    for _ in range(app.config.max_daily_messages):
        await db.log_sent(info.account_label, "prior-candidate", "message")
    _install_seq_fetch(monkeypatch, _inquiry_script())

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert set(result.keys()) == {"success", "error"}
    assert result["success"] is False


@pytest.mark.asyncio
async def test_T34_send_inquiry_rechecks_daily_cap_before_third_subrequest(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    cap = app.config.max_daily_messages
    real_count_fn = db.get_daily_sent_count
    call_count = {"n": 0}

    async def flaky_count(account_label):
        call_count["n"] += 1
        return cap - 1 if call_count["n"] == 1 else cap

    monkeypatch.setattr(db, "get_daily_sent_count", flaky_count)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 2
    assert result.get("success") is False
    assert await real_count_fn(info.account_label) == 0


@pytest.mark.asyncio
async def test_T35_send_inquiry_never_leaks_bridge_or_last_info_extra_fields(tmp_path, monkeypatch, caplog):
    ctx, _info, _db = await _new_session(tmp_path)
    resolve_body = {"data": {"idNo": "30000006675849", "userName": "SYNTHETIC-NAME"},
                     "metadata": {"hid": "HID-XYZ"}}
    last_info_body = {"data": {"emailCC": [], "recruiters": [{"id": 1, "email": "rec@example.invalid"}]},
                       "metadata": {"userEmail": "op@example.invalid", "quota": 299}}
    script = [
        _raw_from_body(resolve_body),
        _raw_from_body(last_info_body),
        _raw_from_body(_outbound_success_envelope()),
    ]
    _install_seq_fetch(monkeypatch, script)

    with caplog.at_level("INFO"):
        result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    text = json.dumps(result, ensure_ascii=False)
    for banned in ("SYNTHETIC-NAME", "HID-XYZ", "rec@example.invalid", "op@example.invalid", "299"):
        assert banned not in text
    log_text = caplog.text
    for banned in ("SYNTHETIC-NAME", "HID-XYZ", "rec@example.invalid", "op@example.invalid"):
        assert banned not in log_text


@pytest.mark.asyncio
async def test_T36_send_inquiry_confirmed_with_nonempty_event_id(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _inquiry_script(event_id="evt-syn-0007"))

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert result["sent"] == "confirmed"
    assert result["message_id"] == "2095355457"
    assert result["event_id"] == "evt-syn-0007"


@pytest.mark.asyncio
async def test_T37_send_inquiry_failed_nonempty_is_unconfirmed_not_error_and_logs(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    third = {"data": [], "failed": [{"x": 1}], "metadata": {}}
    _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, _LAST_INFO_EMPTY_CC, _raw_from_body(third)])

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == "contacted"


@pytest.mark.asyncio
async def test_T38_send_inquiry_daily_cap_reached_sends_zero_requests(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    for _ in range(app.config.max_daily_messages):
        await db.log_sent(info.account_label, "prior-candidate", "message")
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert len(spy.calls) == 0


@pytest.mark.asyncio
async def test_T39_emailcc_value_itself_never_leaks_into_response(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _inquiry_script(emailcc=[_SYNTHETIC_CC]))

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    text = json.dumps(result, ensure_ascii=False)
    assert _SYNTHETIC_CC not in text


@pytest.mark.asyncio
async def test_T40_send_inquiry_validation_on_third_subrequest_is_error_not_unconfirmed(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    script = [_RESOLVE_IDNO_OK, _LAST_INFO_EMPTY_CC, _raw_from_wrapper("failure_send_validation.json")]
    _install_seq_fetch(monkeypatch, script)

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert "error" in result
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_T41_five_shapes_are_mutually_exclusive_across_both_send_tools(tmp_path, monkeypatch):
    variants = [
        ("data_empty_no_failed", {"data": [], "metadata": {}}, False),
        ("data_nonempty_no_failed", {"data": [{
            "pId": "073748", "idNo": "X", "streamId": "S",
            "messageId": ["1"], "eventId": "", "isSynchronized": True,
        }], "metadata": {}}, False),
        ("failed_nonempty", {"data": [], "failed": [{"x": 1}], "metadata": {}}, True),
    ]
    for label, body, failed_present in variants:
        ctx, _info, _db = await _new_session(tmp_path)
        _install_fake_fetch(monkeypatch, _raw_from_body(body))
        result = await send_message(ctx=ctx, job_id="12355016", candidate_id="0225677", message=_SYNTHETIC_LETTER)
        assert result["sent"] == "unconfirmed", (label, "send_message")
        assert ("failed" in result) is failed_present, (label, "send_message")

        ctx2, _info2, _db2 = await _new_session(tmp_path)
        _install_seq_fetch(monkeypatch, [_RESOLVE_IDNO_OK, _LAST_INFO_EMPTY_CC, _raw_from_body(body)])
        result2 = await send_inquiry(ctx=ctx2, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
        assert result2["sent"] == "unconfirmed", (label, "send_inquiry")
        assert ("failed" in result2) is failed_present, (label, "send_inquiry")

@pytest.mark.asyncio
async def test_T43_send_inquiry_rejects_13_digit_candidate_id(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script())

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_IDNO_SHAPED_ID, message=_SYNTHETIC_LETTER)

    assert result["success"] is False
    assert "error" in result
    assert len(spy.calls) == 0
    assert await db.get_daily_sent_count(info.account_label) == 0

@pytest.mark.asyncio
async def test_T45_all_three_message_tools_accept_a_6_digit_candidate_id(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy1 = _install_fake_fetch(monkeypatch, _raw_from_body(_outbound_success_envelope()))
    result1 = await send_message(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
    assert len(spy1.calls) == 1
    assert result1.get("success") is not False

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install_seq_fetch(monkeypatch, _inquiry_script())
    result2 = await send_inquiry(ctx=ctx2, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
    assert len(spy2.calls) == 3
    assert result2.get("success") is not False

    ctx3, _info3, _db3 = await _new_session(tmp_path)
    spy3 = _install_fake_fetch(monkeypatch, _raw_from_body(_CONVERSATION_ENVELOPE_SINGLE_PAGE))
    result3 = await get_conversation(ctx=ctx3, job_id="12355016", candidate_id=_PID_SHAPED_ID)
    assert len(spy3.calls) == 1
    assert "error" not in result3

@pytest.mark.asyncio
async def test_T46_empty_or_whitespace_message_rejected_before_any_request_both_send_tools(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    for message in ("", "   "):
        _install_never_called_fetch(monkeypatch)
        result = await send_message(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=message)
        assert result["success"] is False

        result2 = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=message)
        assert result2["success"] is False

@pytest.mark.asyncio
async def test_T47_daily_cap_is_shared_across_both_send_tools(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    cap = app.config.max_daily_messages
    for _ in range(cap - 1):
        await db.log_sent(info.account_label, "prior-candidate", "message")

    _install_seq_fetch(monkeypatch, _inquiry_script())
    result1 = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
    assert result1.get("sent") == "confirmed"
    assert await db.get_daily_sent_count(info.account_label) == cap

    _install_never_called_fetch(monkeypatch)
    result2 = await send_message(ctx=ctx, job_id="12355016", candidate_id="0225677", message=_SYNTHETIC_LETTER)
    assert result2.get("success") is False
    assert "上限" in result2["error"]

@pytest.mark.asyncio
async def test_T49_sent_log_rows_read_back_with_message_id_source_for_both_tools(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_outbound_success_envelope()))
    await send_message(ctx=ctx, job_id="12355016", candidate_id="012975", message=_SYNTHETIC_LETTER)
    assert await check_already_contacted(candidate_id="012975", id_source="message", ctx=ctx) is True

    ctx2, info2, db2 = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _inquiry_script())
    await send_inquiry(ctx=ctx2, job_id="12355016", candidate_id="085242", message=_SYNTHETIC_LETTER)
    assert await check_already_contacted(candidate_id="085242", id_source="message", ctx=ctx2) is True

    async with db._conn.execute("SELECT id_source FROM sent_log WHERE candidate_id = ?", ("012975",)) as cursor:
        rows = await cursor.fetchall()
    assert [r[0] for r in rows] == ["message"]

    async with db2._conn.execute("SELECT id_source FROM sent_log WHERE candidate_id = ?", ("085242",)) as cursor:
        rows2 = await cursor.fetchall()
    assert [r[0] for r in rows2] == ["message"]


# ═════════════════════════════════════════════════════════════════════════════════
# Event-send skeleton — new cases. Every route here is a test-only EventRoute
# declaration (send_event is exercised through it, never through a real route's
# body), except the send_inquiry cases, which go through the real tool.
# ═════════════════════════════════════════════════════════════════════════════════

_BRIDGE_IDNO = "30000000000001"
_CC = "cc-a@example.invalid"
_RECRUITER_RAW = [{"id": "r1", "email": "r1@example.invalid"}]


def _full_success_envelope(*, stream_id: str = "073748_12355016") -> dict:
    """design Testing Strategy「完整成功形狀」."""
    return {
        "data": [{
            "pId": "073748",
            "idNo": _BRIDGE_IDNO,
            "streamId": stream_id,
            "messageId": ["1"],
            "eventId": "e1",
            "isSynchronized": True,
        }],
        "failed": [],
        "metadata": {},
    }


def _standard_script(*, last_info_data: dict | None = None, third=None) -> list:
    """design Testing Strategy「標準腳本」: bridge -> last-info -> send body."""
    info = {"emailCC": [_CC]} if last_info_data is None else last_info_data
    return [
        _raw_from_body({"data": {"idNo": _BRIDGE_IDNO}, "metadata": {}}),
        _raw_from_body({"data": info, "metadata": {}}),
        third if third is not None else _raw_from_body(_full_success_envelope()),
    ]


def _route(*, measured: bool = False, status_write: str = "contacted_if_unset",
           caveats: frozenset = frozenset(), endpoint_key: str = "send_willingness_event") -> EventRoute:
    return EventRoute(
        tool_name="test_route",
        endpoint_key=endpoint_key,
        success_shape_measured=measured,
        status_write=status_write,
        accepted_with_caveat_codes=caveats,
    )


def _fixed_body(**_kwargs) -> dict:
    return {"SYNTHETIC-KEY": "SYNTHETIC-VALUE", "nested": [1, 2]}


# ── send_event: body hand-off, last-info projection — T-48 (interface) ──────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("recruiters_raw, expected_recruiters", [
    (_RECRUITER_RAW, _RECRUITER_RAW),
    (None, None),
], ids=["recruiters_present", "recruiters_absent"])
async def test_send_event_posts_exactly_the_body_the_builder_returns(
    tmp_path, monkeypatch, recruiters_raw, expected_recruiters,
):
    """design T-48: the POST body is build_body's return value; build_body receives
    id_no from the bridge and last_info carrying email_cc and the raw recruiters (or None)."""
    ctx, _info, _db = await _new_session(tmp_path)
    last_info = {"emailCC": [_CC]}
    if recruiters_raw is not None:
        last_info["recruiters"] = recruiters_raw
    spy = _install_seq_fetch(monkeypatch, _standard_script(last_info_data=last_info))
    received: list[dict] = []

    def build_body(**kwargs):
        received.append(kwargs)
        return _fixed_body()

    await send_event(ctx, _route(measured=True), job_id="12355016",
                     candidate_id=_PID_SHAPED_ID, build_body=build_body)

    assert len(spy.calls) == 3
    _endpoint, _params, body = spy.calls[2]
    assert body == _fixed_body()
    assert len(received) == 1
    assert set(received[0]) == {"id_no", "last_info"}
    assert received[0]["id_no"] == _BRIDGE_IDNO
    assert received[0]["last_info"].email_cc == [_CC]
    assert received[0]["last_info"].recruiters == expected_recruiters


# ── send_event: daily cap re-checked before the body request — T-49 (interface) ─────

@pytest.mark.asyncio
async def test_send_event_rechecks_the_daily_cap_before_the_body_request(tmp_path, monkeypatch):
    """design T-49: cap not full at the entry hook, full by the body hook — rejected
    with two requests issued and no sent_log row written."""
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    cap = app.config.max_daily_messages
    for _ in range(cap - 1):
        await db.log_sent(info.account_label, "prior-candidate", "message")
    inner = _SeqFetchSpy(_standard_script())

    async def fetch_filling_the_cap_during_last_info(endpoint, *, cookie_header, params=None, body=None):
        if len(inner.calls) == 1:  # the last-info request: another send fills the last slot
            await db.log_sent(info.account_label, "prior-candidate", "message")
        return await inner(endpoint, cookie_header=cookie_header, params=params, body=body)

    monkeypatch.setattr("mcp104.browser.api_client.fetch", fetch_filling_the_cap_during_last_info)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", fetch_filling_the_cap_during_last_info, raising=False)

    result = await send_event(ctx, _route(measured=True), job_id="12355016",
                              candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert result["success"] is False
    assert len(inner.calls) == 2
    assert await db.get_daily_sent_count(info.account_label) == cap


# ── send_inquiry: full success, request sequence, body and return — T-18 (behavior) ──

@pytest.mark.asyncio
async def test_send_inquiry_full_success_request_sequence_body_and_return(tmp_path, monkeypatch):
    """design T-18: send_inquiry on the standard script with the full success shape —
    request order, the body's measured keys and values it owns, the confirmed return,
    one sent_log row, and status contacted."""
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _inquiry_script(event_id="evt-syn-0001", emailcc=[_CC]))

    result = await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID,
                                message=_SYNTHETIC_LETTER)

    assert [ep.key for ep, _params, _body in spy.calls] == [
        "resolve_candidate_idno", "event_last_info", "send_willingness_event",
    ]
    _endpoint, _params, body = spy.calls[2]
    assert set(body.keys()) == {
        "candidate", "contactJobNo", "content", "templateId",
        "isRequiredReplyDay", "replyDay", "contact", "contactTel",
        "isWithDetail", "file", "ec", "rc", "emailCC",
    }
    assert body["candidate"] == [{"idNo": "30000006675849"}]
    assert body["contactJobNo"] == "12355016"
    assert body["content"] == _SYNTHETIC_LETTER
    assert body["templateId"] == ""
    assert body["ec"] == ""
    assert body["rc"] == "13011211"
    assert body["isWithDetail"] is True
    assert body["emailCC"] == [_CC]

    assert result["sent"] == "confirmed"
    assert result["message_id"] == "2095355457"
    assert result["stream_id"] == "073748_12355016"
    assert result["p_id"] == "073748"
    assert result["event_id"] == "evt-syn-0001"
    assert result["warnings"] == []
    assert await db.get_daily_sent_count(info.account_label) == 1
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == "contacted"


# ── send_inquiry: last-info recruiters never change the body — T-51 (behavior) ──────

@pytest.mark.asyncio
async def test_send_inquiry_body_is_identical_whether_or_not_last_info_carries_recruiters(tmp_path, monkeypatch):
    """design T-51: same job_id / message / template_id; last-info with and without
    recruiters yields the same send body, and recruiters never appear in it."""
    bodies: list[dict] = []
    for last_info in ({"emailCC": [_CC]}, {"emailCC": [_CC], "recruiters": _RECRUITER_RAW}):
        ctx, _info, _db = await _new_session(tmp_path)
        spy = _install_seq_fetch(monkeypatch, [
            _raw_from_body({"data": {"idNo": "30000006675849"}, "metadata": {}}),
            _raw_from_body({"data": last_info, "metadata": {}}),
            _raw_from_body(_outbound_success_envelope()),
        ])
        await send_inquiry(ctx=ctx, job_id="12355016", candidate_id=_PID_SHAPED_ID,
                           message=_SYNTHETIC_LETTER, template_id="SYNTHETIC-TEMPLATE-ID")
        assert len(spy.calls) == 3
        bodies.append(spy.calls[2][2])

    assert bodies[0] == bodies[1]
    assert "recruiters" not in bodies[0]
    assert bodies[0]["templateId"] == "SYNTHETIC-TEMPLATE-ID"
    assert bodies[0]["content"] == _SYNTHETIC_LETTER
    assert bodies[0]["contactJobNo"] == "12355016"
    assert bodies[0]["emailCC"] == [_CC]


# ── Pre-send validators — T-52 (interface) ──────────────────────────────────────────

def test_check_message_accepts_text_up_to_1000_and_rejects_blank_or_longer():
    assert check_message("SYNTHETIC-LETTER") is None
    assert check_message("字" * 1000) is None
    for bad in ("", "   ", "字" * 1001):
        msg = check_message(bad)
        assert isinstance(msg, str) and msg


def test_check_reply_days_accepts_none_and_the_six_values_only():
    assert check_reply_days(None) is None
    for ok in (1, 2, 3, 5, 7, 14):
        assert check_reply_days(ok) is None
    for bad in (0, 4, 6, 15, -1):
        msg = check_reply_days(bad)
        assert isinstance(msg, str) and msg
        assert "reply_days" in msg


def test_check_phase_accepts_one_to_three_only():
    for ok in (1, 2, 3):
        assert check_phase(ok) is None
    for bad in (0, 4):
        msg = check_phase(bad)
        assert isinstance(msg, str) and msg
        assert "phase" in msg


def test_check_text_strips_then_checks_non_empty_and_length_and_names_the_parameter():
    assert check_text("SYNTHETIC-PLACE", name="location", max_len=100) is None
    assert check_text("a" * 100, name="location", max_len=100) is None
    assert check_text("  " + "a" * 100 + "  ", name="location", max_len=100) is None
    for bad in ("", "   ", "a" * 101, "  " + "a" * 101 + "  "):
        msg = check_text(bad, name="location", max_len=100)
        assert isinstance(msg, str) and msg
        assert "location" in msg


# ── check_meeting_time — T-53 (interface) ───────────────────────────────────────────

@pytest.mark.parametrize("value, expected_ok", [
    ("2026-11-12 10:00", True),    # equal to now is accepted
    ("2026-11-12 10:01", True),
    ("2026-11-12 11:30", True),
    ("2026-11-12 09:59", False),   # one minute before now
    ("2026-11-11 23:59", False),
    ("2026-11-12 9:00", False),    # strict format: two-digit hour
    ("2026/11/12 10:00", False),
    ("2026-11-12T10:00", False),
    ("2026-11-12 10:00 ", False),  # strict format: no trailing characters
    ("2026-02-30 10:00", False),   # not an existing date
    ("2026-11-12 24:00", False),   # not an existing time
    ("", False),
])
def test_check_meeting_time_format_existence_and_not_before_now(value, expected_ok):
    now = datetime(2026, 11, 12, 10, 0, tzinfo=TAIPEI)
    msg = check_meeting_time(value, now=now, name="meeting_time")
    if expected_ok:
        assert msg is None
    else:
        assert isinstance(msg, str) and msg
        assert "meeting_time" in msg


def test_check_meeting_time_reads_the_value_as_taipei_local_time_whatever_the_zone_of_now():
    now_utc = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)  # the same instant as Taipei 10:00
    assert check_meeting_time("2026-11-12 10:00", now=now_utc, name="meeting_time") is None
    msg = check_meeting_time("2026-11-12 09:59", now=now_utc, name="meeting_time")
    assert isinstance(msg, str) and msg


# ── send_event status write rules — T-55 (interface) ────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("status_write, prior, expected", [
    ("scheduled", "interested", "scheduled"),
    ("scheduled", None, "scheduled"),
    ("contacted_if_unset", "interested", "interested"),
    ("contacted_if_unset", None, "contacted"),
], ids=["scheduled-overwrites", "scheduled-fills-blank", "if-unset-keeps-existing", "if-unset-fills-blank"])
async def test_status_write_scheduled_overwrites_while_contacted_if_unset_only_fills_a_blank(
    tmp_path, monkeypatch, status_write, prior, expected,
):
    """design T-55: 'scheduled' overwrites any prior status; 'contacted_if_unset' writes
    only into a blank candidate."""
    ctx, info, db = await _new_session(tmp_path)
    if prior is not None:
        await db.upsert_candidate(_PID_SHAPED_ID, "message", info.account_label, status=prior)
    _install_seq_fetch(monkeypatch, _standard_script())

    result = await send_event(ctx, _route(measured=True, status_write=status_write),
                              job_id="12355016", candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert result["sent"] == "confirmed"
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == expected


@pytest.mark.asyncio
async def test_status_write_database_failure_is_swallowed_and_the_verdict_matches_a_healthy_run(
    tmp_path, monkeypatch,
):
    """design T-55 (database error): the candidates table is broken on disk just before
    the status write (sent_log still works). The write error is contained: the call
    returns what a healthy run returns and does not raise."""
    ctx_ok, _info_ok, _db_ok = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())
    healthy = await send_event(ctx_ok, _route(measured=True, status_write="scheduled"),
                               job_id="12355016", candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    ctx, info, db, db_path = await _new_session_with_db_file(tmp_path)
    inner = _SeqFetchSpy(_standard_script())

    async def fetch_then_break_candidates(endpoint, *, cookie_header, params=None, body=None):
        if len(inner.calls) == 2:  # the send-body request; the status write follows it
            con = sqlite3.connect(db_path)
            try:
                con.execute("DROP TABLE candidates")
                con.commit()
            finally:
                con.close()
        return await inner(endpoint, cookie_header=cookie_header, params=params, body=body)

    monkeypatch.setattr("mcp104.browser.api_client.fetch", fetch_then_break_candidates)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", fetch_then_break_candidates, raising=False)

    failing = await send_event(ctx, _route(measured=True, status_write="scheduled"),
                               job_id="12355016", candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert failing == healthy
    assert await db.get_daily_sent_count(info.account_label) == 1


# ── send_event throttle rejection — T-65 (interface) ────────────────────────────────

@pytest.mark.asyncio
async def test_throttle_rejection_returns_the_guard_payload_with_retry_after_and_writes_nothing(tmp_path, monkeypatch):
    """design T-65: the rolling window has room for only two of the three requests;
    the guard's payload comes back with retry_after_seconds, no request is sent, and
    neither sent_log nor candidates is written."""
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    for _ in range(app.config.max_requests_per_hour - 2):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await send_event(ctx, _route(measured=True, status_write="scheduled"),
                              job_id="12355016", candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert len(spy.calls) == 0
    assert "error" in result
    assert "retry_after_seconds" in result
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


# ── send_event guard aborts at each step — T-66 (interface) ─────────────────────────

_GUARD_FAILURE_RESPONSES = {
    # A 302 to an auth host is how an expired session reaches guarded_api.
    "expired": lambda: RawResponse(
        status=302, location="https://bsignin.104.com.tw/login",
        content_type="text/html; charset=utf-8", body="", body_bytes=b"", parsed_json=None,
    ),
    "blocked": lambda: _raw(403, "text/html; charset=utf-8", "blocked", None),
    "challenge": lambda: _raw(200, "text/html; charset=utf-8",
                              "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。", None),
}
_GUARD_FAILURE_MARKERS = {"expired": "已過期", "blocked": None, "challenge": "暫停操作並稍後再試"}


@pytest.mark.asyncio
@pytest.mark.parametrize("position", [0, 1, 2], ids=["at_bridge", "at_last_info", "at_send_body"])
@pytest.mark.parametrize("failure", ["expired", "blocked", "challenge"])
async def test_guard_abort_at_any_step_returns_the_guard_payload_and_writes_nothing(
    tmp_path, monkeypatch, position, failure,
):
    """design T-66: the guard's own payload comes back unchanged (the challenge stop
    instruction included); the sequence stops at the failing step; nothing is logged
    and no status is written."""
    ctx, info, db = await _new_session(tmp_path)
    script = _standard_script()[:position] + [_GUARD_FAILURE_RESPONSES[failure]()]
    spy = _install_seq_fetch(monkeypatch, script)

    result = await send_event(ctx, _route(measured=True, status_write="scheduled"),
                              job_id="12355016", candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert len(spy.calls) == position + 1
    assert "error" in result
    assert "sent" not in result
    marker = _GUARD_FAILURE_MARKERS[failure]
    if marker is not None:
        assert marker in result["error"]
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


# ── register_event_tools published schema — T-58 (interface) ────────────────────────

@pytest.mark.asyncio
async def test_send_inquiry_published_schema_requires_job_candidate_and_message_and_takes_no_ctx():
    """design T-58: after register_event_tools, send_inquiry is published with
    job_id, candidate_id and message required, template_id optional, and no ctx."""
    mcp = FastMCP("event-schema-test")
    register_event_tools(mcp)
    tools = {tool.name: tool for tool in await mcp.list_tools()}

    assert "send_inquiry" in tools
    schema = tools["send_inquiry"].inputSchema
    assert set(schema.get("required", [])) == {"job_id", "candidate_id", "message"}
    assert "template_id" in schema.get("properties", {})
    assert "template_id" not in schema.get("required", [])
    assert "ctx" not in schema.get("properties", {})
    assert "ctx" not in schema.get("required", [])


# ── Unmeasured route verdict — T-74 (interface) ─────────────────────────────────────

@pytest.mark.asyncio
async def test_unmeasured_route_is_never_confirmed_and_says_the_success_shape_is_unmeasured(tmp_path, monkeypatch):
    """design T-74: with success_shape_measured=False the willingness success shape is
    still unconfirmed, with the unmeasured message; an unparseable success body gets the
    same unmeasured message; a measured route with the same script is confirmed."""
    ctx, _info, _db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())
    unmeasured = await send_event(ctx, _route(measured=False), job_id="12355016",
                                  candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert unmeasured["sent"] == "unconfirmed"
    assert "message_id" not in unmeasured
    assert "stream_id" not in unmeasured
    assert "尚未實測" in unmeasured["message"]
    assert "重送" in unmeasured["message"]

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    broken = _full_success_envelope()
    del broken["data"][0]["streamId"]
    _install_seq_fetch(monkeypatch, _standard_script(third=_raw_from_body(broken)))
    unparseable = await send_event(ctx2, _route(measured=False), job_id="12355016",
                                   candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert unparseable["sent"] == "unconfirmed"
    assert unparseable["message"] == unmeasured["message"]

    ctx3, _info3, _db3 = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())
    measured = await send_event(ctx3, _route(measured=True), job_id="12355016",
                                candidate_id=_PID_SHAPED_ID, build_body=_fixed_body)

    assert measured["sent"] == "confirmed"


# ── Caveat codes ("聯絡成功，但……") — T-75 (interface) ──────────────────────────────

_CAVEAT_ERROR_ENVELOPE = {"code": "00305", "message": "SYNTHETIC-CAVEAT-TEXT", "detail": []}


@pytest.mark.asyncio
async def test_declared_caveat_code_on_an_error_envelope_is_ambiguous_and_logged_and_status_written(
    tmp_path, monkeypatch,
):
    """design T-75: a declared caveat code on a 104 error envelope is accepted (unconfirmed,
    shape 5), the message names the code, one sent_log row is written, and the route's
    status is written."""
    ctx, info, db = await _new_session(tmp_path)
    route = _route(measured=False, status_write="scheduled", caveats=frozenset({"00305"}))
    _install_seq_fetch(monkeypatch, _standard_script(
        third=_raw_from_body(_CAVEAT_ERROR_ENVELOPE, status=400),
    ))

    result = await send_event(ctx, route, job_id="12355016", candidate_id=_PID_SHAPED_ID,
                              build_body=_fixed_body)

    assert result["sent"] == "unconfirmed"
    assert "failed" not in result
    assert "00305" in result["message"]
    assert await db.get_daily_sent_count(info.account_label) == 1
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == "scheduled"


@pytest.mark.asyncio
async def test_declared_caveat_code_inside_failed_is_unconfirmed_with_failed_in_place(tmp_path, monkeypatch):
    """design T-75: the same code inside a 200 envelope's failed list is shape 4, with
    failed passed through unchanged."""
    ctx, _info, _db = await _new_session(tmp_path)
    route = _route(measured=False, status_write="scheduled", caveats=frozenset({"00305"}))
    failed_list = [{"code": "00305"}]
    _install_seq_fetch(monkeypatch, _standard_script(
        third=_raw_from_body({"data": [], "failed": failed_list, "metadata": {}}),
    ))

    result = await send_event(ctx, route, job_id="12355016", candidate_id=_PID_SHAPED_ID,
                              build_body=_fixed_body)

    assert result["sent"] == "unconfirmed"
    assert result["failed"] == failed_list


@pytest.mark.asyncio
async def test_empty_caveat_set_keeps_the_same_error_envelope_as_not_sent(tmp_path, monkeypatch):
    """design T-75: with an empty caveat set, the same 104 error envelope is a plain
    refusal: an error payload, no sent_log row, no candidate status."""
    ctx, info, db = await _new_session(tmp_path)
    route = _route(measured=False, status_write="scheduled", caveats=frozenset())
    _install_seq_fetch(monkeypatch, _standard_script(
        third=_raw_from_body(_CAVEAT_ERROR_ENVELOPE, status=400),
    ))

    result = await send_event(ctx, route, job_id="12355016", candidate_id=_PID_SHAPED_ID,
                              build_body=_fixed_body)

    assert "error" in result
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


# ═════════════════════════════════════════════════════════════════════════════════
# Four new event tools (send_interview_invite, send_start_date_reminder,
# send_personality_test_invite, send_thank_you_letter). Same transport seam as the
# send_inquiry cases above: api_client.fetch is replaced, while classify(),
# guarded_sequence and the SQLite file stay real.
# ═════════════════════════════════════════════════════════════════════════════════

_EV_BODY = "SYNTHETIC-LETTER-BODY"
_EV_JOB = "12355016"
_EV_FUTURE = "2099-01-01 09:00"
_EV_PLACE = "SYNTHETIC-PLACE"
_EV_CONTACT = "SYNTHETIC-CONTACT"
_EV_TEL = "SYNTHETIC-TEL"
_EV_TEMPLATE = "SYNTHETIC-TEMPLATE-ID"
_TWO_RECRUITERS = [
    {"id": "r1", "email": "r1@example.invalid"},
    {"id": "r2", "email": "r2@example.invalid"},
]
# Shape of 104's own validation refusal, as recorded in tests/fixtures/failure_send_validation.json:
# detail maps each field to a list of explanation strings.
_VALIDATION_ENVELOPE = {
    "code": "00005",
    "message": "帶入內容驗證失敗",
    "detail": {"content": ["SYNTHETIC-FIELD-EXPLANATION"]},
}
_CAVEAT_CODES = ("00305", "00310", "00311")
_NEW_ROUTE_TOOLS = (
    "send_interview_invite",
    "send_start_date_reminder",
    "send_personality_test_invite",
    "send_thank_you_letter",
)
_SCHEDULE_TOOLS = ("send_interview_invite", "send_start_date_reminder")
_REPLY_DAY_TOOLS = (
    "send_interview_invite",
    "send_start_date_reminder",
    "send_personality_test_invite",
)
_CONTACTED_TOOLS = (
    "send_start_date_reminder",
    "send_personality_test_invite",
    "send_thank_you_letter",
)
_SEND_KEY = {
    "send_interview_invite": "send_interview_event",
    "send_start_date_reminder": "send_inquire_event",
    "send_personality_test_invite": "send_exam_event",
    "send_thank_you_letter": "send_thank_event",
}
_TIME_FIELD = {"send_interview_invite": "meeting_time", "send_start_date_reminder": "start_time"}
# The five closed result shapes (send_message / send_inquiry contract, shared by the new tools).
_FIVE_SHAPES = [
    {"success", "error"},
    {"error"},
    {"error", "retry_after_seconds"},
    {"sent", "message_id", "stream_id", "p_id", "event_id", "warnings"},
    {"sent", "message", "failed", "warnings"},
    {"sent", "message", "warnings"},
]


async def _call_interview(ctx, **overrides):
    kwargs = dict(
        job_id=_EV_JOB, candidate_id=_PID_SHAPED_ID, message=_EV_BODY,
        meeting_time=_EV_FUTURE, location=_EV_PLACE, phase=1,
        contact_name=_EV_CONTACT, contact_phone=_EV_TEL,
    )
    kwargs.update(overrides)
    return await TOOLS["send_interview_invite"](ctx=ctx, **kwargs)


async def _call_reminder(ctx, **overrides):
    kwargs = dict(
        job_id=_EV_JOB, candidate_id=_PID_SHAPED_ID, message=_EV_BODY,
        start_time=_EV_FUTURE, location=_EV_PLACE,
        contact_name=_EV_CONTACT, contact_phone=_EV_TEL,
    )
    kwargs.update(overrides)
    return await TOOLS["send_start_date_reminder"](ctx=ctx, **kwargs)


async def _call_exam(ctx, **overrides):
    kwargs = dict(job_id=_EV_JOB, candidate_id=_PID_SHAPED_ID, message=_EV_BODY)
    kwargs.update(overrides)
    return await TOOLS["send_personality_test_invite"](ctx=ctx, **kwargs)


async def _call_thank(ctx, **overrides):
    kwargs = dict(job_id=_EV_JOB, candidate_id=_PID_SHAPED_ID, message=_EV_BODY)
    kwargs.update(overrides)
    return await TOOLS["send_thank_you_letter"](ctx=ctx, **kwargs)


_NEW_CALLS = {
    "send_interview_invite": _call_interview,
    "send_start_date_reminder": _call_reminder,
    "send_personality_test_invite": _call_exam,
    "send_thank_you_letter": _call_thank,
}

# Scripted outcomes for the three sub-requests. "blank" and "expired" are the two
# cases that never reach the third request; the others differ only in the third response.
_SCENARIOS = {
    "blank": (lambda: _standard_script(), {"message": "   "}),
    "accepted": (lambda: _standard_script(), {}),
    "failed": (
        lambda: _standard_script(third=_raw_from_body({"data": [], "failed": [{"x": 1}], "metadata": {}})),
        {},
    ),
    "refused": (lambda: _standard_script(third=_raw_from_body(_VALIDATION_ENVELOPE, status=400)), {}),
    "transport": (lambda: _standard_script(third=TimeoutError("simulated network timeout")), {}),
    "expired": (lambda: [_GUARD_FAILURE_RESPONSES["expired"]()], {}),
}


async def _run_scenario(tool, ctx, scenario, monkeypatch):
    script_for, overrides = _SCENARIOS[scenario]
    spy = _install_seq_fetch(monkeypatch, script_for())
    result = await _NEW_CALLS[tool](ctx, **overrides)
    return result, spy


async def _published_tools() -> dict:
    mcp = FastMCP("event-tools-probe")
    register_event_tools(mcp)
    return {tool.name: tool for tool in await mcp.list_tools()}


def _squash(text: str) -> str:
    return re.sub(r"\s+", "", text)


async def _sent_log_sources(db: Database) -> list[str]:
    async with db._conn.execute("SELECT id_source FROM sent_log") as cursor:
        rows = await cursor.fetchall()
    return [row[0] for row in rows]


def _expected_base_body() -> dict:
    return {
        "candidate": [{"idNo": _BRIDGE_IDNO}],
        "emailCC": [_CC],
        "contactJobNo": _EV_JOB,
        "content": _EV_BODY,
        "templateId": "",
        "ec": "",
        "rc": "13011211",
    }


def _expected_body(tool: str) -> dict:
    base = _expected_base_body()
    if tool == "send_thank_you_letter":
        return base
    if tool == "send_personality_test_invite":
        return {**base, "isRequiredReplyDay": False, "replyDay": 1, "examNo": 1}
    schedule = {
        "isRequiredReplyDay": False, "replyDay": 1,
        "contact": _EV_CONTACT, "contactTel": _EV_TEL,
        "isWithDetail": True, "file": [],
        "syncToCalendar": False, "meetingTime": _EV_FUTURE, "location": _EV_PLACE,
        "syncRecruiters": False, "recruiters": [],
    }
    if tool == "send_start_date_reminder":
        return {**base, **schedule}
    return {**base, **schedule, "syncContacts": False, "contacts": [], "phase": 1}


# ── Route wiring, body keys and body values — T-1..T-11 (behavior) ──────────────────

@pytest.mark.asyncio
async def test_T1_send_interview_invite_sends_the_callers_values_trimming_only_place_and_contact(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_interview(ctx, phase=2, location="  SYNTHETIC-PLACE ",
                          contact_name="  SYNTHETIC-CONTACT  ", contact_phone=" SYNTHETIC-TEL ")

    assert len(spy.calls) == 3
    endpoint, _params, body = spy.calls[2]
    assert endpoint.path == "/bc-comm/event/interview"
    assert body["content"] == _EV_BODY
    assert body["meetingTime"] == _EV_FUTURE
    assert body["phase"] == 2
    assert body["location"] == "SYNTHETIC-PLACE"
    assert body["contact"] == "SYNTHETIC-CONTACT"
    assert body["contactTel"] == "SYNTHETIC-TEL"


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [1, 2, 3])
async def test_T2_send_interview_invite_sends_phase_as_an_integer_and_never_looks_up_the_phase(tmp_path, monkeypatch, phase):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_interview(ctx, phase=phase)

    assert [ep.key for ep, _p, _b in spy.calls] == [
        "resolve_candidate_idno", "event_last_info", "send_interview_event",
    ]
    body = spy.calls[2][2]
    assert body["phase"] == phase
    assert type(body["phase"]) is int


@pytest.mark.asyncio
@pytest.mark.parametrize("include_recruiters", [False, True])
async def test_T3_send_interview_invite_never_syncs_calendar_recruiters_or_contacts(tmp_path, monkeypatch, include_recruiters):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": _RECRUITER_RAW}))

    await _call_interview(ctx, include_recruiters=include_recruiters)

    body = spy.calls[2][2]
    assert body["syncToCalendar"] is False
    assert body["syncRecruiters"] is False
    assert body["syncContacts"] is False
    assert body["contacts"] == []
    properties = set((await _published_tools())["send_interview_invite"].inputSchema.get("properties", {}))
    assert not any("sync" in name.lower() or "calendar" in name.lower() for name in properties)


@pytest.mark.asyncio
async def test_T4_send_interview_invite_attaches_no_file(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_interview(ctx)

    assert spy.calls[2][2]["file"] == []


_REPLY_DAY_CASES = [
    pytest.param({"reply_days": 7}, True, 7, id="given"),
    pytest.param({}, False, 1, id="omitted"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, required, days", _REPLY_DAY_CASES)
async def test_T5_send_interview_invite_reply_day_flag_and_value(tmp_path, monkeypatch, extra, required, days):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_interview(ctx, **extra)

    body = spy.calls[2][2]
    assert body["isRequiredReplyDay"] is required
    assert body["replyDay"] == days


@pytest.mark.asyncio
async def test_T6_send_start_date_reminder_sends_start_time_as_meeting_time_and_no_interview_only_keys(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_reminder(ctx, location="  SYNTHETIC-PLACE ",
                         contact_name=" SYNTHETIC-CONTACT ", contact_phone="SYNTHETIC-TEL ")

    assert len(spy.calls) == 3
    endpoint, _params, body = spy.calls[2]
    assert endpoint.path == "/bc-comm/event/inquire"
    assert body["content"] == _EV_BODY
    assert body["meetingTime"] == _EV_FUTURE
    assert body["location"] == "SYNTHETIC-PLACE"
    assert body["contact"] == "SYNTHETIC-CONTACT"
    assert body["contactTel"] == "SYNTHETIC-TEL"
    for absent in ("phase", "syncContacts", "contacts"):
        assert absent not in body


@pytest.mark.asyncio
@pytest.mark.parametrize("include_recruiters, expected_recruiters", [(False, []), (True, ["r1", "r2"])])
async def test_T7_send_start_date_reminder_never_syncs_and_follows_the_recruiters_rule(tmp_path, monkeypatch, include_recruiters, expected_recruiters):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": _TWO_RECRUITERS}))

    await _call_reminder(ctx, include_recruiters=include_recruiters)

    body = spy.calls[2][2]
    assert body["syncToCalendar"] is False
    assert body["syncRecruiters"] is False
    assert body["file"] == []
    assert body["recruiters"] == expected_recruiters


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, required, days", _REPLY_DAY_CASES)
async def test_T8_send_start_date_reminder_reply_day_flag_and_value(tmp_path, monkeypatch, extra, required, days):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_reminder(ctx, **extra)

    body = spy.calls[2][2]
    assert body["isRequiredReplyDay"] is required
    assert body["replyDay"] == days


@pytest.mark.asyncio
async def test_T9_send_personality_test_invite_fixes_exam_number_one_and_takes_no_test_kind_parameter(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_exam(ctx)

    endpoint, _params, body = spy.calls[2]
    assert endpoint.path == "/bc-comm/event/exam"
    assert body["examNo"] == 1
    properties = set((await _published_tools())["send_personality_test_invite"].inputSchema.get("properties", {}))
    assert not any("exam" in name.lower() or "test" in name.lower() for name in properties)


@pytest.mark.asyncio
@pytest.mark.parametrize("extra, required, days", _REPLY_DAY_CASES)
async def test_T10_send_personality_test_invite_reply_day_flag_and_value(tmp_path, monkeypatch, extra, required, days):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_exam(ctx, **extra)

    body = spy.calls[2][2]
    assert body["isRequiredReplyDay"] is required
    assert body["replyDay"] == days


@pytest.mark.asyncio
async def test_T11_send_thank_you_letter_posts_exactly_the_thank_route_key_set(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _call_thank(ctx)

    endpoint, _params, body = spy.calls[2]
    assert endpoint.path == "/bc-comm/event/thank"
    assert set(body) == {"candidate", "emailCC", "contactJobNo", "content", "templateId", "ec", "rc"}


# ── Published descriptions — T-12, T-43, T-72 (behavior / interface) ─────────────────

@pytest.mark.asyncio
async def test_T12_thank_letter_description_states_the_source_limit_only_as_far_as_the_evidence_goes():
    description = _squash((await _published_tools())["send_thank_you_letter"].description or "")
    for concept in ("清單頁", "對話頁", "應徵", "未知", "確定沒有送出"):
        assert concept in description, concept
    assert "前端只對應徵來源" not in description


_REPLY_DAY_LIST = re.compile(r"1[、,]2[、,]3[、,]5[、,]7[、,]14")
_TIME_FORMAT = re.compile(r"YYYY-MM-DD\s+HH:mm")


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T43_new_descriptions_carry_the_risk_timeout_guidance_and_the_argument_rules(tool):
    from mcp104.tools.discovery import UNMEASURED_EVENT_ROUTE_NOTE

    description = (await _published_tools())[tool].description or ""
    squashed = _squash(description)
    for phrase in ("真實求職者", "無法撤回", "先讓真人看過"):
        assert phrase in squashed, phrase
    assert "dry-run" in description.lower()
    assert UNMEASURED_EVENT_ROUTE_NOTE in description
    assert "重送" in squashed
    assert "read_messages" in description
    if tool in _REPLY_DAY_TOOLS:
        assert _REPLY_DAY_LIST.search(squashed), "the six reply-day values are not listed"
    if tool in _SCHEDULE_TOOLS:
        assert _TIME_FORMAT.search(description), "YYYY-MM-DD HH:mm is not stated"
        assert "台北" in squashed
        for phrase in ("再呼叫一次會送出第二封", "不會改期", "不會取消", "104後台"):
            assert phrase in squashed, phrase
        recruiters_at = squashed.find("include_recruiters")
        assert recruiters_at >= 0 and "未實測" in squashed[recruiters_at:]
    if tool == "send_interview_invite":
        for phrase in ("一面", "二面", "三面"):
            assert phrase in squashed, phrase


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T72_published_description_contains_the_unmeasured_note_and_no_placeholder(tool):
    from mcp104.tools.discovery import UNMEASURED_EVENT_ROUTE_NOTE

    description = (await _published_tools())[tool].description or ""
    assert UNMEASURED_EVENT_ROUTE_NOTE in description
    assert not re.search(r"\{\{[A-Z_]+\}\}", description)


# ── Route wiring: the three request sequence and the sub-request keys — T-13, T-14, T-15, T-16 ──

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T13_new_route_resolves_the_pid_first_and_sends_only_the_bridge_idno_as_candidate(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx)

    bridge_endpoint, bridge_params, _body = spy.calls[0]
    assert bridge_endpoint.key == "resolve_candidate_idno"
    assert ("p_id", _PID_SHAPED_ID) in bridge_params
    assert spy.calls[2][2]["candidate"] == [{"idNo": _BRIDGE_IDNO}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T14_new_route_body_has_exactly_one_candidate(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx)

    assert len(spy.calls[2][2]["candidate"]) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T15_template_id_is_passed_through_and_omission_sends_a_present_empty_string(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())
    await _NEW_CALLS[tool](ctx, template_id=_EV_TEMPLATE)
    body = spy.calls[2][2]
    assert body["templateId"] == _EV_TEMPLATE
    assert body["content"] == _EV_BODY

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install_seq_fetch(monkeypatch, _standard_script())
    await _NEW_CALLS[tool](ctx2)
    assert "templateId" in spy2.calls[2][2]
    assert spy2.calls[2][2]["templateId"] == ""


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
@pytest.mark.parametrize("email_cc", [[_CC], []], ids=["one_cc", "empty_cc"])
async def test_T16_email_cc_from_last_info_reaches_the_body_unchanged(tmp_path, monkeypatch, tool, email_cc):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(last_info_data={"emailCC": email_cc}))

    await _NEW_CALLS[tool](ctx)

    assert spy.calls[2][2]["emailCC"] == email_cc


# ── Daily cap shared with send_message — T-17 (behavior) ──────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T17_daily_cap_reached_refuses_every_new_route_without_any_request(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    cap = ctx.request_context.lifespan_context.config.max_daily_messages
    for _ in range(cap):
        await db.log_sent(info.account_label, "prior-candidate", "message")
    _install_never_called_fetch(monkeypatch)

    result = await _NEW_CALLS[tool](ctx)

    assert result["success"] is False
    assert "error" in result


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T17_an_accepted_new_route_counts_toward_the_cap_send_message_checks(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    cap = ctx.request_context.lifespan_context.config.max_daily_messages
    for _ in range(cap - 1):
        await db.log_sent(info.account_label, "prior-candidate", "message")
    _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx)

    assert result.get("sent") == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == cap
    _install_never_called_fetch(monkeypatch)
    blocked = await TOOLS["send_message"](ctx=ctx, job_id=_EV_JOB, candidate_id="0225677",
                                          message=_SYNTHETIC_LETTER)
    assert blocked.get("success") is False
    assert "上限" in blocked["error"]


# ── Pre-send refusals — T-20..T-26 (behavior) ─────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
@pytest.mark.parametrize("bad", ["", "   ", "字" * 1001], ids=["empty", "blank", "1001_chars"])
async def test_T20_blank_or_over_1000_message_is_refused_before_any_request(tmp_path, monkeypatch, tool, bad):
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx, message=bad)

    assert result["success"] is False
    assert "內容" in result["error"]
    assert len(spy.calls) == 0
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T20_exactly_1000_character_message_is_sent(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx, message="字" * 1000)

    assert len(spy.calls) == 3
    assert result.get("sent") == "unconfirmed"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T21_thirteen_digit_candidate_id_is_refused_before_any_request(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx, candidate_id=_IDNO_SHAPED_ID)

    assert result["success"] is False
    assert len(spy.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _REPLY_DAY_TOOLS)
@pytest.mark.parametrize("bad_days", [4, 0, 15])
async def test_T22_reply_days_outside_the_six_values_is_refused_before_any_request(tmp_path, monkeypatch, tool, bad_days):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx, reply_days=bad_days)

    assert result["success"] is False
    assert len(spy.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _REPLY_DAY_TOOLS)
async def test_T22_reply_days_one_two_three_five_seven_fourteen_are_sent(tmp_path, monkeypatch, tool):
    for days in (1, 2, 3, 5, 7, 14):
        ctx, _info, _db = await _new_session(tmp_path)
        spy = _install_seq_fetch(monkeypatch, _standard_script())

        result = await _NEW_CALLS[tool](ctx, reply_days=days)

        assert len(spy.calls) == 3, days
        assert result.get("sent") == "unconfirmed", days


_BAD_SCHEDULE_TIMES = ["2099-01-01 9:00", "2099/01/01 09:00", "2099-02-30 10:00", "2000-01-01 10:00"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
@pytest.mark.parametrize("bad", _BAD_SCHEDULE_TIMES)
async def test_T23_malformed_nonexistent_or_past_schedule_time_is_refused_before_any_request(tmp_path, monkeypatch, tool, bad):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())
    field = _TIME_FIELD[tool]

    result = await _NEW_CALLS[tool](ctx, **{field: bad})

    assert result["success"] is False
    assert field in result["error"]
    assert len(spy.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
async def test_T23_well_formed_future_time_is_sent_as_typed(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx, **{_TIME_FIELD[tool]: _EV_FUTURE})

    assert len(spy.calls) == 3
    assert spy.calls[2][2]["meetingTime"] == _EV_FUTURE


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
@pytest.mark.parametrize("bad", ["   ", "a" * 101], ids=["blank", "101_chars"])
async def test_T24_blank_or_over_100_location_is_refused_before_any_request(tmp_path, monkeypatch, tool, bad):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx, location=bad)

    assert result["success"] is False
    assert "location" in result["error"]
    assert len(spy.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
async def test_T24_exactly_100_character_location_is_sent(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx, location="a" * 100)

    assert len(spy.calls) == 3


_CONTACT_BAD_VALUES = [
    ("contact_name", "   "),
    ("contact_name", "a" * 51),
    ("contact_phone", ""),
    ("contact_phone", "a" * 51),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
@pytest.mark.parametrize("field, bad", _CONTACT_BAD_VALUES)
async def test_T25_blank_or_over_50_contact_field_is_refused_before_any_request(tmp_path, monkeypatch, tool, field, bad):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx, **{field: bad})

    assert result["success"] is False
    assert field in result["error"]
    assert len(spy.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("phase", [0, 4])
async def test_T26_phase_outside_one_two_three_is_refused_before_any_request(tmp_path, monkeypatch, phase):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _call_interview(ctx, phase=phase)

    assert result["success"] is False
    assert "phase" in result["error"]
    assert len(spy.calls) == 0


# ── Meeting time: taipei-local reading, verbatim body — T-27 (behavior) ───────────────

def test_T27_meeting_time_boundary_holds_for_taipei_now_and_for_the_same_instant_in_utc():
    taipei_now = datetime(2026, 11, 12, 10, 0, tzinfo=TAIPEI)
    utc_now = datetime(2026, 11, 12, 2, 0, tzinfo=timezone.utc)
    for now in (taipei_now, utc_now):
        assert check_meeting_time("2026-11-12 10:00", now=now, name="meeting_time") is None
        assert isinstance(check_meeting_time("2026-11-12 09:59", now=now, name="meeting_time"), str)


@pytest.mark.asyncio
async def test_T27_meeting_time_reaches_the_body_exactly_as_typed(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())
    typed = "2099-12-31 23:59"

    await _call_interview(ctx, meeting_time=typed)

    assert spy.calls[2][2]["meetingTime"] == typed


# ── Send result verdicts — T-28..T-33 (behavior) ──────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T28_full_success_shape_is_unconfirmed_and_never_carries_ids(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx)

    assert result["sent"] == "unconfirmed"
    assert "message_id" not in result
    assert "stream_id" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T29_unconfirmed_message_says_the_shape_is_unmeasured_and_not_to_resend(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx)

    assert "尚未實測" in result["message"]
    assert "重送" in result["message"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T30_nonempty_failed_list_is_unconfirmed_and_passed_through(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script(
        third=_raw_from_body({"data": [], "failed": [{"x": 1}], "metadata": {}})))

    result = await _NEW_CALLS[tool](ctx)

    assert result["sent"] == "unconfirmed"
    assert result["failed"] == [{"x": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T31_a_104_validation_refusal_is_not_sent_and_carries_the_field_explanation(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script(
        third=_raw_from_body(_VALIDATION_ENVELOPE, status=400)))

    result = await _NEW_CALLS[tool](ctx)

    assert "error" in result
    assert "sent" not in result
    assert "SYNTHETIC-FIELD-EXPLANATION" in result["error"]
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", list(_SCENARIOS))
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T32_every_return_is_one_of_the_five_closed_shapes(tmp_path, monkeypatch, tool, scenario):
    ctx, _info, _db = await _new_session(tmp_path)

    result, _spy = await _run_scenario(tool, ctx, scenario, monkeypatch)

    assert set(result) in _FIVE_SHAPES, (scenario, result)


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario, rows", [
    ("accepted", ["message"]),
    ("transport", ["message"]),
    ("blank", []),
    ("refused", []),
])
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T33_sent_log_gains_a_row_only_when_the_request_was_accepted_or_may_have_landed(tmp_path, monkeypatch, tool, scenario, rows):
    ctx, _info, db = await _new_session(tmp_path)

    await _run_scenario(tool, ctx, scenario, monkeypatch)

    assert await _sent_log_sources(db) == rows


# ── Candidate status — T-34..T-38 (behavior) ──────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("prior", [None, "interested"], ids=["blank", "interested"])
async def test_T34_interview_invite_accepted_sets_scheduled_whatever_the_prior_status(tmp_path, monkeypatch, prior):
    ctx, info, db = await _new_session(tmp_path)
    if prior is not None:
        await db.upsert_candidate(_PID_SHAPED_ID, "message", info.account_label, status=prior)
    _install_seq_fetch(monkeypatch, _standard_script())

    result = await _call_interview(ctx)

    assert result.get("sent") == "unconfirmed"
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == "scheduled"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _CONTACTED_TOOLS)
@pytest.mark.parametrize("prior, expected", [(None, "contacted"), ("interested", "interested")],
                         ids=["blank_becomes_contacted", "interested_kept"])
async def test_T35_other_three_routes_fill_a_blank_status_and_keep_an_existing_one(tmp_path, monkeypatch, tool, prior, expected):
    ctx, info, db = await _new_session(tmp_path)
    if prior is not None:
        await db.upsert_candidate(_PID_SHAPED_ID, "message", info.account_label, status=prior)
    _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx)

    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == expected


@pytest.mark.asyncio
async def test_T36_thank_letter_never_writes_not_interested(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())

    await _call_thank(ctx)

    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == "contacted"


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T37_status_row_reads_back_under_the_message_pid_and_not_under_the_resume_idno(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx)

    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is not None
    assert await db.get_candidate(_BRIDGE_IDNO, "resume", info.account_label) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("scenario", ["blank", "refused"])
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T38_refused_or_pre_send_rejected_writes_no_status(tmp_path, monkeypatch, tool, scenario):
    ctx, info, db = await _new_session(tmp_path)

    await _run_scenario(tool, ctx, scenario, monkeypatch)

    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


# ── Request count, order, throttle, guard aborts — T-39..T-42 ─────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T39_new_route_sends_bridge_then_last_info_then_the_body_in_that_order(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx)

    assert [ep.key for ep, _p, _b in spy.calls] == [
        "resolve_candidate_idno", "event_last_info", _SEND_KEY[tool],
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T39_two_free_slots_refuse_the_three_request_route_with_retry_after(tmp_path, monkeypatch, tool):
    ctx, info, _db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    now = time.time()
    for _ in range(app.config.max_requests_per_hour - 2):
        info.throttle.unpersisted_timestamps.append(now)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    result = await _NEW_CALLS[tool](ctx)

    assert "retry_after_seconds" in result
    assert "sent" not in result
    assert len(spy.calls) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T40_non_list_email_cc_aborts_before_the_body_request(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(last_info_data={"emailCC": _CC}))

    result = await _NEW_CALLS[tool](ctx)

    assert "error" in result
    assert "sent" not in result
    assert len(spy.calls) == 2
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T41_bridge_not_found_stops_after_one_request_with_no_record(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    not_found = _raw_from_body({"code": "00004", "message": "找不到對應資源", "detail": []}, status=404)
    spy = _install_seq_fetch(monkeypatch, [not_found])

    result = await _NEW_CALLS[tool](ctx)

    assert "error" in result
    assert "sent" not in result
    assert len(spy.calls) == 1
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


def test_T42_cold_path_worst_case_stays_within_80_seconds_of_the_120_second_client_timeout():
    from mcp104.browser.api_client import FETCH_TIMEOUT_SECONDS

    interval = get_config().min_call_interval_seconds
    warm = interval + 3 * FETCH_TIMEOUT_SECONDS
    cold = warm + (interval + FETCH_TIMEOUT_SECONDS)
    assert warm <= cold <= 80, f"cold worst case {cold}s exceeds 80s (120s client timeout minus 40s headroom)"


# ── Declarations and body key set — T-47, T-50 (interface / behavior) ─────────────────

def test_T47_five_route_declarations_only_send_inquiry_is_measured_and_each_points_at_a_real_endpoint_and_tool():
    import mcp104.tools.events as events_module
    from mcp104.browser.api_client import ENDPOINTS

    routes = [value for value in vars(events_module).values() if isinstance(value, EventRoute)]
    assert len(routes) == 5
    assert {route.tool_name for route in routes} == {"send_inquiry", *_NEW_ROUTE_TOOLS}
    assert {route.tool_name for route in routes if route.success_shape_measured} == {"send_inquiry"}
    for route in routes:
        assert route.endpoint_key in ENDPOINTS
        assert route.tool_name in TOOLS


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T50_each_new_route_body_matches_its_declared_key_set_and_values(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script())

    await _NEW_CALLS[tool](ctx)

    body = spy.calls[2][2]
    assert body == _expected_body(tool)
    assert "resumeSources" not in body


# ── Verdict table for send_message / send_inquiry and the new routes — T-54 ───────────

def _row_failed_payload() -> dict:
    return {"data": [], "failed": [{"x": 1}], "metadata": {}}


def _row_unrecognisable_payload() -> dict:
    payload = _full_success_envelope()
    del payload["data"][0]["streamId"]
    return payload


@pytest.mark.asyncio
async def test_T54_send_message_and_send_inquiry_keep_confirmed_failed_and_unrecognised_verdicts(tmp_path, monkeypatch):
    rows = [
        (_full_success_envelope(), "confirmed", False),
        (_row_failed_payload(), "unconfirmed", True),
        (_row_unrecognisable_payload(), "unconfirmed", False),
    ]
    for payload, expected, has_failed in rows:
        ctx, _info, _db = await _new_session(tmp_path)
        _install_fake_fetch(monkeypatch, _raw_from_body(payload))
        message_result = await TOOLS["send_message"](ctx=ctx, job_id=_EV_JOB,
                                                     candidate_id="012975", message=_SYNTHETIC_LETTER)
        assert message_result["sent"] == expected
        if has_failed:
            assert message_result["failed"] == [{"x": 1}]

        ctx2, _info2, _db2 = await _new_session(tmp_path)
        _install_seq_fetch(monkeypatch, _standard_script(third=_raw_from_body(payload)))
        inquiry_result = await TOOLS["send_inquiry"](ctx=ctx2, job_id=_EV_JOB,
                                                     candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
        assert inquiry_result["sent"] == expected
        if has_failed:
            assert inquiry_result["failed"] == [{"x": 1}]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T54_new_routes_never_confirm_and_keep_failed_in_place(tmp_path, monkeypatch, tool):
    rows = [
        (_full_success_envelope(), False),
        (_row_failed_payload(), True),
        (_row_unrecognisable_payload(), False),
    ]
    for payload, has_failed in rows:
        ctx, _info, _db = await _new_session(tmp_path)
        _install_seq_fetch(monkeypatch, _standard_script(third=_raw_from_body(payload)))

        result = await _NEW_CALLS[tool](ctx)

        assert result["sent"] == "unconfirmed"
        if has_failed:
            assert result["failed"] == [{"x": 1}]
        else:
            assert "尚未實測" in result["message"]


# ── Transport loss after the body was sent — T-56 (behavior) ──────────────────────────

@pytest.mark.asyncio
async def test_T56_transport_loss_after_the_body_is_unconfirmed_and_logged_for_interview_and_send_message(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, [TimeoutError("simulated network timeout")])
    message_result = await TOOLS["send_message"](ctx=ctx, job_id=_EV_JOB,
                                                 candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
    assert message_result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1
    assert (await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label))["status"] == "contacted"

    ctx2, info2, db2 = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script(third=TimeoutError("simulated network timeout")))
    interview_result = await _call_interview(ctx2)
    assert interview_result["sent"] == "unconfirmed"
    assert await db2.get_daily_sent_count(info2.account_label) == 1
    assert (await db2.get_candidate(_PID_SHAPED_ID, "message", info2.account_label))["status"] == "scheduled"


# ── Recruiters: default empty, explicit opt-in, malformed shapes — T-60, T-61, T-62 ───

@pytest.mark.asyncio
@pytest.mark.parametrize("extra", [{}, {"include_recruiters": False}], ids=["omitted", "false"])
async def test_T60_recruiters_stay_empty_by_default_even_when_last_info_lists_them(tmp_path, monkeypatch, extra):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": _TWO_RECRUITERS}))

    await _call_interview(ctx, **extra)

    assert spy.calls[2][2]["recruiters"] == []


@pytest.mark.asyncio
async def test_T61_include_recruiters_true_lists_the_ids_in_order_and_sync_flags_stay_off(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": _TWO_RECRUITERS}))

    await _call_interview(ctx, include_recruiters=True)

    body = spy.calls[2][2]
    assert body["recruiters"] == ["r1", "r2"]
    assert body["syncToCalendar"] is False
    assert body["syncRecruiters"] is False
    assert body["syncContacts"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
@pytest.mark.parametrize("last_info, expected", [
    ({"emailCC": [_CC]}, []),
    ({"emailCC": [_CC], "recruiters": []}, []),
], ids=["absent", "empty"])
async def test_T62_absent_or_empty_recruiters_send_an_empty_list_when_explicitly_requested(tmp_path, monkeypatch, tool, last_info, expected):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(last_info_data=last_info))

    result = await _NEW_CALLS[tool](ctx, include_recruiters=True)

    assert result.get("sent") == "unconfirmed"
    assert spy.calls[2][2]["recruiters"] == expected


_MALFORMED_RECRUITERS = [
    pytest.param("r1", id="string"),
    pytest.param(["r1"], id="non_object_element"),
    pytest.param([{"email": "r1@example.invalid"}], id="element_missing_id"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
@pytest.mark.parametrize("raw", _MALFORMED_RECRUITERS)
async def test_T62_malformed_recruiters_abort_when_explicitly_requested_with_no_record(tmp_path, monkeypatch, tool, raw):
    ctx, info, db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": raw}))

    result = await _NEW_CALLS[tool](ctx, include_recruiters=True)

    assert "error" in result
    assert "sent" not in result
    assert len(spy.calls) == 2
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
@pytest.mark.parametrize("raw", _MALFORMED_RECRUITERS)
async def test_T62_malformed_recruiters_do_not_matter_when_not_requested(tmp_path, monkeypatch, tool, raw):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": raw}))

    result = await _NEW_CALLS[tool](ctx, include_recruiters=False)

    assert result.get("sent") == "unconfirmed"
    assert len(spy.calls) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _SCHEDULE_TOOLS)
async def test_T62_integer_recruiter_id_is_forwarded_as_is(tmp_path, monkeypatch, tool):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_seq_fetch(monkeypatch, _standard_script(
        last_info_data={"emailCC": [_CC], "recruiters": [{"id": 7, "email": "r7@example.invalid"}]}))

    await _NEW_CALLS[tool](ctx, include_recruiters=True)

    assert spy.calls[2][2]["recruiters"] == [7]


# ── Caveat codes ("聯絡成功，但……") — T-63, T-64 (behavior) ───────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("code", _CAVEAT_CODES)
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T63_caveat_code_on_an_error_envelope_is_accepted_for_every_new_route(tmp_path, monkeypatch, tool, code):
    ctx, info, db = await _new_session(tmp_path)
    envelope = {"code": code, "message": "SYNTHETIC-CAVEAT-TEXT", "detail": {}}
    _install_seq_fetch(monkeypatch, _standard_script(third=_raw_from_body(envelope, status=400)))

    result = await _NEW_CALLS[tool](ctx)

    assert result["sent"] == "unconfirmed"
    assert code in result["message"]
    assert await db.get_daily_sent_count(info.account_label) == 1
    row = await db.get_candidate(_PID_SHAPED_ID, "message", info.account_label)
    assert row["status"] == ("scheduled" if tool == "send_interview_invite" else "contacted")


@pytest.mark.asyncio
async def test_T63_send_message_and_send_inquiry_still_treat_the_caveat_code_as_a_refusal(tmp_path, monkeypatch):
    envelope = {"code": "00305", "message": "SYNTHETIC-CAVEAT-TEXT", "detail": {}}
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(envelope, status=400))
    message_result = await TOOLS["send_message"](ctx=ctx, job_id=_EV_JOB,
                                                 candidate_id="012975", message=_SYNTHETIC_LETTER)
    assert "error" in message_result
    assert "sent" not in message_result
    assert await db.get_daily_sent_count(info.account_label) == 0
    assert await db.get_candidate("012975", "message", info.account_label) is None

    ctx2, info2, db2 = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script(third=_raw_from_body(envelope, status=400)))
    inquiry_result = await TOOLS["send_inquiry"](ctx=ctx2, job_id=_EV_JOB,
                                                 candidate_id=_PID_SHAPED_ID, message=_SYNTHETIC_LETTER)
    assert "error" in inquiry_result
    assert "sent" not in inquiry_result
    assert await db2.get_daily_sent_count(info2.account_label) == 0
    assert await db2.get_candidate(_PID_SHAPED_ID, "message", info2.account_label) is None


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", _NEW_ROUTE_TOOLS)
async def test_T64_failed_list_carrying_a_caveat_code_is_unconfirmed_and_logged(tmp_path, monkeypatch, tool):
    ctx, info, db = await _new_session(tmp_path)
    _install_seq_fetch(monkeypatch, _standard_script(
        third=_raw_from_body({"data": [], "failed": [{"code": "00305"}], "metadata": {}})))

    result = await _NEW_CALLS[tool](ctx)

    assert result["sent"] == "unconfirmed"
    assert result["failed"] == [{"code": "00305"}]
    assert await db.get_daily_sent_count(info.account_label) == 1


# ── Published schemas — T-71 (interface) ──────────────────────────────────────────────

_PUBLISHED_EXPECTATIONS = {
    "send_inquiry": ({"job_id", "candidate_id", "message"}, {"template_id"}),
    "send_interview_invite": (
        {"job_id", "candidate_id", "message", "meeting_time", "location", "phase",
         "contact_name", "contact_phone"},
        {"reply_days", "include_recruiters", "template_id"},
    ),
    "send_start_date_reminder": (
        {"job_id", "candidate_id", "message", "start_time", "location",
         "contact_name", "contact_phone"},
        {"reply_days", "include_recruiters", "template_id"},
    ),
    "send_personality_test_invite": ({"job_id", "candidate_id", "message"}, {"reply_days", "template_id"}),
    "send_thank_you_letter": ({"job_id", "candidate_id", "message"}, {"template_id"}),
}


@pytest.mark.asyncio
async def test_T71_register_event_tools_publishes_exactly_the_five_event_tools():
    assert set(await _published_tools()) == set(_PUBLISHED_EXPECTATIONS)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", list(_PUBLISHED_EXPECTATIONS))
async def test_T71_published_schema_matches_the_declared_signature_and_takes_no_ctx(tool):
    schema = (await _published_tools())[tool].inputSchema
    required, optional = _PUBLISHED_EXPECTATIONS[tool]
    assert set(schema.get("required", [])) == required
    assert set(schema.get("properties", {})) == required | optional
    assert "ctx" not in schema.get("properties", {})
    assert "ctx" not in schema.get("required", [])
