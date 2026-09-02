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


class _FakeApp:
    def __init__(self):
        self._watcher_tasks = {}
        self._pending_logins = {}
        self._finished_logins = {}
        self.session_pool = _FakeSessionPool()
        self.config = _FakeConfig()
        self.auth_site = None


@pytest.mark.asyncio
async def test_finalize_pending_login_does_not_hang_on_a_stuck_watcher(monkeypatch):
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

    app = _FakeApp()
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
