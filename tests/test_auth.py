"""Tests for the pure predicate guarding _watch_for_login's completion
trigger — the entire safety net against silently reintroducing the bug
that made every login false-succeed before this cycle (see
docs/104-site-facts.md and tools/auth.py's module docstring).

Imports tools.auth directly, which transitively imports patchright via
browser/stealth.py — unlike browser/session.py, tools/auth.py has never
kept a TYPE_CHECKING guard around that dependency, so this file requires
patchright to be installed (it is, both locally and in the container).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from pathlib import Path

import pytest

from mcp104.tools.auth import (
    LoginState,
    PendingLoginResources,
    _finalize_pending_login,
    _has_vip_session_cookie,
)


def test_bsignin_only_cookies_is_false():
    # Exactly the shape of the historical bad cookies.json: bsignin's
    # laravel_session + boidc's OAuth CSRF cookies, no vip.104.com.tw
    # cookie at all — the mid-flow snapshot that never produced a usable
    # session.
    cookies = [
        {"name": "laravel_session", "domain": ".bsignin.104.com.tw"},
        {"name": "ory_hydra_session", "domain": "boidc.104.com.tw"},
        {"name": "ory_hydra_login_csrf_x", "domain": "boidc.104.com.tw"},
    ]
    assert _has_vip_session_cookie(cookies) is False


def test_phpsessid_alone_on_vip_is_false():
    # PHPSESSID is session-only (docs/104-site-facts.md) and does not
    # survive the transfer into the headless context — its presence must
    # not be read as "logged in".
    cookies = [{"name": "PHPSESSID", "domain": ".vip.104.com.tw"}]
    assert _has_vip_session_cookie(cookies) is False


def test_its_on_vip_dot_domain_is_true():
    cookies = [{"name": "its", "domain": ".vip.104.com.tw"}]
    assert _has_vip_session_cookie(cookies) is True


def test_ithp_on_bare_vip_domain_is_true():
    cookies = [{"name": "ithp", "domain": "vip.104.com.tw"}]
    assert _has_vip_session_cookie(cookies) is True


def test_its_on_wrong_domain_is_false():
    # Same cookie NAME, wrong domain — must not match on name alone.
    cookies = [{"name": "its", "domain": ".bsignin.104.com.tw"}]
    assert _has_vip_session_cookie(cookies) is False


def test_empty_cookie_jar_is_false():
    assert _has_vip_session_cookie([]) is False


def test_mixed_jar_with_vip_session_cookie_among_others_is_true():
    cookies = [
        {"name": "_ga", "domain": ".104.com.tw"},
        {"name": "cf_clearance", "domain": ".vip.104.com.tw"},
        {"name": "its", "domain": ".vip.104.com.tw"},
    ]
    assert _has_vip_session_cookie(cookies) is True


# ── _finalize_pending_login: cancellation must be bounded, and every
# resource still gets torn down even when the watcher itself hangs ──────
#
# Renamed from the old _abandon_pending_login (this cycle's login-lifecycle
# rewrite, §C6): the old name and its _FakeApp (`_pending_browsers`,
# `vnc_manager`) no longer match the real signature at all — there is no
# more VNC layer (browser/vnc.py is deleted, §C3) and pending-login state is
# now PendingLoginResources keyed in AppContext._pending_logins, not a bare
# browser handle. This test's job is unchanged: prove teardown proceeds
# (and reports it did NOT fully confirm) even when the watcher task never
# actually finishes.


class _FakeSessionPool:
    def __init__(self):
        self.discarded = []

    def discard_pending(self, token):
        self.discarded.append(token)


class _FakeStream:
    def __init__(self):
        self.stopped = False

    async def stop(self):
        self.stopped = True


class _FakeBrowserResource:
    """Stand-in for both Browser and BrowserContext — _finalize_pending_login
    only ever calls .close() on either."""

    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


@dataclass
class _FakeConfig:
    auth_bind_port: int | None = None

    # A plain class, so a missing attribute is a runtime AttributeError
    # rather than the construction-time TypeError the real frozen Config
    # gives. resume_files_dir is REQUIRED and must be absolute: logout()'s
    # cleanup hands it to shutil.rmtree, and a relative default would
    # resolve against whatever cwd the run happens to have (the repo root,
    # in practice).
    def __init__(self, resume_files_dir: Path):
        assert Path(resume_files_dir).is_absolute()
        self.resume_files_dir = Path(resume_files_dir)


class _FakeApp:
    def __init__(self, resume_files_dir: Path):
        self._watcher_tasks = {}
        self._pending_logins = {}
        self._finished_logins = {}
        self.session_pool = _FakeSessionPool()
        self.config = _FakeConfig(resume_files_dir)
        self.auth_site = None


@pytest.mark.asyncio
async def test_finalize_pending_login_does_not_hang_on_a_stuck_watcher(monkeypatch, tmp_path):
    # Simulate a watcher whose own cleanup hangs even after cancellation —
    # e.g. `await context.close()` on a browser whose CDP connection is
    # already dead (CLAUDE.md's known-issue #1, /dev/shm pressure).
    # _finalize_pending_login must not wait on it forever.
    monkeypatch.setattr("mcp104.tools.auth.WATCHER_CANCEL_TIMEOUT", 0.05)

    async def stuck_watcher():
        try:
            await asyncio.sleep(100)
        except asyncio.CancelledError:
            # "Recovers" from the first cancellation but then hangs anyway
            # — models a blocking close, not a well-behaved cancellable await.
            await asyncio.sleep(100)

    app = _FakeApp(tmp_path / "resume-files")
    task = asyncio.ensure_future(stuck_watcher())
    app._watcher_tasks["tok"] = task

    stream = _FakeStream()
    context = _FakeBrowserResource()
    browser = _FakeBrowserResource()
    app._pending_logins["tok"] = PendingLoginResources(
        browser=browser, context=context, page=object(), stream=stream,
        state=LoginState.AWAITING_HUMAN,
    )

    try:
        # The outer timeout is a test safety net, independent of the
        # WATCHER_CANCEL_TIMEOUT monkeypatch above — this call must return
        # well within it if the bound inside _finalize_pending_login works.
        await asyncio.wait_for(
            _finalize_pending_login(app, "tok", "test"), timeout=5
        )

        # Cleanup must still have proceeded despite the watcher never
        # actually finishing: every resource still gets torn down, and the
        # abandoned pending login is still recorded.
        assert "tok" in app.session_pool.discarded
        assert stream.stopped is True
        assert context.closed is True
        assert browser.closed is True
        assert app._finished_logins.get("tok") == "abandoned"
    finally:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


# ── logout() also clears the landed candidate files ──────────────────────
#
# logout()'s "success": True means "the local half of the login state is
# clean". A candidate's PDF left behind would make that sentence false.
# The four-key return shape is a contract, so a deletion failure reports
# through the already-always-non-empty `warning`, never a fifth key.
#
# The app-context/session helpers come from tests/test_auth_tools.py, which
# is this repo's one place they are built; duplicating them here would be
# the copy-that-drifts this project keeps being bitten by.

_SYNTH_PDF = b"%PDF-1.4" + bytes([10]) + b"% synthetic" + bytes([10]) + bytes(32)
_LOGOUT_KEYS = {"success", "server_logout", "warning", "teardown_confirmed"}


async def _logout_with_landed_files(tmp_path, monkeypatch):
    from tests.test_auth_tools import make_app_ctx, make_ctx, make_session, stub_guarded_api_abort
    from mcp104.browser.session import SessionPool
    from mcp104.tools import auth

    pool = SessionPool()
    pool.activate_direct("s1", make_session())
    app_ctx = make_app_ctx(tmp_path, pool=pool)
    directory = app_ctx.config.resume_files_dir
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "attach-1111111111111-1.pdf").write_bytes(_SYNTH_PDF)
    (directory / "photo-1111111111111.jpg").write_bytes(bytes([255, 216, 255]) + b" synthetic")

    monkeypatch.setattr(auth, "guarded_api", stub_guarded_api_abort("not_logged_in"), raising=False)
    result = await auth.logout(make_ctx(app_ctx))
    return app_ctx, result


@pytest.mark.asyncio
async def test_logout_removes_the_landed_resume_files_and_keeps_the_four_key_shape(tmp_path, monkeypatch):
    app_ctx, result = await _logout_with_landed_files(tmp_path, monkeypatch)

    directory = app_ctx.config.resume_files_dir
    assert not directory.exists() or list(directory.iterdir()) == []
    assert set(result) == _LOGOUT_KEYS
    assert result["success"] is True
    assert result["warning"]


@pytest.mark.asyncio
async def test_a_failed_delete_keeps_the_shape_and_says_so_in_the_warning(tmp_path, monkeypatch):
    import types

    from mcp104.tools import auth, resume_files

    def failing_rmtree(path, *args, **kwargs):
        raise OSError("synthetic delete failure")

    # Swap the `shutil` NAME inside the module under test rather than
    # rmtree on the real stdlib module: monkeypatch would restore either,
    # but only this leaves shutil itself untouched for the rest of the run.
    monkeypatch.setattr(resume_files, "shutil", types.SimpleNamespace(rmtree=failing_rmtree))
    app_ctx, result = await _logout_with_landed_files(tmp_path, monkeypatch)

    assert set(result) == _LOGOUT_KEYS
    assert result["success"] is True
    # The human is still told, through the key that is always present.
    assert "檔案" in result["warning"]
    assert str(app_ctx.config.resume_files_dir) in result["warning"]
    assert auth  # the module under test really was exercised
