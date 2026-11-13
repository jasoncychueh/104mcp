"""Tests for the four template tools in src/mcp104/tools/templates.py: create_template,
update_template, delete_template and list_templates (moved here from tests/test_messaging.py).

Written against design.md's declared interfaces and the measured shapes in
docs/104-site-facts.md §8.33 only (spec-tester Mode 1). The transport seam is the one
tests/test_events.py uses: tests/conftest.py's _SeqFetchSpy is patched into
browser.api_client.fetch and tools.helpers.fetch and replays scripted responses in call order.
classify(), guarded_sequence, the throttle window and the SQLite file are all real.

Every title, description, attachment name, job name and code in this file is synthetic.

Case coverage here: T-1..T-54, T-57..T-67, T-76, T-78..T-88. T-55 and T-77 live in
tests/test_contract_docs.py; T-56 belongs to tests/test_packaging.py.
"""

import json
import logging
import re
import sys
import time
from uuid import uuid4

import pytest
import pytest_asyncio

import mcp104.tools.helpers as helpers_mod
from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.browser.throttle import ThrottleAbort
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.discovery import register_discovery_tools
from mcp104.tools.helpers import get_session_id
from mcp104.tools.templates import (
    create_body,
    create_outcome,
    create_preflight,
    js_length,
    merge_update,
    parse_template_detail,
    parse_template_list,
    parse_template_types,
    register_template_tools,
    text_errors,
    update_outcome,
    used_by_jobs,
)
from tests.conftest import _SeqFetchSpy


# ── Synthetic constants ───────────────────────────────────────────────────────────────

_TYPES_PATH = "/bc-comm/template-type"
_LIST_PATH = "/bc-comm/template"
_ONE_PATH = "/bc-comm/template/{template_id}"
_CHECK_PATH = "/bc-comm/template/delete-check/{template_id}"

_SIX_TYPES = (
    ("0", "合成甲"), ("1", "合成乙"), ("2", "合成丙"),
    ("3", "合成丁"), ("4", "合成戊"), ("5", "合成己"),
)
_OLD_ID = "800001"
_NEW_ID = "900001"
_OLD_TITLE = "舊合成標題"
_OLD_DESC = "舊合成內容"
_TITLE = "合成標題"
_NEW_TITLE = "新合成標題"
_DESC = "合成內容，非真實信件。"
_TEMPLATE_KEYS = {"id", "title", "description", "type_id", "type_desc"}
_OK_WRITE = {"data": True, "metadata": {}}
_NOT_FOUND = {"code": "00802", "message": "找不到指定範本", "detail": []}
_IN_USE = {
    "code": "00803",
    "message": "SYNTH-IN-USE-MESSAGE",
    "detail": [
        {"jobNo": "12355016", "jobName": "合成職缺甲"},
        {"jobNo": "12355017", "jobName": "合成職缺乙"},
    ],
}
_CHALLENGE_TEXT = "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。"
# 404 bodies that are NOT the measured "範本不存在" code 00802 (T-88): another code, or none.
_OTHER_404_BODIES = (
    {"code": "99999", "message": "SYNTH-OTHER-404", "detail": []},
    {"message": "SYNTH-OTHER-404", "detail": []},
)


# ── Fixture / fake-transport plumbing ─────────────────────────────────────────────────

def _raw(status: int, content_type: str, body: str, parsed_json, location: str | None = None) -> RawResponse:
    return RawResponse(status=status, location=location, content_type=content_type,
                       body=body, body_bytes=body.encode("utf-8"), parsed_json=parsed_json)


def _json(body, status: int = 200) -> RawResponse:
    return _raw(status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)


def _challenge() -> RawResponse:
    return _raw(200, "text/html; charset=utf-8", _CHALLENGE_TEXT, None)


def _install(monkeypatch, scripted) -> _SeqFetchSpy:
    spy = _SeqFetchSpy(scripted)
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


def _sent(spy) -> list:
    """(method, path, params as a list of (name, str value), body) per request, in order."""
    return [(ep.method, ep.path, [(k, str(v)) for k, v in (params or [])], body)
            for ep, params, body in spy.calls]


def _verbs(spy) -> list:
    return [(ep.method, ep.path) for ep, _params, _body in spy.calls]


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
    register_template_tools(mcp)
    return mcp.tools, mcp.descriptions


TOOLS, TOOL_DESCRIPTIONS = _register_tools()
list_templates = TOOLS["list_templates"]
create_template = TOOLS["create_template"]
update_template = TOOLS["update_template"]
delete_template = TOOLS["delete_template"]


_OPENED_DATABASES: list[Database] = []


async def _new_session(tmp_path) -> tuple[FakeCtx, SessionInfo, Database]:
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight off SessionInfo.cookies; no BrowserContext is faked.
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


@pytest.fixture
def _int_str_limit_enforced():
    previous = sys.get_int_max_str_digits()
    sys.set_int_max_str_digits(4300)
    yield
    sys.set_int_max_str_digits(previous)


# ── Synthetic bodies in the measured shapes (docs/104-site-facts.md §8.33) ─────────────

def _row(id_: str, title: str, description: str, type_id: str = "1", type_desc: str = "合成乙") -> dict:
    """One template-list row: exactly the five string fields the list route returns."""
    return {"id": id_, "title": title, "description": description,
            "typeId": type_id, "typeDesc": type_desc}


def _list_body(rows, *, total=None, total_pages: int = 1, per_page: int = 100) -> dict:
    rows = list(rows)
    return {"data": rows, "metadata": {
        "perPage": per_page, "page": 1, "totalPage": total_pages,
        "total": len(rows) if total is None else total,
    }}


def _types_body(pairs=_SIX_TYPES) -> dict:
    return {"data": [{"typeId": code, "name": name, "amount": 0, "isDefault": True}
                     for code, name in pairs],
            "metadata": {"page": 1, "perPage": 6, "totalPage": 1, "total": len(pairs)}}


def _detail_body(id_: str, title: str, description: str, type_id: str = "1", files=()) -> dict:
    """Single-template read: {id, title, description, typeId, files} with no typeDesc (§8.33)."""
    return {"data": {"id": id_, "title": title, "description": description,
                     "typeId": type_id, "files": list(files)},
            "metadata": {}}


def _types_raw(pairs=_SIX_TYPES) -> RawResponse:
    return _json(_types_body(pairs))


def _list_raw(rows, **kwargs) -> RawResponse:
    return _json(_list_body(rows, **kwargs))


def _create_script(before_rows, after_rows, post=None) -> list:
    """Types, pre-write list, the POST, then the reread list."""
    return [
        _types_raw(),
        _list_raw(before_rows),
        post if post is not None else _json(_OK_WRITE),
        _list_raw(after_rows),
    ]


def _delete_ok_check() -> RawResponse:
    return _json({"data": True, "metadata": {}})


async def _create(ctx, title: str = _TITLE, description: str = _DESC, type_id: str = "1"):
    return await create_template(ctx=ctx, title=title, description=description, type_id=type_id)


def _fill_window(info, app, leave: int) -> None:
    """Spend the rolling window down to `leave` free slots, the way tests/test_jobs.py does."""
    now = time.time()
    for _ in range(app.config.max_requests_per_hour - leave):
        info.throttle.unpersisted_timestamps.append(now)


# ── Parameter checks — T-1..T-9 ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T1_blank_title_is_refused_before_any_request_and_takes_no_slot(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])
    before = len(info.throttle.request_timestamps)

    result = await create_template(ctx=ctx, title="   ", description=_DESC, type_id="1")

    assert "error" in result
    assert "created" not in result
    assert spy.calls == []
    assert len(info.throttle.request_timestamps) == before


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", [{"title": "   "}, {"type_id": "  "}], ids=["blank-title", "blank-type-id"])
async def test_T1_update_with_a_blank_title_or_type_is_refused_before_any_request_and_takes_no_slot(
        tmp_path, monkeypatch, blank):
    ctx, info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])
    before = len(info.throttle.request_timestamps)

    result = await update_template(ctx=ctx, template_id=_OLD_ID, **blank)

    assert "error" in result
    assert "updated" not in result
    assert spy.calls == []
    assert len(info.throttle.request_timestamps) == before


@pytest.mark.asyncio
async def test_T2_title_limit_counts_utf16_units_so_sixteen_passes_and_seventeen_fails(tmp_path, monkeypatch):
    sixteen = "甲" * 16
    assert js_length(sixteen) == 16
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script([], [_row(_NEW_ID, sixteen, _DESC)]))

    ok = await _create(ctx, title=sixteen)

    assert "error" not in ok
    assert ("POST", _LIST_PATH) in _verbs(spy)

    seventeen = "甲" * 17
    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install(monkeypatch, [])
    too_long = await _create(ctx2, title=seventeen)
    assert "error" in too_long
    assert "16" in too_long["error"] and "17" in too_long["error"]
    assert spy2.calls == []

    # Fifteen CJK characters and one emoji: 16 code points, but 17 UTF-16 units on the 104 page.
    emoji_title = "甲" * 15 + "\U0001F600"
    assert len(emoji_title) == 16
    assert js_length(emoji_title) == 17
    ctx3, _info3, _db3 = await _new_session(tmp_path)
    spy3 = _install(monkeypatch, [])
    emoji_result = await _create(ctx3, title=emoji_title)
    assert "error" in emoji_result
    assert spy3.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("blank", ["", "\n\t  \r\n"])
async def test_T3_blank_description_is_refused_before_any_request(tmp_path, monkeypatch, blank):
    assert blank.strip() == ""
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])

    created = await create_template(ctx=ctx, title=_TITLE, description=blank, type_id="1")
    updated = await update_template(ctx=ctx, template_id=_OLD_ID, description=blank)

    assert "error" in created
    assert "error" in updated
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T4_description_limit_is_one_thousand_units_for_create_and_update(tmp_path, monkeypatch):
    limit = "甲" * 1000
    assert js_length(limit) == 1000
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script([], [_row(_NEW_ID, _TITLE, limit)]))

    ok = await _create(ctx, description=limit)

    assert "error" not in ok
    assert ("POST", _LIST_PATH) in _verbs(spy)

    over = "甲" * 1001
    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install(monkeypatch, [])
    created = await _create(ctx2, description=over)
    assert "error" in created
    assert "1000" in created["error"] and "1001" in created["error"]
    assert spy2.calls == []

    ctx3, _info3, _db3 = await _new_session(tmp_path)
    spy3 = _install(monkeypatch, [])
    updated = await update_template(ctx=ctx3, template_id=_OLD_ID, description=over)
    assert "error" in updated
    assert spy3.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_type", ["", "   "])
async def test_T5_blank_type_id_is_refused_before_any_request(tmp_path, monkeypatch, bad_type):
    assert bad_type.strip() == ""
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])

    result = await _create(ctx, type_id=bad_type)

    assert "error" in result
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_id", ["", "abc", "12a"])
async def test_T6_a_template_id_that_is_not_all_digits_is_refused_and_names_list_templates(
        tmp_path, monkeypatch, bad_id):
    assert not (bad_id.isdigit() and bad_id != "")
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])

    updated = await update_template(ctx=ctx, template_id=bad_id, title=_TITLE)
    deleted = await delete_template(ctx=ctx, template_id=bad_id)

    assert "error" in updated and "list_templates" in updated["error"]
    assert "error" in deleted and "list_templates" in deleted["error"]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T7_update_with_no_field_to_change_is_refused_before_any_request(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])

    result = await update_template(ctx=ctx, template_id="123")

    assert "error" in result
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T8_title_and_description_are_sent_verbatim_with_no_trim_and_no_placeholder_change(
        tmp_path, monkeypatch):
    spaced_title = " 面試 "
    described = "\n開頭換行 #jname# 內容"
    assert spaced_title != spaced_title.strip()
    assert described.startswith("\n") and "#jname#" in described
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script([], [_row(_NEW_ID, spaced_title, described)]))

    await _create(ctx, title=spaced_title, description=described)

    posts = [body for ep, _params, body in spy.calls if ep.method == "POST"]
    assert len(posts) == 1
    assert posts[0]["title"] == spaced_title
    assert posts[0]["description"] == described


@pytest.mark.asyncio
async def test_T9_refused_parameters_leave_the_throttle_window_untouched(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [])
    before = len(info.throttle.request_timestamps)

    await create_template(ctx=ctx, title="", description=_DESC, type_id="1")
    await create_template(ctx=ctx, title=_TITLE, description="   ", type_id="1")
    await create_template(ctx=ctx, title=_TITLE, description=_DESC, type_id="")
    await update_template(ctx=ctx, template_id="abc", title=_TITLE)
    await update_template(ctx=ctx, template_id="123")
    await delete_template(ctx=ctx, template_id="")

    assert spy.calls == []
    assert len(info.throttle.request_timestamps) == before


# ── Type checks — T-10..T-12, T-57 ────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_T10_unknown_type_lists_every_code_and_name_and_sends_only_the_type_read(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_types_raw()])

    result = await _create(ctx, type_id="9")

    assert "error" in result
    assert _verbs(spy) == [("GET", _TYPES_PATH)]
    for code, name in _SIX_TYPES:
        assert name in result["error"], name
        assert code in result["error"], code


@pytest.mark.asyncio
async def test_T11_update_reads_the_type_list_only_when_a_type_is_given_and_before_the_put(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC)),
    ])
    await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)
    assert _TYPES_PATH not in [path for _method, path in _verbs(spy)]

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)),
        _types_raw(),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="2")),
    ])
    await update_template(ctx=ctx2, template_id=_OLD_ID, type_id="2")
    verbs = _verbs(spy2)
    assert ("GET", _TYPES_PATH) in verbs
    assert ("PUT", _ONE_PATH) in verbs
    assert verbs.index(("GET", _TYPES_PATH)) < verbs.index(("PUT", _ONE_PATH))


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["create", "update"])
async def test_T12_a_type_list_auth_failure_refuses_the_write(tmp_path, monkeypatch, which):
    unauthorised = _json({"message": "SYNTH-401"}, status=401)
    ctx, _info, _db = await _new_session(tmp_path)
    if which == "create":
        spy = _install(monkeypatch, [unauthorised])
        result = await _create(ctx)
    else:
        spy = _install(monkeypatch, [_json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)), unauthorised])
        result = await update_template(ctx=ctx, template_id=_OLD_ID, type_id="2")

    assert "error" in result
    assert not [m for m, _p in _verbs(spy) if m in ("POST", "PUT")]


@pytest.mark.asyncio
async def test_T57_update_to_a_type_missing_from_the_list_is_refused_before_the_put(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)),
        _types_raw(),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, type_id="9")

    assert "error" in result
    for _code, name in _SIX_TYPES:
        assert name in result["error"], name
    assert ("PUT", _ONE_PATH) not in _verbs(spy)


# ── create_template — T-13..T-24, T-80, T-82 ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_T13_create_sends_the_four_requests_in_order_with_the_right_params_and_body(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC)]
    spy = _install(monkeypatch, _create_script(before, after))

    await _create(ctx)

    assert _verbs(spy) == [("GET", _TYPES_PATH), ("GET", _LIST_PATH), ("POST", _LIST_PATH), ("GET", _LIST_PATH)]
    sent = _sent(spy)
    assert sent[0][2] == [("withoutPaging", "1")]
    for index in (1, 3):
        params = sent[index][2]
        assert dict(params) == {"page": "1", "typeId": "1"}, params
    assert sent[2][3] == {"title": _TITLE, "description": _DESC, "typeId": "1", "files": []}
    assert sum(1 for method, _p in _verbs(spy) if method == "POST") == 1


@pytest.mark.asyncio
async def test_T14_create_refuses_when_the_pre_write_list_spans_more_than_one_page(tmp_path, monkeypatch):
    rows = [_row(str(700000 + i), f"舊{i}", _OLD_DESC) for i in range(100)]
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_types_raw(), _list_raw(rows, total=150, total_pages=2)])

    result = await _create(ctx)

    assert "error" in result
    assert ("POST", _LIST_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T15_create_refuses_a_full_page_and_sends_when_one_slot_is_left(tmp_path, monkeypatch):
    full = [_row(str(700000 + i), f"舊{i}", _OLD_DESC) for i in range(100)]
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_types_raw(), _list_raw(full)])
    result = await _create(ctx)
    assert "error" in result
    assert ("POST", _LIST_PATH) not in _verbs(spy)

    almost = full[:99]
    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install(monkeypatch, _create_script(almost, almost + [_row(_NEW_ID, _TITLE, _DESC)]))
    await _create(ctx2)
    assert ("POST", _LIST_PATH) in _verbs(spy2)


@pytest.mark.asyncio
async def test_T16_create_refuses_when_the_listed_rows_disagree_with_the_total(tmp_path, monkeypatch):
    rows = [_row("700001", _OLD_TITLE, _OLD_DESC), _row("700002", _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_types_raw(), _list_raw(rows, total=3)])

    result = await _create(ctx)

    assert "error" in result
    assert ("POST", _LIST_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T17_a_challenge_page_on_the_pre_write_list_refuses_without_a_post(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_types_raw(), _challenge()])

    result = await _create(ctx)

    assert "error" in result
    assert "機器人驗證" in result["error"]
    assert ("POST", _LIST_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T18_create_confirms_on_exactly_one_matching_new_row_and_returns_the_five_keys(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC, type_id="1", type_desc="合成乙")]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, after))

    result = await _create(ctx)

    assert result["created"] == "confirmed"
    assert result["template"] == {
        "id": _NEW_ID, "title": _TITLE, "description": _DESC,
        "type_id": "1", "type_desc": "合成乙",
    }
    assert isinstance(result["message"], str)
    assert isinstance(result["warnings"], list)


@pytest.mark.asyncio
async def test_T19_create_with_no_new_row_is_unconfirmed_and_says_not_to_resend(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, before))

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None
    text = result["message"] + " ".join(result["warnings"])
    assert "重送" in text
    assert "後台" in text


@pytest.mark.asyncio
async def test_T20_create_with_two_new_rows_is_unconfirmed_even_if_one_matches(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC), _row("900002", "別人的標題", "別人的內容")]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, after))

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None


@pytest.mark.asyncio
async def test_T21_create_with_one_new_row_whose_title_differs_is_unconfirmed(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, "不同的標題", _DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, after))

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None


@pytest.mark.asyncio
async def test_T22_create_whose_reread_spans_two_pages_is_unconfirmed_with_a_warning(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _types_raw(), _list_raw(before), _json(_OK_WRITE),
        _list_raw(after, total=150, total_pages=2),
    ])

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None
    assert len(result["warnings"]) >= 1


@pytest.mark.asyncio
async def test_T22_create_whose_one_page_reread_disagrees_with_its_total_is_unconfirmed(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _types_raw(), _list_raw(before), _json(_OK_WRITE),
        _list_raw(after, total=len(after) + 1),
    ])

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None
    assert len(result["warnings"]) >= 1


@pytest.mark.asyncio
async def test_T23_create_whose_reread_fails_is_unconfirmed_with_the_reason_in_warnings(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _types_raw(), _list_raw(before), _json(_OK_WRITE),
        _json({"message": "SYNTH-401"}, status=401),
    ])

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None
    assert len(result["warnings"]) >= 1


@pytest.mark.asyncio
async def test_T24_a_200_on_the_post_alone_does_not_confirm_the_create(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, before, post=_json(_OK_WRITE, status=200)))

    result = await _create(ctx)

    assert "error" not in result
    assert result["created"] == "unconfirmed"
    assert result["template"] is None


@pytest.mark.asyncio
async def test_T80_the_request_after_the_post_repeats_the_pre_write_list_read(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script(before, after))

    await _create(ctx)

    sent = _sent(spy)
    assert len(sent) == 4
    assert sent[3][:3] == sent[1][:3]


@pytest.mark.asyncio
async def test_T82_the_first_template_in_a_category_is_confirmed_against_an_empty_baseline(tmp_path, monkeypatch):
    after = [_row(_NEW_ID, _TITLE, _DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _types_raw(),
        _list_raw([], total=0, total_pages=0),
        _json(_OK_WRITE),
        _list_raw(after),
    ])

    result = await _create(ctx)

    assert result["created"] == "confirmed"
    assert result["template"]["id"] == _NEW_ID


# ── update_template — T-25..T-35, T-85 (update half), T-86 ───────────────────────────

@pytest.mark.asyncio
async def test_T25_update_title_only_sends_the_read_description_and_type_with_empty_files(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _DESC, type_id="1")),
    ])

    await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    puts = [body for ep, _params, body in spy.calls if ep.method == "PUT"]
    assert puts == [{"title": _NEW_TITLE, "description": _DESC, "typeId": "1", "files": []}]


@pytest.mark.asyncio
async def test_T26_update_of_a_missing_template_is_refused_without_a_put(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(_NOT_FOUND, status=404)])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert "不存在" in result["error"]
    assert ("PUT", _ONE_PATH) not in _verbs(spy)
    assert len(spy.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("not_found_body", _OTHER_404_BODIES, ids=["other-code", "no-code"])
async def test_T88_update_first_read_404_without_the_not_found_code_is_a_generic_error_not_a_missing_template(
        tmp_path, monkeypatch, not_found_body):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(not_found_body, status=404)])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert "不存在" not in result["error"]
    assert ("PUT", _ONE_PATH) not in _verbs(spy)
    assert len(spy.calls) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("files_value", ["absent", None])
async def test_T27_a_read_without_a_readable_attachment_list_is_refused_and_says_attachments_were_not_overwritten(
        tmp_path, monkeypatch, files_value):
    body = _detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)
    if files_value == "absent":
        del body["data"]["files"]
    else:
        body["data"]["files"] = None
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(body)])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert "附件" in result["error"]
    assert "沒有送出" in result["error"]
    assert ("PUT", _ONE_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T28_a_non_list_attachment_value_is_refused_without_a_put(tmp_path, monkeypatch):
    body = _detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)
    body["data"]["files"] = "SYNTH-NOT-A-LIST"
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(body)])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert ("PUT", _ONE_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T29_an_update_that_changes_nothing_is_refused_after_a_single_read(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1"))])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_OLD_TITLE)

    assert "error" in result
    assert len(spy.calls) == 1


@pytest.mark.asyncio
async def test_T30_update_confirms_on_a_matching_reread_with_the_five_list_keys_and_no_warnings(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "confirmed"
    assert set(result["template"].keys()) == _TEMPLATE_KEYS
    assert "type_desc" in result["template"]
    assert result["template"]["type_desc"] is None
    assert result["template"]["id"] == _OLD_ID
    assert result["template"]["title"] == _NEW_TITLE
    assert result["warnings"] == []


@pytest.mark.asyncio
async def test_T31_update_whose_reread_still_shows_the_old_title_is_unconfirmed_with_the_reread_template(
        tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "unconfirmed"
    assert result["template"] is not None
    assert set(result["template"].keys()) == _TEMPLATE_KEYS
    assert result["template"]["type_desc"] is None


@pytest.mark.asyncio
async def test_T32_update_whose_reread_has_an_attachment_is_unconfirmed(tmp_path, monkeypatch):
    attachment = {"name": "合成附件", "fileToken": "syn-token-1"}
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1", files=[attachment])),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "unconfirmed"


@pytest.mark.asyncio
async def test_T33_update_whose_reread_is_not_found_is_unconfirmed_and_says_the_template_is_gone(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_NOT_FOUND, status=404),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "unconfirmed"
    assert result["template"] is None
    assert "不存在" in " ".join(result["warnings"])


@pytest.mark.asyncio
async def test_T34_a_200_put_with_a_mismatching_reread_is_unconfirmed(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, "別的內容", type_id="1")),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "unconfirmed"


@pytest.mark.asyncio
async def test_T35_a_put_answering_data_false_adds_one_warning_and_the_reread_decides(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json({"data": False, "metadata": {}}),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "confirmed"
    assert len(result["warnings"]) == 1


@pytest.mark.asyncio
async def test_T45_a_put_refused_with_401_is_an_error_and_is_not_reread(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json({"message": "SYNTH-401"}, status=401),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert len(spy.calls) == 2


@pytest.mark.asyncio
async def test_T86_an_attached_template_is_not_modified_and_the_error_points_to_the_backend(tmp_path, monkeypatch):
    attachment = {"name": "合成附件", "fileToken": "syn-token-2"}
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1", files=[attachment])),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert "附件" in result["error"]
    assert "後台" in result["error"]
    assert len(spy.calls) == 1
    assert ("PUT", _ONE_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T85_update_whose_reread_differs_only_by_a_trailing_newline_is_unconfirmed_with_a_whitespace_note(
        tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC + "\n", type_id="1")),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "unconfirmed"
    assert any(("空白" in w or "換行" in w) for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_T85_create_whose_title_differs_only_by_crlf_is_unconfirmed_with_a_whitespace_note(tmp_path, monkeypatch):
    sent_title = "第一行\r\n第二行"
    reread_title = "第一行\n第二行"
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, reread_title, _DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, after))

    result = await _create(ctx, title=sent_title)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None
    assert any(("空白" in w or "換行" in w) for w in result["warnings"]), result["warnings"]


# ── delete_template — T-36..T-43, T-46, T-83, T-87 and the T-39 / T-40 refusals ───────

@pytest.mark.asyncio
async def test_T36_a_400_in_use_refusal_carries_the_message_and_the_jobs_and_sends_no_delete(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(_IN_USE, status=400)])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert "error" in result
    assert "SYNTH-IN-USE-MESSAGE" in result["error"]
    assert result["used_by_jobs"] == [
        {"jobno": "12355016", "job": "合成職缺甲"},
        {"jobno": "12355017", "job": "合成職缺乙"},
    ]
    assert not [m for m, _p in _verbs(spy) if m == "DELETE"]


@pytest.mark.asyncio
async def test_T37_a_403_in_use_refusal_is_the_same_refusal_and_not_a_block(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [_json(_IN_USE, status=403)])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert "error" in result
    assert "SYNTH-IN-USE-MESSAGE" in result["error"]
    assert result["used_by_jobs"] == [
        {"jobno": "12355016", "job": "合成職缺甲"},
        {"jobno": "12355017", "job": "合成職缺乙"},
    ]
    assert "封鎖" not in result["error"]


@pytest.mark.asyncio
async def test_T38_an_in_use_refusal_without_a_readable_job_list_has_no_used_by_jobs_key(tmp_path, monkeypatch):
    body = {"code": "00803", "message": "SYNTH-IN-USE-MESSAGE", "detail": "SYNTH-NOT-A-LIST"}
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(body, status=400)])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert "error" in result
    assert "used_by_jobs" not in result
    assert "職缺" in result["error"]
    assert "SYNTH-IN-USE-MESSAGE" in result["error"]
    assert not [m for m, _p in _verbs(spy) if m == "DELETE"]


@pytest.mark.asyncio
async def test_T39_a_check_on_a_missing_template_is_an_error_and_sends_no_delete(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(_NOT_FOUND, status=404)])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert "error" in result
    assert "不存在" in result["error"]
    assert not [m for m, _p in _verbs(spy) if m == "DELETE"]


@pytest.mark.asyncio
async def test_T40_a_challenge_page_on_the_check_is_refused_and_sends_no_delete(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_challenge()])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert "error" in result
    assert "機器人驗證" in result["error"]
    assert not [m for m, _p in _verbs(spy) if m == "DELETE"]


@pytest.mark.asyncio
async def test_T41_a_passing_check_then_delete_then_a_not_found_reread_confirms_the_delete(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _delete_ok_check(),
        _json(_OK_WRITE),
        _json(_NOT_FOUND, status=404),
    ])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert result["deleted"] == "confirmed"
    assert result["template_id"] == _OLD_ID
    assert _verbs(spy) == [("GET", _CHECK_PATH), ("DELETE", _ONE_PATH), ("GET", _ONE_PATH)]
    delete_call = spy.calls[1]
    assert ("template_id", _OLD_ID) in list(delete_call[1])
    assert delete_call[2] is None


@pytest.mark.asyncio
async def test_T42_a_delete_followed_by_a_reread_that_still_finds_the_template_is_unconfirmed(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _delete_ok_check(),
        _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)),
    ])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert result["deleted"] == "unconfirmed"


@pytest.mark.asyncio
async def test_T43_a_delete_followed_by_a_failed_reread_is_unconfirmed_with_the_reason(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _delete_ok_check(),
        _json(_OK_WRITE),
        _json({"message": "SYNTH-500"}, status=500),
    ])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert result["deleted"] == "unconfirmed"
    assert len(result["warnings"]) >= 1


@pytest.mark.asyncio
@pytest.mark.parametrize("not_found_body", _OTHER_404_BODIES, ids=["other-code", "no-code"])
async def test_T88_delete_reread_404_without_the_not_found_code_is_unconfirmed(tmp_path, monkeypatch, not_found_body):
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _delete_ok_check(),
        _json(_OK_WRITE),
        _json(not_found_body, status=404),
    ])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert result["deleted"] == "unconfirmed"


@pytest.mark.asyncio
async def test_T46_a_delete_that_times_out_is_not_resent_and_is_confirmed_by_the_reread(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [
        _delete_ok_check(),
        TimeoutError("SYNTH-TIMEOUT"),
        _json(_NOT_FOUND, status=404),
    ])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert result["deleted"] == "confirmed"
    assert len([m for m, _p in _verbs(spy) if m == "DELETE"]) == 1
    assert len(result["warnings"]) >= 1


@pytest.mark.asyncio
async def test_T83_a_200_check_carrying_an_in_use_code_and_no_data_still_refuses_without_a_delete(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [_json(_IN_USE, status=200)])

    result = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert "error" in result
    assert result["used_by_jobs"] == [
        {"jobno": "12355016", "job": "合成職缺甲"},
        {"jobno": "12355017", "job": "合成職缺乙"},
    ]
    assert not [m for m, _p in _verbs(spy) if m == "DELETE"]


@pytest.mark.asyncio
async def test_T87_a_check_answering_data_false_is_refused_and_so_is_a_non_boolean_data(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy_false = _install(monkeypatch, [_json({"data": False, "metadata": {}})])
    refused_false = await delete_template(ctx=ctx, template_id=_OLD_ID)
    assert "error" in refused_false
    assert "沒有確認" in refused_false["error"]
    assert "結構異常" not in refused_false["error"]
    assert len(spy_false.calls) == 1
    assert not [m for m, _p in _verbs(spy_false) if m == "DELETE"]

    for odd in ({}, "true"):
        ctx2, _info2, _db2 = await _new_session(tmp_path)
        spy_odd = _install(monkeypatch, [_json({"data": odd, "metadata": {}})])
        refused_odd = await delete_template(ctx=ctx2, template_id=_OLD_ID)
        assert "error" in refused_odd
        assert "104 回應結構異常" in refused_odd["error"]
        assert refused_odd["error"] != refused_false["error"]
        assert len(spy_odd.calls) == 1
        assert not [m for m, _p in _verbs(spy_odd) if m == "DELETE"]


# ── Wrong-host and refused writes — T-44, T-78 ────────────────────────────────────────

@pytest.mark.asyncio
async def test_T44_a_post_refused_with_a_validation_error_returns_the_104_message_and_no_reread(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    refused = _json({"code": "SYN-VALIDATION", "message": "SYNTH-VALIDATION-MESSAGE", "detail": []}, status=400)
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script(before, before, post=refused))

    result = await _create(ctx)

    assert "error" in result
    assert "SYNTH-VALIDATION-MESSAGE" in result["error"]
    assert len(spy.calls) == 3


@pytest.mark.asyncio
async def test_T47_a_post_with_a_non_json_answer_is_reread_and_the_reread_decides(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    after = before + [_row(_NEW_ID, _TITLE, _DESC)]
    not_json = _raw(200, "text/plain; charset=utf-8", "not json", None)
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script(before, after, post=not_json))

    result = await _create(ctx)

    assert len(spy.calls) == 4
    assert result["created"] == "confirmed"
    assert len(result["warnings"]) >= 1


@pytest.mark.asyncio
async def test_T78_a_post_answered_by_a_wrong_host_page_is_an_error_and_is_not_reread(tmp_path, monkeypatch):
    wrong_host = _raw(404, "text/html; charset=utf-8", "<html>SYNTH-WRONG-HOST</html>", None)
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, _create_script(before, before, post=wrong_host))

    created = await _create(ctx)

    assert "error" in created
    assert len(spy.calls) == 3

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    spy2 = _install(monkeypatch, [_delete_ok_check(), wrong_host])
    deleted = await delete_template(ctx=ctx2, template_id=_OLD_ID)

    assert "error" in deleted
    assert len(spy2.calls) == 2


# ── Return shapes and bookkeeping — T-48..T-54, T-81 ──────────────────────────────────

@pytest.fixture
def _unprintable_list_row_body():
    """A reread row whose integer typeId is too long for str() under the 4300-digit limit."""
    body = {"data": [{"id": _NEW_ID, "title": _TITLE, "description": _DESC,
                      "typeId": 10 ** 4300, "typeDesc": "合成乙"}],
            "metadata": {"perPage": 100, "page": 1, "totalPage": 1, "total": 1}}
    return _raw(200, "application/json; charset=utf-8", "{}", body)


@pytest.mark.asyncio
async def test_T48_an_unexpected_error_after_the_post_is_unconfirmed_and_asks_for_a_backend_check(
        tmp_path, monkeypatch, _int_str_limit_enforced, _unprintable_list_row_body):
    # Precondition: under the 4300-digit limit this typeId really cannot be stringified, so any
    # later pass cannot come from the trigger silently not firing.
    with pytest.raises(ValueError):
        str(_unprintable_list_row_body.parsed_json["data"][0]["typeId"])
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _types_raw(), _list_raw(before), _json(_OK_WRITE), _unprintable_list_row_body,
    ])

    result = await _create(ctx)

    assert result["created"] == "unconfirmed"
    assert result["template"] is None
    assert "後台" in result["message"] + " ".join(result["warnings"])
    # The 內部錯誤 wording is what separates the unexpected-exception path from an ordinary mismatch.
    assert any("內部錯誤" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
async def test_T48_an_unexpected_error_before_the_post_is_an_error_that_says_nothing_was_sent(
        tmp_path, monkeypatch, _int_str_limit_enforced):
    unprintable_types = _raw(200, "application/json; charset=utf-8", "{}", {
        "data": [{"typeId": 10 ** 4300, "name": "合成甲", "amount": 0, "isDefault": True}],
        "metadata": {"page": 1, "perPage": 6, "totalPage": 1, "total": 1},
    })
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [unprintable_types])

    result = await _create(ctx)

    assert "error" in result
    assert "沒有送出" in result["error"]
    assert ("POST", _LIST_PATH) not in _verbs(spy)


def _unprintable_detail() -> RawResponse:
    """A single-template read whose integer typeId is too long for str() under the 4300-digit limit."""
    body = {"data": {"id": _OLD_ID, "title": _OLD_TITLE, "description": _OLD_DESC,
                     "typeId": 10 ** 4300, "files": []},
            "metadata": {}}
    return _raw(200, "application/json; charset=utf-8", "{}", body)


@pytest.mark.asyncio
async def test_T48_update_an_unexpected_error_before_the_put_is_an_error_that_says_nothing_was_sent(
        tmp_path, monkeypatch, _int_str_limit_enforced):
    unprintable_read = _unprintable_detail()
    # Precondition: under the 4300-digit limit this typeId really cannot be stringified.
    with pytest.raises(ValueError):
        str(unprintable_read.parsed_json["data"]["typeId"])
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install(monkeypatch, [unprintable_read])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" in result
    assert "沒有送出" in result["error"]
    assert ("PUT", _ONE_PATH) not in _verbs(spy)


@pytest.mark.asyncio
async def test_T48_update_an_unexpected_error_after_the_put_is_unconfirmed_and_asks_for_a_backend_check(
        tmp_path, monkeypatch, _int_str_limit_enforced):
    unprintable_reread = _unprintable_detail()
    # Precondition: under the 4300-digit limit this typeId really cannot be stringified.
    with pytest.raises(ValueError):
        str(unprintable_reread.parsed_json["data"]["typeId"])
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
        unprintable_reread,
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert result["updated"] == "unconfirmed"
    assert "後台" in result["message"] + " ".join(result["warnings"])
    assert any("內部錯誤" in w for w in result["warnings"]), result["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("which", ["create", "update", "delete"])
async def test_T49_each_tool_returns_only_its_own_closed_set_of_keys_and_status_values(tmp_path, monkeypatch, which):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    if which == "create":
        _install(monkeypatch, _create_script(before, before + [_row(_NEW_ID, _TITLE, _DESC)]))
        confirmed = await _create(ctx)
        _install(monkeypatch, _create_script(before, before))
        unconfirmed = await _create(ctx)
        status_key, expected = "created", {"created", "template", "message", "warnings"}
        results = (confirmed, unconfirmed)
    elif which == "update":
        _install(monkeypatch, [
            _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
            _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
        ])
        confirmed = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)
        _install(monkeypatch, [
            _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
            _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")),
        ])
        unconfirmed = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)
        status_key, expected = "updated", {"updated", "template", "message", "warnings"}
        results = (confirmed, unconfirmed)
    else:
        _install(monkeypatch, [_delete_ok_check(), _json(_OK_WRITE), _json(_NOT_FOUND, status=404)])
        confirmed = await delete_template(ctx=ctx, template_id=_OLD_ID)
        _install(monkeypatch, [_delete_ok_check(), _json(_OK_WRITE), _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC))])
        unconfirmed = await delete_template(ctx=ctx, template_id=_OLD_ID)
        status_key, expected = "deleted", {"deleted", "template_id", "message", "warnings"}
        results = (confirmed, unconfirmed)

    for result in results:
        assert set(result.keys()) == expected, result
        assert result[status_key] in ("confirmed", "unconfirmed")
    assert results[0][status_key] == "confirmed"
    assert results[1][status_key] == "unconfirmed"


@pytest.mark.asyncio
async def test_T50_a_throttle_refusal_is_an_error_with_retry_after_and_no_status_key(tmp_path, monkeypatch):
    for which in ("create", "update", "delete"):
        ctx, info, _db = await _new_session(tmp_path)
        app = ctx.request_context.lifespan_context
        _fill_window(info, app, leave=0)
        spy = _install(monkeypatch, [])
        if which == "create":
            result = await _create(ctx)
        elif which == "update":
            result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)
        else:
            result = await delete_template(ctx=ctx, template_id=_OLD_ID)
        assert "error" in result, which
        assert "retry_after_seconds" in result, which
        assert not ({"created", "updated", "deleted", "template", "template_id"} & set(result)), which
        assert spy.calls == [], which


@pytest.mark.asyncio
async def test_T51_created_and_updated_templates_have_the_same_keys_as_a_list_row(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, _info, _db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, before + [_row(_NEW_ID, _TITLE, _DESC)]))
    created = await _create(ctx)

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
    ])
    updated = await update_template(ctx=ctx2, template_id=_OLD_ID, title=_NEW_TITLE)

    ctx3, _info3, _db3 = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _json(_list_body(before)))
    listed = await list_templates(ctx=ctx3)
    list_keys = set(listed["results"][0].keys())

    assert set(created["template"].keys()) == list_keys == _TEMPLATE_KEYS
    assert set(updated["template"].keys()) == list_keys == _TEMPLATE_KEYS


@pytest.mark.asyncio
async def test_T52_no_sent_log_or_candidate_row_is_written_by_any_template_write(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, info, db = await _new_session(tmp_path)
    _install(monkeypatch, _create_script(before, before + [_row(_NEW_ID, _TITLE, _DESC)]))
    await _create(ctx)
    _install(monkeypatch, _create_script(before, before))
    await _create(ctx)
    _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
    ])
    await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)
    _install(monkeypatch, [_delete_ok_check(), _json(_OK_WRITE), _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC))])
    await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert await db.get_daily_sent_count(info.account_label) == 0
    for candidate_id in (_NEW_ID, _OLD_ID):
        for source in ("message", "resume"):
            assert await db.get_candidate(candidate_id, source, info.account_label) is None


@pytest.mark.asyncio
async def test_T52_a_full_daily_message_quota_does_not_stop_any_template_write(tmp_path, monkeypatch):
    before = [_row(_OLD_ID, _OLD_TITLE, _OLD_DESC)]
    ctx, info, db = await _new_session(tmp_path)
    cap = ctx.request_context.lifespan_context.config.max_daily_messages
    for _ in range(cap):
        await db.log_sent(info.account_label, "prior-candidate", "message")

    spy_create = _install(monkeypatch, _create_script(before, before + [_row(_NEW_ID, _TITLE, _DESC)]))
    created = await _create(ctx)
    spy_update = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
    ])
    updated = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)
    spy_delete = _install(monkeypatch, [_delete_ok_check(), _json(_OK_WRITE), _json(_NOT_FOUND, status=404)])
    deleted = await delete_template(ctx=ctx, template_id=_OLD_ID)

    assert created["created"] == "confirmed"
    assert updated["updated"] == "confirmed"
    assert deleted["deleted"] == "confirmed"
    assert ("POST", _LIST_PATH) in _verbs(spy_create)
    assert ("PUT", _ONE_PATH) in _verbs(spy_update)
    assert ("DELETE", _ONE_PATH) in _verbs(spy_delete)
    assert await db.get_daily_sent_count(info.account_label) == cap


@pytest.mark.asyncio
async def test_T53a_create_needs_four_slots_and_is_refused_with_only_three_left(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    _fill_window(info, ctx.request_context.lifespan_context, leave=3)
    spy = _install(monkeypatch, [])

    result = await _create(ctx)

    assert "error" in result
    assert "retry_after_seconds" in result
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T53b_update_without_a_type_runs_with_three_slots_left(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    _fill_window(info, ctx.request_context.lifespan_context, leave=3)
    spy = _install(monkeypatch, [
        _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
        _json(_detail_body(_OLD_ID, _NEW_TITLE, _OLD_DESC, type_id="1")),
    ])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE)

    assert "error" not in result
    assert len(spy.calls) == 3


@pytest.mark.asyncio
async def test_T53c_update_with_a_type_needs_four_slots_and_is_refused_with_three_left(tmp_path, monkeypatch):
    ctx, info, _db = await _new_session(tmp_path)
    _fill_window(info, ctx.request_context.lifespan_context, leave=3)
    spy = _install(monkeypatch, [])

    result = await update_template(ctx=ctx, template_id=_OLD_ID, title=_NEW_TITLE, type_id="2")

    assert "error" in result
    assert "retry_after_seconds" in result
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T54_tool_descriptions_carry_the_required_evidence_and_warnings_for_each_write():
    create_desc = TOOL_DESCRIPTIONS["create_template"]
    update_desc = TOOL_DESCRIPTIONS["update_template"]
    delete_desc = TOOL_DESCRIPTIONS["delete_template"]
    create_s = re.sub(r"\s+", "", create_desc)
    update_s = re.sub(r"\s+", "", update_desc)
    delete_s = re.sub(r"\s+", "", delete_desc)

    for desc in (create_desc, update_desc):
        assert "#jname#" in desc
    for desc in (create_desc, update_desc, delete_desc):
        assert "重讀" in desc

    assert "前端碼證據" in create_s
    assert "附件" in create_s and "重複" in create_s and "重送" in create_s

    assert "量測過" in update_s
    assert "只量過" in update_s and "沒有附件" in update_s
    assert "只有前端碼" not in update_s
    assert "附件" in update_s and "後台" in update_s

    assert "前端碼證據" in delete_s
    assert "量測過" in delete_s
    assert "刪除前檢查只有前端碼" not in delete_s


@pytest.mark.asyncio
async def test_T81_delete_description_says_it_cannot_be_undone_and_a_person_must_confirm():
    desc = re.sub(r"\s+", "", TOOL_DESCRIPTIONS["delete_template"])
    assert "無法復原" in desc
    assert "真人" in desc


@pytest.mark.asyncio
async def test_T79_no_project_log_record_carries_a_submitted_title_or_description(tmp_path, monkeypatch, caplog):
    secret_title = "SYN-TITLE-7731"
    secret_desc = "SYN-DESC-7731-body"
    rejected_title = "SYN-TITLE-7731-v"
    ctx, _info, _db = await _new_session(tmp_path)
    before = [_row(_OLD_ID, secret_title, secret_desc)]

    with caplog.at_level(logging.DEBUG):
        _install(monkeypatch, _create_script([], [_row(_NEW_ID, secret_title, secret_desc)]))
        await _create(ctx, title=secret_title, description=secret_desc)

        _install(monkeypatch, [
            _json(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1")), _json(_OK_WRITE),
            _json(_detail_body(_OLD_ID, secret_title, "別的內容", type_id="1")),
        ])
        await update_template(ctx=ctx, template_id=_OLD_ID, title=secret_title, description=secret_desc)

        refused = _json({"code": "SYN-VALIDATION", "message": "SYNTH-LOG-104-MESSAGE", "detail": []}, status=400)
        _install(monkeypatch, _create_script(before, before, post=refused))
        await _create(ctx, title=rejected_title, description=secret_desc)

    project_records = [
        record for record in caplog.records
        if record.name == "104-mcp" or record.name.startswith("104-mcp.")
    ]
    assert project_records, "the template writes must leave at least one 104-mcp log record, or the leak check is vacuous"
    log_text = "\n".join(caplog.handler.format(record) for record in project_records)
    for forbidden in (secret_title, secret_desc, rejected_title):
        assert forbidden not in log_text, f"{forbidden!r} leaked into the project log"


# ── Pure functions — T-58..T-67, T-84 ─────────────────────────────────────────────────

def test_T58_js_length_counts_utf16_units_and_a_supplementary_emoji_as_two():
    assert js_length("abc") == 3
    assert js_length("面試") == 2
    assert js_length("\U0001F600") == 2


def test_T59_none_fields_are_not_checked_and_two_good_fields_pass():
    assert text_errors(title=None, description=None) is None
    assert text_errors(title=None, description="內容") is None
    assert text_errors(title="面試", description=None) is None
    assert text_errors(title="面試", description="內容") is None
    assert text_errors(title="   ", description=None) is not None


def test_T60_list_envelope_needs_integer_pagination_and_normalises_integer_type_ids():
    no_total_pages = {"data": [_row("1", "t", "d")], "metadata": {"perPage": 100, "page": 1, "total": 1}}
    with pytest.raises(helpers_mod.MalformedResponseError):
        parse_template_list(no_total_pages)

    string_total_pages = {"data": [_row("1", "t", "d")],
                          "metadata": {"perPage": 100, "page": 1, "totalPage": "1", "total": 1}}
    with pytest.raises(helpers_mod.MalformedResponseError):
        parse_template_list(string_total_pages)

    integer_type = {"data": [{"id": "1", "title": "t", "description": "d", "typeId": 1, "typeDesc": "x"}],
                    "metadata": {"perPage": 100, "page": 1, "totalPage": 1, "total": 1}}
    assert parse_template_list(integer_type).rows[0]["typeId"] == "1"

    normal = parse_template_list(_list_body([_row("1", "t", "d")]))
    assert (normal.total, normal.total_pages, normal.per_page, len(normal.rows)) == (1, 1, 100, 1)


def test_T61_type_list_normalises_integer_type_ids_and_refuses_a_non_list_data():
    types = parse_template_types({"data": [{"typeId": 3, "name": "合成丁", "amount": 0, "isDefault": True}],
                                  "metadata": {"page": 1, "perPage": 6, "totalPage": 1, "total": 1}})
    assert [tuple(item) for item in types] == [("3", "合成丁")]

    with pytest.raises(helpers_mod.MalformedResponseError):
        parse_template_types({"data": {"typeId": "3"}, "metadata": {}})


def test_T62_a_detail_must_match_the_requested_id_count_attachments_and_read_type_desc_when_present():
    single_read = parse_template_detail(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC), requested_id=_OLD_ID)
    assert single_read.id == _OLD_ID
    assert single_read.type_id == "1"
    assert single_read.file_count == 0
    assert single_read.type_desc is None

    two_files = _detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, files=[
        {"name": "合成附件一", "fileToken": "syn-1"}, {"name": "合成附件二", "fileToken": "syn-2"},
    ])
    assert parse_template_detail(two_files, requested_id=_OLD_ID).file_count == 2

    with_type_desc = _detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)
    with_type_desc["data"]["typeDesc"] = "合成乙"
    assert parse_template_detail(with_type_desc, requested_id=_OLD_ID).type_desc == "合成乙"

    with pytest.raises(helpers_mod.MalformedResponseError):
        parse_template_detail(_detail_body("999999", _OLD_TITLE, _OLD_DESC), requested_id=_OLD_ID)

    no_files = _detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC)
    del no_files["data"]["files"]
    with pytest.raises(helpers_mod.MalformedResponseError):
        parse_template_detail(no_files, requested_id=_OLD_ID)


def test_T63_an_empty_list_with_zero_pages_or_one_page_allows_a_create():
    assert create_preflight(parse_template_list(_list_body([], total=0, total_pages=0))) is None
    assert create_preflight(parse_template_list(_list_body([], total=0, total_pages=1))) is None


def test_T64_create_outcome_ignores_rows_removed_elsewhere_and_needs_one_new_matching_id():
    sent = create_body(_TITLE, _DESC, "1")
    before = parse_template_list(_list_body([_row("700001", "舊甲", "舊"), _row("700002", "舊乙", "舊")]))
    after = parse_template_list(_list_body([_row("700002", "舊乙", "舊"), _row(_NEW_ID, _TITLE, _DESC)]))

    status, row, reason = create_outcome(before, after, sent)

    assert status == "confirmed"
    assert reason is None
    assert row["id"] == _NEW_ID


def test_T65_merge_update_refuses_attachments_keeps_unchanged_fields_and_refuses_no_change():
    with_file = parse_template_detail(
        _detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, files=[{"name": "合成附件", "fileToken": "syn"}]),
        requested_id=_OLD_ID,
    )
    refused = merge_update(with_file, title=None, description=None, type_id="2")
    assert isinstance(refused, str)
    assert "後台" in refused

    plain = parse_template_detail(_detail_body(_OLD_ID, _OLD_TITLE, _OLD_DESC, type_id="1"), requested_id=_OLD_ID)
    merged = merge_update(plain, title=None, description=None, type_id="2")
    assert merged == {"title": _OLD_TITLE, "description": _OLD_DESC, "typeId": "2", "files": []}

    same = merge_update(plain, title=_OLD_TITLE, description=_OLD_DESC, type_id="1")
    assert isinstance(same, str)


def test_T66_update_outcome_needs_no_attachments_and_names_a_whitespace_only_difference():
    sent = {"title": _NEW_TITLE, "description": "第一行\r\n第二行", "typeId": "1", "files": []}
    with_file = parse_template_detail(
        _detail_body(_OLD_ID, _NEW_TITLE, "第一行\r\n第二行", files=[{"name": "合成附件", "fileToken": "syn"}]),
        requested_id=_OLD_ID,
    )
    status, _reason = update_outcome(sent, with_file)
    assert status == "unconfirmed"

    newline_only = parse_template_detail(_detail_body(_OLD_ID, _NEW_TITLE, "第一行\n第二行"), requested_id=_OLD_ID)
    status2, reason2 = update_outcome(sent, newline_only)
    assert status2 == "unconfirmed"
    assert "空白" in reason2 or "換行" in reason2


def test_T67_used_by_jobs_turns_integer_job_numbers_into_strings_and_fails_on_a_missing_number():
    assert used_by_jobs([{"jobNo": 12355016, "jobName": "合成職缺甲"}]) == [
        {"jobno": "12355016", "job": "合成職缺甲"},
    ]
    assert used_by_jobs([{"jobNo": "12355016", "jobName": "甲"}, {"jobName": "乙"}]) is None
    assert used_by_jobs("SYNTH-NOT-A-LIST") is None


def test_T84_a_job_whose_name_is_not_a_string_keeps_its_row_with_job_none():
    result = used_by_jobs([
        {"jobNo": "12355016", "jobName": 5},
        {"jobNo": "12355017", "jobName": "合成職缺乙"},
        {"jobNo": "12355018"},
    ])
    assert result == [
        {"jobno": "12355016", "job": None},
        {"jobno": "12355017", "job": "合成職缺乙"},
        {"jobno": "12355018", "job": None},
    ]


# ── list_templates — moved from tests/test_messaging.py (T-76) ────────────────────────

_TEMPLATE_ROW_A = {
    "id": "tpl-syn-1", "title": "詢問意願-合成範本A", "description": "測試範本內容-合成A",
    "typeId": "1", "typeDesc": "詢問意願",
}


@pytest.mark.asyncio
async def test_T76_list_templates_params_carry_page_and_optional_type_id(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = {"data": [_TEMPLATE_ROW_A], "metadata": {"page": 1, "totalPage": 1, "total": 1}}

    spy = _install_fake_fetch(monkeypatch, _raw(200, "application/json; charset=utf-8", json.dumps(body), body))
    await list_templates(ctx=ctx)
    _endpoint, params, _body = spy.calls[0]
    assert any(k == "page" for k, _ in params)
    assert not any(k == "typeId" for k, _ in params)

    spy2 = _install_fake_fetch(monkeypatch, _raw(200, "application/json; charset=utf-8", json.dumps(body), body))
    await list_templates(ctx=ctx, type_id="1")
    _endpoint2, params2, _body2 = spy2.calls[0]
    assert any(k == "page" for k, _ in params2)
    assert any((k, v) == ("typeId", "1") for k, v in params2)


@pytest.mark.asyncio
async def test_T76_list_templates_rows_are_allow_listed_and_envelope_is_fixed_shape(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    row = dict(_TEMPLATE_ROW_A)
    row["files"] = ["synthetic-file-marker"]
    row["createDate"] = "2026-01-01"
    body = {"data": [row], "metadata": {"page": 1, "totalPage": 1, "total": 1}}
    _install_fake_fetch(monkeypatch, _json(body))

    result = await list_templates(ctx=ctx)

    assert set(result.keys()) == {"results", "pagination", "browse_limit", "warnings"}
    assert result["browse_limit"] is None
    assert len(result["results"]) == 1
    assert set(result["results"][0].keys()) == {"id", "title", "description", "type_id", "type_desc"}
    text = json.dumps(result, ensure_ascii=False)
    assert "synthetic-file-marker" not in text
    assert "createDate" not in text


@pytest.mark.asyncio
async def test_T76_list_templates_pagination_and_more_pages_warning(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = {"data": [_TEMPLATE_ROW_A], "metadata": {"page": 1, "totalPage": 3, "total": 65}}
    _install_fake_fetch(monkeypatch, _json(body))

    result = await list_templates(ctx=ctx)

    assert result["pagination"] == {"page": 1, "total_pages": 3, "total": 65}
    assert len(result["warnings"]) == 1
    assert "3" in result["warnings"][0]


@pytest.mark.asyncio
async def test_T76_list_templates_missing_metadata_and_upstream_error_both_fail_without_results(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _json({"data": []}))
    result = await list_templates(ctx=ctx)
    assert "error" in result
    assert "results" not in result

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw(403, "text/html; charset=utf-8", "blocked", None))
    result2 = await list_templates(ctx=ctx2)
    assert "error" in result2
    assert "results" not in result2


@pytest.mark.asyncio
async def test_T76_list_templates_requires_login_and_goes_through_the_real_throttle_gate(tmp_path, monkeypatch):
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)  # no session activated -> not logged in
    _install_never_called_fetch(monkeypatch)

    result = await list_templates(ctx=ctx)
    assert result == {"error": "請先呼叫 login()"}

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)
    real_enforce_throttle = helpers_mod.enforce_throttle

    async def rejecting_throttle(*args, **kwargs):
        return ThrottleAbort(kind="throttled", payload={"error": "節流測試", "retry_after_seconds": 7}, detail="")

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", rejecting_throttle)
    result2 = await list_templates(ctx=ctx2)
    assert "error" in result2
    assert result2.get("retry_after_seconds") == 7
    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", real_enforce_throttle)

    ctx3, info3, _db3 = await _new_session(tmp_path)
    body = {"data": [_TEMPLATE_ROW_A], "metadata": {"page": 1, "totalPage": 1, "total": 1}}
    _install_fake_fetch(monkeypatch, _json(body))
    before = len(info3.throttle.request_timestamps)
    await list_templates(ctx=ctx3)
    after = len(info3.throttle.request_timestamps)
    assert after == before + 1


def test_list_templates_description_no_longer_claims_one_event_route_or_future_events():
    """The list_templates description states the current five-route situation (moved with T-76)."""
    description = TOOL_DESCRIPTIONS["list_templates"]
    assert description
    assert "只有一條事件路由" not in description
    assert "未來量到其他招募事件" not in description


# ── describe_result_fields(row_type="template") — T-54 (the type_desc note) ───────────

@pytest.mark.asyncio
async def test_T54_template_row_type_desc_note_says_update_returns_it_empty_and_points_to_list_templates():
    mcp = FakeMCP()
    register_discovery_tools(mcp)

    result = await mcp.tools["describe_result_fields"](row_type="template")

    note = re.sub(r"\s+", "", result["fields"]["type_desc"])
    assert "update_template" in note
    assert "list_templates" in note
    assert "空" in note
