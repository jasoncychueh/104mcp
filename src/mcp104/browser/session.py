from __future__ import annotations

import asyncio
import json
import logging
import random
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urlparse

from mcp104.browser.throttle import ThrottleState

if TYPE_CHECKING:
    from patchright.async_api import BrowserContext, Response

log = logging.getLogger("104-mcp.session")


@dataclass
class PendingLogin:
    display: str
    mcp_session_id: str


DEFAULT_ACCOUNT = "default"


@dataclass
class SessionInfo:
    browser_context: BrowserContext
    account_email: str = DEFAULT_ACCOUNT
    last_active: datetime = field(default_factory=datetime.now)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Request-count/pacing bookkeeping (browser/throttle.py), one instance
    # per session so budgets and pacing never leak across accounts/tabs.
    throttle: ThrottleState = field(default_factory=ThrottleState)
    # Has an API call on THIS session ever reached a successful Verdict?
    # tools/helpers.py's guarded_api reads this to pick between two HTTP
    # 403 wordings: the first call of a session takes the plain "blocked"
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
        mcp_session_id. Used to clean up a stale VNC stack before starting
        a new one, rather than stacking a second Xvfb + x11vnc + headed
        Chromium on top of an abandoned one."""
        return [t for t, sid in self._token_to_session.items() if sid == mcp_session_id]

    def activate(self, token: str, info: SessionInfo) -> bool:
        """Register `info` under the mcp_session_id that requested `token`.

        Returns False (without registering anything) if the pending entry
        was already consumed — e.g. by a concurrent caller — so the caller
        knows the BrowserContext it built was never handed to the pool and
        must close it itself rather than leak it.
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

    async def remove(self, mcp_session_id: str):
        # Deliberately does not acquire info.lock: cleanup_all() at shutdown
        # must not block behind a stuck tool call, and a caller that already
        # holds the lock (there are none today) would deadlock on itself.
        info = self._sessions.pop(mcp_session_id, None)
        if info:
            await info.browser_context.close()

    async def cleanup_stale(self, max_idle_minutes: int = 30):
        now = datetime.now()
        stale = [
            sid for sid, info in self._sessions.items()
            if (now - info.last_active).total_seconds() > max_idle_minutes * 60
            and not info.lock.locked()
        ]
        for sid in stale:
            await self.remove(sid)

    async def cleanup_all(self):
        for sid in list(self._sessions):
            await self.remove(sid)


async def random_delay(min_sec: float = 3.0, max_sec: float = 8.0):
    """Sleep a uniformly-drawn interval — meant to pace a tool's own
    DOM interactions AFTER a guarded_page navigation returns (see
    tools/helpers.py's guarded_page docstring for why it is never called
    from inside the guard itself).

    ★ Has NO caller anywhere under src/ or tests/ as of the JSON-API
    messaging migration — tools/messaging.py's five call sites (the DOM
    version of send_message) were its only users; the eight tools that now
    go through guarded_api never adopted it, matching the same "kept, not
    deleted, until guarded_page's replacement is verified live" ruling
    guarded_page and attach_request_counter (browser/throttle.py) carry.
    Same removal trigger: once §Verification's live steps pass.
    """
    await asyncio.sleep(random.uniform(min_sec, max_sec))


# ── Auth host / status predicates ─────────────────────────────────────
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


def check_session_expired(url: str, response: Response | None) -> str:
    """Judge auth status from an already-completed navigation's settled
    final URL and Response. Does NOT navigate — callers must have already
    performed the goto and let the page settle (this is a Vue SPA; a
    client-side redirect can still be in flight right after goto returns).

    The hostname check ("expired" on a redirect to an auth host) is
    measured (docs/104-site-facts.md). The 401/403 status handling is
    NOT measured — docs/104-site-facts.md explicitly lists "session 過期
    時受保護頁面的實際回應" as unobserved; the only confirmed unauthenticated
    behaviour so far is the hostname redirect. 401/403 are a reasonable
    inferred default (a login-form-at-200 on vip.104.com.tw is the failure
    mode this predicate CANNOT currently detect — see the zero-results
    fail-fast in tools/helpers.require_nonempty, which exists precisely
    because this gap isn't measured yet), not an established fact.

    Returns:
        "expired" — redirected to an auth host, or HTTP 401 (inferred, see
            above). A fresh login() will fix this.
        "blocked" — HTTP 403 (inferred), a plausible bot-detection response.
            A re-login would not help and would just generate more unpaced
            traffic.
        "ok" — no known expiry signal fired. Does NOT prove the session is
            alive — see the caveat above.
    """
    hostname = urlparse(url).hostname or ""
    if matches_auth_host(hostname):
        return "expired"
    status = response.status if response else None
    if status == 401:
        return "expired"
    if status == 403:
        return "blocked"
    return "ok"


LIVENESS_SETTLE_SECONDS = 2.0  # module-level so tests can shrink it


async def check_login_liveness(context: BrowserContext) -> str:
    """Navigation-based liveness probe for a restored/existing session.

    Unlike check_session_expired, this DOES navigate — it exists
    specifically for tools/auth.py's login() restore / already-logged-in
    paths, where there is no tool-of-the-caller's-own URL to piggyback an
    auth check on.

    Three states, not two — a boolean conflates "definitely logged out"
    with "couldn't tell", and login() cannot treat those the same: MFA
    (validation_type: unreliable_device) fires on every fresh browser
    profile (docs/104-site-facts.md), so clearing cookies on a mere
    hiccup — a 15s timeout, a transient Cloudflare interstitial, a 403
    bot-challenge — would force a full human MFA cycle to recover a session
    that may still have been perfectly healthy.

    Returns:
        "alive" — final hostname is vip.104.com.tw (and not a 403
            challenge on that host).
        "logged_out" — final hostname is a known auth host. This is the
            ONLY state safe to react to with clear_cookies() + remove().
        "indeterminate" — the navigation raised (timeout, DNS, a transient
            interstitial), or resolved to vip.104.com.tw with a 403 status
            (agrees with check_session_expired's "blocked" — a bot
            challenge is not proof of either logged-in or logged-out).
            Callers must leave the session/cookies untouched and ask the
            caller to retry.
    """
    page = context.pages[0] if context.pages else await context.new_page()
    try:
        response = await page.goto(
            "https://vip.104.com.tw/rms/index",
            wait_until="domcontentloaded",
            timeout=15000,
        )
        await asyncio.sleep(LIVENESS_SETTLE_SECONDS)  # settle window for client-side redirects
    except Exception as exc:
        log.warning("check_login_liveness: navigation failed, indeterminate: %s", exc)
        return "indeterminate"

    hostname = urlparse(page.url).hostname or ""
    if hostname == "vip.104.com.tw":
        if response is not None and response.status == 403:
            log.warning("check_login_liveness: 403 on vip.104.com.tw, indeterminate")
            return "indeterminate"
        return "alive"
    if matches_auth_host(hostname):
        return "logged_out"
    log.warning("check_login_liveness: unrecognized final host %s, indeterminate", hostname)
    return "indeterminate"


# ── Cookie persistence (single-user) ─────────────────────────────────

COOKIES_FILE = Path("/data/cookies.json")


def save_cookies(cookies: list[dict]):
    """Persist browser cookies to disk."""
    COOKIES_FILE.parent.mkdir(parents=True, exist_ok=True)
    COOKIES_FILE.write_text(json.dumps(cookies, ensure_ascii=False, indent=2))


def load_cookies() -> list[dict] | None:
    """Load persisted cookies. Returns cookies list or None."""
    if not COOKIES_FILE.exists():
        return None
    try:
        return json.loads(COOKIES_FILE.read_text())
    except (json.JSONDecodeError, ValueError):
        return None


def clear_cookies():
    """Remove persisted cookies."""
    if COOKIES_FILE.exists():
        COOKIES_FILE.unlink()
