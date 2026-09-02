"""Tests for src/mcp104/tools/auth.py -- login/check_login/logout, the watcher's
atomic handoff block, the RestoreVerdict disposition table, ServerLogoutResult,
and the logout_unconfirmed marker.

Written blind against .spec/specs/stdio-cdp-rearchitecture/design.md (Section C6,
Architecture lifecycle table, Data Models, Error Handling) while tools/auth.py is
being implemented in parallel -- this file does not import anything from
tools/auth.py's internals, only the public surface the design document names.
Where the design does not name an internal wiring detail (how `ctx` reaches
AppContext, the watcher's function name), a best-effort assumption is made and
flagged below under "CONTRACT UNCLEAR".
"""
from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import inspect
import json
import types
from dataclasses import fields, is_dataclass
from pathlib import Path

import pytest
import pytest_asyncio

from mcp104.config import Config
from mcp104.browser.session import SessionInfo, SessionPool


# --- shared fakes ---------------------------------------------------------

def make_config(tmp_path: Path, **overrides) -> Config:
    data_dir = tmp_path / "data"
    kwargs = dict(
        data_dir=data_dir,
        db_path=str(data_dir / "104.db"),
        cookies_path=data_dir / "cookies.json",
        account_label="acct",
        login_timeout_seconds=900,
        max_daily_messages=50,
        max_requests_per_hour=300,
        max_inline_wait_seconds=20,
        activity_streak_limit_minutes=20,
        rest_duration_minutes=3,
        min_call_interval_seconds=5,
        throttle_state_path=data_dir / "throttle.json",
        logout_unconfirmed_path=data_dir / "logout_unconfirmed",
        auth_bind_port=None,
        auth_base_url=None,
    )
    kwargs.update(overrides)
    return Config(**kwargs)


_CURRENT_APP_CTXS: list = []  # registry for the autouse watcher-cleanup fixture below


def make_app_ctx(tmp_path: Path, pool: SessionPool | None = None, **overrides):
    """A stand-in for AppContext (design.md Section C1), shaped per its declared
    field list: config, db, session_pool, _pending_logins, _finished_logins,
    _watcher_tasks, auth_site, logout_epoch."""
    base = types.SimpleNamespace(
        config=make_config(tmp_path),
        db=None,
        session_pool=pool if pool is not None else SessionPool(),
        _pending_logins={},
        _finished_logins={},
        _watcher_tasks={},
        auth_site=None,
        logout_epoch=0,
    )
    for k, v in overrides.items():
        setattr(base, k, v)
    _CURRENT_APP_CTXS.append(base)
    return base


# --- Mode-2 fix: login()'s fresh-human-login branch (_start_human_login in
# tools/auth.py) really launches a headless browser, a stealth context, a
# page, navigates it, starts a CdpLoginStream, and spawns a background
# watcher task. None of that is allowed to touch a real browser or the
# network in this test file (Test Approach: "the whole suite must not need
# a browser installed", the sole exception being T-32). Every name below is
# imported into mcp104.tools.auth's own namespace via `from ... import`, so
# it must be patched on that namespace, not on its origin module. ---

class _FakeCdpLoginStream:
    def __init__(self, page):
        self.page = page
        self.state = "created"

    async def start(self):
        pass

    async def stop(self):
        self.state = "closed"

    def add_viewer(self, sink):
        pass

    def remove_viewer(self, sink):
        pass

    async def refresh_for_new_viewer(self):
        pass

    async def dispatch_input(self, event):
        pass

    def mark_completed(self):
        self.state = "completed"

    async def announce_completed(self):
        pass


class _FakePage:
    """Stands in for the Playwright Page login()'s fresh-login branch
    creates. Supports on()/remove_listener()/main_frame (no-ops beyond
    bookkeeping) so a background watcher spawned against it can run
    _watch_for_login's framenavigated listener registration without
    crashing -- it never actually navigates to vip.104.com.tw here, so
    such a watcher simply times out and abandons like any other test that
    doesn't drive it further, instead of raising AttributeError."""

    def __init__(self):
        self.url = "about:blank"
        self._handlers: dict[str, list] = {}

    @property
    def main_frame(self):
        return self

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    async def goto(self, url, *a, **k):
        self.url = url

    def new_cdp_session(self, *a, **k):
        return types.SimpleNamespace()

    async def close(self):
        pass


class _FakeContext:
    def __init__(self):
        self._cookies: list[dict] = []

    async def new_page(self):
        return _FakePage()

    async def cookies(self, *a, **k):
        return self._cookies

    async def close(self):
        pass


class _FakeBrowser:
    async def new_context(self, *a, **k):
        return _FakeContext()

    async def close(self):
        pass


class _FakeAuthEndpoint:
    base_url = "http://127.0.0.1:0"
    port = 0

    async def close(self):
        pass


def patch_login_infra(monkeypatch, auth_module):
    """Replace every browser/network-touching name login()'s fresh-login
    branch calls, on mcp104.tools.auth's own namespace."""

    async def fake_launch_browser(*a, **k):
        return _FakeBrowser()

    async def fake_create_stealth_context(browser, *a, **k):
        return await browser.new_context()

    async def fake_start_auth_site(*a, **k):
        return _FakeAuthEndpoint()

    def fake_create_auth_app(*a, **k):
        return object()

    monkeypatch.setattr(auth_module, "launch_browser", fake_launch_browser, raising=False)
    monkeypatch.setattr(auth_module, "create_stealth_context", fake_create_stealth_context, raising=False)
    monkeypatch.setattr(auth_module, "start_auth_site", fake_start_auth_site, raising=False)
    monkeypatch.setattr(auth_module, "create_auth_app", fake_create_auth_app, raising=False)
    monkeypatch.setattr(auth_module, "CdpLoginStream", _FakeCdpLoginStream, raising=False)


@pytest.fixture(autouse=True)
def _patch_login_infra_for_every_test(monkeypatch):
    """Autouse so no individual test has to remember to call this -- every
    test in this file that reaches login()'s fresh-login branch gets fake
    browser/context/page/stream/auth-site objects instead of real ones."""
    try:
        from mcp104.tools import auth
    except ImportError:
        return  # tools/auth.py not importable yet -- nothing to patch
    patch_login_infra(monkeypatch, auth)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_watcher_tasks():
    """Autouse: cancel and await any background watcher task a test's
    login() calls spawned on any AppContext it created via make_app_ctx,
    so nothing keeps running (or keeps a fake object referencing patched-
    away names) past the test's own event loop."""
    _CURRENT_APP_CTXS.clear()
    yield
    for app_ctx in _CURRENT_APP_CTXS:
        tasks = list(getattr(app_ctx, "_watcher_tasks", {}).values())
        for t in tasks:
            t.cancel()
        for t in tasks:
            try:
                await t
            except BaseException:
                pass
    _CURRENT_APP_CTXS.clear()


def make_ctx(app_ctx, mcp_session_id: str = "s1"):
    """A stand-in for the MCP `Context` object passed into tool functions.
    AppContext comes from `ctx.request_context.lifespan_context` (assumed;
    design.md does not name this wiring). The guard's real
    `get_session_id(ctx)` (tools/helpers.py) does NOT use any
    "mcp_session_id" of the kind this file used to assume -- it stamps a
    uuid onto `ctx.session` the first time it is called and reuses it
    thereafter. To keep this file's session id deterministic (so
    `pool.activate_direct(mcp_session_id, ...)` calls elsewhere in this
    file line up with what the real code will look up), `_mcp104_sid` is
    pre-stamped here rather than left for get_session_id to mint."""
    rc = types.SimpleNamespace(lifespan_context=app_ctx)
    ctx = types.SimpleNamespace(
        request_context=rc,
        session=types.SimpleNamespace(_mcp104_sid=mcp_session_id),
        session_id=mcp_session_id,
        mcp_session_id=mcp_session_id,
        app_ctx=app_ctx,
    )
    return ctx


def make_session(cookies=None, account_label="acct", has_succeeded=False):
    return SessionInfo(
        cookies=cookies if cookies is not None else [{"name": "its", "value": "x"}],
        account_label=account_label,
        has_succeeded_api_call=has_succeeded,
    )


def guard_abort(kind: str, payload: dict | None = None):
    """Construct a GuardAbort with the given kind/payload.
    GuardAbort(payload, kind) -- kind is required, per the implementer."""
    from mcp104.tools.helpers import GuardAbort

    payload = payload if payload is not None else {"error": "(" + kind + ")"}
    return GuardAbort(payload, kind=kind)


def _session_info_from_args(a, k):
    """Best-effort: resolve the SessionInfo the real guarded_api would hand
    back as the second element of its yielded tuple, from whatever ctx the
    caller passed. Falls back to None -- no assigned case currently asserts
    on this element, only on the first (the payload)."""
    ctx = a[0] if a else k.get("ctx")
    try:
        app_ctx = ctx.request_context.lifespan_context
        sid = getattr(ctx, "session_id", None) or getattr(ctx, "mcp_session_id", None)
        return app_ctx.session_pool.get_session(sid)
    except Exception:
        return None


def stub_guarded_api_ok(result=None):
    """A guarded_api replacement that is itself the async-context-manager
    factory (per Mode-2 fix: guarded_api is used as
    `async with guarded_api(...) as (payload, info):`)."""

    @contextlib.asynccontextmanager
    async def _cm(*a, **k):
        info = _session_info_from_args(a, k)
        yield (result if result is not None else {"status": "SUCCESS"}), info

    return _cm


def stub_guarded_api_ok_counting(counter, result=None):
    @contextlib.asynccontextmanager
    async def _cm(*a, **k):
        counter["n"] += 1
        info = _session_info_from_args(a, k)
        yield (result if result is not None else {"status": "SUCCESS"}), info

    return _cm


def stub_guarded_api_abort(kind, payload=None):
    @contextlib.asynccontextmanager
    async def _cm(*a, **k):
        raise guard_abort(kind, payload)
        yield  # pragma: no cover -- unreachable; keeps this an async generator

    return _cm


def stub_guarded_api_abort_counting(counter, kind, payload=None):
    @contextlib.asynccontextmanager
    async def _cm(*a, **k):
        counter["n"] += 1
        raise guard_abort(kind, payload)
        yield  # pragma: no cover -- unreachable; keeps this an async generator

    return _cm


ALL_DISPOSITION_KINDS = [
    "expired", "blocked", "challenge", "transport", "throttled",
    "not_logged_in", "internal_config", "wrong_host", "header_fault",
    "missing_param", "not_found", "unrecognised_status", "malformed",
    "non_json", "validation",
]


# --- T-66: verify_restored_session disposition table ----------------------

@pytest.mark.asyncio
async def test_t066_expired_deletes_cookie_file(tmp_path, monkeypatch):
    """verify_restored_session's own responsibility for an "expired" verdict
    is the cookie jar (delete it, keep_jar=False); removing the pool session
    is orchestrated by login() itself and is covered there (see
    test_t067_branch_in_memory_session_expired_pool_state), not by this
    lower-level disposition-table unit."""
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text("[]")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired", {"error": "session expired"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.alive is False
    assert verdict.kind == "expired"
    assert verdict.keep_jar is False
    assert not app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
async def test_t066_challenge_keeps_cookie_file_and_pool_session(tmp_path, monkeypatch):
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text("[]")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("challenge")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.alive is False
    assert verdict.kind == "challenge"
    assert verdict.keep_jar is True
    assert app_ctx.config.cookies_path.exists()
    assert pool.get_session("s1") is not None


@pytest.mark.asyncio
async def test_t066_blocked_uses_restore_specific_wording_not_default_403s(tmp_path, monkeypatch):
    from mcp104.tools import auth
    from mcp104.tools import helpers

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("blocked", {"error": "generic blocked wording"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.kind == "blocked"
    assert verdict.keep_jar is True
    assert verdict.payload != {"error": "generic blocked wording"}
    assert verdict.payload == helpers.ERROR_BLOCKED_API_RESTORE_VERIFY
    assert verdict.payload != helpers.ERROR_BLOCKED_API_FIRST_CALL
    assert verdict.payload != helpers.ERROR_BLOCKED_API_AFTER_SUCCESS


@pytest.mark.asyncio
async def test_t066_uncovered_kind_keeps_jar_and_reports_loudly(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text("[]")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("some_never_before_seen_kind")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.alive is False
    assert verdict.kind == "some_never_before_seen_kind"
    assert verdict.keep_jar is True
    assert app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ALL_DISPOSITION_KINDS)
async def test_t066_every_disposition_row_matches_keep_jar_rule(tmp_path, monkeypatch, kind):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text("[]")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort(kind)

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.kind == kind
    if kind == "expired":
        assert verdict.keep_jar is False
        assert not app_ctx.config.cookies_path.exists()
    else:
        assert verdict.keep_jar is True
        assert app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
async def test_t066_alive_reports_alive_true(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.alive is True
    assert verdict.keep_jar is True


def test_t066_restore_verdict_is_frozen_dataclass_with_four_fields():
    from mcp104.tools.auth import RestoreVerdict

    assert is_dataclass(RestoreVerdict)
    assert RestoreVerdict.__dataclass_params__.frozen is True
    names = {f.name for f in fields(RestoreVerdict)}
    assert names == {"alive", "kind", "payload", "keep_jar"}


# --- T-110 / T-115: request_server_logout ---------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["not_logged_in", "internal_config"])
async def test_t110_not_sent_kinds(tmp_path, monkeypatch, kind):
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort(kind)

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.request_server_logout(ctx)
    assert result.state == "not_sent"
    assert result.kind == kind


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "kind", ["expired", "transport", "challenge", "blocked", "an_uncovered_kind"]
)
async def test_t110_unconfirmed_kinds(tmp_path, monkeypatch, kind):
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort(kind)

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.request_server_logout(ctx)
    assert result.state == "unconfirmed"
    assert result.kind == kind


@pytest.mark.asyncio
async def test_t110_never_produces_confirmed_state(tmp_path, monkeypatch):
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    for kind in ALL_DISPOSITION_KINDS + ["never_before_seen"]:
        fake_guarded_api = stub_guarded_api_abort(kind)

        monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)
        result = await auth.request_server_logout(ctx)
        assert result.state != "confirmed"
        assert result.state in ("unconfirmed", "not_sent")


@pytest.mark.asyncio
async def test_t110_never_raises(tmp_path, monkeypatch):
    """A non-GuardAbort exception out of guarded_api must not propagate --
    request_server_logout must land it as a conservative ("unconfirmed",
    not "not_sent") answer with kind="internal_config", never let it
    escape and take down logout()'s local-half teardown with it."""
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    @contextlib.asynccontextmanager
    async def raises_something_unexpected(*a, **k):
        raise RuntimeError("boom, not a GuardAbort")
        yield  # pragma: no cover -- unreachable; keeps this an async generator

    monkeypatch.setattr(auth, "guarded_api", raises_something_unexpected, raising=False)

    result = await auth.request_server_logout(ctx)
    assert result.state == "unconfirmed"
    assert result.kind == "internal_config"
    assert result.detail


def test_t115_endpoints_throttle_exempt_set_is_exactly_logout_session():
    from mcp104.browser.api_client import ENDPOINTS

    exempt = {ep.key for ep in ENDPOINTS.values() if not ep.throttle_gated}
    assert exempt == {"logout_session"}


# --- T-69 / T-112: logout_unconfirmed marker write/clear rules -----------

def test_t069_marker_written_regardless_of_server_logout_value(tmp_path, monkeypatch):
    """Section C6 step 5: every logout() writes the marker, independent of
    the resulting server_logout value."""
    from mcp104.tools import auth

    for kind, expect_state in (("expired", "unconfirmed"), ("not_logged_in", "not_sent")):
        pool = SessionPool()
        session = make_session()
        pool.activate_direct("s1", session)
        app_ctx = make_app_ctx(tmp_path / kind, pool=pool)
        app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
        app_ctx.config.cookies_path.write_text("[]")
        ctx = make_ctx(app_ctx)

        fake_guarded_api = stub_guarded_api_abort(kind)

        monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

        result = asyncio.run(auth.logout(ctx))
        assert result["server_logout"] == expect_state
        assert app_ctx.config.logout_unconfirmed_path.exists()


def test_t112_marker_existence_is_not_coupled_to_cookie_file_existence(tmp_path):
    """(d) narrow, always-true slice of the marker's clear rule: writing a
    fresh cookies file by itself (without going through the watcher's
    post-atomic-block clear step) must not be what makes the marker
    disappear -- only a completed fresh human login clears it, and that is
    exercised end-to-end in T-112's batch-2/3 companion tests."""
    from mcp104.browser.session import save_cookies

    cfg = make_config(tmp_path)
    cfg.logout_unconfirmed_path.parent.mkdir(parents=True, exist_ok=True)
    cfg.logout_unconfirmed_path.write_text("")
    assert cfg.logout_unconfirmed_path.exists()

    save_cookies(cfg.cookies_path, [{"name": "its", "value": "y"}])
    assert cfg.logout_unconfirmed_path.exists()


# ===========================================================================
# Batch 2: login() / check_login() state machine
# ===========================================================================

def _find_first_attr(module, candidates):
    """Best-effort lookup for an internal symbol design.md does not name
    (the dual-factor completion predicate, the watcher coroutine). Returns
    (name, obj) for the first candidate that exists, else (None, None)."""
    for name in candidates:
        obj = getattr(module, name, None)
        if obj is not None:
            return name, obj
    return None, None


# --- T-1 (R1.1): login() returns immediately, does not wait for the human ---

@pytest.mark.asyncio
async def test_t001_login_returns_immediately_without_waiting_for_human(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    # No cookies file, no pending login, no pool session -> fresh human login
    # branch. This must not block on anything resembling "wait for the human
    # to finish MFA" (login_timeout_seconds is 900s in make_config -- if the
    # call actually waited on that, this test would time out).
    result = await asyncio.wait_for(auth.login(ctx), timeout=5.0)

    assert "login_url" in result
    assert "token" in result


@pytest.mark.asyncio
async def test_t001_login_registers_a_pending_login_not_a_completed_one(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    result = await asyncio.wait_for(auth.login(ctx), timeout=5.0)

    assert result["token"] in app_ctx._pending_logins
    assert app_ctx.session_pool.get_session("s1") is None


# --- T-12 (R2.2): fresh process restores without human action ------------

@pytest.mark.asyncio
async def test_t012_fresh_process_restores_without_human_action(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)  # empty pool, no pending logins
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)

    assert result.get("status") in ("restored", "already_logged_in")
    assert "login_url" not in result
    assert "token" not in result


# --- T-13 (R2.4): both clauses, (b) not by (a)'s absence alone -----------

@pytest.mark.asyncio
async def test_t013a_logout_removes_persisted_login_state(tmp_path, monkeypatch):
    from mcp104.tools import auth

    session = make_session()
    pool = SessionPool()
    pool.activate_direct("s1", session)
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text("[]")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("not_logged_in")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.logout(ctx)

    assert not app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
async def test_t013b_residual_cookie_file_still_gets_reverified_and_rejected(tmp_path, monkeypatch):
    """The 104-answers-not-alive half: even if a cookie file reappears after
    logout (cross-process residue simulated by writing it back), the next
    run's login() still sends a live restore-verification request -- it does
    not treat file-presence alone as proof of login."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    called = {"n": 0}

    fake_guarded_api = stub_guarded_api_abort_counting(called, "expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)

    assert called["n"] == 1  # a verification request really was sent
    assert "login_url" in result  # rejected -> falls to human login flow
    assert result.get("status") not in ("restored", "already_logged_in")


@pytest.mark.asyncio
async def test_t013b_residual_cookie_file_alive_is_not_silent_restore(tmp_path, monkeypatch):
    """The 104-answers-alive half: this must NOT read as a silent restore --
    it must carry the logout_unconfirmed warning (the only cross-process
    notice channel for this scenario, per Section C6)."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    app_ctx.config.logout_unconfirmed_path.write_text("")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)

    assert result.get("status") in ("restored", "already_logged_in")
    assert "warning" in result
    assert result["warning"]


# --- T-14 (R2.5): removed login state actually gets removed --------------

@pytest.mark.asyncio
async def test_t014_definitively_unusable_login_state_is_removed(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.login(ctx)

    assert not app_ctx.config.cookies_path.exists()


# --- T-15 (R3.1, R3.2): parseable-but-rejected state is not "restored" ---

@pytest.mark.asyncio
async def test_t015_parseable_but_rejected_state_is_not_restore_success(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)

    assert result.get("status") not in ("restored", "already_logged_in")


# --- T-16 (R3.3): after restore success, a login-required call is not "please login" ---

@pytest.mark.asyncio
async def test_t016_restore_success_leaves_session_resolvable_by_pool(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert result.get("status") in ("restored", "already_logged_in")
    # A tool guarded by require_login()/guarded_api resolves the session
    # from the pool -- if restore success didn't commit it, every guarded
    # tool call afterwards would answer "please call login() first".
    assert app_ctx.session_pool.is_logged_in("s1") is True


# --- T-17 (R3.4): failed confirmation still hands back an openable login address ---

@pytest.mark.asyncio
async def test_t017_confirmation_failure_carries_an_openable_login_url(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)

    assert result.get("login_url")
    assert result.get("token")


# --- T-19 (R3.6): the whole restore-confirmation path runs with no browser ---

@pytest.mark.asyncio
async def test_t019_restore_confirmation_needs_no_browser_object(tmp_path, monkeypatch):
    """ctx/app_ctx here expose no browser, page, or Playwright/patchright
    object anywhere -- if verify_restored_session touched one, this would
    AttributeError rather than complete."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)
    assert verdict.alive is True


# --- T-20 (R3.7): cookie expiry attribute alone must not decide aliveness ---

@pytest.mark.asyncio
async def test_t020_verification_ignores_cookie_expiry_attribute_and_asks_104(tmp_path, monkeypatch):
    """A cookie that LOOKS unexpired by its own attributes, but which 104
    rejects, must not be judged alive -- the judgment comes only from 104's
    response (via guarded_api/classify), never from parsing the cookie."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    # Cookie with a far-future expiry attribute -- if the implementation
    # peeked at this instead of asking 104, it would wrongly report alive.
    session = make_session(cookies=[{"name": "its", "value": "x", "expires": 4102444800}])
    pool = SessionPool()
    pool.activate_direct("s1", session)
    app_ctx.session_pool = pool

    verdict = await auth.verify_restored_session(ctx)
    assert verdict.alive is False


# --- T-22..T-25, T-27 (R4.x): verify_restored_session's response shapes --

@pytest.mark.asyncio
async def test_t022_accepted_means_usable_and_no_error(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)
    assert verdict.alive is True
    assert "error" not in (verdict.payload or {})


@pytest.mark.asyncio
async def test_t023_expired_is_the_need_relogin_shape(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert "login_url" in result and "token" in result


@pytest.mark.asyncio
async def test_t024_challenge_is_a_distinct_stop_and_wait_error(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("challenge", {"error": "cloudflare challenge, stop and wait"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.alive is False
    assert verdict.kind == "challenge"
    # Not an empty result, not success, not a "please re-login" shape.
    assert verdict.payload
    assert "login_url" not in verdict.payload


@pytest.mark.asyncio
async def test_t025_transport_failure_reports_unknown_and_keeps_state(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("transport")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)

    assert result.get("status") == "unknown"
    assert "error" in result
    assert app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
async def test_t027_html_redirect_body_is_judged_failure_not_success(tmp_path, monkeypatch):
    """CONTRACT-ADJACENT LIMITATION: the design requires this judgment to go
    through the real classify() (an un-mockable seam per Test Approach), but
    api_client.py is off-limits to read this round (it is being rewritten in
    parallel). This test therefore stays at the verify_restored_session
    boundary: whatever kind classify() would have produced for a
    COMPANY_SWITCH-style HTML redirect body, verify_restored_session must
    not treat it as alive just because the HTTP status was 200."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    # Stands in for classify() rejecting an HTTP-200-but-HTML-redirect
    # body -- verify_restored_session must not special-case status 200.
    fake_guarded_api = stub_guarded_api_abort("malformed", {"error": "unexpected HTML body"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)
    assert verdict.alive is False


# --- shared driver for _watch_for_login (T-42..T-45, T-8, T-94/T-97, T-109,
# T-120): a controllable fake page/context/browser/stream, driven directly
# through auth._watch_for_login (awaited in-line, not spawned as a task) so
# each case can move the clock and the page/cookie state deterministically
# without racing a background task. ---

class _ControllableFramePage:
    """Stands in for the Playwright Page _watch_for_login listens on. Its
    `main_frame` is itself; `url` starts off-host and is moved by
    `navigate_to`, which synchronously invokes every registered
    "framenavigated" handler -- mirroring Playwright's own synchronous
    event dispatch closely enough for this predicate."""

    def __init__(self, start_url: str = "https://bsignin.104.com.tw/login"):
        self.url = start_url
        self._handlers: dict[str, list] = {}

    @property
    def main_frame(self):
        return self

    def on(self, event, handler):
        self._handlers.setdefault(event, []).append(handler)

    def remove_listener(self, event, handler):
        handlers = self._handlers.get(event, [])
        if handler in handlers:
            handlers.remove(handler)

    def navigate_to(self, url: str):
        self.url = url
        for h in list(self._handlers.get("framenavigated", [])):
            h(self)

    async def new_cdp_session(self, *a, **k):
        return types.SimpleNamespace()

    async def close(self):
        pass


class _ControllableContext:
    def __init__(self):
        self._cookies: list[dict] = []
        self.closed = False

    async def new_page(self):
        return _ControllableFramePage()

    async def cookies(self, *a, **k):
        return list(self._cookies)

    async def close(self):
        self.closed = True


class _ControllableBrowser:
    def __init__(self):
        self.closed = False

    async def new_context(self, *a, **k):
        return _ControllableContext()

    async def close(self):
        self.closed = True


def make_driven_pending_login(app_ctx, token: str, page_url="https://bsignin.104.com.tw/login"):
    """Registers a PendingLoginResources entry directly (bypassing
    _start_human_login's browser-launch plumbing, already covered
    elsewhere) so a test can await auth._watch_for_login(app, token,
    app.logout_epoch) directly and assert on the fakes' own observable
    state afterward."""
    from mcp104.tools import auth
    from mcp104.browser.session import PendingLogin

    page = _ControllableFramePage(page_url)
    context = _ControllableContext()
    browser = _ControllableBrowser()
    stream = _FakeCdpLoginStream(page)
    resource = auth.PendingLoginResources(
        browser=browser, context=context, page=page, stream=stream,
        state=auth.LoginState.AWAITING_HUMAN,
    )
    app_ctx.session_pool.add_pending(token, PendingLogin(mcp_session_id="s1"))
    app_ctx._pending_logins[token] = resource
    return resource, page, context, browser, stream


# --- T-42, T-43, T-44 (R9.1, R9.2): two-factor completion predicate ------

async def _drive_to_watching(app_ctx, token: str):
    """Starts auth._watch_for_login as a background task and lets it run
    until it has registered its "framenavigated" listener and suspended on
    the initial wait -- at which point the caller can call
    page.navigate_to(...) and have the handler actually observe it."""
    from mcp104.tools import auth

    task = asyncio.create_task(auth._watch_for_login(app_ctx, token, app_ctx.logout_epoch))
    for _ in range(3):
        await asyncio.sleep(0)
    return task


@pytest.mark.asyncio
async def test_t042_hostname_alone_is_not_enough_keeps_waiting(tmp_path, monkeypatch):
    """R9.1/R9.2: the main frame settles on vip.104.com.tw, but the
    app-session cookie never shows up -- the predicate must not treat the
    hostname alone as completion; the login must still be judged
    incomplete (here: abandoned once the shared deadline passes), not
    activated."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=0.1)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)

    token = "tok-t042"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to("https://vip.104.com.tw/rms/index")
    # No its/ithp cookie is ever added to `context._cookies`.
    await asyncio.wait_for(task, timeout=5)

    assert app_ctx.session_pool.get_session("s1") is None
    assert not app_ctx.config.cookies_path.exists()
    assert app_ctx._finished_logins.get(token) == "abandoned"
    assert token not in app_ctx._pending_logins


@pytest.mark.asyncio
async def test_t043_browser_session_only_cookie_is_not_enough(tmp_path, monkeypatch):
    """R9.1: a cookie that only lives in the browser session (PHPSESSID)
    must not satisfy the second factor -- only its/ithp count, per the
    VIP_SESSION_COOKIE_NAMES contract."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=0.1)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)

    token = "tok-t043"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to("https://vip.104.com.tw/rms/index")
    context._cookies = [
        {"name": "PHPSESSID", "domain": "vip.104.com.tw", "value": "x"},
        {"name": "PHPSESSID", "domain": ".vip.104.com.tw", "value": "x"},
    ]
    await asyncio.wait_for(task, timeout=5)

    assert app_ctx.session_pool.get_session("s1") is None
    assert not app_ctx.config.cookies_path.exists()
    assert app_ctx._finished_logins.get(token) == "abandoned"
    assert token not in app_ctx._pending_logins


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_hostname",
    ["https://evil-vip.104.com.tw/rms/index", "https://x.vip.104.com.tw/rms/index"],
)
async def test_t044_hostname_match_is_exact_not_substring(tmp_path, monkeypatch, bad_hostname):
    """R9.1: hostname comparison must be an exact match against
    vip.104.com.tw -- neither a prefixed nor a subdomain-prefixed
    look-alike host may pass. The app-session cookie is made available
    immediately, so if the hostname check were buggy (substring/suffix
    match) this case would incorrectly succeed instead of timing out."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=0.1)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)

    token = "tok-t044"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)
    context._cookies = [{"name": "its", "domain": ".vip.104.com.tw", "value": "x"}]

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to(bad_hostname)
    await asyncio.wait_for(task, timeout=5)

    assert app_ctx.session_pool.get_session("s1") is None
    assert not app_ctx.config.cookies_path.exists()
    assert app_ctx._finished_logins.get(token) == "abandoned"
    assert token not in app_ctx._pending_logins


# --- T-45 (R9.3, R9.4): abandon-on-timeout releases every resource -------

@pytest.mark.asyncio
async def test_t045_abandon_on_timeout_releases_every_resource(tmp_path, monkeypatch):
    """Neither factor (hostname, then session cookie) becomes true within
    the login timeout budget -> the login is abandoned and every resource
    it held is released: browser, context, and CDP stream all closed, the
    pending registration is gone, and the outcome is recorded as
    "abandoned"."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=0)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.001, raising=False)

    token = "tok-t045"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)
    # Page never navigates anywhere -- the hostname factor never becomes true.

    await auth._watch_for_login(app_ctx, token, app_ctx.logout_epoch)

    assert app_ctx._finished_logins.get(token) == "abandoned"
    assert token not in app_ctx._pending_logins
    assert app_ctx.session_pool.find_pending_tokens_for_session("s1") == []
    assert stream.state == "closed"
    assert context.closed is True
    assert browser.closed is True


# --- T-67 (auth.login): five branches ------------------------------------

@pytest.mark.asyncio
async def test_t067_branch_no_state_returns_login_url_and_token(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    result = await auth.login(ctx)
    assert set(result.keys()) >= {"login_url", "token"}


@pytest.mark.asyncio
async def test_t067_branch_valid_state_returns_restored(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert result.get("status") == "restored"


@pytest.mark.asyncio
async def test_t067_branch_invalid_state_falls_to_human_login(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert set(result.keys()) >= {"login_url", "token"}


@pytest.mark.asyncio
async def test_t067_branch_in_memory_session_alive_keeps_pool_entry(tmp_path, monkeypatch):
    from mcp104.tools import auth

    session = make_session()
    pool = SessionPool()
    pool.activate_direct("s1", session)
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert result.get("status") == "already_logged_in"
    assert pool.get_session("s1") is not None


@pytest.mark.asyncio
async def test_t067_branch_in_memory_session_non_alive_pool_state(tmp_path, monkeypatch):
    """Non-expired non-alive verdicts must NOT drop the pool session; only
    expired does (per the disposition table's asymmetric rule)."""
    from mcp104.tools import auth

    session = make_session()
    pool = SessionPool()
    pool.activate_direct("s1", session)
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("challenge")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.login(ctx)
    assert pool.get_session("s1") is not None


@pytest.mark.asyncio
async def test_t067_branch_in_memory_session_expired_pool_state(tmp_path, monkeypatch):
    from mcp104.tools import auth

    session = make_session()
    pool = SessionPool()
    pool.activate_direct("s1", session)
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.login(ctx)
    assert pool.get_session("s1") is None


@pytest.mark.asyncio
async def test_t067_branch_pending_login_returns_that_login(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    first = await auth.login(ctx)
    second = await auth.login(ctx)
    assert second == first


# --- T-68 (auth.check_login): four-state priority order -------------------

@pytest.mark.asyncio
async def test_t068_success_beats_everything_regardless_of_token(tmp_path):
    from mcp104.tools import auth

    session = make_session()
    pool = SessionPool()
    pool.activate_direct("s1", session)
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    ctx = make_ctx(app_ctx)

    result = await auth.check_login("some-random-token-never-issued", ctx)
    assert result["status"] == "success"


@pytest.mark.asyncio
async def test_t068_pending_token_reports_pending(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    login_result = await auth.login(ctx)
    token = login_result["token"]

    result = await auth.check_login(token, ctx)
    assert result["status"] == "pending"


@pytest.mark.asyncio
async def test_t068_this_run_minted_and_timed_out_token_reports_failed(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    login_result = await auth.login(ctx)
    token = login_result["token"]
    # Simulate the watcher having given up on this token.
    app_ctx._finished_logins[token] = "abandoned"
    app_ctx._pending_logins.pop(token, None)

    result = await auth.check_login(token, ctx)
    assert result["status"] == "failed"


@pytest.mark.asyncio
async def test_t068_never_before_seen_token_reports_unknown_not_failed(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    result = await auth.check_login("token-from-a-previous-process", ctx)
    assert result["status"] == "unknown"
    assert result["status"] != "failed"


@pytest.mark.asyncio
async def test_t068_check_login_does_no_io(tmp_path, monkeypatch):
    """check_login must not issue any request or read persisted state."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    def boom(*a, **k):
        raise AssertionError("check_login must not touch guarded_api")

    monkeypatch.setattr(auth, "guarded_api", boom, raising=False)

    def boom_load(*a, **k):
        raise AssertionError("check_login must not read persisted cookies")

    monkeypatch.setattr(auth, "load_cookies", boom_load, raising=False)

    result = await auth.check_login("any-token", ctx)
    assert result["status"] == "unknown"


# --- T-77 (R3.1, R4.2) e2e: rejected persisted state -> address, no contradictions ---

@pytest.mark.asyncio
async def test_t077_rejected_persisted_state_gives_consistent_answers(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    login_result = await auth.login(ctx)
    assert "login_url" in login_result and "token" in login_result

    # check_login on the fresh token must not contradict login()'s answer
    # by claiming success.
    check_result = await auth.check_login(login_result["token"], ctx)
    assert check_result["status"] != "success"


# --- T-79 (R1.4): typed input never leaks through any of four channels ---

def test_t079_input_value_absent_from_agent_response_channel(tmp_path):
    """CONTRACT-ADJACENT LIMITATION: full coverage of all four channels
    (Agent response, protocol channel, diagnostic output, disk) requires
    driving a real login interaction through CdpLoginStream.dispatch_input
    and the watcher, whose wiring into tools/auth.py is not named in
    design.md's C6 Interfaces. This narrows to the one channel testable
    without that wiring: login()'s own return value must never echo back
    typed content (it only ever carries login_url/token/status/warning/error)."""
    import asyncio as _asyncio
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    secret = "sekritPassw0rd!!"
    result = _asyncio.run(auth.login(ctx))
    assert secret not in json.dumps(result)


# --- T-80, T-81, T-82 (R3.9, R3.10) ---------------------------------------

@pytest.mark.asyncio
async def test_t080_never_issued_token_is_undetermined_not_failed(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    result = await auth.check_login("token-nobody-here-ever-minted", ctx)
    assert result["status"] == "unknown"


@pytest.mark.asyncio
async def test_t081_own_timed_out_token_still_reports_failed_distinct_from_unknown(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    login_result = await auth.login(ctx)
    token = login_result["token"]
    app_ctx._finished_logins[token] = "abandoned"
    app_ctx._pending_logins.pop(token, None)

    failed = await auth.check_login(token, ctx)
    unknown = await auth.check_login("some-other-never-issued-token", ctx)

    assert failed["status"] == "failed"
    assert unknown["status"] == "unknown"
    assert failed["status"] != unknown["status"]


@pytest.mark.asyncio
async def test_t082_unknown_response_points_to_login_as_the_authority(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    result = await auth.check_login("token-from-a-previous-process", ctx)
    assert result["status"] == "unknown"
    blob = json.dumps(result)
    assert "login" in blob.lower()


# --- T-83 (R4.8): 403 is its own fourth shape, keeps the cookie file -----

@pytest.mark.asyncio
async def test_t083_blocked_is_its_own_shape_and_keeps_cookie_file(tmp_path, monkeypatch):
    from mcp104.tools import auth
    from mcp104.tools import helpers

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("blocked")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.kind == "blocked"
    assert verdict.payload == helpers.ERROR_BLOCKED_API_RESTORE_VERIFY
    assert app_ctx.config.cookies_path.exists()


# --- T-84 (R4.9): uncovered result keeps cookie file and reports plainly -

@pytest.mark.asyncio
async def test_t084_uncovered_result_keeps_jar_and_names_itself(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    ctx = make_ctx(app_ctx)

    novel_kind = "a_kind_the_disposition_table_has_never_heard_of"

    fake_guarded_api = stub_guarded_api_abort(novel_kind)

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    verdict = await auth.verify_restored_session(ctx)

    assert verdict.keep_jar is True
    assert verdict.kind == novel_kind
    assert app_ctx.config.cookies_path.exists()


# --- T-86 (R1.14): repeated login() returns the original, uninterrupted --

@pytest.mark.asyncio
async def test_t086_repeated_login_returns_original_pending_login(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    first = await auth.login(ctx)
    second = await auth.login(ctx)
    third = await auth.login(ctx)

    assert first["token"] == second["token"] == third["token"]
    assert first["login_url"] == second["login_url"] == third["login_url"]
    # The original pending login was not superseded/discarded.
    assert first["token"] in app_ctx._pending_logins


# --- T-87 (R9.5): default human-wait budget >= measured full login time --

def test_t087_login_timeout_seconds_default_is_at_least_measured_human_login_time(monkeypatch):
    """R9.5: the default human-wait budget must not undercut the measured
    265s full human login time (MFA + product selection + repeatLogin)."""
    from mcp104.config import get_config

    monkeypatch.setenv("MCP104_ACCOUNT_LABEL", "acct")
    monkeypatch.delenv("LOGIN_TIMEOUT_SECONDS", raising=False)

    assert get_config().login_timeout_seconds >= 265


# --- T-95 (auth.provisional_session) --------------------------------------

@pytest.mark.asyncio
async def test_t095_commit_keeps_registration(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    async with auth.provisional_session(ctx, [{"name": "its", "value": "x"}]) as reg:
        reg.commit()

    assert app_ctx.session_pool.get_session("s1") is not None


@pytest.mark.asyncio
async def test_t095_no_commit_deregisters_on_exit(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    async with auth.provisional_session(ctx, [{"name": "its", "value": "x"}]):
        assert app_ctx.session_pool.get_session("s1") is not None

    assert app_ctx.session_pool.get_session("s1") is None


@pytest.mark.asyncio
async def test_t095_exception_deregisters(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    with pytest.raises(ValueError):
        async with auth.provisional_session(ctx, [{"name": "its", "value": "x"}]):
            raise ValueError("boom")

    assert app_ctx.session_pool.get_session("s1") is None


@pytest.mark.asyncio
async def test_t095_cancellation_deregisters_and_propagates(tmp_path):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    async def enter_and_hang():
        async with auth.provisional_session(ctx, [{"name": "its", "value": "x"}]):
            await asyncio.sleep(10)

    task = asyncio.create_task(enter_and_hang())
    await asyncio.sleep(0)  # let it enter the context manager
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert app_ctx.session_pool.get_session("s1") is None


# --- T-112 (auth.login): logout_unconfirmed marker read side, remaining halves ---

@pytest.mark.asyncio
async def test_t112a_warning_present_when_marker_exists_and_alive(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    app_ctx.config.logout_unconfirmed_path.write_text("")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert result.get("status") in ("restored", "already_logged_in")
    assert "warning" in result and result["warning"]


@pytest.mark.asyncio
async def test_t112b_no_warning_when_marker_absent(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    assert not app_ctx.config.logout_unconfirmed_path.exists()
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_ok({"status": "SUCCESS"})

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert result.get("status") in ("restored", "already_logged_in")
    assert "warning" not in result


@pytest.mark.asyncio
async def test_t112c_no_warning_when_verdict_not_alive(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config.cookies_path.parent.mkdir(parents=True, exist_ok=True)
    app_ctx.config.cookies_path.write_text(json.dumps([{"name": "its", "value": "x"}]))
    app_ctx.config.logout_unconfirmed_path.write_text("")
    ctx = make_ctx(app_ctx)

    fake_guarded_api = stub_guarded_api_abort("expired")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.login(ctx)
    assert "warning" not in result


# ===========================================================================
# Batch 3: logout() and the watcher's atomic handoff block
# ===========================================================================
#
# CONTRACT UNCLEAR (applies to the whole batch): the watcher coroutine that
# performs "reread AppContext.logout_epoch -> judge -> save_cookies ->
# activate() -> flip -> CdpLoginStream.mark_completed()" is not named among
# C6's public Interfaces list. design.md's Test Approach for T-93/T-116
# assumes the test can find and statically inspect this block, but does not
# name the function it lives in. Below, a source-scan over the whole
# tools/auth.py module is used instead of naming one function -- it looks
# for any async function whose body calls something ending in
# "save_cookies", something ending in "activate", and "mark_completed" (or,
# failing that, "announce_completed") in sequence, and asserts no `await`
# node falls between the first of those calls and the last. This is
# necessarily best-effort without reading the module for understanding.

def _find_atomic_handoff_function_ast():
    import ast as _ast

    src_path = Path(__file__).resolve().parents[1] / "src" / "mcp104" / "tools" / "auth.py"
    tree = _ast.parse(src_path.read_text(encoding="utf-8"))

    class Finder(_ast.NodeVisitor):
        def __init__(self):
            self.hits = []

        def visit_AsyncFunctionDef(self, node):
            calls_save = calls_activate = calls_mark = False
            for n in _ast.walk(node):
                if isinstance(n, _ast.Call):
                    fname = None
                    if isinstance(n.func, _ast.Attribute):
                        fname = n.func.attr
                    elif isinstance(n.func, _ast.Name):
                        fname = n.func.id
                    if fname and "save_cookies" in fname:
                        calls_save = True
                    if fname == "activate":
                        calls_activate = True
                    if fname == "mark_completed":
                        calls_mark = True
            if calls_save and calls_activate and calls_mark:
                self.hits.append(node)
            self.generic_visit(node)

    f = Finder()
    f.visit(tree)
    return f.hits


def _stmt_reads_logout_epoch(stmt):
    import ast as _ast

    return any(
        isinstance(n, _ast.Attribute) and n.attr == "logout_epoch"
        for n in _ast.walk(stmt)
    )


def _stmt_calls_mark_completed(stmt):
    import ast as _ast

    for n in _ast.walk(stmt):
        if not isinstance(n, _ast.Call):
            continue
        fname = None
        if isinstance(n.func, _ast.Attribute):
            fname = n.func.attr
        elif isinstance(n.func, _ast.Name):
            fname = n.func.id
        if fname == "mark_completed":
            return True
    return False


def _find_atomic_handoff_watcher_function():
    """Per design.md SS C6, the atomic handoff block lives in exactly one
    coroutine: the one that both rereads AppContext.logout_epoch (an
    attribute access, not just a textual mention) and calls
    mark_completed(). This is a narrower, more specific identity than
    _find_atomic_handoff_function_ast's "calls save_cookies, activate, and
    mark_completed somewhere in its body" -- that broader check can also
    match a function which merely awaits something else that happens to
    reference those names (e.g. the restore-verification path), which is
    exactly the false-positive T-93/T-116 exists to rule out. Returns the
    list of matching FunctionDef/AsyncFunctionDef nodes; callers must
    require this list to have exactly one element."""
    import ast as _ast

    src_path = Path(__file__).resolve().parents[1] / "src" / "mcp104" / "tools" / "auth.py"
    tree = _ast.parse(src_path.read_text(encoding="utf-8"))

    candidates = []
    for node in _ast.walk(tree):
        if not isinstance(node, (_ast.FunctionDef, _ast.AsyncFunctionDef)):
            continue
        reads_epoch = any(
            isinstance(n, _ast.Attribute) and n.attr == "logout_epoch"
            for n in _ast.walk(node)
        )
        calls_mark = any(
            isinstance(n, _ast.Call)
            and (
                (isinstance(n.func, _ast.Attribute) and n.func.attr == "mark_completed")
                or (isinstance(n.func, _ast.Name) and n.func.id == "mark_completed")
            )
            for n in _ast.walk(node)
        )
        if reads_epoch and calls_mark:
            candidates.append(node)
    return candidates


def _find_atomic_block_spans():
    """Locates the exact sibling statement list the atomic block lives in
    -- which may be nested inside a try/while, not the function's own
    top-level body (it is, in the real _watch_for_login: the epoch-reread
    through mark_completed() are all direct siblings inside the enclosing
    `try:` block, alongside an earlier cookie-polling loop that legitimately
    awaits). Slicing at the function's top level would wrongly include that
    loop's await.

    Scope is first narrowed to the single function
    _find_atomic_handoff_watcher_function identifies (0 or >1 matches is a
    hard failure, not a skip -- an ambiguous or missing watcher symbol is a
    fact worth failing loudly on, not silently tolerating). Within that
    function only, this walks every node that owns a statement list
    (body/orelse/finalbody) and, within each such list on its own, looks
    for the sibling span from the first statement that reads the
    logout_epoch attribute to the last statement that calls
    mark_completed()."""
    import ast as _ast

    candidates = _find_atomic_handoff_watcher_function()
    assert len(candidates) == 1, (
        "expected exactly one function in tools/auth.py that both rereads "
        "AppContext.logout_epoch and calls mark_completed() (the watcher's "
        "atomic-block coroutine per design.md SS C6); found "
        f"{len(candidates)}: {[getattr(c, 'name', '?') for c in candidates]}"
    )
    func = candidates[0]

    spans = []
    for node in _ast.walk(func):
        for field in ("body", "orelse", "finalbody"):
            stmts = getattr(node, field, None)
            if not isinstance(stmts, list) or not stmts or not isinstance(stmts[0], _ast.stmt):
                continue
            start_idx = end_idx = None
            for i, stmt in enumerate(stmts):
                if start_idx is None and _stmt_reads_logout_epoch(stmt):
                    start_idx = i
                if _stmt_calls_mark_completed(stmt):
                    end_idx = i
            if start_idx is not None and end_idx is not None and end_idx >= start_idx:
                spans.append(stmts[start_idx:end_idx + 1])

    # A single compound statement (If/Try/While/For/With/...) whose *nested*
    # body is where logout_epoch and mark_completed actually live will also
    # satisfy the scan above at its own enclosing list, trivially wrapping
    # the real match in one outer statement -- and that wrapper's span then
    # contains every await buried anywhere inside it, which is exactly the
    # false positive this rewrite exists to avoid (matching a `try:` that
    # merely contains the real block, rather than the block itself). Drop
    # any single-statement span whose statement is itself compound; the
    # narrower span from that statement's own body list is found separately
    # by the walk above and is the one that actually reflects the block.
    _compound = (
        _ast.If, _ast.Try, _ast.While, _ast.For, _ast.AsyncFor,
        _ast.With, _ast.AsyncWith, _ast.FunctionDef, _ast.AsyncFunctionDef,
    )
    spans = [
        span for span in spans
        if not (len(span) == 1 and isinstance(span[0], _compound))
    ]
    return spans


def test_t093_t116_atomic_handoff_block_contains_no_await():
    """T-93 (structural): the block from the logout_epoch reread through
    save_cookies() -> activate() -> the internal state flip contains no
    `await`. T-116 adds: mark_completed() is called right after the flip,
    still inside that no-await span, and mark_completed itself is not a
    coroutine function."""
    spans = _find_atomic_block_spans()
    assert spans, (
        "the watcher function (identified as the sole function reading "
        "AppContext.logout_epoch and calling mark_completed()) has no "
        "sibling-statement span running from a logout_epoch read to a "
        "mark_completed() call -- design.md SS C6's atomic block "
        "(epoch-check -> save_cookies -> activate -> flip -> "
        "mark_completed) could not be located inside it."
    )

    import ast as _ast

    for span in spans:
        for stmt in span:
            for n in _ast.walk(stmt):
                assert not isinstance(n, _ast.Await), (
                    f"await found inside the supposedly atomic handoff "
                    f"block: {_ast.dump(n)[:120]}"
                )


def test_t116_mark_completed_is_not_a_coroutine_function():
    from mcp104.browser.cdp_stream import CdpLoginStream

    assert not inspect.iscoroutinefunction(CdpLoginStream.mark_completed)


# --- T-94, T-97 (R1.11/R1.13, R1.2/R1.13): admission around the settling edge ---
#
# Both routes (viewer page, WebSocket) share exactly one admission source:
# `_make_get_admissible_stream(app)`'s returned `token -> CdpLoginStream |
# None` lookup (design.md Section C5 / Interfaces: create_auth_app's sole
# input). That function is what these two cases drive directly -- it is
# named in C6's public Interfaces list, so this is not fabricated wiring.

@pytest.mark.asyncio
async def test_t097_pre_judgment_admission_is_open(tmp_path):
    """R1.2/R1.13: before the dual-factor judgment holds (state is still
    awaiting_human), a new connection is admitted -- get_admissible_stream
    hands back the live stream, which is what both routes key off of."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    token = "tok-t097"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)
    assert resource.state == auth.LoginState.AWAITING_HUMAN

    get_admissible_stream = auth._make_get_admissible_stream(app_ctx)
    assert get_admissible_stream(token) is stream


@pytest.mark.asyncio
async def test_t094_settling_admission_closes_but_existing_stream_survives_to_settle(
    tmp_path, monkeypatch
):
    """R1.11/R1.13: the instant the judgment holds (state flips to
    settling), a new connection is rejected (get_admissible_stream ->
    None) -- but the existing stream is not torn down: it stays alive,
    receives the completion notice, and only actually closes once the
    settle window elapses."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=5)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(auth, "POST_SUCCESS_SETTLE_SECONDS", 0.15, raising=False)

    token = "tok-t094"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)
    get_admissible_stream = auth._make_get_admissible_stream(app_ctx)

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to("https://vip.104.com.tw/rms/index")
    context._cookies = [{"name": "its", "domain": ".vip.104.com.tw", "value": "x"}]

    for _ in range(500):
        if resource.state == auth.LoginState.SETTLING:
            break
        await asyncio.sleep(0.005)
    assert resource.state == auth.LoginState.SETTLING

    # New connections closed off immediately...
    assert get_admissible_stream(token) is None
    # ...but the existing stream survives, already marked completed.
    assert stream.state == "completed"
    assert not task.done()

    await asyncio.wait_for(task, timeout=5)

    # Only now, after the settle window elapsed, does it actually close.
    assert stream.state == "closed"
    assert get_admissible_stream(token) is None


# --- Regression (I1-A): _start_human_login closes browser/context on a
# failed startup step, before any pending registration exists to own them ---

@pytest.mark.asyncio
async def test_regression_start_human_login_closes_browser_and_context_on_goto_failure(
    tmp_path, monkeypatch
):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    closed = {"context": False, "browser": False}

    class _FailingPage:
        async def goto(self, url, *a, **k):
            raise RuntimeError("navigation failed")

    class _TrackedContext:
        async def new_page(self):
            return _FailingPage()

        async def close(self):
            closed["context"] = True

    class _TrackedBrowser:
        async def close(self):
            closed["browser"] = True

    async def fake_launch_browser(*a, **k):
        return _TrackedBrowser()

    async def fake_create_stealth_context(browser, *a, **k):
        return _TrackedContext()

    monkeypatch.setattr(auth, "launch_browser", fake_launch_browser, raising=False)
    monkeypatch.setattr(auth, "create_stealth_context", fake_create_stealth_context, raising=False)

    with pytest.raises(RuntimeError):
        await auth.login(ctx)

    assert closed["context"] is True
    assert closed["browser"] is True
    assert app_ctx._pending_logins == {}


# --- Regression (I1-B): an exception after the atomic completion block
# (succeeded=True) still runs _finalize_pending_login, not left stuck in
# `settling` ---

@pytest.mark.asyncio
async def test_regression_post_success_exception_still_finalizes_not_stuck_in_settling(
    tmp_path, monkeypatch
):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=5)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)

    token = "tok-regression-post-success"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)

    async def failing_announce():
        raise OSError("disk full")

    stream.announce_completed = failing_announce

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to("https://vip.104.com.tw/rms/index")
    context._cookies = [{"name": "its", "domain": ".vip.104.com.tw", "value": "x"}]

    await asyncio.wait_for(task, timeout=5)

    assert token not in app_ctx._pending_logins
    assert stream.state == "closed"


# --- T-98 (R2.4): logout() during an in-flight (not yet completed) login ---

@pytest.mark.asyncio
async def test_t098_logout_during_pending_login_tears_down_that_login(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    login_result = await auth.login(ctx)
    token = login_result["token"]
    assert token in app_ctx._pending_logins

    fake_guarded_api = stub_guarded_api_abort("not_logged_in")  # awaiting_human: no session yet

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.logout(ctx)

    assert token not in app_ctx._pending_logins
    # No login state must have been persisted by the torn-down login.
    assert not app_ctx.config.cookies_path.exists()


@pytest.mark.asyncio
async def test_t098_login_after_logout_during_pending_opens_a_new_login(tmp_path, monkeypatch):
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    ctx = make_ctx(app_ctx)

    first = await auth.login(ctx)

    fake_guarded_api = stub_guarded_api_abort("not_logged_in")

    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.logout(ctx)

    second = await auth.login(ctx)
    assert second["token"] != first["token"]
    assert second["login_url"] != first["login_url"]


# --- T-109 (R2.4): a watcher landing "login complete" after logout() must not write ---

class _SlowCancelContext(_ControllableContext):
    """A context whose first cookies() call ignores its first
    CancelledError and delays past WATCHER_CANCEL_TIMEOUT before actually
    unwinding -- simulates a watcher whose cancellation does not land
    within budget, the exact scenario T-109 exists to guard."""

    def __init__(self, uncancellable_delay: float):
        super().__init__()
        self._uncancellable_delay = uncancellable_delay
        self._first_call = True

    async def cookies(self, *a, **k):
        if self._first_call:
            self._first_call = False
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(self._uncancellable_delay)
                raise
        return list(self._cookies)


@pytest.mark.asyncio
async def test_t109_watcher_that_completes_after_logout_writes_nothing(tmp_path, monkeypatch):
    """R2.4: a watcher whose cancellation does not land within
    WATCHER_CANCEL_TIMEOUT (logout() therefore returns
    teardown_confirmed: False) must still discard itself once it resumes
    and finds the login epoch has moved on -- it must not write the
    credential file or activate a pool session a second time. The
    abandoned record comes from logout()'s own _finalize_pending_login
    call, not from the watcher writing it again."""
    from mcp104.tools import auth
    from mcp104.browser.session import PendingLogin

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=5)
    monkeypatch.setattr(auth, "WATCHER_CANCEL_TIMEOUT", 0.02, raising=False)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)

    token = "tok-t109"
    page = _ControllableFramePage()
    context = _SlowCancelContext(uncancellable_delay=0.1)
    browser = _ControllableBrowser()
    stream = _FakeCdpLoginStream(page)
    resource = auth.PendingLoginResources(
        browser=browser, context=context, page=page, stream=stream,
        state=auth.LoginState.AWAITING_HUMAN,
    )
    app_ctx.session_pool.add_pending(token, PendingLogin(mcp_session_id="s1"))
    app_ctx._pending_logins[token] = resource

    task = await _drive_to_watching(app_ctx, token)
    app_ctx._watcher_tasks[token] = task
    page.navigate_to("https://vip.104.com.tw/rms/index")
    # Let the watcher wake from the nav-wait and suspend inside its first
    # cookies() call (the uncancellable point _SlowCancelContext sets up).
    await asyncio.sleep(0.01)

    ctx = make_ctx(app_ctx)
    fake_guarded_api = stub_guarded_api_abort("not_logged_in")
    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    result = await auth.logout(ctx)

    assert result["teardown_confirmed"] is False
    assert app_ctx._finished_logins.get(token) == "abandoned"
    assert token not in app_ctx._pending_logins
    assert app_ctx.session_pool.get_session("s1") is None
    assert not app_ctx.config.cookies_path.exists()

    # Let the watcher's own delayed unwind finish -- assert it wrote
    # nothing further once it does.
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout=2)
    assert not app_ctx.config.cookies_path.exists()
    assert app_ctx.session_pool.get_session("s1") is None


@pytest.mark.asyncio
async def test_t109_reverse_without_logout_watcher_writes_normally(tmp_path, monkeypatch):
    """Reverse half: absent any logout(), the same watcher path completes
    normally -- writes the credential file and activates the pool
    session. Guards against an epoch-gate implementation that discards
    unconditionally; T-109's "writes nothing" half only holds once
    logout() has actually bumped the epoch."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=5)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)

    token = "tok-t109-rev"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to("https://vip.104.com.tw/rms/index")
    context._cookies = [{"name": "its", "domain": ".vip.104.com.tw", "value": "x"}]

    await asyncio.wait_for(task, timeout=5)

    assert app_ctx.config.cookies_path.exists()
    assert app_ctx.session_pool.get_session("s1") is not None


# --- T-120 (R1.13): logout() during `settling` is the handed_off 4th path ---

@pytest.mark.asyncio
async def test_t120_logout_during_settling_closes_early_and_next_login_reads_cookie_file(
    tmp_path, monkeypatch
):
    """R1.13: calling logout() while a login is `settling` takes the 4th
    handed_off path, distinct from the other three in two ways:
    (a) the stream/socket close early -- the caller does not wait out the
    remainder of POST_SUCCESS_SETTLE_SECONDS;
    (b) the next login() call takes the cookie-file branch, not the pool
    branch -- this logout() already emptied the pool (Architecture
    lifecycle table, handed_off row)."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=5)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)
    monkeypatch.setattr(auth, "POST_SUCCESS_SETTLE_SECONDS", 5, raising=False)

    token = "tok-t120"
    resource, page, context, browser, stream = make_driven_pending_login(app_ctx, token)

    task = await _drive_to_watching(app_ctx, token)
    app_ctx._watcher_tasks[token] = task
    page.navigate_to("https://vip.104.com.tw/rms/index")
    context._cookies = [{"name": "its", "domain": ".vip.104.com.tw", "value": "x"}]

    # Wait until the watcher has completed the atomic block (mark_completed
    # + cookie file written + pool activated) and is now sitting inside the
    # settle-window sleep -- i.e. `settling`, not yet `handed_off`.
    for _ in range(500):
        if resource.state == auth.LoginState.SETTLING:
            break
        await asyncio.sleep(0.005)
    assert resource.state == auth.LoginState.SETTLING
    assert stream.state == "completed"
    assert app_ctx.session_pool.get_session("s1") is not None
    assert not task.done()  # still inside the 5s settle sleep

    ctx = make_ctx(app_ctx)
    fake_guarded_api = stub_guarded_api_abort("not_logged_in")
    monkeypatch.setattr(auth, "guarded_api", fake_guarded_api, raising=False)

    await auth.logout(ctx)

    # (a) early closure -- did not wait out the 5s settle window.
    assert stream.state == "closed"
    assert context.closed is True
    assert browser.closed is True
    assert token not in app_ctx._pending_logins

    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, timeout=2)

    # (b) next login() does not take the pool branch (branch B) -- this
    # logout() already emptied the pool, per the Architecture lifecycle
    # table's handed_off row ("等 logout() 回傳之後兩份狀態都不存在 -> C").
    # Branch C attempts the credential file; logout()'s own Step 3 also
    # cleared it, so C's own fallthrough to a fresh human login is what's
    # observable here -- the assertion that matters is that this is NOT
    # branch B's stale "already_logged_in"/"restored" pool answer.
    assert app_ctx.session_pool.get_session("s1") is None
    result = await auth.login(ctx)
    assert result.get("status") not in ("already_logged_in", "restored")
    assert "token" in result and "login_url" in result


# --- T-8 (R1.11): settle survives for exactly the named constant ----------

@pytest.mark.asyncio
async def test_t008_settle_window_survives_for_exactly_the_named_duration(tmp_path, monkeypatch):
    """R1.11: after dual-factor completion, the viewer page gets the
    completion notice (announce_completed()) first, and only after
    surviving a duration equal to the named POST_SUCCESS_SETTLE_SECONDS
    constant does the stream actually close (stop()). The duration must
    be assertable, not "about" -- so this measures the elapsed time
    between the two and checks it against the (monkeypatched, small)
    constant with a tight tolerance, not just "eventually happened"."""
    from mcp104.tools import auth

    app_ctx = make_app_ctx(tmp_path)
    app_ctx.config = make_config(tmp_path, login_timeout_seconds=5)
    monkeypatch.setattr(auth, "COOKIE_POLL_INTERVAL", 0.01, raising=False)
    settle_seconds = 0.15
    monkeypatch.setattr(auth, "POST_SUCCESS_SETTLE_SECONDS", settle_seconds, raising=False)

    token = "tok-t008"
    resource, page, context, browser, _unused_stream = make_driven_pending_login(app_ctx, token)

    class _TimedStream(_FakeCdpLoginStream):
        def __init__(self, page):
            super().__init__(page)
            self.announced_at = None
            self.stopped_at = None

        async def announce_completed(self):
            self.announced_at = asyncio.get_event_loop().time()

        async def stop(self):
            self.stopped_at = asyncio.get_event_loop().time()
            await super().stop()

    timed_stream = _TimedStream(page)
    resource.stream = timed_stream

    task = await _drive_to_watching(app_ctx, token)
    page.navigate_to("https://vip.104.com.tw/rms/index")
    context._cookies = [{"name": "its", "domain": ".vip.104.com.tw", "value": "x"}]

    await asyncio.wait_for(task, timeout=5)

    assert timed_stream.announced_at is not None
    assert timed_stream.stopped_at is not None
    elapsed = timed_stream.stopped_at - timed_stream.announced_at
    # Must have actually survived (at least) the named window -- not closed
    # immediately alongside the completion notice.
    assert elapsed >= settle_seconds
    # And not an unrelated, much longer wait either -- the duration is this
    # named constant, assertable, not "about".
    assert elapsed < settle_seconds + 0.3
    assert timed_stream.state == "closed"
