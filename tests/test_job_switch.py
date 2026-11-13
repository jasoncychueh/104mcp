"""Tests for the job-switch write tools close_jobs, open_jobs and delete_job.

Cases in this file:
  T-1 .. T-24, T-26 .. T-54, T-62, T-63, T-65 .. T-74, T-76, T-77
(There is no T-25.) T-55 .. T-61, T-64, T-75 and T-78 are in tests/test_api_client.py,
tests/test_helpers.py and tests/test_jobs.py.

Blindness: assertions come from the public interfaces only, not from the implementation. The three tools are reached
through register_job_switch_tools, the pure helpers through module attributes, and every 104
request goes through the real guard, the real classifier and the real throttle gate. Only the
transport (fetch) is replaced, by the scripted _SeqFetchSpy from tests/conftest.py. delete_job's
published inputSchema is read from a real FastMCP server.

Synthetic data: no body in this file was read from tests/fixtures/ or from a live 104 response.
The jobList result follows the measured key set (docs/104-site-facts.md §8.31). The
bodies of /api/job/more and of the company identity follow the key sets measured by the read-only
probe of 2026-11-13 (docs/104-site-facts.md §8.32); their values are synthetic. The other write routes
(close pre-check, close, refund-points, openJob, jobDelete) and their responses are synthesised from
the front-end code shape; nothing about them has been measured, so the
assertions hold only to the extent the tools' public contract specifies. The ithp cookies are synthetic JWT-shaped
strings.
"""

import base64
import copy
import importlib
import json
import logging
import re
from uuid import uuid4

import pytest
import pytest_asyncio

from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.helpers import get_session_id
from tests.conftest import _SeqFetchSpy


CHALLENGE_TEXT = "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。"

J1, J2, J3 = "1000001", "1000002", "1000003"
TOOLS = ["close_jobs", "open_jobs"]

JOB_LIST = ("GET", "/api/job/jobList")
PRECHECK = ("POST", "/vip-job/close/pre-check")
PRECHECK_GET = ("GET", "/job-api/jobs/close/pre-check")
CLOSE = ("POST", "/job-api/jobs/close")
REFUND = ("GET", "/commodity-job-sort/refund-points/jobs")
OPEN = ("POST", "/api/job/openJob")

MEASURED_PERMIT_KEYS = ("job__1", "job__2", "job__3", "job__5", "job__10", "job__11",
                        "job__18", "job__19", "job__23", "job__27")
OPERATOR = {
    "email": "SYNTH-OPERATOR@example.invalid",
    "loginNo": "SYNTH-OPERATOR-LOGIN",
    "nccPayload": {"web": "vip_new", "ext": {"Buserid": "SYNTH-OPERATOR-BUSERID", "custno": "1"}},
}
OPERATOR_STRINGS = ("SYNTH-OPERATOR@example.invalid", "SYNTH-OPERATOR-LOGIN", "SYNTH-OPERATOR-BUSERID")
SESSION_COOKIES = [
    {"name": "its", "value": "SYNTH-COOKIE-ITS-VALUE", "domain": "vip.104.com.tw", "path": "/"},
    {"name": "ithp", "value": "SYNTH-COOKIE-ITHP-VALUE", "domain": "auth.vip.104.com.tw", "path": "/"},
]
COOKIE_STRINGS = ("SYNTH-COOKIE-ITS-VALUE", "SYNTH-COOKIE-ITHP-VALUE")
NO_PERMISSION = "此功能您尚無權限使用"
FOCUS_ROW = {"jobNo": J1, "jobName": "SYNTH-FOCUS-JOB", "department": "SYNTH-DEPT",
             "startDate": "2026/11/01", "endDate": "2026/12/01"}
AD_ROW = {"jobNo": J1, "jobName": "SYNTH-AD-JOB", "refundPoint": 1}

CLOSE_SUCCESS_KEYS = {"results", "ad_pause", "message", "warnings"}
OPEN_SUCCESS_KEYS = {"results", "refusal", "message", "warnings"}
CLOSE_ERROR_KEYS = [{"error"}, {"error", "retry_after_seconds"}, {"error", "add_ons"}]
OPEN_ERROR_KEYS = CLOSE_ERROR_KEYS + [{"error", "refusal"}]
FORBIDDEN_SOURCE_WORDS = ("帳號持有人", "使用者說", "持有人說")


# ── Fixture / fake-transport plumbing (mirrors tests/test_jobs.py) ──────────────


def _raw(status, content_type, body, parsed_json, location=None) -> RawResponse:
    return RawResponse(status=status, location=location, content_type=content_type,
                       body=body, body_bytes=body.encode("utf-8"), parsed_json=parsed_json)


def _json_raw(status, obj) -> RawResponse:
    text = json.dumps(obj, ensure_ascii=False)
    return _raw(status, "application/json; charset=utf-8", text, obj)


def _challenge_raw() -> RawResponse:
    return _raw(200, "text/html; charset=utf-8", CHALLENGE_TEXT, None)


def _non_json_raw() -> RawResponse:
    return _raw(200, "text/html; charset=utf-8", "<html>SYNTH-NON-JSON</html>", None)


def _expired_raw() -> RawResponse:
    body = {"status": "COMPANY_SWITCH", "message": "SYNTH-EXPIRED",
            "url": "https://vip.104.com.tw/company/status/switchCompany"}
    return _json_raw(200, body)


def _forbidden_raw() -> RawResponse:
    return _json_raw(403, {"status": "ACCESS_DENIED", "message": "SYNTH-403"})


def _server_error_raw() -> RawResponse:
    return _json_raw(500, {"status": "SYNTH-ERROR", "message": "SYNTH-500"})


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


async def _new_session(tmp_path, *, cookies=None):
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = _FakeCtx(pool, database)
    info = SessionInfo(cookies=list(cookies or []), account_label="test@104.com")
    pool.activate_direct(get_session_id(ctx), info)
    return ctx, info


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED_DATABASES:
        await _OPENED_DATABASES.pop().close()


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """The pacing floor's inline sleep is neutralised; pacing is tests/test_throttle.py's job."""
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


class _FakeMCP:
    """Records each tool under its name and its effective description."""

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


def _js():
    return importlib.import_module("mcp104.tools.job_switch")


def _jobs():
    return importlib.import_module("mcp104.tools.jobs")


def _tool(name: str):
    mcp = _FakeMCP()
    _js().register_job_switch_tools(mcp)
    return mcp.tools[name]


async def _run(tmp_path, monkeypatch, tool_name, script, *, cookies=None, **kwargs):
    ctx, info = await _new_session(tmp_path, cookies=cookies)
    spy = _install(monkeypatch, script)
    result = await _tool(tool_name)(ctx=ctx, **kwargs)
    return result, spy, info


def _kw(tool, jobnos, **extra):
    """Arguments for a write call. close_jobs gets one reason and the pausing flag set,
    so tests that close the last open job need not repeat them."""
    kwargs = {"jobnos": jobnos}
    if tool == "close_jobs":
        kwargs.update({"reasons": [1], "allow_pausing_company_ads": True})
    kwargs.update(extra)
    return kwargs


# ── Request inspection ──────────────────────────────────────────────────────────


def _routes(spy):
    return [(endpoint.method, endpoint.path) for endpoint, _params, _body in spy.calls]


def _calls_to(spy, route):
    return [(params, body) for endpoint, params, body in spy.calls
            if (endpoint.method, endpoint.path) == route]


def _bodies(spy, route):
    return [body for _params, body in _calls_to(spy, route)]


def _params(spy, route):
    return [_pairs(params) for params, _body in _calls_to(spy, route)]


def _pairs(value):
    if value is None:
        return []
    items = value.items() if isinstance(value, dict) else value
    return [(str(k), str(v)) for k, v in items]


def _strings(obj):
    out = []
    if isinstance(obj, str):
        out.append(obj)
    elif isinstance(obj, dict):
        for key, value in obj.items():
            out.append(str(key))
            out.extend(_strings(value))
    elif isinstance(obj, (list, tuple)):
        for value in obj:
            out.extend(_strings(value))
    return out


def _is_error(result) -> bool:
    return "error" in result and "results" not in result


def _result_by(result, key):
    return {row["jobno"]: row[key] for row in result["results"]}


def _warnings(result):
    return result.get("warnings", [])


# ── Synthetic bodies ────────────────────────────────────────────────────────────


def _permit(changes=None):
    """Every measured permit key allowed. A change maps a key to a bool, or to None to drop it."""
    permit = {key: {"result": True, "message": ""} for key in MEASURED_PERMIT_KEYS}
    for key, value in (changes or {}).items():
        if value is None:
            permit.pop(key, None)
        else:
            permit[key] = {"result": value, "message": ""}
    return permit


def _row(jobno, *, switch, in_verify=0, can_delete=True):
    return {"jobno": jobno, "job": f"SYNTH-JOB-{jobno}", "switch": switch,
            "canDelete": can_delete, "isInVerify": in_verify,
            "job_setting": "SYNTH-SETTING", "role": "1", "custno": "1"}


def _on(jobno, **kw):
    return _row(jobno, switch="on", **kw)


def _off(jobno, **kw):
    return _row(jobno, switch="off", **kw)


def _listed(tool, jobno, **kw):
    """The row state a write expects before it is sent."""
    return _on(jobno, **kw) if tool == "close_jobs" else _off(jobno, **kw)


def _job_list(rows, *, account=None, omit=(), total_page=1):
    """The jobList result (inner object), measured key set, operator identity included."""
    fields = {"permit": _permit(), "coStatus": "on", "isHunter": False, "isDispatch": False,
              "isNewModel": False, "poNum": 0, "onJobCnt": 13, "isWeb": False}
    fields.update(account or {})
    for key in omit:
        fields.pop(key, None)
    result = {"jobDataAry": rows,
              "pageInfo": {"curPage": "1", "total_page": total_page, "total": len(rows)}}
    result.update(copy.deepcopy(OPERATOR))
    result.update(fields)
    return result


def _envelope(result):
    return {"status": "SUCCESS", "message": "", "url": "", "data": None, "result": result}


def _list_ok(rows, **kw):
    return _json_raw(200, _envelope(_job_list(rows, **kw)))


def _list_failure(name):
    if name == "expired":
        return _expired_raw()
    if name == "challenge":
        return _challenge_raw()
    return _json_raw(200, _envelope({**_job_list([]), "jobDataAry": "SYNTH-NOT-A-LIST"}))


def _precheck(**categories):
    data = {"focus": [], "pickUp": [], "priority": [], "jobSort": [], "hunterJobPoint": []}
    data.update(categories)
    return _json_raw(200, {"data": data, "metadata": {}})


def _close_ok(metadata=None):
    return _json_raw(200, {"data": True, "metadata": {} if metadata is None else metadata})


def _close_error(status, code, message):
    return _json_raw(status, {"error": {"code": code, "message": message, "details": {}}})


def _refund_ok(rows=()):
    return _json_raw(200, {"data": list(rows), "metadata": {}})


def _open_env(status="SUCCESS", message="openAll", result=None, code=None):
    body = {"status": status, "message": message, "url": "", "data": None, "result": result}
    if code is not None:
        body["code"] = code
    return body


def _open_ok(**kw):
    return _json_raw(200, _open_env(**kw))


def _pause_meta(**changes):
    meta = {"lastJob": True, "noPauseDays": False,
            "pauseStartdate": "SYNTH-PAUSE-START", "pauseEnddate": "SYNTH-PAUSE-END",
            "adOpenDate": "SYNTH-AD-OPEN", "setAdPause": True, "returnExtra": False,
            "adPeriod": {"remainingDays": 5}}
    meta.update(changes)
    return meta


def _close_script(rows, *, precheck=None, write=None, reread=None):
    """jobList, pre-check, close, reread: the close path as the front end walks it."""
    return [_list_ok(rows), precheck or _precheck(), write or _close_ok(),
            _list_ok(reread if reread is not None else rows)]


def _open_script(rows, *, refund=None, write=None, reread=None):
    """jobList, refund-points, openJob, reread: the open path as the front end walks it."""
    return [_list_ok(rows), refund if refund is not None else _refund_ok(),
            write or _open_ok(), _list_ok(reread if reread is not None else rows)]


def _boom(*args, **kwargs):
    raise RuntimeError("SYNTH-INTERNAL-FAULT")


# ── Parameter checks (no request) ───────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_an_empty_jobno_list_is_refused_with_no_all_option_and_no_request(tmp_path, monkeypatch, tool):
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, [], **_kw(tool, []))
    assert _is_error(result)
    assert "全部" in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["12a", "10 01", "1000001a", "-1000001"])
@pytest.mark.parametrize("tool", TOOLS)
async def test_a_jobno_that_is_not_all_digits_is_refused_naming_list_jobs_with_no_request(
        tmp_path, monkeypatch, tool, bad):
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, [], **_kw(tool, [J1, bad]))
    assert _is_error(result)
    assert "list_jobs" in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_a_repeated_jobno_is_refused_naming_it_with_no_request(tmp_path, monkeypatch, tool):
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, [], **_kw(tool, [J2, J1, J2]))
    assert _is_error(result)
    assert J2 in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_parameter_refusals_take_no_throttle_slot(tmp_path, monkeypatch):
    ctx, info = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])
    close, opened = _tool("close_jobs"), _tool("open_jobs")
    refusals = [
        (close, _kw("close_jobs", [])),
        (opened, _kw("open_jobs", [])),
        (close, _kw("close_jobs", ["12a"])),
        (opened, _kw("open_jobs", [J1, J1])),
        (close, _kw("close_jobs", [J1], reasons=[])),
        (close, _kw("close_jobs", [J1], reasons=[3])),
        (close, _kw("close_jobs", [J1], reasons=[64])),
        (close, _kw("close_jobs", [J1], reasons=[1, 1])),
        (close, _kw("close_jobs", [J1], reasons=[1, 32])),
        (close, _kw("close_jobs", [J1], reasons=[32], other_reason="   ")),
        (close, _kw("close_jobs", [J1], reasons=[32], other_reason="X" * 41)),
        (close, _kw("close_jobs", [J1], reasons=[1], other_reason="SYNTH-OTHER")),
    ]
    before = len(info.throttle.request_timestamps)
    for fn, kwargs in refusals:
        result = await fn(ctx=ctx, **kwargs)
        assert _is_error(result)
    assert spy.calls == []
    assert len(info.throttle.request_timestamps) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("reasons", [[], [3], [64], [1, 1]], ids=["empty", "not-a-code", "unknown-bit", "repeated"])
async def test_invalid_reasons_are_refused_listing_the_legal_codes_with_no_request(tmp_path, monkeypatch, reasons):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs", [], jobnos=[J1], reasons=reasons)
    assert _is_error(result)
    assert all(str(code) in result["error"] for code in (1, 2, 4, 8, 16, 32))
    assert spy.calls == []


@pytest.mark.asyncio
async def test_reasons_are_sent_as_their_sum_in_status(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], reread=[_off(J1)]),
                                **_kw("close_jobs", [J1], reasons=[1, 4, 16]))
    assert _bodies(spy, CLOSE)[0]["status"] == 21
    assert "error" not in result


@pytest.mark.asyncio
@pytest.mark.parametrize("reasons, other_reason", [
    ([1, 32], None),
    ([32], "   "),
    ([32], "X" * 41),
    ([1], "SYNTH-OTHER"),
], ids=["32-without-text", "32-blank-text", "32-over-40", "text-without-32"])
async def test_other_reason_rules_refuse_with_no_request(tmp_path, monkeypatch, reasons, other_reason):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs", [],
                                **_kw("close_jobs", [J1], reasons=reasons, other_reason=other_reason))
    assert _is_error(result)
    assert spy.calls == []


@pytest.mark.asyncio
async def test_other_reason_is_sent_stripped_when_32_is_chosen(tmp_path, monkeypatch):
    core = "SYNTH-REASON-" + "A" * 27
    assert len(core) == 40
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], reread=[_off(J1)]),
                                **_kw("close_jobs", [J1], reasons=[32], other_reason="  " + core + "  "))
    assert _bodies(spy, CLOSE)[0]["description"] == core
    assert "error" not in result


@pytest.mark.asyncio
async def test_description_is_empty_when_32_is_not_chosen(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], reread=[_off(J1)]),
                                **_kw("close_jobs", [J1], reasons=[1]))
    assert _bodies(spy, CLOSE)[0]["description"] == ""


# ── Reading the job list before a write (T-5..T-9) ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_the_first_request_reads_job_list_page_one(tmp_path, monkeypatch, tool):
    _result, spy, _ = await _run(tmp_path, monkeypatch, tool, [_list_ok([])], **_kw(tool, [J1]))
    assert _routes(spy)[0] == JOB_LIST
    assert ("page", "1") in _params(spy, JOB_LIST)[0]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_a_job_list_of_two_pages_is_refused_after_one_request(tmp_path, monkeypatch, tool):
    result, spy, _ = await _run(tmp_path, monkeypatch, tool,
                                [_list_ok([_listed(tool, J1)], total_page=2)], **_kw(tool, [J1]))
    assert _is_error(result)
    assert "一頁" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_jobnos_missing_from_the_list_are_named_in_request_order_after_one_request(
        tmp_path, monkeypatch, tool):
    result, spy, _ = await _run(tmp_path, monkeypatch, tool,
                                [_list_ok([_listed(tool, J1)])], **_kw(tool, [J3, J1, J2]))
    assert _is_error(result)
    text = result["error"]
    assert J3 in text and J2 in text
    assert text.index(J3) < text.index(J2)
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("failure", ["expired", "challenge", "job_data_not_a_list"])
async def test_a_failed_first_job_list_read_is_an_error_and_sends_nothing_more(
        tmp_path, monkeypatch, tool, failure):
    result, spy, _ = await _run(tmp_path, monkeypatch, tool,
                                [_list_failure(failure)], **_kw(tool, [J1]))
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_close_sends_the_listed_jobnos_as_given_in_the_requested_order(tmp_path, monkeypatch):
    rows = [_on(J1), _on(J2), _on(J3)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script(rows, reread=[_off(J1), _on(J2), _off(J3)]),
                                **_kw("close_jobs", [J3, J1]))
    assert _bodies(spy, CLOSE)[0]["jobNos"] == [J3, J1]
    assert "error" not in result


@pytest.mark.asyncio
async def test_open_sends_the_jobnos_comma_joined_in_the_requested_order(tmp_path, monkeypatch):
    rows = [_off(J1), _off(J2), _off(J3)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script(rows, reread=[_on(J1), _off(J2), _on(J3)]),
                                jobnos=[J3, J1])
    assert ("jobno", f"{J3},{J1}") in _pairs(_bodies(spy, OPEN)[0])
    assert "error" not in result


# ── Close: state, reasons, pausing (T-12, T-13, T-14) ──────────────────────────


@pytest.mark.asyncio
async def test_closing_a_job_already_off_is_refused_naming_it_after_one_request(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_off(J1)])], **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert J1 in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_closing_the_last_open_job_is_refused_without_the_pausing_flag(tmp_path, monkeypatch):
    rows = [_on(J1), _on(J2)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs", [_list_ok(rows)],
                                jobnos=[J1, J2], reasons=[1])
    assert _is_error(result)
    assert "公司廣告" in result["error"]
    assert "暫停" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_closing_the_last_open_job_proceeds_with_the_pausing_flag(tmp_path, monkeypatch):
    rows = [_on(J1), _on(J2)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script(rows, reread=[_off(J1), _off(J2)]),
                                **_kw("close_jobs", [J1, J2]))
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
async def test_no_pausing_flag_is_needed_while_another_job_stays_open(tmp_path, monkeypatch):
    rows = [_on(J1), _on(J2), _on(J3)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script(rows, reread=[_off(J1), _off(J2), _on(J3)]),
                                jobnos=[J1, J2], reasons=[1])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
async def test_precheck_is_a_post_with_the_listing_body_and_no_get_precheck_is_sent(tmp_path, monkeypatch):
    rows = [_on(J1), _on(J2), _on(J3)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script(rows, reread=[_off(J2), _off(J1), _on(J3)]),
                                **_kw("close_jobs", [J2, J1]))
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]
    assert _bodies(spy, PRECHECK)[0] == {"jobs": [
        {"jobNo": J2, "jobSortBookingDates": None},
        {"jobNo": J1, "jobSortBookingDates": None},
    ]}
    assert PRECHECK_GET not in _routes(spy)


# ── Close: pre-check add-ons and hunter points (T-15..T-19, T-53) ───────────────


@pytest.mark.asyncio
async def test_focus_add_on_without_the_return_flag_is_refused_with_add_ons_after_two_requests(
        tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]), _precheck(focus=[FOCUS_ROW])],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert "add_ons" in result
    assert "歸還" in result["error"]
    assert "SYNTH-FOCUS-JOB" in json.dumps(result["add_ons"], ensure_ascii=False)
    assert _routes(spy) == [JOB_LIST, PRECHECK]


@pytest.mark.asyncio
async def test_return_flag_sends_return_extra_one_and_closes(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], precheck=_precheck(focus=[FOCUS_ROW]),
                                              reread=[_off(J1)]),
                                **_kw("close_jobs", [J1], return_add_ons=True))
    assert _bodies(spy, CLOSE)[0]["returnExtra"] == 1
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
async def test_hunter_points_are_refused_pointing_to_the_backend(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]),
                                 _precheck(hunterJobPoint=[{"jobName": "SYNTH-HUNTER", "refundPoint": 1}])],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert "後台" in result["error"]
    assert _routes(spy) == [JOB_LIST, PRECHECK]


@pytest.mark.asyncio
async def test_empty_add_ons_send_return_extra_zero_and_an_unused_flag_is_warned(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], reread=[_off(J1)]),
                                **_kw("close_jobs", [J1]))
    assert _bodies(spy, CLOSE)[0]["returnExtra"] == 0
    assert "error" not in result

    result2, spy2, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                  _close_script([_on(J1)], reread=[_off(J1)]),
                                  **_kw("close_jobs", [J1], return_add_ons=True))
    assert _bodies(spy2, CLOSE)[0]["returnExtra"] == 0
    assert "error" not in result2
    assert any(("return_add_ons" in w) or ("沒有使用" in w) or ("未使用" in w)
               for w in _warnings(result2))


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["http_400", "data_not_an_object", "focus_is_a_string"])
async def test_a_failed_or_unreadable_pre_check_is_an_error_and_no_close_is_sent(tmp_path, monkeypatch, broken):
    precheck = {
        "http_400": _json_raw(400, {"error": {"code": 12, "message": "SYNTH-PRECHECK", "details": {}}}),
        "data_not_an_object": _json_raw(200, {"data": [], "metadata": {}}),
        "focus_is_a_string": _json_raw(200, {"data": {"focus": "SYNTH", "pickUp": [], "priority": [],
                                                      "jobSort": [], "hunterJobPoint": []},
                                             "metadata": {}}),
    }[broken]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]), precheck], **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST, PRECHECK]


@pytest.mark.asyncio
async def test_pick_up_object_without_the_return_flag_is_an_add_on_refusal(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]), _precheck(pickUp={"jobNo": J1})],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert "add_ons" in result
    assert _routes(spy) == [JOB_LIST, PRECHECK]


@pytest.mark.asyncio
@pytest.mark.parametrize("categories", [{"priority": True}, {"jobSort": 1}],
                         ids=["priority-boolean", "job-sort-number"])
async def test_add_on_categories_of_an_unexpected_type_are_refused_before_any_close(
        tmp_path, monkeypatch, categories):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]), _precheck(**categories)], **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST, PRECHECK]


@pytest.mark.asyncio
async def test_hunter_points_with_empty_add_on_categories_are_refused_without_an_add_ons_key(
        tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]),
                                 _precheck(hunterJobPoint=[{"jobName": "SYNTH-HUNTER", "refundPoint": 1}])],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert "add_ons" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK]


# ── Close: body, ad pause, error codes (T-20..T-23) ─────────────────────────────


@pytest.mark.asyncio
async def test_close_body_has_exactly_the_front_end_keys_and_pauses_on_the_last_job(tmp_path, monkeypatch):
    _result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1)], reread=[_off(J1)]),
                                 **_kw("close_jobs", [J1]))
    body = _bodies(spy, CLOSE)[0]
    assert set(body) == {"jobNos", "status", "description", "returnExtra", "pauseIfLastJob"}
    assert body["pauseIfLastJob"] is True


@pytest.mark.asyncio
async def test_a_last_job_close_reports_the_company_ad_pause_and_its_dates(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], write=_close_ok(_pause_meta()),
                                              reread=[_off(J1)]),
                                **_kw("close_jobs", [J1]))
    ad = result["ad_pause"]
    assert set(ad) == {"last_job", "no_pause_days", "pause_startdate", "pause_enddate",
                       "ad_open_date", "set_ad_pause", "return_extra", "ad_period"}
    assert ad["last_job"] is True
    assert ad["pause_startdate"] == "SYNTH-PAUSE-START"
    assert ad["pause_enddate"] == "SYNTH-PAUSE-END"
    assert any("SYNTH-PAUSE-START" in w and "SYNTH-PAUSE-END" in w for w in _warnings(result))
    assert len(spy.calls) == 4


@pytest.mark.asyncio
async def test_no_pause_days_says_the_company_ad_keeps_running(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1)], write=_close_ok(_pause_meta(noPauseDays=True)),
                                               reread=[_off(J1)]),
                                 **_kw("close_jobs", [J1]))
    assert result["ad_pause"]["no_pause_days"] is True
    assert any("持續刊登" in w for w in _warnings(result))


@pytest.mark.asyncio
async def test_last_day_of_the_ad_period_says_the_company_ad_keeps_running(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1)],
                                               write=_close_ok(_pause_meta(adPeriod={"remainingDays": 0})),
                                               reread=[_off(J1)]),
                                 **_kw("close_jobs", [J1]))
    assert any(("最後一天" in w) and ("持續刊登" in w) for w in _warnings(result))


@pytest.mark.asyncio
async def test_a_close_without_metadata_or_with_last_job_false_has_no_ad_pause(tmp_path, monkeypatch):
    for write in (_json_raw(200, {"data": True}), _close_ok(_pause_meta(lastJob=False))):
        result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                     _close_script([_on(J1)], write=write, reread=[_off(J1)]),
                                     **_kw("close_jobs", [J1]))
        assert result["ad_pause"] is None


@pytest.mark.asyncio
async def test_a_close_whose_data_is_not_a_boolean_still_reads_metadata_and_rereads(tmp_path, monkeypatch):
    write = _json_raw(200, {"data": {"x": 1}, "metadata": _pause_meta()})
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], write=write, reread=[_off(J1)]),
                                **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert result["ad_pause"] is not None
    assert len(spy.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [400, 403, 500])
async def test_close_error_code_57_rereads_and_warns_the_company_ad_did_not_pause(tmp_path, monkeypatch, status):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], write=_close_error(status, 57, "SYNTH-57"),
                                              reread=[_off(J1)]),
                                **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]
    assert any("廣告管理" in w for w in _warnings(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("status, code, text", [(400, 50, "關閉失敗"), (400, 66, "不存在"), (404, 66, "不存在")],
                         ids=["code-50", "code-66-400", "code-66-404"])
async def test_close_error_codes_50_and_66_reread_and_say_what_104_reported(
        tmp_path, monkeypatch, status, code, text):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], write=_close_error(status, code, "SYNTH-CODE"),
                                              reread=[_off(J1)]),
                                **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert len(spy.calls) == 4
    assert any(text in w for w in _warnings(result))


@pytest.mark.asyncio
async def test_an_unlisted_close_error_code_is_an_error_carrying_104s_message_not_a_dict(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]), _precheck(),
                                 _close_error(400, 12, "SYNTH-12-MESSAGE")],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert "SYNTH-12-MESSAGE" in result["error"]
    assert "{" not in result["error"]
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE]


# ── Open: state, add-on refund listing (T-24, T-26..T-28) ──────────────────────


@pytest.mark.asyncio
async def test_opening_a_job_already_on_is_refused_naming_it_after_one_request(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_on(J1)])], jobnos=[J1])
    assert _is_error(result)
    assert J1 in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_refund_query_sends_one_jobnos_parameter_per_jobno_in_requested_order(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1), _off(J2)], reread=[_on(J1), _on(J2)]),
                                jobnos=[J2, J1])
    assert _params(spy, REFUND)[0] == [("jobNos[]", J2), ("jobNos[]", J1)]
    assert "error" not in result


@pytest.mark.asyncio
async def test_add_on_listing_is_refused_without_the_cancel_flag_after_two_requests(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)]), _refund_ok([AD_ROW])], jobnos=[J1])
    assert _is_error(result)
    assert "add_ons" in result
    assert "取消" in result["error"]
    assert "SYNTH-AD-JOB" in json.dumps(result["add_ons"], ensure_ascii=False)
    assert _routes(spy) == [JOB_LIST, REFUND]


@pytest.mark.asyncio
async def test_the_cancel_flag_lets_the_open_proceed(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1)], refund=_refund_ok([AD_ROW]), reread=[_on(J1)]),
                                jobnos=[J1], cancel_add_ons=True)
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
async def test_an_empty_add_on_listing_needs_no_flag(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1)], refund=_refund_ok([]), reread=[_on(J1)]),
                                jobnos=[J1])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("broken", ["http_404", "data_not_a_list"])
async def test_a_failed_or_unreadable_refund_query_is_an_error_and_no_open_is_sent(tmp_path, monkeypatch, broken):
    refund = {
        "http_404": _json_raw(404, {"error": {"code": 12, "message": "SYNTH-REFUND", "details": {}}}),
        "data_not_a_list": _json_raw(200, {"data": {"x": 1}, "metadata": {}}),
    }[broken]
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)]), refund], jobnos=[J1])
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST, REFUND]


# ── Open: the form, refusals, unrecognised responses (T-29..T-32, T-54) ─────────


@pytest.mark.asyncio
async def test_a_single_open_posts_a_form_with_type_single(tmp_path, monkeypatch):
    _result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                 _open_script([_off(J1)], reread=[_on(J1)]), jobnos=[J1])
    assert sorted(_pairs(_bodies(spy, OPEN)[0])) == sorted(
        [("jobno", J1), ("ignoreOrdinance", "1"), ("type", "single")])


@pytest.mark.asyncio
async def test_a_two_job_open_posts_comma_joined_jobnos_and_type_multi(tmp_path, monkeypatch):
    _result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                 _open_script([_off(J1), _off(J2)], reread=[_on(J1), _on(J2)]),
                                 jobnos=[J1, J2])
    assert sorted(_pairs(_bodies(spy, OPEN)[0])) == sorted(
        [("jobno", f"{J1},{J2}"), ("ignoreOrdinance", "1"), ("type", "multi")])


@pytest.mark.asyncio
async def test_a_single_validate_refusal_is_an_error_with_refusal_after_three_requests(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)]), _refund_ok(),
                                 _open_ok(message="validate", result="SYNTH-VALIDATE")],
                                jobnos=[J1])
    assert _is_error(result)
    assert result["refusal"] == "validate"
    assert "後台" in result["error"]
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN]


SINGLE_OPEN_REFUSALS = [
    ("promise", _open_env(message="promise", result="SYNTH-PROMISE-TEXT"), 200, "SYNTH-PROMISE-TEXT"),
    ("open_failed", _open_env(message="openFailed", result="SYNTH-OPEN-FAILED-TEXT"), 200, "SYNTH-OPEN-FAILED-TEXT"),
    ("no_remaining_quota", _open_env(message="NO_REMAIN_JOB", result="SYNTH-NO-REMAIN-TEXT"), 200, "SYNTH-NO-REMAIN-TEXT"),
    ("job_limit", _open_env(status="USER_ERROR", message="", result="SYNTH-LIMIT-TEXT", code="001001"), 200, "SYNTH-LIMIT-TEXT"),
    ("job_limit", {"status": "USER_ERROR", "message": "SYNTH-400", "code": "001001", "result": "SYNTH-LIMIT-TEXT"}, 400, None),
    ("access_denied", _open_env(status="ACCESS_DENIED", message="openAll", result="SYNTH-DENIED-TEXT"), 200, "SYNTH-DENIED-TEXT"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("refusal, body, status, text", SINGLE_OPEN_REFUSALS,
                         ids=["promise", "open-failed", "no-remaining-quota", "job-limit-200",
                              "job-limit-400", "access-denied"])
async def test_single_open_refusals_are_errors_with_their_class_after_three_requests(
        tmp_path, monkeypatch, refusal, body, status, text):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)]), _refund_ok(), _json_raw(status, body)],
                                jobnos=[J1])
    assert _is_error(result)
    assert result["refusal"] == refusal
    if text is not None:
        assert text in result["error"]
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN]


@pytest.mark.asyncio
async def test_open_partial_rereads_and_warns_how_many_failed(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1), _off(J2)], write=_open_ok(message="openPartial", result=1),
                                             reread=[_on(J1), _off(J2)]),
                                jobnos=[J1, J2])
    assert _result_by(result, "opened") == {J1: "confirmed", J2: "unconfirmed"}
    assert any(("1" in w) and ("開啟失敗" in w) for w in _warnings(result))
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
async def test_a_two_job_validate_rereads_and_reports_the_refusal_in_the_success_shape(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1), _off(J2)],
                                             write=_open_ok(message="validate", result="SYNTH-VALIDATE")),
                                jobnos=[J1, J2])
    assert "error" not in result
    assert result["refusal"] == "validate"
    assert _result_by(result, "opened") == {J1: "unconfirmed", J2: "unconfirmed"}
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
async def test_a_single_access_denied_status_is_an_access_denied_refusal(tmp_path, monkeypatch):
    body = _open_env(status="ACCESS_DENIED", message="openAll", result="SYNTH-DENIED-TEXT")
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)]), _refund_ok(), _json_raw(200, body)], jobnos=[J1])
    assert _is_error(result)
    assert result["refusal"] == "access_denied"
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN]


@pytest.mark.asyncio
async def test_a_single_user_error_with_code_001001_is_job_limit(tmp_path, monkeypatch):
    body = _open_env(status="USER_ERROR", message="", result="SYNTH-LIMIT-TEXT", code="001001")
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)]), _refund_ok(), _json_raw(200, body)], jobnos=[J1])
    assert _is_error(result)
    assert result["refusal"] == "job_limit"
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [_open_env(message="SYNTH-UNKNOWN-MESSAGE"), {}],
                         ids=["unknown-success-message", "empty-object"])
async def test_an_unrecognised_open_response_rereads_and_says_so(tmp_path, monkeypatch, body):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1)], write=_json_raw(200, body), reread=[_on(J1)]),
                                jobnos=[J1])
    assert "error" not in result
    assert any("無法辨識" in w for w in _warnings(result))
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


# ── Reread: confirmation per job, order, failures (T-37, T-38, T-40..T-43) ──────


@pytest.mark.asyncio
async def test_close_two_jobs_confirms_only_the_one_that_turned_off(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1), _on(J2)], reread=[_off(J1), _on(J2)]),
                                 **_kw("close_jobs", [J1, J2]))
    assert _result_by(result, "closed") == {J1: "confirmed", J2: "unconfirmed"}


@pytest.mark.asyncio
async def test_open_two_jobs_confirms_only_the_one_that_turned_on(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                 _open_script([_off(J1), _off(J2)], reread=[_on(J1), _off(J2)]),
                                 jobnos=[J1, J2])
    assert _result_by(result, "opened") == {J1: "confirmed", J2: "unconfirmed"}


@pytest.mark.asyncio
async def test_close_results_follow_the_requested_order_not_the_listing_order(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1), _on(J2)], reread=[_off(J1), _on(J2)]),
                                 **_kw("close_jobs", [J2, J1]))
    assert [row["jobno"] for row in result["results"]] == [J2, J1]
    assert _result_by(result, "closed") == {J2: "unconfirmed", J1: "confirmed"}


@pytest.mark.asyncio
async def test_open_results_follow_the_requested_order_not_the_listing_order(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                 _open_script([_off(J1), _off(J2)], reread=[_on(J1), _on(J2)]),
                                 jobnos=[J2, J1])
    assert [row["jobno"] for row in result["results"]] == [J2, J1]
    assert _result_by(result, "opened") == {J2: "confirmed", J1: "confirmed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("failure", ["expired", "challenge", "job_data_not_a_list"])
async def test_a_failed_reread_after_an_accepted_write_is_a_success_shape_all_unconfirmed(
        tmp_path, monkeypatch, tool, failure):
    if tool == "close_jobs":
        script = [_list_ok([_on(J1)]), _precheck(), _close_ok(), _list_failure(failure)]
    else:
        script = [_list_ok([_off(J1)]), _refund_ok(), _open_ok(), _list_failure(failure)]
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, script, **_kw(tool, [J1]))
    key = "closed" if tool == "close_jobs" else "opened"
    assert "error" not in result
    assert _result_by(result, key) == {J1: "unconfirmed"}
    assert any("重讀" in w for w in _warnings(result))
    assert len(spy.calls) == 4


@pytest.mark.asyncio
async def test_a_close_job_missing_from_the_reread_is_unconfirmed_and_named(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1), _on(J2)], reread=[_off(J1)]),
                                 **_kw("close_jobs", [J1, J2]))
    assert _result_by(result, "closed") == {J1: "confirmed", J2: "unconfirmed"}
    assert any(J2 in w for w in _warnings(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("failure", ["timeout", "non_json", "server_error"])
async def test_an_ambiguous_write_failure_rereads_and_says_the_reread_decides(tmp_path, monkeypatch, tool, failure):
    item = {
        "timeout": TimeoutError("SYNTH-TIMEOUT"),
        "non_json": _non_json_raw(),
        "server_error": _server_error_raw(),
    }[failure]
    if tool == "close_jobs":
        script = [_list_ok([_on(J1)]), _precheck(), item, _list_ok([_off(J1)])]
    else:
        script = [_list_ok([_off(J1)]), _refund_ok(), item, _list_ok([_on(J1)])]
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, script, **_kw(tool, [J1]))
    assert "error" not in result
    assert len(spy.calls) == 4
    assert any("重讀" in w for w in _warnings(result))


# ── Write refusals by 104 (T-44, T-45) ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_a_single_close_rejected_by_104_is_an_error_with_104s_text_after_three_requests(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)]), _precheck(), _close_error(400, 12, "SYNTH-REJECT-TEXT")],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert "SYNTH-REJECT-TEXT" in result["error"]
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE]


@pytest.mark.asyncio
async def test_a_batch_close_rejected_by_104_rereads_and_quotes_the_rejection(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1), _on(J2)], write=_close_error(400, 12, "SYNTH-REJECT-TEXT"),
                                              reread=[_on(J1), _on(J2)]),
                                **_kw("close_jobs", [J1, J2]))
    assert "error" not in result
    assert any(("SYNTH-REJECT-TEXT" in w) and ("拒絕" in w) for w in _warnings(result))
    assert _result_by(result, "closed") == {J1: "unconfirmed", J2: "unconfirmed"}
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("failure", ["expired", "forbidden", "challenge"])
async def test_a_write_104_did_not_accept_is_an_error_with_no_reread(tmp_path, monkeypatch, tool, failure):
    item = {"expired": _expired_raw(), "forbidden": _forbidden_raw(), "challenge": _challenge_raw()}[failure]
    if tool == "close_jobs":
        script = [_list_ok([_on(J1)]), _precheck(), item]
        expected = [JOB_LIST, PRECHECK, CLOSE]
    else:
        script = [_list_ok([_off(J1)]), _refund_ok(), item]
        expected = [JOB_LIST, REFUND, OPEN]
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, script, **_kw(tool, [J1]))
    assert _is_error(result)
    assert _routes(spy) == expected


# ── Messages and internal errors (T-46, T-47) ───────────────────────────────────


@pytest.mark.asyncio
async def test_an_unconfirmed_message_says_it_is_not_a_failure_and_not_to_resend(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1), _on(J2)], reread=[_on(J1), _off(J2)]),
                                 **_kw("close_jobs", [J1, J2]))
    message = result["message"]
    assert "不代表失敗" in message
    assert "list_jobs" in message
    assert "重送" in message


@pytest.mark.asyncio
async def test_an_all_confirmed_message_says_the_reread_job_list_is_the_basis(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1)], reread=[_off(J1)]),
                                 **_kw("close_jobs", [J1]))
    assert "重讀" in result["message"]
    assert _result_by(result, "closed") == {J1: "confirmed"}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_an_unexpected_error_before_the_write_is_an_error_saying_nothing_was_sent(
        tmp_path, monkeypatch, tool):
    monkeypatch.setattr(_js(), "preflight_rows", _boom)
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, [_list_ok([_listed(tool, J1)])],
                                **_kw(tool, [J1]))
    assert _is_error(result)
    assert "沒有送出" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_an_unexpected_error_after_the_write_is_success_shape_all_unconfirmed(tmp_path, monkeypatch, tool):
    monkeypatch.setattr(_js(), "confirm_switch", _boom)
    if tool == "close_jobs":
        script = [_list_ok([_on(J1)]), _precheck(), _close_ok(), _list_ok([_off(J1)])]
        expected = [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]
    else:
        script = [_list_ok([_off(J1)]), _refund_ok(), _open_ok(), _list_ok([_on(J1)])]
        expected = [JOB_LIST, REFUND, OPEN, JOB_LIST]
    result, spy, _ = await _run(tmp_path, monkeypatch, tool, script, **_kw(tool, [J1]))
    key = "closed" if tool == "close_jobs" else "opened"
    assert set(result) in (CLOSE_SUCCESS_KEYS, OPEN_SUCCESS_KEYS)
    assert _result_by(result, key) == {J1: "unconfirmed"}
    assert any("無法判斷" in w for w in _warnings(result))
    assert _routes(spy) == expected


# ── Return shapes (T-48) ────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_every_outcome_is_one_of_the_closed_return_shapes(tmp_path, monkeypatch):
    scenarios = [
        ("close", "close_jobs", _kw("close_jobs", []), []),
        ("close", "close_jobs", _kw("close_jobs", [J1]), [_list_ok([_off(J1)])]),
        ("close", "close_jobs", _kw("close_jobs", [J1]),
         [_list_ok([_on(J1)]), _json_raw(400, {"error": {"code": 12, "message": "X", "details": {}}})]),
        ("close", "close_jobs", _kw("close_jobs", [J1]),
         [_list_ok([_on(J1)]), _precheck(focus=[FOCUS_ROW])]),
        ("close", "close_jobs", _kw("close_jobs", [J1]), _close_script([_on(J1)], reread=[_off(J1)])),
        ("close", "close_jobs", _kw("close_jobs", [J1, J2]),
         _close_script([_on(J1), _on(J2)], reread=[_on(J1), _on(J2)])),
        ("close", "close_jobs", _kw("close_jobs", [J1]),
         [_list_ok([_on(J1)]), _precheck(), _close_ok(), _expired_raw()]),
        ("open", "open_jobs", _kw("open_jobs", []), []),
        ("open", "open_jobs", _kw("open_jobs", [J1]), [_list_ok([_off(J1)]), _refund_ok([AD_ROW])]),
        ("open", "open_jobs", _kw("open_jobs", [J1]),
         [_list_ok([_off(J1)]), _refund_ok(), _open_ok(message="validate", result="X")]),
        ("open", "open_jobs", _kw("open_jobs", [J1, J2]),
         _open_script([_off(J1), _off(J2)], write=_open_ok(message="validate", result="X"),
                      reread=[_off(J1), _off(J2)])),
        ("open", "open_jobs", _kw("open_jobs", [J1]), _open_script([_off(J1)], reread=[_on(J1)])),
    ]
    allowed = {"close_jobs": [CLOSE_SUCCESS_KEYS] + CLOSE_ERROR_KEYS,
               "open_jobs": [OPEN_SUCCESS_KEYS] + OPEN_ERROR_KEYS}
    for label, tool, kwargs, script in scenarios:
        result, _spy, _ = await _run(tmp_path, monkeypatch, tool, script, **kwargs)
        assert set(result) in allowed[tool], (label, result)


# ── Descriptions and registration (T-49..T-51) ──────────────────────────────────


def _descriptions():
    mcp = _FakeMCP()
    _js().register_job_switch_tools(mcp)
    return mcp.descriptions


def test_both_descriptions_state_immediacy_visibility_and_the_human_check():
    descriptions = _descriptions()
    for name in ("close_jobs", "open_jobs"):
        text = descriptions[name]
        assert re.search("立[刻即]生效", text), name
        assert "求職者" in text, name
        assert "真人" in text and "確認" in text, name


def test_close_description_names_the_last_job_pause_and_its_flag():
    text = _descriptions()["close_jobs"]
    assert "allow_pausing_company_ads" in text
    assert "暫停" in text


def test_open_description_names_the_quota_the_add_ons_and_the_cancel():
    text = _descriptions()["open_jobs"]
    assert "額度" in text
    assert "加值廣告" in text
    assert "取消" in text


@pytest.mark.asyncio
async def test_no_description_or_returned_text_names_a_person_as_the_source(tmp_path, monkeypatch):
    texts = list(_descriptions().values())
    runs = [
        (("close_jobs", [J1, J2]), _close_script([_on(J1), _on(J2)], reread=[_off(J1), _on(J2)]), _kw("close_jobs", [J1, J2])),
        (("open_jobs", [J1]), _open_script([_off(J1)], write=_open_ok(message="validate", result="SYNTH")),
         _kw("open_jobs", [J1])),
        (("close_jobs", [J1]), [_list_ok([_on(J1)]), _precheck(focus=[FOCUS_ROW])], _kw("close_jobs", [J1])),
    ]
    for (tool, _jobnos), script, kwargs in runs:
        result, _spy, _ = await _run(tmp_path, monkeypatch, tool, script, **kwargs)
        texts.extend(_strings(result))
    for text in texts:
        assert not any(word in text for word in FORBIDDEN_SOURCE_WORDS), text


def test_register_all_tools_includes_both_job_switch_tools_within_the_description_budget():
    from mcp104.main import register_all_tools
    mcp = _FakeMCP()
    register_all_tools(mcp)
    for name in ("close_jobs", "open_jobs"):
        assert name in mcp.tools
        assert len(mcp.descriptions[name]) <= 2048 - 100


# ── Throttle window (T-52, close and open rows) ─────────────────────────────────


@pytest.mark.asyncio
async def test_with_four_slots_free_close_is_sent_in_full(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP104_MAX_REQUESTS_PER_HOUR", "4")
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                _close_script([_on(J1)], reread=[_off(J1)]), **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert len(spy.calls) == 4


@pytest.mark.asyncio
async def test_with_four_slots_free_open_is_sent_in_full(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP104_MAX_REQUESTS_PER_HOUR", "4")
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                _open_script([_off(J1)], reread=[_on(J1)]), jobnos=[J1])
    assert "error" not in result
    assert len(spy.calls) == 4


# ── Pure helpers reached through module attributes (T-62) ───────────────────────


def _parsed(rows, **kw):
    return _jobs().parse_job_list(_job_list(rows, **kw))


def test_confirm_switch_marks_by_the_wanted_state_and_names_the_absent_job():
    after = _parsed([_on(J1), _off(J2)])
    rows, warnings = _js().confirm_switch([J2, J1, J3], after, want="off", key="closed")
    assert [row["jobno"] for row in rows] == [J2, J1, J3]
    assert [row["closed"] for row in rows] == ["confirmed", "unconfirmed", "unconfirmed"]
    assert any(J3 in w for w in warnings)


def test_confirm_deleted_depends_on_presence_and_on_the_list_being_one_page():
    one_page = _parsed([_off(J2)])
    rows, _ = _js().confirm_deleted(J1, one_page)
    assert rows[0]["deleted"] == "confirmed"
    rows, _ = _js().confirm_deleted(J2, one_page)
    assert rows[0]["deleted"] == "unconfirmed"
    two_pages = _parsed([_off(J2)], total_page=2)
    rows, warnings = _js().confirm_deleted(J1, two_pages)
    assert rows[0]["deleted"] == "unconfirmed"
    assert any(J1 in w for w in warnings)


def test_preflight_rows_returns_rows_in_requested_order_or_names_the_missing_in_order():
    job_list = _parsed([_on(J1), _on(J2)])
    rows = _js().preflight_rows([J2, J1], job_list)
    assert [row.jobno for row in rows] == [J2, J1]

    missing = _js().preflight_rows([J3, J1, "1000009"], job_list)
    assert isinstance(missing, str)
    assert J3 in missing and "1000009" in missing
    assert missing.index(J3) < missing.index("1000009")

    assert isinstance(_js().preflight_rows([J1], _parsed([_on(J1)], total_page=2)), str)


# ── Open: poNum only for new-model accounts (T-63) ──────────────────────────────


@pytest.mark.asyncio
async def test_a_non_new_model_account_with_zero_po_num_is_not_stopped_by_po_num(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], account={"isNewModel": False, "poNum": 0}),
                                 _refund_ok(), _open_ok(), _list_ok([_on(J1)])],
                                jobnos=[J1])
    assert "error" not in result
    assert len(spy.calls) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("po_num", [0, "0"], ids=["int-zero", "string-zero"])
async def test_a_new_model_account_with_no_jobs_left_is_refused_before_the_advertising_query(
        tmp_path, monkeypatch, po_num):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], account={"isNewModel": True, "poNum": po_num})],
                                jobnos=[J1])
    assert _is_error(result)
    assert "您目前可使用的職務數為0" in result["error"]
    assert _routes(spy) == [JOB_LIST]


# ── Identity and cookies never reach a return or a log (T-65, close and open rows) ─


def _path_script(tool, path):
    if tool == "close_jobs":
        if path == "rejected":
            return [_list_ok([_on(J1)]), _precheck(), _close_error(400, 12, "SYNTH-REJECT-TEXT")]
        if path == "timeout":
            return [_list_ok([_on(J1)]), _precheck(), TimeoutError("SYNTH-TIMEOUT"), _list_ok([_off(J1)])]
        return _close_script([_on(J1)], reread=[_off(J1)])
    if path == "rejected":
        return [_list_ok([_off(J1)]), _refund_ok(),
                _json_raw(200, _open_env(status="ACCESS_DENIED", message="openAll", result="SYNTH-REJECT-TEXT"))]
    if path == "timeout":
        return [_list_ok([_off(J1)]), _refund_ok(), TimeoutError("SYNTH-TIMEOUT"), _list_ok([_on(J1)])]
    return _open_script([_off(J1)], reread=[_on(J1)])


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, path", [(t, p) for t in TOOLS
                                        for p in ("success", "rejected", "timeout", "unexpected")])
async def test_cookies_and_operator_identity_reach_no_return_and_no_log(
        tmp_path, monkeypatch, caplog, tool, path):
    caplog.set_level(logging.DEBUG)
    if path == "unexpected":
        monkeypatch.setattr(_js(), "confirm_switch", _boom)
    result, _spy, _ = await _run(tmp_path, monkeypatch, tool, _path_script(tool, path),
                                 cookies=SESSION_COOKIES, **_kw(tool, [J1]))
    seen = _strings(result) + [caplog.text]
    for secret in COOKIE_STRINGS + OPERATOR_STRINGS:
        assert not any(secret in text for text in seen), secret


# ── Front-end gates on a single job (T-66, T-76 single rows) ────────────────────


SINGLE_OPEN_GATES = [
    ("job-being-verified", {"in_verify": 1}, {}, (), "職缺確認中，目前無法開啟"),
    ("listing-limited", {}, {"coStatus": "off"}, (), "限刊登中會員使用"),
    ("listing-paused", {}, {"coStatus": "pause"}, (), "暫停徵才中，目前無法開啟"),
    ("guarantee-extended", {}, {"isOutEnsure": True}, (), "保障方案延長期間不得開啟職務"),
    ("job10-denied", {}, {"permit": _permit({"job__10": False})}, (), NO_PERMISSION),
    ("job10-absent", {}, {"permit": _permit({"job__10": None})}, (), NO_PERMISSION),
    ("permit-absent", {}, {}, ("permit",), NO_PERMISSION),
    ("permit-not-an-object", {}, {"permit": "SYNTH-PERMIT"}, (), NO_PERMISSION),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label, row, account, omit, text", SINGLE_OPEN_GATES,
                         ids=[gate[0] for gate in SINGLE_OPEN_GATES])
async def test_a_single_open_is_refused_by_the_front_end_gate_before_any_advertising_query(
        tmp_path, monkeypatch, label, row, account, omit, text):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1, **row)], account=account, omit=omit)], jobnos=[J1])
    assert _is_error(result)
    assert text in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_single_open_checks_only_job10_so_a_denied_job5_does_not_block(tmp_path, monkeypatch):
    script = [_list_ok([_off(J1)], account={"permit": _permit({"job__5": False})}),
              _refund_ok(), _open_ok(), _list_ok([_on(J1)])]
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs", script, jobnos=[J1])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


# ── Front-end gates on two or more jobs (T-67, T-68) ────────────────────────────


TWO_OPEN_GATES = [
    ("job5-denied", [_off(J1), _off(J2)], {"permit": _permit({"job__5": False})}, NO_PERMISSION, None),
    ("job-being-verified", [_off(J1), _off(J2, in_verify=1)], {}, "職缺確認中，目前無法開啟", J2),
    ("listing-paused", [_off(J1), _off(J2)], {"coStatus": "pause"}, "暫停徵才中，目前無法開啟", None),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label, rows, account, text, named", TWO_OPEN_GATES,
                         ids=[gate[0] for gate in TWO_OPEN_GATES])
async def test_a_two_job_open_is_refused_by_the_listing_toolbar_gate(
        tmp_path, monkeypatch, label, rows, account, text, named):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs", [_list_ok(rows, account=account)],
                                jobnos=[J1, J2])
    assert _is_error(result)
    assert text in result["error"]
    if named is not None:
        assert named in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_two_job_open_is_not_blocked_by_an_absent_job5_entry(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1), _off(J2)], account={"permit": _permit({"job__5": None})}),
                                 _refund_ok(), _open_ok(), _list_ok([_on(J1), _on(J2)])],
                                jobnos=[J1, J2])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
async def test_a_two_job_open_is_not_blocked_by_a_denied_job10_when_job5_is_allowed(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1), _off(J2)], account={"permit": _permit({"job__10": False})}),
                                 _refund_ok(), _open_ok(), _list_ok([_on(J1), _on(J2)])],
                                jobnos=[J1, J2])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


CLOSE_SINGLE_GATES = [
    ("job-being-verified", {"in_verify": 1}, {}, "職缺確認中，目前無法關閉"),
    ("job11-denied", {}, {"permit": _permit({"job__11": False})}, NO_PERMISSION),
    ("job11-absent", {}, {"permit": _permit({"job__11": None})}, NO_PERMISSION),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("label, row, account, text", CLOSE_SINGLE_GATES,
                         ids=[gate[0] for gate in CLOSE_SINGLE_GATES])
async def test_a_single_close_is_refused_by_the_front_end_gate_after_one_request(
        tmp_path, monkeypatch, label, row, account, text):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1, **row)], account=account)], **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert text in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_single_close_ignores_a_paused_listing(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)], account={"coStatus": "pause"}), _precheck(),
                                 _close_ok(), _list_ok([_off(J1)])],
                                **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("co_status, text", [("off", "限刊登中會員使用"), ("pause", "暫停徵才中")],
                         ids=["off", "pause"])
async def test_a_two_job_close_is_refused_while_the_listing_is_limited_or_paused(
        tmp_path, monkeypatch, co_status, text):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1), _on(J2)], account={"coStatus": co_status})],
                                **_kw("close_jobs", [J1, J2]))
    assert _is_error(result)
    assert text in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_two_job_close_without_a_job11_entry_proceeds(tmp_path, monkeypatch):
    rows = [_on(J1), _on(J2)]
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok(rows, account={"permit": _permit({"job__11": None})}),
                                 _precheck(), _close_ok(), _list_ok([_off(J1), _off(J2)])],
                                **_kw("close_jobs", [J1, J2]))
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
async def test_a_two_job_close_with_job11_denied_is_refused(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1), _on(J2)], account={"permit": _permit({"job__11": False})})],
                                **_kw("close_jobs", [J1, J2]))
    assert _is_error(result)
    assert NO_PERMISSION in result["error"]
    assert _routes(spy) == [JOB_LIST]


# ── Hunter and dispatch new-model accounts (T-69) ───────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["isHunter", "isDispatch"])
async def test_a_new_model_hunter_or_dispatch_account_is_sent_to_the_backend(tmp_path, monkeypatch, flag):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], account={flag: True, "isNewModel": True, "poNum": 5})],
                                jobnos=[J1])
    assert _is_error(result)
    assert "104 後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("flag", ["isHunter", "isDispatch"])
async def test_a_hunter_or_dispatch_account_that_is_not_new_model_is_not_stopped(tmp_path, monkeypatch, flag):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], account={flag: True, "isNewModel": False}),
                                 _refund_ok(), _open_ok(), _list_ok([_on(J1)])],
                                jobnos=[J1])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


# ── Absent front-end fields follow the front end's own reading (T-70) ───────────


OMITTED_ACCOUNT_KEYS = ("isOutEnsure", "isNewModel", "isHunter", "isDispatch")


@pytest.mark.asyncio
async def test_open_proceeds_when_the_front_end_flags_are_absent_from_the_listing(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], omit=OMITTED_ACCOUNT_KEYS), _refund_ok(), _open_ok(),
                                 _list_ok([_on(J1)])],
                                jobnos=[J1])
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
async def test_close_proceeds_when_the_front_end_flags_are_absent_from_the_listing(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)], omit=OMITTED_ACCOUNT_KEYS), _precheck(), _close_ok(),
                                 _list_ok([_off(J1)])],
                                **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


# ── Close metadata.failed and the web-account toolbar rule (T-74, T-76) ─────────


@pytest.mark.asyncio
async def test_close_reporting_failed_rows_warns_the_count_and_the_reread_decides(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                 _close_script([_on(J1), _on(J2)], write=_close_ok({"failed": 2}),
                                               reread=[_off(J1), _off(J2)]),
                                 **_kw("close_jobs", [J1, J2]))
    assert any(("2" in w) and ("失敗" in w) for w in _warnings(result))
    assert _result_by(result, "closed") == {J1: "confirmed", J2: "confirmed"}


@pytest.mark.asyncio
async def test_a_web_account_opens_a_single_job_by_the_toolbar_rule_and_posts_type_multi(tmp_path, monkeypatch):
    account = {"isWeb": True, "permit": _permit({"job__10": False})}
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], account=account), _refund_ok(), _open_ok(),
                                 _list_ok([_on(J1)])],
                                jobnos=[J1])
    assert "error" not in result
    assert ("type", "multi") in _pairs(_bodies(spy, OPEN)[0])
    assert _routes(spy) == [JOB_LIST, REFUND, OPEN, JOB_LIST]


@pytest.mark.asyncio
async def test_a_web_account_open_of_a_single_job_is_refused_by_the_job5_rule(tmp_path, monkeypatch):
    account = {"isWeb": True, "permit": _permit({"job__5": False})}
    result, spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                [_list_ok([_off(J1)], account=account)], jobnos=[J1])
    assert _is_error(result)
    assert NO_PERMISSION in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("co_status, text", [("pause", "暫停徵才中"), ("off", "限刊登中會員使用")],
                         ids=["pause", "off"])
async def test_a_web_account_close_of_a_single_job_follows_the_toolbar_rule(tmp_path, monkeypatch, co_status, text):
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)], account={"isWeb": True, "coStatus": co_status})],
                                **_kw("close_jobs", [J1]))
    assert _is_error(result)
    assert text in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_web_account_close_of_a_single_job_without_a_job11_entry_proceeds(tmp_path, monkeypatch):
    account = {"isWeb": True, "permit": _permit({"job__11": None})}
    result, spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                [_list_ok([_on(J1)], account=account), _precheck(), _close_ok(),
                                 _list_ok([_off(J1)])],
                                **_kw("close_jobs", [J1]))
    assert "error" not in result
    assert _routes(spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("web_key", ["false", "absent"])
async def test_a_non_web_single_job_follows_the_single_button_rules(tmp_path, monkeypatch, web_key):
    omit = ("isWeb",) if web_key == "absent" else ()
    opened, opened_spy, _ = await _run(tmp_path, monkeypatch, "open_jobs",
                                       [_list_ok([_off(J1)], account={"permit": _permit({"job__10": False})},
                                                 omit=omit)],
                                       jobnos=[J1])
    assert _is_error(opened)
    assert NO_PERMISSION in opened["error"]
    assert _routes(opened_spy) == [JOB_LIST]

    closed, closed_spy, _ = await _run(tmp_path, monkeypatch, "close_jobs",
                                       [_list_ok([_on(J1)], account={"coStatus": "pause"}, omit=omit),
                                        _precheck(), _close_ok(), _list_ok([_off(J1)])],
                                       **_kw("close_jobs", [J1]))
    assert "error" not in closed
    assert "type" not in _bodies(closed_spy, CLOSE)[0]
    assert _routes(closed_spy) == [JOB_LIST, PRECHECK, CLOSE, JOB_LIST]


# ── delete_job: routes, bodies built from the measured key sets, script builders ─

DELETE = ("POST", "/api/job/jobDelete")
MORE = ("GET", "/api/job/more")
IDENTITY = ("GET", "/vipapi/company/profile/identity")
DELETE_ROUTES = [JOB_LIST, MORE, IDENTITY, DELETE, JOB_LIST]
DELETE_SUCCESS_KEYS = {"results", "message", "warnings"}
DELETE_ERROR_KEYS = [{"error"}, {"error", "retry_after_seconds"}]
ATSP_TIP = "此功能請至【104 招募管理 Pro】進行操作"
WEB_REFUSAL = "網頁上沒有這個操作"
ATS_STATE_KEY = "urn:104:v3:entity:ats_state"
JWT_HEADER = "eyJhbGciOiJIUzI1NiJ9"

# Key sets as the read-only probe measured them. Every value in this file is synthetic.
MORE_PERMIT_KEYS = ("job__3", "job__4", "job__6", "job__7", "job__8", "job__14", "job__15",
                    "job__18", "job__19", "job__25", "job__27", "job__29", "job__30")
IDENTITY_KEYS = ("isBlackList", "isMarketing", "isInsurance", "isFuneral", "isPostOnly",
                 "isBrushOff", "isBad", "isBadCredit", "isIndemnity", "isOutEnsure", "isEasy",
                 "isHunter", "isDispatch", "isWeb", "isOb", "isDissolution", "isTheNetwork",
                 "isInvalidState", "isOversea", "isRegisterDispatchedWorker", "isWatchList",
                 "isAllowPostJobInChina", "isInactive", "isSimpleOnly", "isStopAuthority",
                 "isAllowPostJobInSpecialArea", "isOffOver60Days", "isHasSubDepartments",
                 "isPreparatory", "isInterviewSecretaryUser", "isAtsp")


def _more_permit(changes=None):
    """job__6 (the delete permit) is granted by default; job__29 is refused, as measured."""
    permit = {key: {"result": True, "message": ""} for key in MORE_PERMIT_KEYS}
    permit["job__29"] = {"result": False, "message": "SYNTH-NOT-PERMITTED"}
    for key, value in (changes or {}).items():
        if value is None:
            permit.pop(key, None)
        else:
            permit[key] = {"result": value, "message": ""}
    return permit


def _more_ok(changes=None, *, drop_permit=False):
    result = {"render": {"jobNo": J1, "switch": "off", "jobOpenDay": "", "role": "1",
                         "isNewModel": False, "poNum": 0},
              "permit": _more_permit(changes)}
    if drop_permit:
        del result["permit"]
    return _json_raw(200, _envelope(result))


def _identity_ok(*, is_atsp=False, drop_is_atsp=False):
    data = {key: False for key in IDENTITY_KEYS}
    data["isHasSubDepartments"] = True
    data["isAtsp"] = is_atsp
    if drop_is_atsp:
        del data["isAtsp"]
    metadata = {"bannedAreaCodes": [f"SYN{n:07d}" for n in range(9)]}
    return _json_raw(200, {"data": data, "metadata": metadata})


def _delete_env(status="SUCCESS", message=""):
    return {"status": status, "message": message, "url": "", "data": None, "result": None}


def _delete_ok(status="SUCCESS", message=""):
    return _json_raw(200, _delete_env(status=status, message=message))


def _delete_script(rows, *, more=None, identity=None, delete=None, reread=None):
    """The delete walk as the front end makes it: job list, /api/job/more, identity, the
    delete POST, and the reread. The default reread no longer lists the job."""
    return [_list_ok(rows),
            more if more is not None else _more_ok(),
            identity if identity is not None else _identity_ok(),
            delete if delete is not None else _delete_ok(),
            reread if reread is not None else _list_ok([])]


def _b64_of(obj, *, padded=True):
    encoded = base64.b64encode(json.dumps(obj).encode("utf-8")).decode("ascii")
    return encoded if padded else encoded.rstrip("=")


def _ats_segment(state, *, padded=True):
    return _b64_of({ATS_STATE_KEY: state}, padded=padded)


def _base64url_segment(state):
    """A base64url second segment that decodes to `state`. The filler is eight '?' bytes (0x3F).
    Any run of five or more such bytes holds an aligned 3-byte group, which encodes to 'Pz8/',
    so the standard encoding carries '/' and base64url writes it as '_'."""
    obj = {ATS_STATE_KEY: state, "SYNTH-FILLER": "?" * 8}
    segment = base64.urlsafe_b64encode(json.dumps(obj).encode("utf-8")).decode("ascii").rstrip("=")
    assert "-" in segment or "_" in segment, "the segment must exercise a base64url character"
    return segment


def _ithp(segment):
    return {"name": "ithp", "value": f"{JWT_HEADER}.{segment}.SYNTH-SIG",
            "domain": "auth.vip.104.com.tw", "path": "/"}


async def _spend_slots(tmp_path, monkeypatch, count):
    """Spend `count` slots of the rolling window through the public tools only. open_jobs on a
    job that is already open sends exactly one request (the job list) and is refused."""
    ctx, _info = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_list_ok([_on(J1)]) for _ in range(count)])
    for _ in range(count):
        assert _is_error(await _tool("open_jobs")(ctx=ctx, jobnos=[J1]))
    assert _routes(spy) == [JOB_LIST] * count
    return ctx


DELETE_GATE_REFUSALS = [
    ("job6-denied", {"changes": {"job__6": False}}, {}, NO_PERMISSION),
    ("job6-absent", {"changes": {"job__6": None}}, {}, NO_PERMISSION),
    ("permit-absent", {"drop_permit": True}, {}, NO_PERMISSION),
    ("ats-pro-account", {}, {"is_atsp": True}, ATSP_TIP),
]

NO_ATS_PLAN_COOKIES = [
    ("inactive", [_ithp(_ats_segment("inactive"))]),
    ("key-absent", [_ithp(_b64_of({"SYNTH-OTHER": "x"}))]),
    ("cookie-absent", []),
    ("not-base64", [_ithp("!!SYNTH-NOT-BASE64!!")]),
]


def _delete_path_script(path):
    if path == "rejected":
        return _delete_script([_off(J1)],
                              delete=_delete_ok(status="ACCESS_DENIED", message="SYNTH-REJECT-TEXT"))
    if path == "timeout":
        return _delete_script([_off(J1)], delete=TimeoutError("SYNTH-TIMEOUT"))
    return _delete_script([_off(J1)])


# ── delete_job: parameter check and the job list (T-2, T-5..T-9, T-33) ──────────


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", ["12a", "10 01", "1000001a", "-1000001"])
async def test_delete_a_jobno_that_is_not_all_digits_is_refused_naming_list_jobs_with_no_request(
        tmp_path, monkeypatch, bad):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [], jobno=bad)
    assert _is_error(result)
    assert "list_jobs" in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_delete_the_first_request_reads_job_list_page_one(tmp_path, monkeypatch):
    _result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_on(J1)])], jobno=J1)
    assert _routes(spy)[0] == JOB_LIST
    assert ("page", "1") in _params(spy, JOB_LIST)[0]


@pytest.mark.asyncio
async def test_delete_a_job_list_of_two_pages_is_refused_after_one_request(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job",
                                [_list_ok([_off(J1)], total_page=2)], jobno=J1)
    assert _is_error(result)
    assert "一頁" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_delete_a_job_missing_from_the_list_is_named_after_one_request(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_off(J2)])], jobno=J1)
    assert _is_error(result)
    assert J1 in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["expired", "challenge", "job_data_not_a_list"])
async def test_delete_a_failed_first_job_list_read_is_an_error_and_sends_nothing_more(
        tmp_path, monkeypatch, failure):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_failure(failure)], jobno=J1)
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_delete_sends_the_listed_jobno_as_the_form_field(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)]), jobno=J1)
    assert "error" not in result
    assert _pairs(_bodies(spy, DELETE)[0]) == [("jobno", J1)]


@pytest.mark.asyncio
async def test_delete_job_publishes_one_string_parameter_named_jobno_and_no_list():
    from mcp.server.fastmcp import FastMCP
    mcp = FastMCP("job-switch-schema")
    _js().register_job_switch_tools(mcp)
    published = {tool.name: tool for tool in await mcp.list_tools()}
    properties = published["delete_job"].inputSchema.get("properties", {})
    assert set(properties) == {"jobno"}
    assert properties["jobno"].get("type") == "string"


# ── delete_job: state gates before the write (T-34, T-35, T-36) ─────────────────


@pytest.mark.asyncio
async def test_only_a_closed_job_can_be_deleted_so_an_open_one_is_refused_after_one_request(
        tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_on(J1)])], jobno=J1)
    assert _is_error(result)
    assert "已關閉" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_job_104_does_not_allow_deleting_is_refused_after_one_request(tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job",
                                [_list_ok([_off(J1, can_delete=False)])], jobno=J1)
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_104_access_denied_on_the_delete_is_an_error_with_104s_message_after_four_requests(
        tmp_path, monkeypatch):
    script = _delete_script([_off(J1)],
                            delete=_delete_ok(status="ACCESS_DENIED", message="SYNTH-DELETE-DENIED"))
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert _is_error(result)
    assert "SYNTH-DELETE-DENIED" in result["error"]
    assert _routes(spy) == DELETE_ROUTES[:4]


# ── delete_job: the /api/job/more permit and the ATS identity read (T-71, T-73) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("label, more, identity, text", DELETE_GATE_REFUSALS,
                         ids=[gate[0] for gate in DELETE_GATE_REFUSALS])
async def test_the_front_end_delete_gate_refuses_after_the_identity_read_and_sends_no_delete(
        tmp_path, monkeypatch, label, more, identity, text):
    script = [_list_ok([_off(J1)]), _more_ok(**more), _identity_ok(**identity), _delete_ok(), _list_ok([])]
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert _is_error(result)
    assert text in result["error"]
    assert _routes(spy) == DELETE_ROUTES[:3]


@pytest.mark.asyncio
async def test_an_absent_is_atsp_flag_is_read_as_not_set_and_the_delete_goes_on(tmp_path, monkeypatch):
    script = [_list_ok([_off(J1)]), _more_ok(), _identity_ok(drop_is_atsp=True), _delete_ok(), _list_ok([])]
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert "error" not in result
    assert _routes(spy) == DELETE_ROUTES


@pytest.mark.asyncio
async def test_a_delete_that_passes_every_check_sends_five_requests_in_order_with_their_parameters(
        tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)]), jobno=J1)
    assert "error" not in result
    assert _routes(spy) == DELETE_ROUTES
    assert ("jobno", J1) in _params(spy, MORE)[0]
    assert ("job_setting", "SYNTH-SETTING") in _params(spy, MORE)[0]
    assert _pairs(_bodies(spy, DELETE)[0]) == [("jobno", J1)]
    assert _result_by(result, "deleted") == {J1: "confirmed"}


# ── delete_job: the web-account rule (T-77) ─────────────────────────────────────


@pytest.mark.asyncio
async def test_delete_on_a_web_account_is_refused_after_one_request_pointing_to_the_backend(
        tmp_path, monkeypatch):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job",
                                [_list_ok([_off(J1)], account={"isWeb": True})], jobno=J1)
    assert _is_error(result)
    assert WEB_REFUSAL in result["error"]
    assert "後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_delete_with_the_web_flag_absent_reads_the_more_menu_next(tmp_path, monkeypatch):
    script = [_list_ok([_off(J1)], omit=("isWeb",)), _more_ok(), _identity_ok(), _delete_ok(), _list_ok([])]
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert _routes(spy)[:2] == [JOB_LIST, MORE]
    assert "error" not in result


# ── delete_job: reading the ATS plan from the ithp cookie (T-72) ───────────────


@pytest.mark.asyncio
async def test_an_active_plan_in_unpadded_standard_base64_refuses_after_the_job_list(tmp_path, monkeypatch):
    segment = _ats_segment("active", padded=False)
    assert "=" not in segment and len(segment) % 4 != 0  # the shape of a real JWT second segment
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_off(J1)])],
                                cookies=[_ithp(segment)], jobno=J1)
    assert _is_error(result)
    assert "後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_an_active_plan_in_padded_standard_base64_refuses_after_the_job_list(tmp_path, monkeypatch):
    segment = _ats_segment("active", padded=True)
    assert segment.endswith("=")
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_off(J1)])],
                                cookies=[_ithp(segment)], jobno=J1)
    assert _is_error(result)
    assert "後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_converting_plan_refuses_after_the_job_list(tmp_path, monkeypatch):
    segment = _ats_segment("converting")
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_off(J1)])],
                                cookies=[_ithp(segment)], jobno=J1)
    assert _is_error(result)
    assert "後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_a_base64url_segment_is_no_ats_plan_even_when_it_decodes_to_active(tmp_path, monkeypatch):
    segment = _base64url_segment("active")
    assert "-" in segment or "_" in segment
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)]),
                                cookies=[_ithp(segment)], jobno=J1)
    assert "error" not in result
    assert _routes(spy) == DELETE_ROUTES


@pytest.mark.asyncio
@pytest.mark.parametrize("label, cookies", NO_ATS_PLAN_COOKIES, ids=[case[0] for case in NO_ATS_PLAN_COOKIES])
async def test_no_ats_plan_reading_lets_the_delete_go_on_to_the_full_walk(
        tmp_path, monkeypatch, label, cookies):
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)]),
                                cookies=cookies, jobno=J1)
    assert "error" not in result
    assert _routes(spy) == DELETE_ROUTES


# ── delete_job: the write, its failures and the reread (T-36, T-39, T-41..T-43, T-45) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("rows, expected", [([], "confirmed"), ([_off(J1)], "unconfirmed")],
                         ids=["absent-from-reread", "still-listed"])
async def test_delete_is_confirmed_only_when_the_job_is_gone_from_the_reread(
        tmp_path, monkeypatch, rows, expected):
    script = _delete_script([_off(J1)], reread=_list_ok(rows))
    result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert _result_by(result, "deleted") == {J1: expected}


@pytest.mark.asyncio
async def test_delete_reread_past_page_one_is_unconfirmed_and_named(tmp_path, monkeypatch):
    script = _delete_script([_off(J1)], reread=_list_ok([], total_page=2))
    result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert "error" not in result
    assert _result_by(result, "deleted") == {J1: "unconfirmed"}
    assert any(J1 in w for w in _warnings(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["expired", "challenge", "job_data_not_a_list"])
async def test_a_failed_reread_after_an_accepted_delete_is_a_success_shape_all_unconfirmed(
        tmp_path, monkeypatch, failure):
    script = _delete_script([_off(J1)], reread=_list_failure(failure))
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert set(result) == DELETE_SUCCESS_KEYS
    assert _result_by(result, "deleted") == {J1: "unconfirmed"}
    assert any("重讀" in w for w in _warnings(result))
    assert _routes(spy) == DELETE_ROUTES


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["timeout", "non_json", "server_error"])
async def test_an_ambiguous_delete_failure_rereads_and_says_the_reread_decides(tmp_path, monkeypatch, failure):
    item = {
        "timeout": TimeoutError("SYNTH-TIMEOUT"),
        "non_json": _non_json_raw(),
        "server_error": _server_error_raw(),
    }[failure]
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)], delete=item),
                                jobno=J1)
    assert "error" not in result
    assert _routes(spy) == DELETE_ROUTES
    assert any("重讀" in w for w in _warnings(result))


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["expired", "forbidden", "challenge"])
async def test_a_delete_104_did_not_accept_is_an_error_with_no_reread(tmp_path, monkeypatch, failure):
    item = {"expired": _expired_raw(), "forbidden": _forbidden_raw(), "challenge": _challenge_raw()}[failure]
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)], delete=item),
                                jobno=J1)
    assert _is_error(result)
    assert _routes(spy) == DELETE_ROUTES[:4]


# ── delete_job: return shapes, descriptions, registration (T-48, T-49, T-51) ────


@pytest.mark.asyncio
async def test_every_delete_outcome_is_one_of_the_closed_return_shapes(tmp_path, monkeypatch):
    scenarios = [
        ({"jobno": "12a"}, []),
        ({"jobno": J1}, [_list_ok([_on(J1)])]),
        ({"jobno": J1}, [_list_ok([_off(J1, can_delete=False)])]),
        ({"jobno": J1}, [_list_ok([_off(J1)]), _more_ok(drop_permit=True)]),
        ({"jobno": J1}, [_list_ok([_off(J1)]), _more_ok(), _identity_ok(is_atsp=True)]),
        ({"jobno": J1}, _delete_script([_off(J1)],
                                       delete=_delete_ok(status="ACCESS_DENIED", message="SYNTH-X"))),
        ({"jobno": J1}, _delete_script([_off(J1)])),
        ({"jobno": J1}, _delete_script([_off(J1)], reread=_list_failure("expired"))),
        ({"jobno": J1}, _delete_script([_off(J1)], delete=_expired_raw())),
    ]
    for kwargs, script in scenarios:
        result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, **kwargs)
        assert set(result) in [DELETE_SUCCESS_KEYS] + DELETE_ERROR_KEYS, (kwargs, result)


def test_delete_description_states_immediacy_visibility_the_human_check_and_irreversibility():
    text = _descriptions()["delete_job"]
    assert re.search("立[刻即]生效", text)
    assert "求職者" in text
    assert "真人" in text and "確認" in text
    assert "無法復原" in text
    assert "應徵履歷" in text and "聯絡紀錄" in text


def test_register_all_tools_includes_delete_job_within_the_description_budget():
    from mcp104.main import register_all_tools
    mcp = _FakeMCP()
    register_all_tools(mcp)
    assert "delete_job" in mcp.tools
    assert len(mcp.descriptions["delete_job"]) <= 2048 - 100


# ── delete_job: identity and cookies never reach a return or a log (T-65) ───────


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["success", "rejected", "timeout", "unexpected"])
async def test_cookies_and_operator_identity_reach_no_delete_return_and_no_log(
        tmp_path, monkeypatch, caplog, path):
    caplog.set_level(logging.DEBUG)
    if path == "unexpected":
        monkeypatch.setattr(_js(), "confirm_deleted", _boom)
    result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_path_script(path),
                                 cookies=SESSION_COOKIES, jobno=J1)
    seen = _strings(result) + [caplog.text]
    for secret in COOKIE_STRINGS + OPERATOR_STRINGS:
        assert not any(secret in text for text in seen), secret


# ── delete_job: the throttle window (T-52, and the 3-slot rows for close and open) ──


@pytest.mark.asyncio
async def test_with_four_slots_free_delete_needs_five_and_is_refused_before_any_request(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP104_MAX_REQUESTS_PER_HOUR", "5")
    ctx = await _spend_slots(tmp_path, monkeypatch, 1)
    spy = _install(monkeypatch, [])
    result = await _tool("delete_job")(ctx=ctx, jobno=J1)
    assert _is_error(result)
    assert "retry_after_seconds" in result
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_with_three_slots_free_a_four_slot_write_is_refused_before_any_request(tmp_path, monkeypatch, tool):
    monkeypatch.setenv("MCP104_MAX_REQUESTS_PER_HOUR", "4")
    ctx = await _spend_slots(tmp_path, monkeypatch, 1)
    spy = _install(monkeypatch, [])
    result = await _tool(tool)(ctx=ctx, **_kw(tool, [J1]))
    assert _is_error(result)
    assert "retry_after_seconds" in result
    assert spy.calls == []


# ── delete_job: message wording, unexpected errors, source words (T-46, T-47, T-50) ──


@pytest.mark.asyncio
async def test_a_delete_message_with_an_unconfirmed_job_says_it_is_not_a_failure_and_not_to_resend(
        tmp_path, monkeypatch):
    script = _delete_script([_off(J1)], reread=_list_ok([_off(J1)]))
    result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
    assert _result_by(result, "deleted") == {J1: "unconfirmed"}
    message = result["message"]
    assert "不代表失敗" in message
    assert "list_jobs" in message
    assert "重送" in message


@pytest.mark.asyncio
async def test_an_all_confirmed_delete_message_says_the_reread_job_list_is_the_basis(tmp_path, monkeypatch):
    result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)]), jobno=J1)
    assert _result_by(result, "deleted") == {J1: "confirmed"}
    assert "重讀" in result["message"]


@pytest.mark.asyncio
async def test_an_unexpected_error_before_the_delete_is_an_error_saying_nothing_was_sent(tmp_path, monkeypatch):
    monkeypatch.setattr(_js(), "preflight_rows", _boom)
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", [_list_ok([_off(J1)])], jobno=J1)
    assert _is_error(result)
    assert "沒有送出" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_an_unexpected_error_after_the_delete_is_success_shape_all_unconfirmed(tmp_path, monkeypatch):
    monkeypatch.setattr(_js(), "confirm_deleted", _boom)
    result, spy, _ = await _run(tmp_path, monkeypatch, "delete_job", _delete_script([_off(J1)]), jobno=J1)
    assert set(result) == DELETE_SUCCESS_KEYS
    assert _result_by(result, "deleted") == {J1: "unconfirmed"}
    assert any("無法判斷" in w for w in _warnings(result))
    assert _routes(spy) == DELETE_ROUTES


@pytest.mark.asyncio
async def test_no_delete_job_description_or_returned_text_names_a_person_as_the_source(tmp_path, monkeypatch):
    texts = [_descriptions()["delete_job"]]
    runs = [
        _delete_script([_off(J1)]),
        _delete_script([_off(J1)], reread=_list_ok([_off(J1)])),
        _delete_script([_off(J1)], delete=_delete_ok(status="ACCESS_DENIED", message="SYNTH-DENIED")),
        _delete_script([_off(J1)], delete=_json_raw(200, {"status": "SYNTH-UNKNOWN", "message": "SYNTH"})),
        [_list_ok([_on(J1)])],
        [_list_ok([_off(J1)]), _more_ok(drop_permit=True)],
        [_list_ok([_off(J1)]), _more_ok(), _identity_ok(is_atsp=True)],
    ]
    for script in runs:
        result, _spy, _ = await _run(tmp_path, monkeypatch, "delete_job", script, jobno=J1)
        texts.extend(_strings(result))
    for text in texts:
        assert not any(word in text for word in FORBIDDEN_SOURCE_WORDS), text
