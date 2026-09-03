from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from mcp104.browser.throttle import ThrottleState

log = logging.getLogger("104-mcp.session")


@dataclass
class PendingLogin:
    mcp_session_id: str


@dataclass
class SessionInfo:
    # Replaces a held BrowserContext: the browser only exists for the
    # duration of login() (see browser/cdp_stream.py), so once login
    # completes there is no context left to query cookies from. The
    # session must hold its own credentials.
    cookies: list[dict]
    account_label: str
    last_active: datetime = field(default_factory=datetime.now)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Request-count/pacing bookkeeping (browser/throttle.py), one instance
    # per session so budgets and pacing never leak across accounts/tabs.
    throttle: ThrottleState = field(default_factory=ThrottleState)
    # Has an API call on THIS session ever reached a successful Verdict?
    # tools/helpers.py's guarded_api/guarded_sequence read this to pick
    # between two HTTP 403 wordings: the first call of a session takes the
    # plain "blocked"
    # wording, but a 403 arriving after a prior success most likely means
    # the Cloudflare clearance cookie expired underneath a still-valid 104
    # session — an expired-clearance remedy (re-login) and a fresh bot
    # block (wait) are the same HTTP status with opposite fixes.
    has_succeeded_api_call: bool = False


class SessionPool:
    def __init__(self):
        self._sessions: dict[str, SessionInfo] = {}
        self._pending: dict[str, PendingLogin] = {}
        self._token_to_session: dict[str, str] = {}

    def add_pending(self, token: str, pending: PendingLogin):
        self._pending[token] = pending
        self._token_to_session[token] = pending.mcp_session_id

    def get_pending(self, token: str) -> PendingLogin | None:
        return self._pending.get(token)

    def discard_pending(self, token: str):
        """Remove a pending login registration without activating it (e.g.
        the watcher gave up, or it was superseded by a new login() call).
        Safe to call even if already consumed — both pops are no-ops then."""
        self._pending.pop(token, None)
        self._token_to_session.pop(token, None)

    def find_pending_tokens_for_session(self, mcp_session_id: str) -> list[str]:
        """Tokens for pending (not-yet-completed) logins registered by this
        mcp_session_id. Used to clean up a stale login stack before starting
        a new one, rather than stacking a second login browser on top of an
        abandoned one."""
        return [t for t, sid in self._token_to_session.items() if sid == mcp_session_id]

    def activate(self, token: str, info: SessionInfo) -> bool:
        """Synchronously pop `token` out of both the pending and
        token-to-session registries and register `info` as the active
        session for the mcp_session_id that requested it.

        The return value carries no obligation the caller must act on: it
        only reports whether `token` was still pending (True) or had
        already been consumed by a concurrent caller (False, and nothing
        is registered). There is no BrowserContext to clean up on the
        False branch — the caller holds no such resource.
        """
        pending = self._pending.pop(token, None)
        if not pending:
            return False
        mcp_sid = self._token_to_session.pop(token, None)
        if mcp_sid:
            self._sessions[mcp_sid] = info
            return True
        return False

    def get_session(self, mcp_session_id: str) -> SessionInfo | None:
        info = self._sessions.get(mcp_session_id)
        if info:
            info.last_active = datetime.now()
        return info

    def activate_direct(self, mcp_session_id: str, info: SessionInfo):
        """Directly activate a session (e.g. from restored cookies), bypassing pending/token flow."""
        self._sessions[mcp_session_id] = info

    def is_logged_in(self, mcp_session_id: str) -> bool:
        return mcp_session_id in self._sessions

    def remove(self, mcp_session_id: str):
        # Deliberately does not acquire info.lock: cleanup_all() at shutdown
        # must not block behind a stuck tool call, and a caller that already
        # holds the lock (there are none today) would deadlock on itself.
        self._sessions.pop(mcp_session_id, None)

    def cleanup_all(self):
        for sid in list(self._sessions):
            self.remove(sid)


# ── Auth host predicate ───────────────────────────────────────────────
#
# Hostname matching (exact, or a dotted suffix) against AUTH_HOSTS is a
# reliable login-flow signal — unlike a substring match, which would treat
# any URL merely containing "bsignin" as a redirect target.
#
# ★ Working host and auth hosts are NOT disjoint by family resemblance —
# only by this exact predicate. auth.vip.104.com.tw (the JSON API host for
# résumé detail and the job list, docs/104-site-facts.md §6b.6-pre) is a
# WORKING host despite its name starting with "auth". matches_auth_host is
# correct today ONLY because it matches on exact hostname or dotted suffix
# against AUTH_HOSTS below, never a substring: "auth.vip.104.com.tw" does
# not equal, and does not end with ".", any AUTH_HOSTS entry, so it
# correctly returns False. The plausible wrong edit is widening this to a
# substring match on "auth", or adding auth.vip.104.com.tw to AUTH_HOSTS
# outright — either would silently classify every healthy résumé-detail
# and job-list response as an expired session, with nothing else in the
# system positioned to catch it.

AUTH_HOSTS = ("bsignin.104.com.tw", "boidc.104.com.tw", "signin.104.com.tw")


def matches_auth_host(hostname: str) -> bool:
    """True iff `hostname` belongs to the login/auth flow (bsignin / boidc
    / signin) — never the authenticated API. auth.vip.104.com.tw MUST
    return False here: it is a working host (résumé detail, job list), not
    an authentication host, despite the name. See the module comment above
    for why a substring match would turn that into a silent, wide-blast-
    radius bug rather than a loud one."""
    return any(hostname == h or hostname.endswith("." + h) for h in AUTH_HOSTS)


# ── Cookie persistence ────────────────────────────────────────────────
#
# Paths are supplied by the caller (Config.cookies_path) rather than held
# as a module-level constant — this is what lets these three functions run
# against a temp directory in tests with no monkeypatching.


def save_cookies(path: Path, cookies: list[dict]):
    """Persist browser cookies to `path`. Creates the parent directory if
    it does not already exist."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))


def load_cookies(path: Path) -> list[dict] | None:
    """Load persisted cookies from `path`. Returns None (never raises) if
    the file does not exist or its contents are not valid JSON."""
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, ValueError):
        return None


def clear_cookies(path: Path):
    """Remove the persisted cookies file at `path`. Safe to call when the
    file does not exist."""
    if path.exists():
        path.unlink()
