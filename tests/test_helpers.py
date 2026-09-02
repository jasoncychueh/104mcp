from __future__ import annotations

import asyncio

import pytest

from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.browser.api_client import ENDPOINTS, RawResponse
from mcp104.tools.helpers import (
    ERROR_API_REQUEST_FAILED,
    GuardAbort,
    SessionUnavailable,
    ToolAbort,
    get_session_id,
    guarded_api,
)


class FakeSessionObj:
    """Stand-in for mcp's ServerSession — a plain object with a __dict__,
    which is exactly what get_session_id relies on being able to stamp."""
    pass


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeApp:
    def __init__(self, session_pool: SessionPool):
        self.session_pool = session_pool
        self.config = get_config()  # real defaults — nothing in this file sets throttle env vars


class FakeCtx:
    def __init__(self, session_pool: SessionPool):
        self.session = FakeSessionObj()
        self.request_context = FakeRequestContext(FakeApp(session_pool))


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """Neutralizes browser/throttle.py's request-pacing floor for every
    test in this file, which isn't exercising throttle behaviour itself.

    Patches mcp104.browser.throttle._sleep — NOT
    mcp104.browser.throttle.asyncio.sleep directly. That looks module-scoped
    but isn't: throttle.py's
    `import asyncio` binds the SAME shared module object everyone else
    imports, so patching its attribute would mutate asyncio for the WHOLE
    process — including this file's own ensure_future-based tests, which
    rely on a real asyncio.sleep(0.01) to let a concurrently-scheduled
    task run at all. _sleep is a throttle.py-local indirection that
    exists specifically so patching stays scoped to calls made through it.

    Previously this fixture also patched `browser.throttle._rng` with a
    `_FixedShortRng` stand-in, to steer the old drawn-delay distribution's
    random branch selection. The pacing rework (design.md Components §7)
    replaced that distribution with a deterministic interval floor —
    `MIN_CALL_INTERVAL_SECONDS` minus elapsed time, no randomness
    involved — so `_rng` no longer exists on browser.throttle at all.
    Patching a since-removed attribute is exactly what made every test in
    this file error at setup (tasks.md "Existing tests this feature
    invalidates"); the fix is simply that there is nothing left to patch
    for randomness, only the sleep itself.
    """
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


def test_get_session_id_distinct_objects_never_collide():
    pool = SessionPool()
    ctx1 = FakeCtx(pool)
    ctx2 = FakeCtx(pool)
    assert get_session_id(ctx1) != get_session_id(ctx2)


def test_get_session_id_stable_across_repeated_calls():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    first = get_session_id(ctx)
    second = get_session_id(ctx)
    assert first == second


# ── guarded_api: body forwarding, the method/body mismatch check (§1/§2) ────────────

def _new_api_ctx_and_session():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight from SessionInfo.cookies now —
    # there is no BrowserContext to fake a `.cookies()` call against
    # post-login (§C7).
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return ctx, info


@pytest.mark.asyncio
async def test_guarded_api_forwards_body_to_fetch_unchanged(monkeypatch):
    ctx, _info = _new_api_ctx_and_session()
    calls = []

    async def fake_fetch(endpoint, *, cookie_header, params=None, body=None):
        calls.append(body)
        return RawResponse(status=200, location=None, content_type="application/json",
                            body='{"data": [], "metadata": {}}',
                            parsed_json={"data": [], "metadata": {}})

    monkeypatch.setattr("mcp104.browser.api_client.fetch", fake_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", fake_fetch, raising=False)

    sent_body = {"content": "hello"}
    async with guarded_api(ctx, ENDPOINTS["send_message"], params=[], body=sent_body) as (_payload, _info2):
        pass

    assert calls == [sent_body]


@pytest.mark.asyncio
async def test_guarded_api_post_endpoint_without_body_is_internal_config_error():
    ctx, _info = _new_api_ctx_and_session()

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["send_message"], params=[], body=None):
            pass  # pragma: no cover

    assert exc_info.value.kind == "internal_config"
    # Must NOT read as a transient network problem (Error Handling §2) — a
    # caller bug reported as "retry later" is exactly what this distinction
    # exists to prevent.
    assert exc_info.value.payload != ERROR_API_REQUEST_FAILED
    assert "內部設定錯誤" in exc_info.value.payload["error"]


@pytest.mark.asyncio
async def test_guarded_api_get_endpoint_with_body_is_internal_config_error():
    ctx, _info = _new_api_ctx_and_session()

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["list_jobs"], params=[], body={"unexpected": True}):
            pass  # pragma: no cover

    assert exc_info.value.kind == "internal_config"
    assert exc_info.value.payload != ERROR_API_REQUEST_FAILED


@pytest.mark.asyncio
async def test_guarded_api_no_session_at_all_raises_not_logged_in_kind():
    # Round I1 Bug A: this test's old name ("...raise_sites_carry_the_
    # classifiers_own_kind") claimed coverage of every guarded_api raise
    # path while asserting only this one — payload wording and `.kind` are
    # independent (the redirect-to-auth-host site proved that: its payload
    # says "已過期" regardless of which kind is attached), so a test that
    # oversells its own scope is worse than a narrow one honestly named.
    # The other guarded_api kinds are pinned where they are actually
    # produced, each against a live path, not a hand-built exception:
    #   - "expired" (auth-host redirect) —
    #     test_api_client.py::test_guarded_api_does_not_follow_redirect_and_reports_auth_host_redirect_as_expired
    #   - "throttled" —
    #     test_api_client.py::test_guarded_api_throttled_rejection_carries_throttled_kind
    #   - "internal_config" (method/body mismatch) — this file's
    #     test_guarded_api_post_endpoint_without_body_is_internal_config_error /
    #     test_guarded_api_get_endpoint_with_body_is_internal_config_error
    #   - "transport", "challenge", "blocked", and the classifier's own
    #     kinds (e.g. "validation") — test_api_client.py's family-A/B
    #     Error Handling suite and tests/test_messaging.py's send_message
    #     taxonomy tests, which drive them through the real classify().
    ctx = FakeCtx(SessionPool())  # no session activated at all
    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["list_jobs"], params=[]):
            pass  # pragma: no cover
    assert exc_info.value.kind == "not_logged_in"
