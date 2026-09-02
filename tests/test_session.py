import pytest
from mcp104.browser.session import SessionPool, SessionInfo, PendingLogin


@pytest.fixture
def pool():
    return SessionPool()


def test_no_session_initially(pool):
    assert pool.get_session("nonexistent") is None


def test_is_logged_in_false_initially(pool):
    assert pool.is_logged_in("s1") is False


def test_pending_login(pool):
    pool.add_pending("token-1", PendingLogin(mcp_session_id="s1"))
    assert pool.get_pending("token-1") is not None
    assert pool.is_logged_in("s1") is False


def test_discard_pending_removes_entry(pool):
    pool.add_pending("t1", PendingLogin(mcp_session_id="s1"))
    pool.discard_pending("t1")
    assert pool.get_pending("t1") is None


def test_discard_pending_is_idempotent(pool):
    pool.discard_pending("nonexistent")  # must not raise


def test_find_pending_tokens_for_session_returns_all_matches(pool):
    pool.add_pending("t1", PendingLogin(mcp_session_id="s1"))
    pool.add_pending("t2", PendingLogin(mcp_session_id="s1"))
    pool.add_pending("t3", PendingLogin(mcp_session_id="s2"))
    assert set(pool.find_pending_tokens_for_session("s1")) == {"t1", "t2"}
    assert pool.find_pending_tokens_for_session("s2") == ["t3"]


def test_find_pending_tokens_for_session_empty_when_none(pool):
    assert pool.find_pending_tokens_for_session("nobody") == []


def test_find_pending_tokens_for_session_excludes_discarded(pool):
    pool.add_pending("t1", PendingLogin(mcp_session_id="s1"))
    pool.discard_pending("t1")
    assert pool.find_pending_tokens_for_session("s1") == []


def test_activate_session(pool):
    pool.add_pending("token-1", PendingLogin(mcp_session_id="s1"))
    pool.activate("token-1", SessionInfo(
        cookies=[{"name": "PHPSESSID", "value": "x"}], account_label="u@104.com",
    ))
    assert pool.is_logged_in("s1")
    assert pool.get_session("s1").account_label == "u@104.com"
    assert pool.get_pending("token-1") is None


def test_remove_session_removes_entry(pool):
    # SessionPool.remove() is synchronous (§C7): there is no BrowserContext
    # left to close post-login, cookies are the session's sole credential
    # holder — so this only asserts removal itself, not a close() call.
    pool.add_pending("t1", PendingLogin(mcp_session_id="s1"))
    pool.activate("t1", SessionInfo(cookies=[], account_label="u@104.com"))
    pool.remove("s1")
    assert pool.get_session("s1") is None


def test_remove_nonexistent_is_noop(pool):
    pool.remove("nonexistent")  # must not raise


def test_cleanup_all(pool):
    pool.add_pending("t1", PendingLogin(mcp_session_id="s1"))
    pool.add_pending("t2", PendingLogin(mcp_session_id="s2"))
    pool.activate("t1", SessionInfo(cookies=[], account_label="a@104.com"))
    pool.activate("t2", SessionInfo(cookies=[], account_label="b@104.com"))
    pool.cleanup_all()
    assert pool.get_session("s1") is None
    assert pool.get_session("s2") is None
