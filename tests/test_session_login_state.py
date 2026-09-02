from __future__ import annotations

import asyncio


from mcp104.browser.session import (
    PendingLogin,
    SessionInfo,
    SessionPool,
    clear_cookies,
    load_cookies,
    matches_auth_host,
    save_cookies,
)


# =========================================================================
# T-11 (R2.1, R2.3): login state is written where config points, and the
# file is enough for a separate process to recover from.
# =========================================================================

def test_saved_login_state_is_recoverable_by_a_fresh_read_of_the_same_path(tmp_path):
    # Simulates a second process: nothing but the path is shared, no
    # object identity, no in-memory state carried over.
    path = tmp_path / "some" / "nested" / "cookies.json"
    cookies = [
        {"name": "its", "value": "abc123", "domain": "vip.104.com.tw"},
        {"name": "ithp", "value": "def456", "domain": "vip.104.com.tw"},
    ]

    save_cookies(path, cookies)
    recovered = load_cookies(path)

    assert recovered == cookies


# =========================================================================
# T-70 (session.SessionInfo, interface): credentials are held by the
# session itself, no browser object required to construct.
# =========================================================================

def test_session_info_constructs_and_holds_cookies_without_any_browser_object():
    cookies = [{"name": "its", "value": "xyz", "domain": "vip.104.com.tw"}]

    info = SessionInfo(
        cookies=cookies,
        account_label="test-account",
        last_active=None,
        lock=asyncio.Lock(),
        throttle=None,
        has_succeeded_api_call=False,
    )

    assert info.cookies == cookies
    assert info.account_label == "test-account"


# =========================================================================
# T-71 (session.save_cookies, interface): writes to caller-given path,
# creating missing parent directories.
# =========================================================================

def test_save_cookies_writes_to_the_given_path(tmp_path):
    path = tmp_path / "cookies.json"
    cookies = [{"name": "its", "value": "1"}]

    save_cookies(path, cookies)

    assert path.exists()
    assert load_cookies(path) == cookies


def test_save_cookies_creates_missing_parent_directories(tmp_path):
    path = tmp_path / "does" / "not" / "exist" / "yet" / "cookies.json"
    assert not path.parent.exists()

    save_cookies(path, [{"name": "its", "value": "1"}])

    assert path.exists()


# =========================================================================
# T-72 (session.load_cookies, interface): missing or corrupt file returns
# None, never raises.
# =========================================================================

def test_load_cookies_returns_none_when_file_does_not_exist(tmp_path):
    path = tmp_path / "does-not-exist.json"
    assert load_cookies(path) is None


def test_load_cookies_returns_none_when_file_content_is_corrupt(tmp_path):
    path = tmp_path / "cookies.json"
    path.write_text("{ this is not valid json ]]]")

    assert load_cookies(path) is None


# =========================================================================
# T-73 (session.clear_cookies, interface): safe to call when the file
# does not exist.
# =========================================================================

def test_clear_cookies_is_safe_when_file_does_not_exist(tmp_path):
    path = tmp_path / "does-not-exist.json"
    clear_cookies(path)  # must not raise


def test_clear_cookies_removes_an_existing_file(tmp_path):
    path = tmp_path / "cookies.json"
    save_cookies(path, [{"name": "its", "value": "1"}])
    assert path.exists()

    clear_cookies(path)

    assert not path.exists()


# =========================================================================
# T-74 (session.SessionPool, interface): registration, deregistration and
# identity lookups don't need any browser resource.
# =========================================================================

def test_session_pool_pending_registration_and_lookup_need_no_browser(tmp_path):
    pool = SessionPool()

    pool.add_pending("token-1", PendingLogin(mcp_session_id="s1"))

    assert pool.get_pending("token-1") is not None
    assert pool.is_logged_in("s1") is False
    assert pool.get_session("s1") is None


def test_session_pool_deregistration_needs_no_browser(tmp_path):
    pool = SessionPool()
    pool.add_pending("token-1", PendingLogin(mcp_session_id="s1"))

    pool.discard_pending("token-1")

    assert pool.get_pending("token-1") is None


def test_session_pool_identity_query_needs_no_browser_for_an_unknown_session():
    pool = SessionPool()
    assert pool.is_logged_in("never-seen") is False
    assert pool.get_session("never-seen") is None


# =========================================================================
# T-89 (session.matches_auth_host, interface): dotted-suffix matching,
# and the specific negative case auth.vip.104.com.tw.
# =========================================================================

def test_matches_auth_host_matches_exact_auth_host():
    assert matches_auth_host("bsignin.104.com.tw") is True


def test_matches_auth_host_matches_a_dotted_subdomain_of_an_auth_host():
    # Dotted-suffix: a subdomain of a known auth host is itself an auth
    # host redirect target.
    assert matches_auth_host("foo.bsignin.104.com.tw") is True


def test_matches_auth_host_rejects_a_non_dotted_substring_match():
    # A hostname that merely CONTAINS an auth host's name as a substring,
    # without a dotted-suffix relationship, must not match — otherwise
    # "notbsignin.104.com.tw" would be misclassified as an auth redirect.
    assert matches_auth_host("notbsignin.104.com.tw") is False


def test_matches_auth_host_rejects_auth_dot_vip_specifically():
    # The named trap: auth.vip.104.com.tw looks auth-related by name, but
    # it is a subdomain of vip.104.com.tw (the application host), not a
    # dotted suffix of any real auth host (bsignin/boidc.104.com.tw). It
    # must return False.
    assert matches_auth_host("auth.vip.104.com.tw") is False


def test_matches_auth_host_rejects_the_plain_application_host():
    assert matches_auth_host("vip.104.com.tw") is False


# =========================================================================
# T-96 (session.SessionPool.remove, interface): synchronous, and removal
# makes the session unresolvable afterward.
# =========================================================================

def test_session_pool_remove_is_a_synchronous_function():
    pool = SessionPool()
    info = SessionInfo(
        cookies=[],
        account_label="test-account",
        last_active=None,
        lock=asyncio.Lock(),
        throttle=None,
        has_succeeded_api_call=False,
    )
    pool.activate_direct("s1", info)

    result = pool.remove("s1")

    assert not asyncio.iscoroutine(result)


def test_session_pool_remove_makes_the_session_unresolvable():
    pool = SessionPool()
    info = SessionInfo(
        cookies=[],
        account_label="test-account",
        last_active=None,
        lock=asyncio.Lock(),
        throttle=None,
        has_succeeded_api_call=False,
    )
    pool.activate_direct("s1", info)
    assert pool.is_logged_in("s1") is True

    pool.remove("s1")

    assert pool.is_logged_in("s1") is False
    assert pool.get_session("s1") is None
