import pytest
from datetime import datetime, timedelta
from unittest.mock import AsyncMock
from mcp104.browser.session import (
    SessionPool, SessionInfo, PendingLogin,
    check_session_expired, check_login_liveness,
)


@pytest.fixture
def pool():
    return SessionPool()


def test_no_session_initially(pool):
    assert pool.get_session("nonexistent") is None


def test_is_logged_in_false_initially(pool):
    assert pool.is_logged_in("s1") is False


def test_pending_login(pool):
    pool.add_pending("token-1", PendingLogin(display=":99", mcp_session_id="s1"))
    assert pool.get_pending("token-1") is not None
    assert pool.is_logged_in("s1") is False


def test_discard_pending_removes_entry(pool):
    pool.add_pending("t1", PendingLogin(display=":99", mcp_session_id="s1"))
    pool.discard_pending("t1")
    assert pool.get_pending("t1") is None


def test_discard_pending_is_idempotent(pool):
    pool.discard_pending("nonexistent")  # must not raise


def test_find_pending_tokens_for_session_returns_all_matches(pool):
    pool.add_pending("t1", PendingLogin(display=":99", mcp_session_id="s1"))
    pool.add_pending("t2", PendingLogin(display=":100", mcp_session_id="s1"))
    pool.add_pending("t3", PendingLogin(display=":101", mcp_session_id="s2"))
    assert set(pool.find_pending_tokens_for_session("s1")) == {"t1", "t2"}
    assert pool.find_pending_tokens_for_session("s2") == ["t3"]


def test_find_pending_tokens_for_session_empty_when_none(pool):
    assert pool.find_pending_tokens_for_session("nobody") == []


def test_find_pending_tokens_for_session_excludes_discarded(pool):
    pool.add_pending("t1", PendingLogin(display=":99", mcp_session_id="s1"))
    pool.discard_pending("t1")
    assert pool.find_pending_tokens_for_session("s1") == []


@pytest.mark.asyncio
async def test_activate_session(pool):
    pool.add_pending("token-1", PendingLogin(display=":99", mcp_session_id="s1"))
    mock_ctx = AsyncMock()
    pool.activate("token-1", SessionInfo(
        browser_context=mock_ctx, account_email="u@104.com",
    ))
    assert pool.is_logged_in("s1")
    assert pool.get_session("s1").account_email == "u@104.com"
    assert pool.get_pending("token-1") is None


@pytest.mark.asyncio
async def test_remove_session_closes_context(pool):
    pool.add_pending("t1", PendingLogin(display=":99", mcp_session_id="s1"))
    mock_ctx = AsyncMock()
    pool.activate("t1", SessionInfo(browser_context=mock_ctx, account_email="u@104.com"))
    await pool.remove("s1")
    mock_ctx.close.assert_awaited_once()
    assert pool.get_session("s1") is None


@pytest.mark.asyncio
async def test_remove_nonexistent_is_noop(pool):
    await pool.remove("nonexistent")


@pytest.mark.asyncio
async def test_cleanup_stale(pool):
    pool.add_pending("t1", PendingLogin(display=":99", mcp_session_id="s1"))
    mock_ctx = AsyncMock()
    info = SessionInfo(browser_context=mock_ctx, account_email="u@104.com")
    info.last_active = datetime.now() - timedelta(minutes=60)
    pool.activate("t1", info)

    pool.add_pending("t2", PendingLogin(display=":100", mcp_session_id="s2"))
    fresh_ctx = AsyncMock()
    pool.activate("t2", SessionInfo(browser_context=fresh_ctx, account_email="f@104.com"))

    await pool.cleanup_stale(max_idle_minutes=30)
    assert pool.get_session("s1") is None
    assert pool.get_session("s2") is not None


@pytest.mark.asyncio
async def test_cleanup_all(pool):
    pool.add_pending("t1", PendingLogin(display=":99", mcp_session_id="s1"))
    pool.add_pending("t2", PendingLogin(display=":100", mcp_session_id="s2"))
    ctx1, ctx2 = AsyncMock(), AsyncMock()
    pool.activate("t1", SessionInfo(browser_context=ctx1, account_email="a@104.com"))
    pool.activate("t2", SessionInfo(browser_context=ctx2, account_email="b@104.com"))
    await pool.cleanup_all()
    ctx1.close.assert_awaited_once()
    ctx2.close.assert_awaited_once()


class FakeResponse:
    def __init__(self, status):
        self.status = status


@pytest.mark.parametrize("url,status,expected", [
    ("https://bsignin.104.com.tw/login", 200, "expired"),
    ("https://signin.104.com.tw/", 200, "expired"),
    ("https://boidc.104.com.tw/oauth2/auth", 200, "expired"),
    # subdomain suffix of an auth host is still an auth host
    ("https://accounts.bsignin.104.com.tw/x", 200, "expired"),
    ("https://vip.104.com.tw/search/searchResult?kws=test", 200, "ok"),
    ("https://vip.104.com.tw/search/searchResult", 401, "expired"),
    ("https://vip.104.com.tw/search/searchResult", 403, "blocked"),
])
def test_check_session_expired(url, status, expected):
    assert check_session_expired(url, FakeResponse(status)) == expected


def test_check_session_expired_no_substring_false_positive():
    # "bsignin" appears only as a substring inside a query-string value on a
    # healthy vip.104.com.tw URL — must not be treated as a redirect to the
    # auth host. Regression test for the old `d in final_url` substring
    # match, which named its list `login_domains` but matched on any
    # occurrence anywhere in the URL.
    url = "https://vip.104.com.tw/search/searchResult?return=bsignin.104.com.tw"
    assert check_session_expired(url, FakeResponse(200)) == "ok"


# ── check_login_liveness: three-state hostname rule ────────────────────
# A boolean here would conflate "definitely logged out" with "couldn't
# tell" — see the function's docstring. These pin down which of the three
# states each situation must produce, since login() reacts very differently
# to "logged_out" (safe to clear cookies) vs "indeterminate" (must not).

class _FakePage:
    def __init__(self, final_url: str, status: int = 200, raise_on_goto: bool = False):
        self.url = "about:blank"
        self._final_url = final_url
        self._status = status
        self._raise = raise_on_goto

    async def goto(self, url, **kwargs):
        if self._raise:
            raise RuntimeError("simulated navigation failure")
        self.url = self._final_url
        return FakeResponse(self._status)


class _FakeContext:
    def __init__(self, page: _FakePage):
        self.pages = [page]


@pytest.fixture(autouse=True)
def _fast_liveness_settle(monkeypatch):
    monkeypatch.setattr("mcp104.browser.session.LIVENESS_SETTLE_SECONDS", 0)


@pytest.mark.asyncio
async def test_check_login_liveness_alive_on_vip_200():
    page = _FakePage("https://vip.104.com.tw/rms/index", status=200)
    assert await check_login_liveness(_FakeContext(page)) == "alive"


@pytest.mark.asyncio
async def test_check_login_liveness_logged_out_on_bsignin():
    page = _FakePage("https://bsignin.104.com.tw/login", status=200)
    assert await check_login_liveness(_FakeContext(page)) == "logged_out"


@pytest.mark.asyncio
async def test_check_login_liveness_logged_out_on_signin_subdomain():
    page = _FakePage("https://accounts.signin.104.com.tw/x", status=200)
    assert await check_login_liveness(_FakeContext(page)) == "logged_out"


@pytest.mark.asyncio
async def test_check_login_liveness_indeterminate_on_navigation_exception():
    # A 15s timeout or a transient Cloudflare interstitial must NOT be
    # treated as "logged out" — that would destroy a healthy 24h session
    # over a hiccup and force a full human MFA cycle to recover it.
    page = _FakePage("https://vip.104.com.tw/rms/index", raise_on_goto=True)
    assert await check_login_liveness(_FakeContext(page)) == "indeterminate"


@pytest.mark.asyncio
async def test_check_login_liveness_indeterminate_on_403():
    # Agrees with check_session_expired's "blocked": a 403 on the target
    # host is a bot-challenge, not proof of either logged-in or logged-out.
    page = _FakePage("https://vip.104.com.tw/rms/index", status=403)
    assert await check_login_liveness(_FakeContext(page)) == "indeterminate"


@pytest.mark.asyncio
async def test_check_login_liveness_indeterminate_on_unrecognized_host():
    page = _FakePage("https://example.com/", status=200)
    assert await check_login_liveness(_FakeContext(page)) == "indeterminate"
