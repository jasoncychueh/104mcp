from pathlib import Path

import pytest

from mcp104.config import ConfigError, get_config, resolve_data_dir


def _configure_env(monkeypatch, data_dir, label="tester@104.com"):
    """Set the two knobs get_config() needs to succeed: a required identity
    value and a data directory. Individual tests override either on top of
    this baseline."""
    monkeypatch.setenv("MCP104_ACCOUNT", label)
    monkeypatch.setenv("MCP104_DATA_DIR", str(data_dir))


# ── Regression: config fields still readable from (unchanged-name) env vars ──
# DB_PATH / AUTH_BASE_URL are gone (§C2 — replaced by MCP104_DATA_DIR and the
# MCP104_AUTH_BASE_URL/MCP104_AUTH_BIND_PORT pair), so the old assertions on
# those two are removed rather than carried forward; the rest of the
# no-prefix knobs are unchanged and still worth a round-trip check.

def test_default_config(monkeypatch, tmp_path):
    _configure_env(monkeypatch, tmp_path)
    cfg = get_config()
    assert cfg.max_daily_messages == 50
    assert cfg.login_timeout_seconds == 900
    assert cfg.max_requests_per_hour == 300
    assert cfg.max_inline_wait_seconds == 20
    assert cfg.activity_streak_limit_minutes == 20
    assert cfg.rest_duration_minutes == 3
    assert cfg.min_call_interval_seconds == 5


def test_config_from_env(monkeypatch, tmp_path):
    _configure_env(monkeypatch, tmp_path)
    monkeypatch.setenv("MAX_DAILY_MESSAGES", "100")
    monkeypatch.setenv("LOGIN_TIMEOUT_SECONDS", "1200")
    monkeypatch.setenv("MAX_REQUESTS_PER_HOUR", "900")
    monkeypatch.setenv("MAX_INLINE_WAIT_SECONDS", "15")
    monkeypatch.setenv("ACTIVITY_STREAK_LIMIT_MINUTES", "10")
    monkeypatch.setenv("REST_DURATION_MINUTES", "5")
    monkeypatch.setenv("MIN_CALL_INTERVAL_SECONDS", "8")
    cfg = get_config()
    assert cfg.max_daily_messages == 100
    assert cfg.login_timeout_seconds == 1200
    assert cfg.max_requests_per_hour == 900
    assert cfg.max_inline_wait_seconds == 15
    assert cfg.activity_streak_limit_minutes == 10
    assert cfg.rest_duration_minutes == 5
    assert cfg.min_call_interval_seconds == 8


# ── I2-I: a non-numeric value for any of the numeric knobs is a startup
# failure naming the offending variable and value, same pattern as T-104's
# MCP104_ACCOUNT coverage ────────────────────────────────────────────

@pytest.mark.parametrize("var_name", [
    "MAX_DAILY_MESSAGES",
    "LOGIN_TIMEOUT_SECONDS",
    "MAX_REQUESTS_PER_HOUR",
    "MAX_INLINE_WAIT_SECONDS",
    "ACTIVITY_STREAK_LIMIT_MINUTES",
    "REST_DURATION_MINUTES",
    "MIN_CALL_INTERVAL_SECONDS",
    "MCP104_AUTH_BIND_PORT",
])
def test_non_numeric_env_value_is_startup_failure_naming_var_and_value(monkeypatch, tmp_path, var_name):
    _configure_env(monkeypatch, tmp_path)
    if var_name == "MCP104_AUTH_BIND_PORT":
        # MCP104_AUTH_BIND_PORT is only parsed when paired with
        # MCP104_AUTH_BASE_URL (§ Server §stdio 模式) -- without the pair set,
        # config.py never reaches this variable's parsing at all.
        monkeypatch.setenv("MCP104_AUTH_BASE_URL", "http://localhost:9000")
    monkeypatch.setenv(var_name, "abc")

    with pytest.raises(ConfigError) as exc_info:
        get_config()

    message = str(exc_info.value)
    assert var_name in message, f"message must name {var_name}: {message!r}"
    assert "'abc'" in message, f"message must quote the bad value: {message!r}"


# ── T-29 (R5.2): no data path comes from a hardcoded absolute path ─────────

def test_t029_data_paths_derive_from_config_not_hardcoded(monkeypatch, tmp_path):
    loc1 = tmp_path / "loc1"
    loc2 = tmp_path / "loc2"

    _configure_env(monkeypatch, loc1)
    cfg1 = get_config()

    _configure_env(monkeypatch, loc2)
    cfg2 = get_config()

    # Every data path tracks data_dir — changing data_dir changes all of them.
    assert cfg1.data_dir == loc1
    assert cfg2.data_dir == loc2
    assert cfg1.db_path != cfg2.db_path
    assert cfg1.cookies_path != cfg2.cookies_path
    assert cfg1.throttle_state_path != cfg2.throttle_state_path
    assert cfg1.logout_unconfirmed_path != cfg2.logout_unconfirmed_path

    # None of them is the old literal-absolute-path default (browser/session.py's
    # COOKIES_FILE = Path("/data/cookies.json"), and the retired DB_PATH default
    # /data/104.db) — a data path that survives regardless of data_dir would be
    # exactly the bug this case exists to catch.
    assert str(cfg1.cookies_path) != "/data/cookies.json"
    assert str(cfg1.db_path) != "/data/104.db"
    assert str(cfg2.cookies_path) != "/data/cookies.json"
    assert str(cfg2.db_path) != "/data/104.db"


# ── T-51 (interface: config.get_config): db + login-state paths live under data_dir ──

def test_t051_db_and_login_state_paths_under_data_dir(monkeypatch, tmp_path):
    data_dir = tmp_path / "data"
    _configure_env(monkeypatch, data_dir)
    cfg = get_config()

    assert Path(cfg.db_path).is_relative_to(cfg.data_dir)
    assert cfg.cookies_path.is_relative_to(cfg.data_dir)


# ── T-52 (interface: config.resolve_data_dir): env override vs. per-user default ──

def test_t052_resolve_data_dir_uses_env_when_set(monkeypatch, tmp_path):
    target = tmp_path / "explicit_location"
    monkeypatch.setenv("MCP104_DATA_DIR", str(target))

    result = resolve_data_dir()

    assert result == target
    # Pure function: it must not require (or create) the directory.
    assert not target.exists()


def test_t052_resolve_data_dir_falls_back_to_per_user_location(monkeypatch):
    monkeypatch.delenv("MCP104_DATA_DIR", raising=False)

    result = resolve_data_dir()  # must not raise just because nothing was set

    assert isinstance(result, Path)


# ── 2026-09-04: no account label is configured any more ─────────────────────
#
# MCP104_ACCOUNT was dropped: the 104 login e-mail is learned from 104 itself
# after login (tools/helpers.py ensure_account_identity) and cached in
# Config.identity_path next to cookies.json.

def test_get_config_needs_no_account_label(monkeypatch, tmp_path):
    from mcp104.config import get_config

    monkeypatch.delenv("MCP104_ACCOUNT", raising=False)
    monkeypatch.setenv("MCP104_DATA_DIR", str(tmp_path))

    cfg = get_config()  # must not raise

    assert not hasattr(cfg, "account_label")
    assert cfg.identity_path == tmp_path / "account.json"
    assert cfg.cookies_path == tmp_path / "cookies.json"


def test_a_stale_account_label_in_the_environment_is_ignored(monkeypatch, tmp_path):
    from mcp104.config import get_config

    monkeypatch.setenv("MCP104_ACCOUNT", "whatever")
    monkeypatch.setenv("MCP104_DATA_DIR", str(tmp_path))

    assert get_config().data_dir == tmp_path
