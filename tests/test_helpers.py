from __future__ import annotations

import asyncio
import logging

import pytest

from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.browser.api_client import ENDPOINTS, RawResponse
from mcp104.tools.helpers import (
    ERROR_API_REQUEST_FAILED,
    ERROR_CHALLENGE,
    GuardAbort,
    SessionUnavailable,
    ToolAbort,
    get_session_id,
    guarded_api,
    guarded_page,
)


class FakeResponse:
    def __init__(self, status: int = 200):
        self.status = status


class FakePage:
    """Stub Page: goto() records call count and jumps straight to the
    configured final URL (simulating a settled navigation)."""

    def __init__(self, final_url: str, status: int = 200, body_text: str = ""):
        self.url = "about:blank"
        self._final_url = final_url
        self._status = status
        self.goto_calls = 0
        self._body_text = body_text

    async def goto(self, url, **kwargs):
        self.goto_calls += 1
        self.url = self._final_url
        return FakeResponse(self._status)

    async def inner_text(self, selector):
        return self._body_text


class FakeBrowserContext:
    def __init__(self, page: FakePage):
        self.pages = [page]
        self.request_handlers = []

    def on(self, event, handler):
        # guarded_page's throttle integration attaches a "request" listener
        # via attach_request_counter — recorded, never fired: nothing in
        # this file simulates Playwright's actual request traffic (that's
        # tests/test_throttle.py's job).
        self.request_handlers.append((event, handler))


class _FakeApiBrowserContext:
    """Stand-in for Playwright's BrowserContext on the API path (guarded_api,
    not guarded_page) — same shape tests/test_api_client.py's identical class
    uses: an async `cookies()` and a harmless `.on()`/`.pages` placeholder."""
    def __init__(self, cookies=None):
        self._cookies = cookies if cookies is not None else []
        self.pages = []

    async def cookies(self):
        return self._cookies

    def on(self, event, handler):
        pass


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
def fast_settle(monkeypatch):
    """guarded_page's settle window defaults to 2s in production; shrink it
    for tests. Still a real asyncio.sleep(0), so it still yields — it does
    not become a no-op, which matters for the serialization test below."""
    monkeypatch.setattr("mcp104.tools.helpers.SETTLE_SECONDS", 0)


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


@pytest.mark.asyncio
async def test_guarded_page_single_navigation():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    info = SessionInfo(browser_context=FakeBrowserContext(page))
    pool.activate_direct(sid, info)

    async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult") as (p, yielded_info):
        assert p is page
        assert yielded_info is info

    assert page.goto_calls == 1
    assert page.url == "https://vip.104.com.tw/search/searchResult"


@pytest.mark.asyncio
async def test_guarded_page_expired_raises_distinct_from_blocked():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://bsignin.104.com.tw/login")
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
            pass
    assert "已過期" in exc_info.value.payload["error"]


@pytest.mark.asyncio
async def test_guarded_page_blocked_403_is_not_reported_as_expired():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://vip.104.com.tw/search/searchResult", status=403)
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
            pass
    assert "已過期" not in exc_info.value.payload["error"]


@pytest.mark.asyncio
async def test_guarded_page_cloudflare_challenge_zh_marker_is_blocked_not_expired():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(
        final_url="https://vip.104.com.tw/search/searchResult",
        body_text="vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。",
    )
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
            pass  # pragma: no cover — must never be reached
    assert exc_info.value.payload == ERROR_CHALLENGE
    assert "已過期" not in exc_info.value.payload["error"]


@pytest.mark.asyncio
async def test_guarded_page_cloudflare_ray_id_marker_is_blocked_and_logs_ray_id(caplog):
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(
        final_url="https://vip.104.com.tw/search/searchResult",
        body_text="Performance and Security by Cloudflare\nRay ID: abc123",
    )
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    with caplog.at_level(logging.WARNING, logger="104-mcp.helpers"):
        with pytest.raises(SessionUnavailable) as exc_info:
            async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
                pass  # pragma: no cover — must never be reached
    assert exc_info.value.payload == ERROR_CHALLENGE
    assert "abc123" in caplog.text


@pytest.mark.asyncio
async def test_guarded_page_cloudflare_footer_mention_alone_is_not_blocked():
    # Negative counterpart to the two tests above: a bare mention of
    # "Cloudflare" (e.g. footer branding on an otherwise normal page with
    # real result cards) must NOT trip the detector — without this guard
    # the false-positive would disable every tool outright.
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(
        final_url="https://vip.104.com.tw/search/searchResult",
        body_text="<result cards omitted> © 2026 104 Corp. Powered in part by Cloudflare.",
    )
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult") as (p, info):
        assert p is page


@pytest.mark.asyncio
async def test_guarded_page_genuinely_empty_result_page_still_yields_normally():
    # Proves the challenge check's new ordering does not swallow a real
    # empty-result page (anchor present, zero rows) — it must let this
    # through to the tool body's own anchor-based empty-result handling.
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(
        final_url="https://vip.104.com.tw/message/msgList",
        body_text="th-candidate-name th-event-progress th-job-name th-time",
    )
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    async with guarded_page(ctx, "https://vip.104.com.tw/message/msgList") as (p, info):
        assert p is page


@pytest.mark.asyncio
async def test_guarded_page_serializes_concurrent_calls():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    events: list[str] = []

    async def fake_tool(label: str):
        async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
            events.append(f"{label}-start")
            await asyncio.sleep(0)  # real yield point inside the locked region
            events.append(f"{label}-end")

    await asyncio.gather(fake_tool("a"), fake_tool("b"))

    # Without the per-session lock, both "start"s could land before either
    # "end" — that must never happen.
    assert events in (
        ["a-start", "a-end", "b-start", "b-end"],
        ["b-start", "b-end", "a-start", "a-end"],
    )


@pytest.mark.asyncio
async def test_guarded_page_does_not_serialize_across_different_sessions():
    # Negative counterpart to test_guarded_page_serializes_concurrent_calls:
    # without this, a regression to one global lock (instead of one lock
    # per SessionInfo) would still pass every other test in this file.
    pool = SessionPool()

    ctx1 = FakeCtx(pool)
    sid1 = get_session_id(ctx1)
    page1 = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    pool.activate_direct(sid1, SessionInfo(browser_context=FakeBrowserContext(page1)))

    ctx2 = FakeCtx(pool)
    sid2 = get_session_id(ctx2)
    page2 = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    pool.activate_direct(sid2, SessionInfo(browser_context=FakeBrowserContext(page2)))

    events: list[str] = []
    release_a = asyncio.Event()

    async def tool_a():
        async with guarded_page(ctx1, "https://vip.104.com.tw/search/searchResult"):
            events.append("a-start")
            await release_a.wait()  # only released once tool_b has run to completion
            events.append("a-end")

    async def tool_b():
        await asyncio.sleep(0)  # let tool_a start and block first
        async with guarded_page(ctx2, "https://vip.104.com.tw/search/searchResult"):
            events.append("b-start")
            events.append("b-end")
        release_a.set()

    await asyncio.wait_for(asyncio.gather(tool_a(), tool_b()), timeout=5)

    # If the two sessions shared one global lock, tool_b would block behind
    # tool_a's still-held lock and this would deadlock (caught by the
    # timeout above) instead of interleaving like this.
    assert events.index("b-start") < events.index("a-end")


@pytest.mark.asyncio
async def test_guarded_page_before_goto_aborts_without_navigating():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://vip.104.com.tw/message/msgMaster/1/2")
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    reject = {"success": False, "error": "已達每日發送上限"}

    async def before_goto(info):
        raise ToolAbort(reject, kind="daily_cap")

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_page(
            ctx, "https://vip.104.com.tw/message/msgMaster/1/2", before_goto=before_goto
        ):
            pass  # pragma: no cover — must never be reached

    assert exc_info.value.payload == reject
    assert page.goto_calls == 0  # rejected before the guard's own navigation
    # ToolAbort must still be catchable via the shared GuardAbort base,
    # since every tool call site catches GuardAbort, not a specific
    # subclass.
    assert isinstance(exc_info.value, GuardAbort)
    # "daily_cap" is the sharpest instance of a hook raising OUTSIDE the
    # guard's own raise sites: omitted, a cap rejection would return
    # "unconfirmed, do not resend" and still write a sent_log row (see
    # tools/messaging.py's send_message and its NOT_SENT set).
    assert exc_info.value.kind == "daily_cap"


@pytest.mark.asyncio
async def test_guarded_page_before_goto_runs_inside_the_lock():
    # The "aborts before goto" test above only proves before_goto runs
    # BEFORE navigation — it would pass identically if before_goto ran
    # entirely outside info.lock. Prove the atomicity claim that justified
    # the hook: two concurrent calls' before_goto (and body) must not
    # interleave.
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://vip.104.com.tw/message/msgMaster/1/2")
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page)))

    events: list[str] = []

    def make_hook(label: str):
        async def hook(info):
            events.append(f"{label}-before_goto-start")
            await asyncio.sleep(0)  # real yield point — proves atomicity, not luck
            events.append(f"{label}-before_goto-end")
        return hook

    async def call(label: str):
        async with guarded_page(
            ctx, "https://vip.104.com.tw/message/msgMaster/1/2", before_goto=make_hook(label)
        ):
            events.append(f"{label}-body")

    await asyncio.gather(call("a"), call("b"))

    assert events in (
        ["a-before_goto-start", "a-before_goto-end", "a-body",
         "b-before_goto-start", "b-before_goto-end", "b-body"],
        ["b-before_goto-start", "b-before_goto-end", "b-body",
         "a-before_goto-start", "a-before_goto-end", "a-body"],
    )


@pytest.mark.asyncio
async def test_guarded_page_rejects_when_pool_entry_removed_while_queued():
    # A presence-only re-check (is_logged_in) would pass against ANY live
    # entry, not necessarily the one whose lock this call is holding. Here
    # the entry is removed outright (e.g. logout()) while a second caller
    # is queued on the first's lock.
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    info = SessionInfo(browser_context=FakeBrowserContext(page))
    pool.activate_direct(sid, info)

    release_holder = asyncio.Event()
    result = {}

    async def holder():
        async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
            await release_holder.wait()

    async def queued():
        try:
            async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
                result["proceeded"] = True
        except SessionUnavailable as e:
            result["error"] = e.payload

    holder_task = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)  # let holder acquire info.lock

    queued_task = asyncio.ensure_future(queued())
    await asyncio.sleep(0.01)  # let `queued` resolve `info` and park on its lock

    # Simulate logout() completing while `queued` is still parked on the
    # (now orphaned) lock — remove() deliberately does not acquire it.
    pool._sessions.pop(sid, None)

    release_holder.set()
    await asyncio.wait_for(asyncio.gather(holder_task, queued_task), timeout=5)

    assert "proceeded" not in result
    assert result.get("error") == {"error": "請先呼叫 login()"}


@pytest.mark.asyncio
async def test_guarded_page_rejects_when_pool_entry_replaced_while_queued():
    # The central race this guards against: logout()+login() completes with
    # a BRAND NEW SessionInfo (new lock, new BrowserContext) registered
    # under the same session_id while a caller is queued on the OLD
    # SessionInfo's lock. is_logged_in(session_id) alone would read True
    # against the new entry and let the queued caller proceed holding a
    # lock nobody else will ever contend for — two calls could then drive
    # the same page concurrently while each believes itself serialized.
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)

    page_old = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    info_old = SessionInfo(browser_context=FakeBrowserContext(page_old))
    pool.activate_direct(sid, info_old)

    release_holder = asyncio.Event()
    result = {}

    async def holder():
        async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult"):
            await release_holder.wait()

    async def queued():
        try:
            async with guarded_page(ctx, "https://vip.104.com.tw/search/searchResult") as (page, info):
                result["proceeded_with_page"] = page
        except SessionUnavailable as e:
            result["error"] = e.payload

    holder_task = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)  # let holder resolve info_old and acquire its lock

    queued_task = asyncio.ensure_future(queued())
    await asyncio.sleep(0.01)  # let `queued` resolve info_old too, then park on its lock

    # Replace the pool entry with a brand-new SessionInfo under the same
    # session_id — simulating logout()+login() completing while `queued`
    # is still parked on info_old.lock.
    page_new = FakePage(final_url="https://vip.104.com.tw/search/searchResult")
    pool.activate_direct(sid, SessionInfo(browser_context=FakeBrowserContext(page_new)))

    release_holder.set()
    await asyncio.wait_for(asyncio.gather(holder_task, queued_task), timeout=5)

    assert "proceeded_with_page" not in result
    assert result.get("error") == {"error": "請先呼叫 login()"}


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
    info = SessionInfo(browser_context=_FakeApiBrowserContext())
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
