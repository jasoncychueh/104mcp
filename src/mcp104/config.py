import os
import sys
from dataclasses import dataclass
from pathlib import Path


class ConfigError(RuntimeError):
    """Raised when environment configuration cannot be parsed into a usable
    Config at startup. The caller (main.py's startup sequence) is expected to
    print this to stderr and exit non-zero rather than let the process come
    up half-configured."""


@dataclass(frozen=True)
class Config:
    data_dir: Path                      # per-user data directory; the four paths below
                                         # are all derived from it
    db_path: str
    cookies_path: Path                  # login state persistence (replaces the old
                                         # hard-coded /data/cookies.json)
    # Identifies which 104 employer account this run's records belong to.
    # candidates/sent_log are keyed on it. Required, no default — see
    # get_config()'s validation below for why a machine-derived fallback
    # (hostname, OS login) can't stand in for it.
    account_label: str
    login_timeout_seconds: int  # How long _watch_for_login waits for a human to finish login
    max_daily_messages: int
    # Request-level throttling (browser/throttle.py) — separate from and in
    # addition to max_daily_messages above, which only ever governed
    # send_message counts, not page-request volume. See docs/104-site-facts.md
    # and browser/throttle.py's module docstring: these are conservative
    # guesses anchored on one short recording, not derived safe thresholds.
    max_requests_per_hour: int
    max_inline_wait_seconds: int
    activity_streak_limit_minutes: int
    rest_duration_minutes: int
    # Minimum spacing between calls on one session, enforced as an inline
    # sleep floor rather than a drawn delay — see browser/throttle.py's
    # module docstring for why a floor and not a manufactured distribution.
    min_call_interval_seconds: int
    throttle_state_path: Path           # append-only request-timestamp log (browser/throttle.py)
    # Marks "the previous logout() could not confirm the server-side 104
    # session was actually invalidated". Existence is its whole content; the
    # recovery path on the next run reads it. Local teardown confirmation is
    # a separate, in-process concern carried by logout()'s own return value —
    # it doesn't need to survive a process boundary.
    logout_unconfirmed_path: Path
    auth_bind_port: int | None          # set: use it. None: an ephemeral port is taken.
    auth_base_url: str | None           # set: use it. None: a localhost URL is built from
                                         # whichever port actually got bound.
    # auth_bind_port and auth_base_url are a pair: either both are None (the
    # human and this process are on the same machine) or both are set (the
    # human is on another machine). Validating that pairing, and deriving the
    # actual bind/announce behavior from it, belongs to web/auth_server.py's
    # resolve_auth_binding — this module only carries the two raw values
    # through from the environment.


def _parse_int_env(name: str, default: int) -> int:
    """Read an integer environment variable, falling back to `default` when
    it is unset or blank. A value that IS set but does not parse as an
    integer is a startup configuration error, not a silent fallback to
    `default` — a typo'd env var should fail loudly at startup, not have
    this process quietly run with a value the operator never intended."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"{name} 的值 {raw!r} 不是合法整數，請設定為一個整數（或不設定以使用"
            f"預設值 {default}）。"
        ) from None


def _parse_optional_int_env(name: str) -> int | None:
    """Same contract as `_parse_int_env`, for a variable whose unset/blank
    state is meaningful on its own (`None`) rather than falling back to a
    default value."""
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return None
    try:
        return int(raw)
    except ValueError:
        raise ConfigError(
            f"{name} 的值 {raw!r} 不是合法整數，請設定為一個整數（或不設定）。"
        ) from None


def resolve_data_dir() -> Path:
    """Pure: reads the environment only, never creates the directory. Callers
    on the startup path are responsible for creating it and treating failure
    to do so as a startup failure.

    MCP104_DATA_DIR, when set, is used as-is. Otherwise this derives a
    per-user application data location using each platform's own convention,
    the same choice a dependency like platformdirs would make, without
    taking on that dependency."""
    env_dir = os.getenv("MCP104_DATA_DIR")
    if env_dir is not None and env_dir.strip():
        return Path(env_dir)

    if sys.platform == "win32":
        base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA") or str(Path.home())
        return Path(base) / "mcp104"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "mcp104"
    xdg_data_home = os.getenv("XDG_DATA_HOME")
    if xdg_data_home:
        return Path(xdg_data_home) / "mcp104"
    return Path.home() / ".local" / "share" / "mcp104"


def get_config() -> Config:
    data_dir = resolve_data_dir()

    # The only required setting in this object. A missing, empty, or
    # whitespace-only value is a configuration error, not a case for a
    # silent default — see the message below for why: any value this
    # process could derive on its own (hostname, OS account) identifies the
    # machine, not the 104 account, and would silently either merge two
    # different 104 accounts' records together or split one account's
    # records across machines.
    account_label = os.getenv("MCP104_ACCOUNT_LABEL")
    if not account_label or not account_label.strip():
        raise ConfigError(
            "MCP104_ACCOUNT_LABEL is not set. Set it to a value that "
            "identifies which 104 employer account this run is signed into "
            "(for example, that account's own login email) — "
            "candidate status and the daily send count are recorded under "
            "this value, so the same 104 account used from a different "
            "machine must be given the same MCP104_ACCOUNT_LABEL, and "
            "switching to a different 104 account must be given a "
            "different one."
        )

    auth_bind_port = _parse_optional_int_env("MCP104_AUTH_BIND_PORT")

    auth_base_url_raw = os.getenv("MCP104_AUTH_BASE_URL")
    auth_base_url = (
        auth_base_url_raw
        if auth_base_url_raw is not None and auth_base_url_raw.strip()
        else None
    )

    return Config(
        data_dir=data_dir,
        db_path=str(data_dir / "104.db"),
        cookies_path=data_dir / "cookies.json",
        account_label=account_label,
        # The real 104 flow is OAuth2 + PKCE + an MFA step that fires on
        # every container login, plus (per docs/104-site-facts.md) two
        # human-click branches — product selection and a repeatLogin
        # "already signed in elsewhere" dialog. A live login measured at
        # 265s; the old hard-coded 300s left almost no margin and two
        # earlier attempts timed out before the user could finish.
        login_timeout_seconds=_parse_int_env("LOGIN_TIMEOUT_SECONDS", 900),
        max_daily_messages=_parse_int_env("MAX_DAILY_MESSAGES", 50),
        # Lowered from 1800: sized for a client that issues 1 HTTP request per
        # tool call, so the old value (sized for ~44 DOM requests per page
        # load) would never bind. After the JSON-API messaging migration this
        # is true of all eight read/write tools, not just five — the shared
        # ThrottleState no longer has a second traffic shape riding on it.
        # See browser/throttle.py's module docstring for why the underlying
        # numbers are still a conservative guess, not a derived safe
        # threshold, and why the migration makes that caveat apply MORE
        # broadly rather than retiring it.
        max_requests_per_hour=_parse_int_env("MAX_REQUESTS_PER_HOUR", 300),
        max_inline_wait_seconds=_parse_int_env("MAX_INLINE_WAIT_SECONDS", 20),
        activity_streak_limit_minutes=_parse_int_env("ACTIVITY_STREAK_LIMIT_MINUTES", 20),
        rest_duration_minutes=_parse_int_env("REST_DURATION_MINUTES", 3),
        min_call_interval_seconds=_parse_int_env("MIN_CALL_INTERVAL_SECONDS", 5),
        throttle_state_path=data_dir / "throttle_state.log",
        logout_unconfirmed_path=data_dir / "logout_unconfirmed",
        auth_bind_port=auth_bind_port,
        auth_base_url=auth_base_url,
    )
