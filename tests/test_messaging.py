"""Tests for the JSON-API messaging migration (`read_messages` / `get_conversation` /
`send_message`), following tests/test_search.py's seam-substitution idiom: the HTTP
transport (`browser.api_client.fetch`) is faked. There is no BrowserContext seam left
post-login (§C7) — `guarded_api` reads credentials straight off `SessionInfo.cookies`,
so a session under test is built by handing that field a cookie list directly.
`guarded_api`, `classify()` and the actual `tools.messaging` functions are exercised
for real, never mocked.

Fixture bodies here are SYNTHETIC, not derived from a real capture — unlike
tests/fixtures/rows_*.json, no raw `all-stream`/conversation response was ever
captured to `research/captures/` (only analysis artifacts under `research/results/`
exist), so there is nothing for `research/probes/redact_fixtures.py` to redact. The
field NAMES and shapes below are the measured ones (docs/104-site-facts.md §6b.7,
§6b.8, §6b.10; cross-checked against `research/results/messaging_contract.json`'s own
field-name sweep), but the VALUES are invented — same treatment
tests/test_search.py already gives `list_jobs`' success body (see that file's module
docstring, "No committed fixture exists for list_jobs' success body").

`failure_send_validation.json` is the one new fixture with a real capture behind it
(`research/results/send_validation.json`, `research/probes/probe_send_validation.py`'s
output) — see `research/probes/redact_fixtures.py`'s `build_failure_send_validation`.
"""
from __future__ import annotations

import json
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

import mcp104.tools.discovery as discovery_mod
from mcp104.browser.api_client import ENDPOINTS, RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.browser.throttle import ThrottleAbort
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.helpers import GuardAbort, get_session_id
from mcp104.tools.messaging import (
    NOT_SENT,
    _convert_inbox_row,
    _convert_message_row,
    _direction,
    _inbox_request_body,
    _more_pages_warning,
    _read_state,
    _send_body,
    _send_verdict,
    _watermark,
    register_messaging_tools,
)

FIXTURES_DIR = Path(__file__).parent / "fixtures"
RESULTS_DIR = Path(__file__).parent.parent / "research" / "results"


def _load(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _messaging_contract() -> dict:
    return json.loads((RESULTS_DIR / "messaging_contract.json").read_text(encoding="utf-8"))


# ── Synthetic inbox rows — the measured 19 field names [M §6b.7, §6b.8-1] ───────────

_INBOX_ROW_LABELLED = {
    "id": "9001", "lastMessageId": "9001", "candidateName": "測試甲",
    "eventType": 1, "eventStatus": 1, "fitnessReportStatus": 0,
    "newestEventTime": "2026-08-10 10:00:00", "content": "面試邀約已送出",
    "contentFrom": 1, "jobName": "應用工程師", "jobNo": "12355016",
    "pId": "7174595", "hid": "555000111", "idNo": "1728037773409",
    "isSemiOn": True, "newestMessageTime": "2026-08-10 10:00:00",
    "hrHasRead": False, "hrHasSubscribed": False, "type": 0,
}
_INBOX_ROW_UNLABELLED = {
    "id": "9002", "lastMessageId": "9002", "candidateName": "測試乙",
    "eventType": 2, "eventStatus": 5, "fitnessReportStatus": 0,
    "newestEventTime": None, "content": "詢問薪資",
    "contentFrom": 1, "jobName": "資深工程師", "jobNo": "12355017",
    "pId": "7174596", "hid": "555000112", "idNo": "1728037773410",
    "isSemiOn": False, "newestMessageTime": "2026-08-11 09:00:00",
    "hrHasRead": True, "hrHasSubscribed": True, "type": 0,
}
_INBOX_ROW_NO_EVENT = {
    "id": "9003", "lastMessageId": "9003", "candidateName": "測試丙",
    "eventType": 0, "eventStatus": 0, "fitnessReportStatus": 0,
    "newestEventTime": None, "content": "您好",
    "contentFrom": 1, "jobName": "測試部門", "jobNo": "12355018",
    "pId": "7174597", "hid": "555000113", "idNo": "1728037773411",
    "isSemiOn": False, "newestMessageTime": "2026-08-12 09:00:00",
    "hrHasRead": False, "hrHasSubscribed": False, "type": 0,
}
_INBOX_ROWS = [_INBOX_ROW_LABELLED, _INBOX_ROW_UNLABELLED, _INBOX_ROW_NO_EVENT]

_INBOX_ENVELOPE_SINGLE_PAGE = {
    "data": _INBOX_ROWS,
    "metadata": {"perPage": 30, "page": 1, "totalPage": 1, "total": 3},
}
_INBOX_ENVELOPE_MORE_PAGES = {
    "data": _INBOX_ROWS,
    "metadata": {"perPage": 30, "page": 1, "totalPage": 8, "total": 213},
}


# ── Synthetic conversation messages — the measured 13 field names [M §6b.7, §6b.8-3,
# §6b.10] — watermark = 9101, so sent id<=9101 is read, sent id>9101 is unread ───────

_MSG_SENT_READ = {
    "id": "9100", "userName": "操作者", "type": 0, "snapshotId": "snap-1",
    "idNo": "1728037773409", "content": "您好，方便聊聊嗎", "unsend": False,
    "ogMeta": None, "source": 0, "createdAt": "2026-08-10 09:00:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_MSG_RECEIVED = {
    "id": "9102", "userName": "測試甲", "type": 0, "snapshotId": "snap-1",
    "idNo": "1728037773409", "content": "有的", "unsend": False,
    "ogMeta": None, "source": 1, "createdAt": "2026-08-10 09:02:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_MSG_TYPE6_INVISIBLE = {
    "id": "9103", "userName": "操作者", "type": 6, "snapshotId": "snap-1",
    "idNo": "1728037773409", "content": "（系統事件，網頁版看不到）", "unsend": False,
    "ogMeta": None, "source": 0, "createdAt": "2026-08-10 09:03:00",
    "event": {}, "file": [], "isSynchronized": True,
}
_MSG_WITH_EVENT = {
    "id": "9104", "userName": "操作者", "type": 1, "snapshotId": "snap-1",
    "idNo": "1728037773409", "content": "已邀請面試", "unsend": False,
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
    "idNo": "1728037773409", "content": "有空嗎？", "unsend": False,
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
_CONVERSATION_ENVELOPE_MORE_PAGES = {
    "data": _CONVERSATION_MESSAGES,
    "metadata": {"perPage": 100, "page": 1, "totalPage": 3, "total": 320, "creadAt": 9101},
}


# ── Fixture / fake-transport plumbing (mirrors tests/test_search.py's idiom) ────────

def _raw(status: int, content_type: str, body: str, parsed_json, location: str | None = None) -> RawResponse:
    return RawResponse(status=status, location=location, content_type=content_type,
                        body=body, parsed_json=parsed_json)


def _raw_from_body(body: dict, status: int = 200) -> RawResponse:
    return _raw(status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)


def _raw_from_wrapper(fixture_name: str) -> RawResponse:
    d = _load(fixture_name)
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
    return mcp.tools, mcp.descriptions


TOOLS, TOOL_DESCRIPTIONS = _register_tools()
read_messages = TOOLS["read_messages"]
get_conversation = TOOLS["get_conversation"]
send_message = TOOLS["send_message"]


_OPENED_DATABASES: list[Database] = []


async def _new_session(tmp_path) -> tuple[FakeCtx, SessionInfo, Database]:
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init("test@104.com")
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight off SessionInfo.cookies now —
    # there is no BrowserContext left to fake post-login (§C7).
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return ctx, info, database


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


# ═════════════════════════════════════════════════════════════════════════════════
# Pure functions — no HTTP, no browser, no MCP Context
# ═════════════════════════════════════════════════════════════════════════════════

# ── _watermark / _read_state / _direction (§4) ──────────────────────────────────────

def test_watermark_reads_creadat_as_int():
    assert _watermark({"creadAt": 9101}) == 9101
    assert _watermark({"creadAt": "9101"}) == 9101


def test_watermark_none_when_metadata_missing_or_unparsable():
    assert _watermark({}) is None
    assert _watermark({"creadAt": None}) is None
    assert _watermark({"creadAt": "not-a-number"}) is None
    assert _watermark("not-a-dict") is None


def test_direction_from_source():
    assert _direction(0) == "sent"
    assert _direction(1) == "received"


def test_read_state_true_when_id_at_or_below_watermark():
    assert _read_state("9100", 9101, "sent") is True
    assert _read_state("9101", 9101, "sent") is True  # boundary: <=, not <


def test_read_state_false_when_id_above_watermark():
    assert _read_state("9105", 9101, "sent") is False


def test_read_state_none_for_received_regardless_of_watermark():
    # received messages: read-state is meaningless, must never be False
    assert _read_state("9100", 9101, "received") is None
    assert _read_state("9999999", 9101, "received") is None


def test_read_state_none_when_watermark_unparsable_or_missing():
    assert _read_state("9100", None, "sent") is None


def test_read_state_none_when_message_id_unparsable():
    assert _read_state("not-a-number", 9101, "sent") is None
    assert _read_state(None, 9101, "sent") is None


# ── _inbox_request_body (§6b.8-1) ────────────────────────────────────────────────────

def test_inbox_request_body_measured_seven_keys_with_defaults():
    body = _inbox_request_body(1, None, None)
    assert body == {
        "jobNos": [], "eventType": None, "eventStatus": None,
        "candidateName": "", "page": 1, "perPage": 30, "departmentIds": [],
    }


def test_inbox_request_body_caller_page_and_filters_land_in_it():
    body = _inbox_request_body(3, ["12355016", "12355017"], "王小明")
    assert body["page"] == 3
    assert body["jobNos"] == ["12355016", "12355017"]
    assert body["candidateName"] == "王小明"
    # Unexposed fields keep their measured defaults regardless of caller input.
    assert body["eventType"] is None
    assert body["eventStatus"] is None
    assert body["departmentIds"] == []
    assert body["perPage"] == 30


# ── _send_body (§6b.9-4) ──────────────────────────────────────────────────────────────

def test_send_body_is_exactly_content():
    assert _send_body("hello") == {"content": "hello"}
    assert set(_send_body("hello").keys()) == {"content"}


# ── _more_pages_warning (§3) ──────────────────────────────────────────────────────────

def test_more_pages_warning_fires_when_page_below_total():
    warning = _more_pages_warning({"page": 1, "total_pages": 8, "total": 213})
    assert warning is not None
    assert "8" in warning


def test_more_pages_warning_silent_when_on_last_page():
    assert _more_pages_warning({"page": 8, "total_pages": 8, "total": 213}) is None
    assert _more_pages_warning({"page": 1, "total_pages": 1, "total": 3}) is None


def test_more_pages_warning_silent_when_pagination_unusable():
    assert _more_pages_warning({"page": None, "total_pages": None, "total": None}) is None


def test_more_pages_warning_extra_only_appended_when_it_fires():
    warning = _more_pages_warning({"page": 1, "total_pages": 3, "total": 320}, extra="EXTRA-MARKER")
    assert warning is not None and "EXTRA-MARKER" in warning
    silent = _more_pages_warning({"page": 3, "total_pages": 3, "total": 320}, extra="EXTRA-MARKER")
    assert silent is None


# ── _event_labels (§5) — imported via tools.discovery, the table's real home ────────

def test_event_labels_pair_1_1_and_2_1_side_by_side():
    # The whole reason the table is pair-keyed: status=1 means a different word
    # under each eventType. A status-keyed implementation would pass every other
    # case here but fail this one.
    assert discovery_mod._event_labels(1, 1) == ("面試同意", "positive")
    assert discovery_mod._event_labels(2, 1) == ("有意願", "positive")


def test_event_labels_all_four_measured_pairs():
    assert discovery_mod._event_labels(2, 2) == ("無意願", "negative")
    assert discovery_mod._event_labels(2, 3) == ("未回覆", "neutral")


def test_event_labels_event_type_zero_yields_no_label_not_neutral():
    assert discovery_mod._event_labels(0, 0) == (None, None)
    assert discovery_mod._event_labels(None, None) == (None, None)


def test_event_labels_unknown_pair_returns_raw_code_and_still_a_row():
    progress, polarity = discovery_mod._event_labels(2, 5)
    assert progress is not None
    assert "2" in progress and "5" in progress
    assert polarity is None


def test_event_labels_pair_absent_from_table_behaves_same_as_unlabelled():
    # A pair EVENT_LABELS has never seen at all (not just an unlabelled status on a
    # known eventType) must behave the same way — explicit marker, not silently
    # dropped or guessed.
    progress, polarity = discovery_mod._event_labels(99, 99)
    assert progress is not None and polarity is None


# ── _convert_inbox_row (§5) ───────────────────────────────────────────────────────────

def test_convert_inbox_row_renames_job_no_and_p_id():
    row = _convert_inbox_row(_INBOX_ROW_LABELLED)
    assert row["job_id"] == "12355016"
    assert row["candidate_id"] == "7174595"
    assert "job_no" not in row
    assert "p_id" not in row


def test_convert_inbox_row_excludes_idno_and_hid_entirely():
    row = _convert_inbox_row(_INBOX_ROW_LABELLED)
    assert "id_no" not in row
    assert "hid" not in row
    # The raw values must not leak under any other key either.
    text = json.dumps(row, ensure_ascii=False)
    assert _INBOX_ROW_LABELLED["idNo"] not in text
    assert _INBOX_ROW_LABELLED["hid"] not in text


def test_convert_inbox_row_derives_event_progress_and_polarity():
    row = _convert_inbox_row(_INBOX_ROW_LABELLED)
    assert row["event_progress"] == "面試同意"
    assert row["event_polarity"] == "positive"


def test_convert_inbox_row_no_event_yields_null_labels():
    row = _convert_inbox_row(_INBOX_ROW_NO_EVENT)
    assert row["event_progress"] is None
    assert row["event_polarity"] is None


def test_inbox_row_gloss_matches_the_independently_swept_field_names():
    # Round I1 Smell H: the old version of this check compared the fixture
    # row (hand-typed in this file) against INBOX_ROW_FIELD_GLOSS (also
    # hand-typed, in discovery.py) — two adjacent hand-typed things checked
    # against each other cannot fail unless someone edits both inconsistently
    # in the same commit. research/results/messaging_contract.json is a
    # COMMITTED, independently-produced sweep of the API's own row field
    # names (inbox_id_mapping.row_field_names) — discovery.py's own docstring
    # claims the allow-list was derived from exactly this sweep; this is the
    # test that actually checks that claim.
    swept = set(_messaging_contract()["inbox_id_mapping"]["row_field_names"])
    assert swept, "sanity: the sweep itself must be non-empty"
    assert set(discovery_mod.INBOX_ROW_FIELD_GLOSS) == swept, (
        f"INBOX_ROW_FIELD_GLOSS has drifted from the independently swept field "
        f"names: missing={swept - set(discovery_mod.INBOX_ROW_FIELD_GLOSS)!r} "
        f"extra={set(discovery_mod.INBOX_ROW_FIELD_GLOSS) - swept!r}"
    )


def test_synthetic_inbox_row_keys_match_the_independently_swept_field_names():
    # The fixture ROW's key set, checked against the same independent
    # source — not against the gloss dict a moment ago, which would still be
    # two hand-typed things agreeing with each other rather than either one
    # being checked against ground truth.
    swept = set(_messaging_contract()["inbox_id_mapping"]["row_field_names"])
    assert set(_INBOX_ROW_LABELLED) == swept, (
        f"the synthetic fixture row's keys have drifted from the independently "
        f"swept field names: missing={swept - set(_INBOX_ROW_LABELLED)!r} "
        f"extra={set(_INBOX_ROW_LABELLED) - swept!r}"
    )


def test_convert_inbox_row_every_measured_field_is_glossed_or_excluded():
    for raw_key in _INBOX_ROW_LABELLED:
        assert (
            raw_key in discovery_mod.INBOX_ROW_FIELD_GLOSS
        ), f"{raw_key!r} on a fixture row is neither glossed nor explicitly excluded"


# ── _convert_message_row (§4, §5) ─────────────────────────────────────────────────────

def test_convert_message_row_derives_direction_and_excludes_source():
    row = _convert_message_row(_MSG_SENT_READ, watermark=9101)
    assert row["direction"] == "sent"
    assert "source" not in row


def test_convert_message_row_read_state_matches_watermark():
    assert _convert_message_row(_MSG_SENT_READ, watermark=9101)["read"] is True
    assert _convert_message_row(_MSG_SENT_UNREAD, watermark=9101)["read"] is False
    assert _convert_message_row(_MSG_RECEIVED, watermark=9101)["read"] is None


def test_convert_message_row_type_6_message_is_still_delivered():
    # The DOM-era tool could not see type:6 messages at all (§6b.10-4) — the API
    # path must deliver every row regardless of type.
    row = _convert_message_row(_MSG_TYPE6_INVISIBLE, watermark=9101)
    assert row["type"] == 6
    assert row["content"] == _MSG_TYPE6_INVISIBLE["content"]


def test_convert_message_row_event_object_survives_with_every_key_and_nesting():
    row = _convert_message_row(_MSG_WITH_EVENT, watermark=9101)
    assert row["event"]["event_id"] == "evt-1"
    assert row["event"]["contact_info"]["meeting_time"] == "2026-08-15 14:00"
    assert row["event"]["contact_info"]["location"] == "台北市信義區某大樓"
    assert len(row["event"]) == len(_MSG_WITH_EVENT["event"])


def test_convert_message_row_empty_event_object_passes_through():
    row = _convert_message_row(_MSG_SENT_READ, watermark=9101)
    assert row["event"] == {}


def test_convert_message_row_excludes_idno_snapshotid_username():
    row = _convert_message_row(_MSG_SENT_READ, watermark=9101)
    assert "id_no" not in row
    assert "snapshot_id" not in row
    assert "user_name" not in row
    text = json.dumps(row, ensure_ascii=False)
    assert _MSG_SENT_READ["userName"] not in text


def test_message_row_gloss_matches_the_independently_swept_field_names():
    # Same fix as the inbox row's twin above, against
    # conversation_semantics.api_message_field_names.
    swept = set(_messaging_contract()["conversation_semantics"]["api_message_field_names"])
    assert swept, "sanity: the sweep itself must be non-empty"
    assert set(discovery_mod.MESSAGE_ROW_FIELD_GLOSS) == swept, (
        f"MESSAGE_ROW_FIELD_GLOSS has drifted from the independently swept field "
        f"names: missing={swept - set(discovery_mod.MESSAGE_ROW_FIELD_GLOSS)!r} "
        f"extra={set(discovery_mod.MESSAGE_ROW_FIELD_GLOSS) - swept!r}"
    )


def test_synthetic_message_row_keys_match_the_independently_swept_field_names():
    swept = set(_messaging_contract()["conversation_semantics"]["api_message_field_names"])
    assert set(_MSG_WITH_EVENT) == swept, (
        f"the synthetic fixture row's keys have drifted from the independently "
        f"swept field names: missing={swept - set(_MSG_WITH_EVENT)!r} "
        f"extra={set(_MSG_WITH_EVENT) - swept!r}"
    )


def test_convert_message_row_every_measured_field_is_glossed_or_excluded():
    for raw_key in _MSG_WITH_EVENT:
        assert (
            raw_key in discovery_mod.MESSAGE_ROW_FIELD_GLOSS
        ), f"{raw_key!r} on a fixture row is neither glossed nor explicitly excluded"


# ── _send_verdict / NOT_SENT (§6) ─────────────────────────────────────────────────────

def test_send_verdict_maps_every_documented_not_sent_kind():
    # Round I1 Low K: iterating NOT_SENT and checking _send_verdict against
    # its own members cannot fail no matter what the set contains — it is
    # the constant testing itself. The kinds are listed as independent
    # literals here (transcribed from §6b's table, not read off NOT_SENT),
    # so removing a member from NOT_SENT — or _send_verdict silently
    # stopping treating one of them as not-sent — actually turns this red.
    for kind in (
        "not_logged_in", "throttled", "daily_cap", "internal_config",
        "empty_content", "expired", "blocked", "challenge", "not_found",
        "validation", "wrong_host", "header_fault", "missing_param",
    ):
        assert _send_verdict(kind) == "not_sent", f"{kind!r} must be NOT SENT"
    # And the converse, over the actual constant: nothing in NOT_SENT may
    # ever be classified as ambiguous by the function that consumes it.
    for kind in NOT_SENT:
        assert _send_verdict(kind) == "not_sent"


def test_send_verdict_unknown_kind_defaults_to_ambiguous():
    # The whitelist's open side — a synthesised kind, never a real one, so this
    # would go red the day a kind is added rather than silently staying green.
    assert _send_verdict("totally-novel-kind-never-seen-9f3e") == "ambiguous"


def test_send_verdict_known_ambiguous_kinds():
    for kind in ("malformed", "unrecognised_status", "non_json", "transport"):
        assert _send_verdict(kind) == "ambiguous"


def test_guard_abort_cannot_be_constructed_without_kind():
    with pytest.raises(TypeError):
        GuardAbort({"error": "x"})


# ═════════════════════════════════════════════════════════════════════════════════
# Tool-level (guarded_api driven) — read_messages / get_conversation
# ═════════════════════════════════════════════════════════════════════════════════

@pytest.mark.asyncio
async def test_read_messages_returns_documented_success_shape(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_INBOX_ENVELOPE_SINGLE_PAGE))

    result = await read_messages(ctx=ctx)

    assert "error" not in result
    assert result["pagination"] == {"page": 1, "total_pages": 1, "total": 3}
    assert result["browse_limit"] is None
    assert result["warnings"] == []
    assert len(result["results"]) == 3
    assert result["results"][0]["candidate_id"] == "7174595"


@pytest.mark.asyncio
async def test_read_messages_body_carries_page_and_filters(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_fake_fetch(monkeypatch, _raw_from_body(_INBOX_ENVELOPE_SINGLE_PAGE))

    await read_messages(ctx=ctx, page=2, job_nos=["12355016"], candidate_name="王")

    assert len(spy.calls) == 1
    _endpoint, _params, body = spy.calls[0]
    assert body["page"] == 2
    assert body["jobNos"] == ["12355016"]
    assert body["candidateName"] == "王"
    assert body["perPage"] == 30


@pytest.mark.asyncio
async def test_read_messages_more_pages_produces_warning(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_INBOX_ENVELOPE_MORE_PAGES))

    result = await read_messages(ctx=ctx)

    assert "error" not in result
    assert len(result["warnings"]) == 1


@pytest.mark.asyncio
async def test_read_messages_missing_metadata_is_malformed_not_empty_result(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = {"data": []}  # metadata absent entirely
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await read_messages(ctx=ctx)

    assert "error" in result
    assert "results" not in result
    assert "104 回應結構異常" in result["error"]


@pytest.mark.asyncio
async def test_get_conversation_returns_documented_success_shape(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_CONVERSATION_ENVELOPE_SINGLE_PAGE))

    result = await get_conversation(ctx=ctx, job_id="12355016", candidate_id="7174595")

    assert "error" not in result
    assert result["browse_limit"] is None
    assert len(result["results"]) == 5
    # Same shape as read_messages — a caller never branches on which tool
    # produced the payload.
    assert set(result.keys()) == {"results", "pagination", "browse_limit", "warnings"}


@pytest.mark.asyncio
async def test_get_conversation_more_pages_warning_names_the_last_page(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_CONVERSATION_ENVELOPE_MORE_PAGES))

    result = await get_conversation(ctx=ctx, job_id="12355016", candidate_id="7174595")

    assert "error" not in result
    assert len(result["warnings"]) == 1
    assert "3" in result["warnings"][0]  # names page 3, the last page


@pytest.mark.asyncio
async def test_get_conversation_watermark_reproduces_read_state(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_CONVERSATION_ENVELOPE_SINGLE_PAGE))

    result = await get_conversation(ctx=ctx, job_id="12355016", candidate_id="7174595")

    by_id = {}
    for msg, raw in zip(result["results"], _CONVERSATION_MESSAGES):
        by_id[raw["id"]] = msg
    assert by_id["9100"]["read"] is True
    assert by_id["9105"]["read"] is False
    assert by_id["9102"]["read"] is None  # received


@pytest.mark.asyncio
async def test_get_conversation_type_6_message_included_in_delivered_results(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_CONVERSATION_ENVELOPE_SINGLE_PAGE))

    result = await get_conversation(ctx=ctx, job_id="12355016", candidate_id="7174595")

    assert len(result["results"]) == len(_CONVERSATION_MESSAGES) == 5
    type_6_rows = [r for r in result["results"] if r["type"] == 6]
    assert len(type_6_rows) == 1


@pytest.mark.asyncio
async def test_get_conversation_missing_metadata_is_malformed(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body({"data": []}))

    result = await get_conversation(ctx=ctx, job_id="12355016", candidate_id="7174595")

    assert "error" in result
    assert "results" not in result


# ═════════════════════════════════════════════════════════════════════════════════
# send_message — the taxonomy (§6), this round's largest behavioural surface
# ═════════════════════════════════════════════════════════════════════════════════

_SEND_SUCCESS_BODY = {"data": [{"messageId": ["9999"]}], "metadata": None}


@pytest.mark.asyncio
async def test_send_message_body_is_exactly_content(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_fake_fetch(monkeypatch, _raw_from_body(_SEND_SUCCESS_BODY))

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    assert len(spy.calls) == 1
    _endpoint, _params, body = spy.calls[0]
    assert body == {"content": "您好"}


@pytest.mark.asyncio
async def test_send_message_successful_call_writes_exactly_one_sent_log_row(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_SEND_SUCCESS_BODY))

    await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    count = await db.get_daily_sent_count(info.account_label)
    assert count == 1


@pytest.mark.asyncio
async def test_send_message_empty_message_rejected_before_any_request(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="   ")

    # Round I1 Low M: comparing against a dict built FROM result["error"]
    # itself can never fail on the error half, whatever its value is — the
    # real claims are the exact key set and a non-empty, human-readable
    # message naming the actual reason (empty content).
    assert set(result) == {"success", "error"}
    assert result["success"] is False
    assert isinstance(result["error"], str) and result["error"]
    assert "空白" in result["error"] or "空" in result["error"]


@pytest.mark.asyncio
async def test_send_message_daily_cap_rejected_before_any_request(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    app = ctx.request_context.lifespan_context
    for _ in range(app.config.max_daily_messages):
        await db.log_sent(info.account_label, "some-candidate", "message")
    _install_never_called_fetch(monkeypatch)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["success"] is False
    assert "上限" in result["error"]
    count = await db.get_daily_sent_count(info.account_label)
    assert count == app.config.max_daily_messages  # unchanged — no new row


@pytest.mark.asyncio
async def test_send_message_throttled_rejection_carries_retry_after_seconds_and_does_not_log(tmp_path, monkeypatch):
    # Round I1 Bug D: shape 3 ({"error": str}) is not always "104 refused" —
    # a throttle rejection is OUR OWN precondition (no request ever reaches
    # 104) and carries a fourth key, retry_after_seconds, the docstring must
    # name. Not sent either way, so no sent_log row.
    ctx, info, db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    # enforce_throttle's contract (§C10): a rejection is a ThrottleAbort, not
    # a bare dict — guarded_api reads .kind/.payload off it directly.
    async def rejecting_throttle(*args, **kwargs):
        return ThrottleAbort(
            kind="throttled",
            payload={"error": "節流測試", "retry_after_seconds": 5},
            detail="",
        )

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", rejecting_throttle)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert "error" in result
    assert result.get("retry_after_seconds") == 5
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0


# ── NOT SENT: driven end to end through the real classify()/guarded_api/send_message,
# never by hand-constructing a GuardAbort with a literal kind ───────────────────────

@pytest.mark.asyncio
async def test_send_message_expired_session_returns_error_payload_and_does_not_log(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_wrapper("failure_family_a_expired.json"))

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert "error" in result
    assert "已過期" in result["error"]
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_send_message_auth_host_redirect_returns_error_not_unconfirmed_and_does_not_log(tmp_path, monkeypatch):
    # Round I1 Bug A: the specific site helpers.py's guarded_api singles out
    # as needing kind="expired" rather than the "transport" kind raised 30
    # lines earlier for a plain fetch() exception — a 3xx response whose
    # Location points at an auth host, read from fetch()'s own RawResponse
    # rather than reconstructed from classify()'s body/EXPIRY_MARKER check
    # (a different code path from the existing
    # test_send_message_expired_session_returns_error_payload_and_does_not_log
    # case above). Getting this wrong (kind="transport") would make this
    # ordinary mid-batch expiry report {"sent": "unconfirmed"}, write a
    # sent_log row, and tell the Agent not to resend a message 104 never saw.
    ctx, info, db = await _new_session(tmp_path)

    async def redirecting_fetch(endpoint, *, cookie_header, params=None, body=None):
        return RawResponse(
            status=302,
            location="https://bsignin.104.com.tw/login",
            content_type="text/html; charset=utf-8",
            body="",
            parsed_json=None,
        )

    monkeypatch.setattr("mcp104.browser.api_client.fetch", redirecting_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", redirecting_fetch, raising=False)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert "error" in result
    assert "已過期" in result["error"]
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_send_message_blocked_403_returns_error_payload_and_does_not_log(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw(403, "text/html; charset=utf-8", "blocked", None))

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert "error" in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_send_message_cloudflare_challenge_returns_stop_and_wait_and_does_not_log(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    challenge_body = "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。"
    _install_fake_fetch(monkeypatch, _raw(200, "text/html; charset=utf-8", challenge_body, None))

    result = await send_message(ctx=ctx, job_id="0", candidate_id="0", message="您好")

    assert "error" in result
    assert "暫停操作並稍後再試" in result["error"]
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_send_message_404_thread_returns_error_payload_and_does_not_log(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body({"error": "找不到這個對話"}, status=404))

    result = await send_message(ctx=ctx, job_id="0", candidate_id="0", message="您好")

    assert "error" in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_send_message_validation_400_surfaces_104s_own_text_and_does_not_log(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_wrapper("failure_send_validation.json"))

    result = await send_message(ctx=ctx, job_id="0", candidate_id="0", message="您好")

    assert "error" in result
    assert "content" in result["error"] and "required" in result["error"].lower()
    assert "缺少必要參數" not in result["error"]
    assert await db.get_daily_sent_count(info.account_label) == 0


# ── AMBIGUOUS: request went out, response not understood ────────────────────────────

@pytest.mark.asyncio
async def test_send_message_malformed_body_reports_unconfirmed_and_logs_one_row(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body({"data": "not-a-list"}))

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1


@pytest.mark.asyncio
async def test_send_message_non_json_response_reports_unconfirmed_and_logs_one_row(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw(200, "text/plain", "not json at all", None))

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1


@pytest.mark.asyncio
async def test_send_message_transport_failure_reports_unconfirmed_and_logs_one_row(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)

    async def failing_fetch(endpoint, *, cookie_header, params=None, body=None):
        raise TimeoutError("simulated network timeout")

    monkeypatch.setattr("mcp104.browser.api_client.fetch", failing_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", failing_fetch, raising=False)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1


# ── The whitelist's open side, end to end: an unnamed status code lands on
# unrecognised_status, which is on the ambiguous side ────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_novel_http_status_defaults_to_unconfirmed_and_logs(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body({"error": "surprise"}, status=418))

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1


# ── A sent_log write that raises does not change either verdict ─────────────────────

@pytest.mark.asyncio
async def test_send_message_log_sent_failure_does_not_change_success_verdict(tmp_path, monkeypatch):
    ctx, _info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(_SEND_SUCCESS_BODY))

    async def failing_log_sent(*args, **kwargs):
        raise RuntimeError("database is locked (simulated)")

    monkeypatch.setattr(db, "log_sent", failing_log_sent)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"


@pytest.mark.asyncio
async def test_send_message_log_sent_failure_does_not_change_ambiguous_verdict(tmp_path, monkeypatch):
    ctx, _info, db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw(200, "text/plain", "not json", None))

    async def failing_log_sent(*args, **kwargs):
        raise RuntimeError("database is locked (simulated)")

    monkeypatch.setattr(db, "log_sent", failing_log_sent)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"


# ── The hook_completed discriminator (§6c/§6d): a non-GuardAbort exception raised
# AFTER the hook returns unconfirmed (and logs); raised BEFORE returns not-sent (and
# does not log) — even when account_label is empty, which an address-keyed
# discriminator would get backwards ─────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_send_message_exception_after_hook_reports_unconfirmed_and_logs(tmp_path, monkeypatch):
    # select_cookies_for_host runs AFTER before_request in guarded_api, ahead of
    # fetch() — raising there simulates a non-GuardAbort exception with the hook
    # already completed. (Post-§C7 there is no BrowserContext.cookies() call left
    # to fault-inject against; credentials are a plain SessionInfo.cookies list
    # that cannot itself raise, so the seam moves to the next call that consumes
    # it.)
    ctx, info, db = await _new_session(tmp_path)

    def failing_select_cookies(*args, **kwargs):
        raise RuntimeError("simulated failure reading cookies for this host")

    monkeypatch.setattr("mcp104.tools.helpers.select_cookies_for_host", failing_select_cookies)
    _install_never_called_fetch(monkeypatch)  # must never reach fetch()

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    assert await db.get_daily_sent_count(info.account_label) == 1


@pytest.mark.asyncio
async def test_send_message_exception_before_hook_reports_not_sent_and_does_not_log(tmp_path, monkeypatch):
    ctx, info, db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    async def failing_throttle(*args, **kwargs):
        raise RuntimeError("simulated failure before the hook ever runs")

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", failing_throttle)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert "error" in result
    assert "sent" not in result
    assert await db.get_daily_sent_count(info.account_label) == 0


@pytest.mark.asyncio
async def test_send_message_exception_after_hook_reports_unconfirmed_even_with_empty_account_label(tmp_path, monkeypatch):
    # Pins the reason hook_completed is its OWN flag rather than inferred from
    # info.account_label: an address-keyed discriminator would read an empty
    # address as "the hook never ran" and wrongly report not-sent here.
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init("test@104.com")
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)
    sid = get_session_id(ctx)
    info = SessionInfo(cookies=[], account_label="")
    pool.activate_direct(sid, info)

    def failing_select_cookies(*args, **kwargs):
        raise RuntimeError("simulated failure reading cookies for this host")

    monkeypatch.setattr("mcp104.tools.helpers.select_cookies_for_host", failing_select_cookies)
    _install_never_called_fetch(monkeypatch)

    result = await send_message(ctx=ctx, job_id="12355016", candidate_id="7174595", message="您好")

    assert result["sent"] == "unconfirmed"
    count = await database.get_daily_sent_count("")
    assert count == 1


# ═════════════════════════════════════════════════════════════════════════════════
# Repo sweeps (§7) — the claims whose falsifier is invisible to any test that reads
# a single file
# ═════════════════════════════════════════════════════════════════════════════════

SRC_ROOT = Path(__file__).parent.parent / "src" / "mcp104"


def _all_src_files():
    return list(SRC_ROOT.rglob("*.py"))


def test_messaging_module_touches_no_browser_page_or_context():
    # Successor to the old test_guarded_page_appears_nowhere_except_its_own_
    # definition: guarded_page itself is removed this cycle (§C7), so a scan
    # for the STRING "guarded_page(" would go silent the moment its subject
    # stopped existing anywhere — this file included — and keep passing
    # forever with nothing left to catch. The property actually worth
    # protecting here — a narrower, permanently-true slice of the repo-wide
    # claim T-113 owns (tests/test_stealth.py / tests/test_main.py; not
    # duplicated here) — is specific to this module: tools/messaging.py
    # migrated fully to the JSON API this cycle (module docstring, top of
    # this file) and must never re-acquire a page/BrowserContext reference,
    # whether via a direct Playwright import or a call into the retired
    # guarded_page.
    import mcp104.tools.messaging as messaging_mod

    text = Path(messaging_mod.__file__).read_text(encoding="utf-8")
    forbidden_markers = ("guarded_page(", "BrowserContext", "import patchright", "from patchright", ".goto(")
    hits = [m for m in forbidden_markers if m in text]
    assert not hits, f"tools/messaging.py references browser-page machinery: {hits}"


def test_acknowledgement_appears_exactly_once_under_src():
    hits = []
    for path in _all_src_files():
        text = path.read_text(encoding="utf-8")
        count = text.count("acknowledgement")
        if count:
            hits.append((str(path), count))
    total = sum(c for _p, c in hits)
    assert total == 1, f"expected exactly one 'acknowledgement' occurrence under src/, found {hits}"


def test_no_dialog_detail_or_message_item_or_msgmaster_selectors_remain():
    forbidden = ("dialog-detail", "message-item", "msgMaster")
    hits = []
    for path in _all_src_files():
        text = path.read_text(encoding="utf-8")
        for needle in forbidden:
            if needle in text:
                hits.append((str(path), needle))
    assert not hits, f"stale DOM-scraping selector reference(s) found: {hits}"


def test_no_endpoint_declares_a_method_outside_get_or_post():
    for key, ep in ENDPOINTS.items():
        assert ep.method in {"GET", "POST"}, f"{key}: unexpected method {ep.method!r}"


# Round I1 Smell B: this file used to carry its own
# test_endpoint_construction_with_delete_method_raises, but it passed
# family_b_shape=None alongside family="B" — the ValueError it caught could
# come from EITHER the missing-shape check OR the method whitelist, so it
# would have stayed green even with the method whitelist deleted outright.
# The correct twin, which supplies a valid FamilyBShape so the failure can
# only be attributed to the method check, lives in
# test_api_client.py::test_endpoint_construction_rejects_a_method_outside_get_or_post
# — kept there rather than duplicated here.
