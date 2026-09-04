"""登入體驗（2026-09-04 實測回饋）：

1. `check_login(token, wait_seconds=N)` 在伺服器端等到登入完成才回傳（長輪詢），
   Agent 不必停下來等使用者說「登入了」。
2. 行程重啟後憑證檔還在時，任何需要登入的工具直接恢復 session，不必再 login() 一次。
3. 缺 Chromium 時 login() 自動在背景安裝，回 installing_browser；裝好後同一個呼叫
   接著開登入。安裝子行程絕不繼承本行程的 stdout（MCP 協定通道）。
4. 同機組態下 login() 用預設瀏覽器打開登入頁；固定埠（真人在別台機器）組態不開。
5. `list_matched_resumes` 的當日零筆形狀（resumes: []、pageInfo: null）是空結果，
   不是結構異常；browse_limit 兩個數字同型別。

全部不開真實瀏覽器、不碰網路、不啟動子行程。
"""

import asyncio
import sys
import types

import pytest

from mcp104.browser.session import SessionInfo, save_cookies, save_identity
# Bound at import time, i.e. before conftest's autouse stub replaces the
# module attribute — these two unit tests exercise the real opener.
from mcp104.tools.auth import _open_in_local_browser as _real_open_in_local_browser
from tests.test_auth_tools import (
    make_app_ctx,
    make_config,
    make_ctx,
    make_session,
    patch_login_infra,
)


async def _cancel_watchers(app_ctx):
    tasks = list(app_ctx._watcher_tasks.values())
    for t in tasks:
        t.cancel()
    for t in tasks:
        try:
            await t
        except BaseException:
            pass


# ── 1. check_login long-poll ───────────────────────────────────────────

@pytest.mark.asyncio
async def test_check_login_returns_as_soon_as_the_login_completes(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    started = await auth.login(ctx)
    token = started["token"]

    async def complete_after_a_moment():
        await asyncio.sleep(0.2)
        app_ctx.session_pool.activate_direct("s1", make_session())

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    completer = asyncio.create_task(complete_after_a_moment())
    result = await auth.check_login(token, ctx, wait_seconds=30)
    elapsed = loop.time() - t0
    await completer
    await _cancel_watchers(app_ctx)

    assert result == {"status": "success"}
    assert elapsed < 5.0, "must return when the login completes, not after the full wait"


@pytest.mark.asyncio
async def test_check_login_waits_the_requested_time_then_reports_pending(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    token = (await auth.login(ctx))["token"]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await auth.check_login(token, ctx, wait_seconds=1)
    elapsed = loop.time() - t0
    await _cancel_watchers(app_ctx)

    assert result["status"] == "pending"
    assert 0.9 <= elapsed < 5.0


@pytest.mark.asyncio
async def test_check_login_default_is_still_immediate(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    token = (await auth.login(ctx))["token"]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await auth.check_login(token, ctx)
    elapsed = loop.time() - t0
    await _cancel_watchers(app_ctx)

    assert result["status"] == "pending"
    assert elapsed < 0.5


@pytest.mark.asyncio
async def test_check_login_wait_is_capped_at_the_module_maximum(tmp_path, monkeypatch):
    """A caller asking for an hour gets the 90 s cap, never an hour — the cap
    exists to stay under the MCP client's per-call timeout."""
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    monkeypatch.setattr(auth, "CHECK_LOGIN_MAX_WAIT_SECONDS", 1)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    token = (await auth.login(ctx))["token"]

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await auth.check_login(token, ctx, wait_seconds=3600)
    elapsed = loop.time() - t0
    await _cancel_watchers(app_ctx)

    assert result["status"] == "pending"
    assert elapsed < 3.0


@pytest.mark.asyncio
async def test_check_login_does_not_wait_on_a_non_pending_status(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    loop = asyncio.get_running_loop()
    t0 = loop.time()
    result = await auth.check_login("never-issued", ctx, wait_seconds=30)
    assert result["status"] == "unknown"
    assert loop.time() - t0 < 0.5


def test_login_description_tells_the_agent_to_poll_check_login_not_the_user():
    """The measured failure: the old docstring said check_login was unnecessary,
    the agent obeyed and stopped to wait for the human. The published text must
    now say the opposite."""
    from mcp.server.fastmcp import FastMCP

    from mcp104.tools.auth import register_auth_tools

    mcp = FastMCP("t")
    register_auth_tools(mcp)
    tools = {t.name: t for t in mcp._tool_manager.list_tools()}
    login_desc = tools["login"].description
    assert "check_login(token, wait_seconds=90)" in login_desc
    assert "不需要手動呼叫" not in login_desc
    assert "通常不需手動呼叫" not in tools["check_login"].description
    assert "wait_seconds" in tools["check_login"].parameters["properties"]


# ── 2. auto-restore from the credential file ───────────────────────────

def _cookie_jar():
    return [{"name": "its", "value": "x", "domain": "vip.104.com.tw", "path": "/"}]


@pytest.mark.asyncio
async def test_require_login_restores_from_the_credential_file_without_login(tmp_path):
    from mcp104.tools.helpers import require_login

    app_ctx = make_app_ctx(tmp_path)
    save_cookies(app_ctx.config.cookies_path, _cookie_jar())
    save_identity(app_ctx.config.identity_path, "who@example.invalid")
    ctx = make_ctx(app_ctx)

    @require_login
    async def tool(ctx):
        return {"ok": True}

    assert await tool(ctx=ctx) == {"ok": True}
    restored = app_ctx.session_pool.get_session("s1")
    assert isinstance(restored, SessionInfo)
    assert restored.cookies == _cookie_jar()
    assert restored.account_label == "who@example.invalid"  # from account.json, no request


def _fake_last_info_fetch(monkeypatch, user_email, calls):
    """Stub the transport so ensure_account_identity's one request answers with
    a family-B envelope carrying metadata.userEmail (measured shape, §8.16)."""
    import json as _json

    from mcp104.browser.api_client import RawResponse
    from mcp104.tools import helpers

    async def fake_fetch(endpoint, *, cookie_header, params=None, body=None):
        calls.append(endpoint.key)
        payload = {"data": {"emailCC": []}, "metadata": {"userEmail": user_email, "quota": 300}}
        return RawResponse(status=200, location=None, content_type="application/json",
                           body=_json.dumps(payload), parsed_json=payload)

    monkeypatch.setattr(helpers, "fetch", fake_fetch)


@pytest.mark.asyncio
async def test_identity_is_learned_from_104_once_and_cached(tmp_path, monkeypatch):
    """No account label is configured any more: the first tool call after a
    login asks 104 who is signed in (event/last-info → metadata.userEmail),
    keys the session on it and caches it in account.json; later calls and a
    later process do not ask again."""
    from mcp104.browser.session import load_identity
    from mcp104.tools.helpers import require_login

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    app_ctx.session_pool.activate_direct("s1", SessionInfo(cookies=_cookie_jar(), account_label=None))
    calls = []
    _fake_last_info_fetch(monkeypatch, "who@example.invalid", calls)

    seen_labels = []

    @require_login
    async def tool(ctx):
        seen_labels.append(app_ctx.session_pool.get_session("s1").account_label)
        return {"ok": True}

    assert await tool(ctx=ctx) == {"ok": True}
    assert await tool(ctx=ctx) == {"ok": True}

    assert seen_labels == ["who@example.invalid", "who@example.invalid"]
    assert calls == ["event_last_info"], "exactly one identity request, then cached"
    assert load_identity(app_ctx.config.identity_path) == "who@example.invalid"


@pytest.mark.asyncio
async def test_identity_request_failure_blocks_the_tool_with_that_error(tmp_path, monkeypatch):
    from mcp104.browser.api_client import RawResponse
    from mcp104.tools import helpers
    from mcp104.tools.helpers import require_login

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    app_ctx.session_pool.activate_direct("s1", SessionInfo(cookies=_cookie_jar(), account_label=None))

    async def expired_fetch(endpoint, *, cookie_header, params=None, body=None):
        return RawResponse(status=401, location=None, content_type="application/json", body="{}", parsed_json={})

    monkeypatch.setattr(helpers, "fetch", expired_fetch)
    ran = []

    @require_login
    async def tool(ctx):
        ran.append(True)
        return {"ok": True}

    result = await tool(ctx=ctx)
    assert "error" in result and ran == []
    assert not app_ctx.config.identity_path.exists()


@pytest.mark.asyncio
async def test_logout_forgets_the_cached_identity(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)
    save_cookies(app_ctx.config.cookies_path, _cookie_jar())
    save_identity(app_ctx.config.identity_path, "who@example.invalid")
    app_ctx.session_pool.activate_direct("s1", SessionInfo(cookies=_cookie_jar(), account_label="who@example.invalid"))

    async def no_server_logout(ctx):
        return types.SimpleNamespace(state="not_sent", detail="(test)")

    monkeypatch.setattr(auth, "request_server_logout", no_server_logout)
    await auth.logout(ctx)

    assert not app_ctx.config.identity_path.exists()
    assert not app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
async def test_require_login_still_refuses_with_no_session_and_no_credential_file(tmp_path):
    from mcp104.tools.helpers import ERROR_NOT_LOGGED_IN, require_login

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    @require_login
    async def tool(ctx):
        return {"ok": True}

    assert await tool(ctx=ctx) == ERROR_NOT_LOGGED_IN
    assert app_ctx.session_pool.get_session("s1") is None


# ── 3. missing browser → background install ────────────────────────────

class _NeverFinishes:
    """An install_browser stand-in the test releases explicitly."""

    def __init__(self):
        self.calls = 0
        self.release = asyncio.Event()
        self.fail_with: Exception | None = None

    async def __call__(self):
        self.calls += 1
        await self.release.wait()
        if self.fail_with is not None:
            raise self.fail_with


def _missing_then_fine(monkeypatch, auth, fail_times: int):
    from mcp104.browser.stealth import MissingBrowserError
    from tests.test_auth_tools import _FakeBrowser

    state = {"launches": 0}

    async def fake_launch_browser(*a, **k):
        state["launches"] += 1
        if state["launches"] <= fail_times:
            raise MissingBrowserError("找不到這個 patchright 版本所需的 Chromium revision")
        return _FakeBrowser()

    monkeypatch.setattr(auth, "launch_browser", fake_launch_browser)
    return state


@pytest.mark.asyncio
async def test_login_with_missing_browser_starts_one_install_and_reports_installing(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    _missing_then_fine(monkeypatch, auth, fail_times=99)
    installer = _NeverFinishes()
    monkeypatch.setattr(auth, "install_browser", installer)
    monkeypatch.setattr(auth, "BROWSER_INSTALL_WAIT_SECONDS", 0.05)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    first = await auth.login(ctx)
    second = await auth.login(ctx)

    assert first["status"] == "installing_browser" and "message" in first
    assert second["status"] == "installing_browser"
    assert installer.calls == 1, "a second login() must join the running install, not start another"
    assert app_ctx._pending_logins == {}
    assert app_ctx.auth_site is None, "no listener is opened while there is nothing to view"

    installer.release.set()
    await _cancel_watchers(app_ctx)
    await asyncio.sleep(0)


@pytest.mark.asyncio
async def test_login_continues_into_a_real_login_once_the_install_finishes(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    _missing_then_fine(monkeypatch, auth, fail_times=1)
    installer = _NeverFinishes()
    installer.release.set()  # finishes immediately
    monkeypatch.setattr(auth, "install_browser", installer)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    result = await auth.login(ctx)
    await _cancel_watchers(app_ctx)

    assert "login_url" in result and "token" in result
    assert installer.calls == 1
    assert app_ctx._browser_install is None


@pytest.mark.asyncio
async def test_failed_install_is_reported_and_retried_on_the_next_login(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    _missing_then_fine(monkeypatch, auth, fail_times=99)
    installer = _NeverFinishes()
    installer.release.set()
    installer.fail_with = RuntimeError("patchright install chromium 以結束碼 1 結束：disk full")
    monkeypatch.setattr(auth, "install_browser", installer)
    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    first = await auth.login(ctx)
    second = await auth.login(ctx)

    assert "error" in first and "disk full" in first["error"]
    assert "error" in second
    assert installer.calls == 2, "a failed install must be retried by the next login()"


@pytest.mark.asyncio
async def test_install_browser_runs_patchright_with_this_interpreter_and_a_captured_stdout(monkeypatch):
    from mcp104.browser import stealth

    seen = {}

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"Chromium 151 downloaded\n", None

    async def fake_exec(*argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    await stealth.install_browser()

    assert seen["argv"] == (sys.executable, "-m", "patchright", "install", "chromium")
    # The child must never inherit this process's stdout: that is the MCP
    # protocol channel. Both streams are captured, stdin is closed.
    assert seen["kwargs"]["stdout"] == asyncio.subprocess.PIPE
    assert seen["kwargs"]["stderr"] == asyncio.subprocess.STDOUT
    assert seen["kwargs"]["stdin"] == asyncio.subprocess.DEVNULL


@pytest.mark.asyncio
async def test_install_browser_raises_with_the_output_tail_on_a_nonzero_exit(monkeypatch):
    from mcp104.browser import stealth

    class _Proc:
        returncode = 7

        async def communicate(self):
            return b"downloading\nError: no space left on device\n", None

    async def fake_exec(*argv, **kwargs):
        return _Proc()

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    with pytest.raises(RuntimeError) as exc_info:
        await stealth.install_browser()
    assert "7" in str(exc_info.value)
    assert "no space left on device" in str(exc_info.value)


def test_missing_browser_message_no_longer_claims_any_installed_browser_will_do():
    """Measured 2026-09-04: ms-playwright already held chromium-1200/1208/1228 from
    other tools and patchright still failed (it wanted 1234). The message must
    say the required revision is missing, not that "any install" would be found."""
    from mcp104.browser.stealth import _MISSING_BROWSER_MESSAGE

    assert "revision" in _MISSING_BROWSER_MESSAGE
    assert "裝過一次" not in _MISSING_BROWSER_MESSAGE
    assert "自動" in _MISSING_BROWSER_MESSAGE


# ── 4. opening the login page in the local browser ─────────────────────

@pytest.mark.asyncio
async def test_login_opens_the_login_url_locally_in_the_same_machine_form(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    opened = []
    monkeypatch.setattr(auth, "_open_in_local_browser", lambda url: opened.append(url) or True)
    app_ctx = make_app_ctx(tmp_path)  # auth_bind_port=None → same-machine form
    ctx = make_ctx(app_ctx)

    result = await auth.login(ctx)
    again = await auth.login(ctx)
    await _cancel_watchers(app_ctx)

    assert result["browser_opened"] is True
    assert opened == [result["login_url"]], "opened exactly once, with the URL the agent got"
    assert again["browser_opened"] is False and again["token"] == result["token"]


@pytest.mark.asyncio
async def test_login_does_not_open_a_browser_when_the_human_is_on_another_machine(tmp_path, monkeypatch):
    from mcp104.tools import auth

    patch_login_infra(monkeypatch, auth)
    opened = []
    monkeypatch.setattr(auth, "_open_in_local_browser", lambda url: opened.append(url) or True)
    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(
        tmp_path, auth_bind_port=8765, auth_base_url="http://remote.example.invalid:8765"
    )
    ctx = make_ctx(app_ctx)

    result = await auth.login(ctx)
    await _cancel_watchers(app_ctx)

    assert result["browser_opened"] is False
    assert opened == []


def test_open_in_local_browser_never_lets_the_child_touch_stdout(monkeypatch):
    """On macOS/Linux the opener is a subprocess; its stdio must be DEVNULL
    because this process's stdout is the MCP protocol channel."""
    import subprocess

    from mcp104.tools import auth

    seen = {}

    def fake_popen(argv, **kwargs):
        seen["argv"] = argv
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(pid=1)

    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth.subprocess, "Popen", fake_popen)

    assert _real_open_in_local_browser("http://localhost:1/auth/t") is True
    assert seen["argv"] == ["xdg-open", "http://localhost:1/auth/t"]
    for stream in ("stdin", "stdout", "stderr"):
        assert seen["kwargs"][stream] == subprocess.DEVNULL


def test_open_in_local_browser_reports_false_instead_of_raising(monkeypatch):
    from mcp104.tools import auth

    def boom(argv, **kwargs):
        raise FileNotFoundError("xdg-open")

    monkeypatch.setattr(auth.sys, "platform", "linux")
    monkeypatch.setattr(auth.subprocess, "Popen", boom)
    assert _real_open_in_local_browser("http://localhost:1/auth/t") is False


# ── 5. list_matched_resumes zero-row shape + browse_limit types ───────────

def _match_envelope(**result):
    base = {"resumes": [], "browseLimit": {"resumeMax": 300, "onThatDayCount": "0"}}
    base.update(result)
    return base


def test_match_today_zero_rows_is_an_empty_result_not_malformed():
    from mcp104.tools.search import _build_match_response

    out = _build_match_response(_match_envelope(pageInfo=None), requested_page=1)
    assert out["results"] == []
    assert out["pagination"] == {"page": 1, "total_pages": 0, "total": 0}
    assert out["browse_limit"] == {"resume_max": 300, "on_that_day_count": 0}
    assert out["warnings"] == []


def test_match_missing_page_info_key_is_still_malformed():
    from mcp104.tools.helpers import MalformedResponseError
    from mcp104.tools.search import _build_match_response

    with pytest.raises(MalformedResponseError):
        _build_match_response(_match_envelope())  # no pageInfo key at all


def test_match_null_page_info_with_rows_is_still_malformed():
    from mcp104.tools.helpers import MalformedResponseError
    from mcp104.tools.search import _build_match_response

    with pytest.raises(MalformedResponseError):
        _build_match_response(_match_envelope(pageInfo=None, resumes=[{"idNo": "1"}]))


def test_browse_limit_numeric_strings_become_ints():
    from mcp104.tools.search import _extract_browse_limit

    assert _extract_browse_limit({"browseLimit": {"resumeMax": "300", "onThatDayCount": "27"}}) == {
        "resume_max": 300, "on_that_day_count": 27,
    }
    assert _extract_browse_limit({"browseLimit": {"resumeMax": 300, "onThatDayCount": "n/a"}}) == {
        "resume_max": 300, "on_that_day_count": "n/a",
    }
    assert _extract_browse_limit({}) is None
