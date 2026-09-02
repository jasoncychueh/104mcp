from __future__ import annotations

import asyncio
import logging
import re
from contextlib import asynccontextmanager
from functools import wraps
from typing import TYPE_CHECKING, AsyncIterator, Awaitable, Callable, Sequence
from urllib.parse import urlparse
from uuid import uuid4

from mcp.server.fastmcp import Context

from mcp104.browser.api_client import Endpoint, classify, fetch, hostname_for, select_cookies_for_host
from mcp104.browser.session import SessionInfo, check_session_expired, matches_auth_host
from mcp104.browser.throttle import attach_request_counter, enforce_throttle, note_request

if TYPE_CHECKING:
    from patchright.async_api import Page

log = logging.getLogger("104-mcp.helpers")

ERROR_NOT_LOGGED_IN = {"error": "請先呼叫 login()"}
ERROR_EXPIRED = {"error": "104 session 已過期，請重新呼叫 login()"}
ERROR_BLOCKED = {"error": "104 拒絕本次請求（可能觸發機器人偵測），請稍後再試"}
ERROR_NAV_FAILED = {"error": "104 頁面載入失敗（可能是逾時或網路問題），請稍後再試"}
# Distinct from ERROR_BLOCKED (403 status) and ERROR_EXPIRED (redirected to a
# login host): this is HTTP 200 on vip.104.com.tw itself, so neither of those
# checks sees it — only the body text gives it away. The wording deliberately
# tells the Agent to stop and wait, not retry or rephrase: this was
# misdiagnosed as a legitimate empty result once already (see
# docs/104-site-facts.md), and an Agent reacting to "no matches" by
# broadening its keyword generates more requests into an active challenge.
ERROR_CHALLENGE = {
    "error": (
        "104 觸發了 Cloudflare 機器人驗證，請暫停操作並稍後再試（建議至少 1 小時）。"
        "這不是查無資料，改變關鍵字重試只會加深封鎖。"
    )
}

# ── guarded_api's own error payloads ─────────────────────────────────────
#
# HTTP 403 is one status with two opposite-remedy causes (docs/104-site-facts.md
# §6b.6): a fresh bot block (wait) and an expired Cloudflare clearance cookie
# underneath an otherwise-healthy 104 session (re-login). guarded_api picks
# between these two wordings using SessionInfo.has_succeeded_api_call — the
# first call of a session cannot yet distinguish the two causes, so it gets
# the plain wording; a 403 arriving after a prior success on the same
# session is more likely the clearance-cookie case.
ERROR_BLOCKED_API_FIRST_CALL = {
    "error": "104 拒絕本次 API 請求（可能觸發機器人偵測），請稍後再試"
}
ERROR_BLOCKED_API_AFTER_SUCCESS = {
    "error": (
        "104 拒絕本次 API 請求。此 session 先前已成功過，最可能原因是 Cloudflare "
        "通關 cookie 已失效，請重新呼叫 login()；若重新登入後仍失敗，才可能是機器人偵測，"
        "請稍後再試"
    )
}
ERROR_API_REQUEST_FAILED = {"error": "104 API 請求失敗（可能是逾時或網路問題），請稍後再試"}


def _error_wrong_host(endpoint: Endpoint) -> dict:
    return {"error": f"內部設定錯誤：{endpoint.key} 指向錯誤的主機（{endpoint.host}），這是程式問題，非站台狀況，請回報"}


def _error_header_fault(endpoint: Endpoint) -> dict:
    return {"error": f"內部設定錯誤：{endpoint.key} 未送出必要標頭，這是程式問題，請回報"}


def _error_not_found(detail: str) -> dict:
    return {"error": f"104 回報找不到資料：{detail or '未提供訊息'}"}


def _error_missing_param(detail: str) -> dict:
    return {"error": f"104 回報缺少必要參數：{detail or '未提供訊息'}"}


def _error_unrecognised_status(detail: str) -> dict:
    return {"error": f"104 回傳未預期的狀態（{detail}），請停止操作並回報，不要更換條件重試"}


def _error_malformed(detail: str) -> dict:
    return {"error": f"104 回應結構異常（{detail}），可能是介面已變更，請回報"}


def _error_non_json(detail: str) -> dict:
    return {"error": f"104 回應不是預期的 JSON 格式（content-type: {detail or '未知'}），請稍後再試"}


def _error_validation(detail: str) -> dict:
    # Family B's third error envelope (docs/104-site-facts.md §6b.9-4) —
    # 104's own per-field explanation, already flattened by
    # browser.api_client._family_b_error_detail. Deliberately its own kind,
    # never folded into _error_missing_param: that wording asserts a
    # missing PAIRED parameter, which is narrower than "the body failed
    # validation" (see api_client._classify_family_b's comment on the same
    # point).
    return {"error": f"104 拒絕了這次請求的內容（{detail or '未提供訊息'}）"}


SETTLE_SECONDS = 2.0  # module-level so tests can shrink it

# Chinese strings actually observed on a live challenge page (2026-08-07,
# see docs/104-site-facts.md). Either alone is a strong signal.
_CHALLENGE_MARKERS_ZH = ("正在執行安全驗證", "安全服務抵禦惡意機器人")
# "Performance and Security by Cloudflare" also appears on that same page,
# but it's generic Cloudflare branding that could in principle appear in
# a footer on a normal page too — so it only counts as a signal paired
# with an actual Ray ID token, not on its own.
_CHALLENGE_RAY_BRAND_MARKER = "Performance and Security by Cloudflare"
_RAY_ID_PATTERN = re.compile(r"Ray ID:\s*([0-9a-zA-Z]+)")
# English-locale interstitial text. INFERRED, not measured — only the
# Chinese strings above and the Ray ID pairing were actually observed live.
# Kept here because 104 could plausibly serve the English variant depending
# on Accept-Language, but do not describe this list as confirmed anywhere.
_CHALLENGE_MARKERS_EN_INFERRED = ("Just a moment", "Checking your browser", "Attention Required")


def _detect_cloudflare_challenge(body_text: str) -> tuple[bool, str | None]:
    """Look for a Cloudflare bot-challenge interstitial in a page's body
    text. Returns (is_challenge, ray_id) — ray_id is the extracted "Ray
    ID: ..." token when present (useful for the log line regardless of
    which marker fired), or None when absent.

    Requires at least one strong signal, deliberately excluding a bare
    mention of "Cloudflare" (e.g. footer branding on an otherwise normal
    page) from counting on its own — that would turn every real page
    naming its own CDN into a false positive and disable the tools
    outright.
    """
    if not body_text:
        return False, None

    ray_match = _RAY_ID_PATTERN.search(body_text)
    ray_id = ray_match.group(1) if ray_match else None

    is_challenge = (
        any(marker in body_text for marker in _CHALLENGE_MARKERS_ZH)
        or (_CHALLENGE_RAY_BRAND_MARKER in body_text and ray_id is not None)
        or any(marker in body_text for marker in _CHALLENGE_MARKERS_EN_INFERRED)
    )
    return is_challenge, ray_id


def get_session_id(ctx: Context) -> str:
    """Extract a stable per-connection identifier from the MCP context.

    `id(ctx.session)` fails open: after a client disconnects and its
    ServerSession is garbage-collected while the pool entry is still alive
    (nothing removes it on disconnect), a new object can later be allocated
    at the same address and would then pass require_login onto another
    login's still-live BrowserContext. Instead stamp a uuid onto the
    session object itself and reuse it on every call.

    Verified against the installed `mcp` package: ServerSession has no
    __slots__, no custom __setattr__/__getattr__, is not a pydantic model,
    and has a normal __dict__ — so this attribute assignment sticks and is
    stable for the object's lifetime.
    """
    session = ctx.session
    sid = getattr(session, "_mcp104_sid", None)
    if sid is None:
        sid = uuid4().hex
        session._mcp104_sid = sid
    return sid


def require_login(func):
    """Decorator: returns error if MCP session has not called login() yet."""
    @wraps(func)
    async def wrapper(*args, **kwargs):
        ctx = kwargs.get("ctx") or next(
            (a for a in args if isinstance(a, Context)), None
        )
        if ctx is None:
            return {"error": "Internal error: no context"}
        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)
        if not app.session_pool.is_logged_in(session_id):
            return ERROR_NOT_LOGGED_IN
        return await func(*args, **kwargs)
    return wrapper


class GuardAbort(Exception):
    """Base for "abort the tool call and return this payload" signals
    raised from within guarded_page's or guarded_api's locked region —
    either by the guard itself or by a before_goto/before_request hook.
    Carries the dict the tool should return as-is. Every tool call site
    catches this (not a specific subclass) identically:

        try:
            async with guarded_api(ctx, ENDPOINTS[...], params=...) as (payload, info):
                ... tool body ...
        except GuardAbort as e:
            return e.payload

    `kind` is a REQUIRED constructor argument, with no default — the same
    rule this project applies to browser.api_client.Endpoint's fields, and
    for the same reason with more at stake here: send_message's taxonomy
    (tools/messaging.py's NOT_SENT / ambiguous split) reads `kind` to
    decide whether a daily-cap slot is spent and a sent_log row is
    written. A raise site that forgot to declare its condition must fail
    at construction — a loud TypeError the moment this module is
    imported — never inherit whichever bucket a default would fall into.
    "unknown" may only ever mean a condition the SERVER produced that this
    module's vocabulary does not cover yet; it must never mean a code path
    that forgot to name its own.
    """

    def __init__(self, payload: dict, kind: str):
        self.payload = payload
        self.kind = kind
        super().__init__(payload.get("error"))


class SessionUnavailable(GuardAbort):
    """The session itself cannot be used right now (not logged in,
    expired, blocked, navigation failed). Raised only by guarded_page
    itself. Kept distinct from the base GuardAbort so that a future
    handler reacting specifically to session trouble (e.g. attempting
    cleanup) has something meaningful to catch — a before_goto rejection
    like a daily-cap hit is NOT a session problem and must not raise this."""


class ToolAbort(GuardAbort):
    """A before_goto hook (or other pre-navigation check) wants to abort
    the tool call for a reason that has nothing to do with session health
    — e.g. send_message's daily-cap check. Using SessionUnavailable for
    this would be a lie: the class docstring above promises "the session
    cannot be used", which is false for a cap rejection, and a future
    session-recovery handler keyed on SessionUnavailable would misfire on
    it."""


class MalformedResponseError(Exception):
    """Raised by a route's own row/page extractor when an expected
    container or page key is absent from an otherwise-successful envelope,
    or when the envelope itself is not a mapping at all. Never silently
    degrades to an empty result — a missing container is a genuine
    departure from the measured envelope, not a zero-row success.

    Moved here from tools/search.py (which imports it back) because
    tools/messaging.py needs the identical class for the identical
    purpose (a response whose `metadata` is absent — both messaging
    routes are recorded carrying `metadata` with all four keys, so its
    absence is a departure worth raising on, not a shape nobody has
    seen). A second class with an identical meaning — alongside the
    identical Chinese string already duplicated between
    search._malformed_response_payload and helpers._error_malformed —
    is the duplication this repo has been bitten by twice; the
    alternative (messaging importing a leading-underscore helper out of
    search.py) would instead couple two tool modules that otherwise
    share nothing.
    """

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def resolve_session(ctx: Context) -> SessionInfo | None:
    """Resolve the caller's SessionInfo without navigating.

    An implementation detail of guarded_page, not a general-purpose
    call-site helper: guarded_page calls it once to get the `info` it then
    validates by identity and locks. Tool code should not call this
    directly to get its own separate `info` — every tool that needs
    SessionInfo now gets it as guarded_page's yielded `(page, info)`, which
    is guaranteed to be the SAME object whose identity was checked and
    whose lock is held (see guarded_page's docstring for why a
    separately-resolved `info` can go stale while queued on the lock).
    """
    app = ctx.request_context.lifespan_context
    session_id = get_session_id(ctx)
    return app.session_pool.get_session(session_id)


@asynccontextmanager
async def guarded_page(
    ctx: Context,
    url: str,
    before_goto: Callable[[SessionInfo], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[Page, SessionInfo]]:
    """Resolve the session, hold its lock for the whole region, navigate to
    `url` (the ONLY navigation this performs), settle, check the
    non-navigating auth predicate, and yield (page, info).

    ★ Has NO production call site as of the JSON-API messaging migration
    (tools/messaging.py's three tools were the last ones using this; they
    now go through guarded_api instead, exactly like the five résumé/job
    tools already did). `login()` drives the browser directly and never
    used this function either. It is kept — not deleted — for one cycle:
    removing an unexercised guard before its replacement has been verified
    against the live site would invert the safe order and fold two
    independent risks into one commit. `guarded_page(` appearing nowhere
    under `src/mcp104/` except this definition is swept by
    tests/test_messaging.py, so a reintroduced call site cannot silently
    reappear unnoticed. Once that live verification passes, this function
    and `attach_request_counter` are removal candidates, at which point
    `steering/structure.md`'s boundary rule naming `guarded_page` also
    needs rewording — both are a follow-up requiring user confirmation,
    not part of this change.

    The call-site convention it defined, when it had callers, is unchanged
    and is still what `guarded_api` below follows:

        try:
            async with guarded_page(ctx, URL) as (page, info):
                ... tool body ...
        except GuardAbort as e:
            return e.payload

    `info` is yielded (not just `page`) because it is the SAME SessionInfo
    object whose identity was validated and whose lock is held — any DB
    write keyed on info.account_email inside the guarded region (or inside
    before_goto) must read it from here, not from a separately-resolved
    `info` captured before entering this function. A caller that resolves
    its own `info` before queuing on the lock and then uses THAT object's
    account_email after waking up can be reading a stale value: see the
    identity check below for why the object itself can change while queued.

    random_delay() (browser/session.py) is deliberately NOT called in
    here — it was meant to belong after the guard returns, pacing a tool's
    own interactions, and must not be what supplies the settle window
    below. It now has no caller anywhere either, for the same reason this
    function has none: messaging.py's five call sites were its only users
    (the five API read tools never adopted it).

    before_goto: optional async callable, run inside the session lock
    before the navigation (and after the request-throttle gate — a
    throttled call never reaches before_goto at all), receiving the
    validated `info`. It may raise
    GuardAbort (or a subclass) itself to abort without navigating — use
    ToolAbort for a rejection unrelated to session health (e.g. a daily-cap
    hit), not SessionUnavailable. This exists for send_message's daily-cap
    check: the count read and the cap decision must happen atomically with
    respect to any concurrent send_message call on the same session, but
    `info.lock` is not reentrant, so the check can't be wrapped in its own
    `async with info.lock` around a call to this function — it has to run
    inside THIS lock acquisition instead.

    A before_goto hook MUST NOT acquire info.lock (or any SessionInfo's
    lock) and MUST NOT call guarded_page itself — the lock is already held
    non-reentrantly, there is no acquisition timeout (bounded waiting was
    explicitly waived), and cleanup_stale skips locked sessions, so doing
    either self-deadlocks permanently with nothing to reclaim it.
    """
    app = ctx.request_context.lifespan_context
    info = await resolve_session(ctx)
    if not info:
        raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

    session_id = get_session_id(ctx)
    async with info.lock:
        # Re-check presence AND identity, not just presence. logout()+
        # login() can remove this session and register a brand-new
        # SessionInfo (new lock, new BrowserContext) under the same
        # session_id while this call was queued waiting for `info.lock` —
        # a presence-only check (is_logged_in) would pass against that NEW
        # entry even though we hold the OLD entry's lock, silently
        # defeating the whole point of per-session locking (two calls
        # could then drive the same page concurrently, believing
        # themselves serialized).
        if app.session_pool.get_session(session_id) is not info:
            raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

        # Request-level throttling (browser/throttle.py) — applied here,
        # not inside individual tools, so every tool call is covered by
        # the same session-wide budget/pacing/rest gate. A throttle
        # rejection is a ToolAbort, not SessionUnavailable: like the
        # daily-cap check below, it has nothing to do with whether the
        # session itself is healthy — it's a self-imposed pace limit.
        attach_request_counter(info.browser_context, info.throttle)
        throttle_result = await enforce_throttle(
            info.throttle,
            max_requests_per_hour=app.config.max_requests_per_hour,
            max_inline_wait_seconds=app.config.max_inline_wait_seconds,
            activity_streak_limit_minutes=app.config.activity_streak_limit_minutes,
            rest_duration_minutes=app.config.rest_duration_minutes,
            min_call_interval_seconds=app.config.min_call_interval_seconds,
        )
        if throttle_result is not None:
            raise ToolAbort(throttle_result, kind="throttled")

        if before_goto is not None:
            await before_goto(info)

        # Resolve the page from the validated `info` directly rather than
        # looking session_id up in the pool again — that would reopen the
        # exact identity gap the check above just closed.
        pages = info.browser_context.pages
        page = pages[0] if pages else await info.browser_context.new_page()
        try:
            response = await page.goto(url, wait_until="domcontentloaded", timeout=15000)
        except Exception as exc:
            log.error("guarded_page: navigation to %s failed: %s", url, exc)
            # nav_failed is a kind nothing else in this design uses — page
            # navigation is the one failure the API path (guarded_api)
            # cannot have, so it never appears in send_message's NOT_SENT
            # enumeration and no send path can produce it.
            raise SessionUnavailable(ERROR_NAV_FAILED, kind="nav_failed")

        # Settle window: this is a Vue SPA and can still redirect
        # client-side right after domcontentloaded fires, so sampling
        # page.url immediately would race the redirect.
        await asyncio.sleep(SETTLE_SECONDS)

        status = check_session_expired(page.url, response)
        if status == "expired":
            log.warning("guarded_page: session %s expired at %s", session_id, page.url)
            raise SessionUnavailable(ERROR_EXPIRED, kind="expired")
        if status == "blocked":
            log.warning("guarded_page: request blocked (403) for session %s at %s", session_id, page.url)
            raise SessionUnavailable(ERROR_BLOCKED, kind="blocked")

        # Must run before any tool body's own zero-rows/anchor check: a
        # challenge page is HTTP 200 on the correct host with zero of
        # whatever selector the tool is about to look for, so it would
        # otherwise fall straight into "legitimate empty result" — telling
        # the Agent to keep searching is exactly the wrong reaction here.
        body_text = await page.inner_text("body")
        is_challenge, ray_id = _detect_cloudflare_challenge(body_text)
        if is_challenge:
            log.warning(
                "guarded_page: Cloudflare challenge detected for session %s at %s (Ray ID: %s)",
                session_id, page.url, ray_id or "unknown",
            )
            raise SessionUnavailable(ERROR_CHALLENGE, kind="challenge")

        yield page, info


# Kind (from browser.api_client.Verdict) -> (payload builder, abort class).
# "expired"/"blocked" mean the SESSION itself is the problem (SessionUnavailable,
# same as guarded_page's own vocabulary); everything else is a per-call
# problem the session may still be perfectly healthy for (ToolAbort) — a
# configuration bug (wrong_host/header_fault) or a shape/status this call's
# response didn't satisfy. "blocked" is special-cased separately in
# guarded_api itself because its payload depends on session history, not
# just the kind.
def _api_error_for_kind(kind: str, endpoint: Endpoint, detail: str) -> tuple[dict, type[GuardAbort]]:
    if kind == "expired":
        return ERROR_EXPIRED, SessionUnavailable
    if kind == "wrong_host":
        return _error_wrong_host(endpoint), ToolAbort
    if kind == "header_fault":
        return _error_header_fault(endpoint), ToolAbort
    if kind == "not_found":
        return _error_not_found(detail), ToolAbort
    if kind == "missing_param":
        return _error_missing_param(detail), ToolAbort
    if kind == "unrecognised_status":
        return _error_unrecognised_status(detail), ToolAbort
    if kind == "malformed":
        return _error_malformed(detail), ToolAbort
    if kind == "non_json":
        return _error_non_json(detail), ToolAbort
    if kind == "validation":
        return _error_validation(detail), ToolAbort
    # Unreachable in practice — classify() only emits the kinds above — but
    # a genuinely unknown kind must still fail loud, never silently proceed.
    return {"error": f"未知的內部錯誤（{kind or 'empty'}）"}, ToolAbort


def _error_internal_config(detail: str) -> dict:
    # Same "this is a code bug, not a site condition" wording family as
    # _error_wrong_host/_error_header_fault — raised ahead of fetch()'s own
    # try/except (see guarded_api below), never inside it, so it reaches
    # the Agent as what it is instead of being converted into
    # ERROR_API_REQUEST_FAILED ("可能是逾時或網路問題，請稍後再試") by the
    # broad `except Exception` that wraps the real transport call.
    return {"error": f"內部設定錯誤：{detail}，這是程式問題，請回報"}


@asynccontextmanager
async def guarded_api(
    ctx: Context,
    endpoint: Endpoint,
    *,
    params: Sequence[tuple[str, str]] | None = None,
    body: dict | None = None,
    before_request: Callable[[SessionInfo], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[object, SessionInfo]]:
    """The API-path equivalent of guarded_page: resolve the session, hold
    its lock for the whole region, issue exactly ONE HTTP request (no
    navigation, no settle window — an HTTP response has no client-side
    after-state the way a Vue SPA page does), and yield (payload, info).

    All eight read/write tools (tools/search.py, tools/messaging.py) use
    this identically:

        try:
            async with guarded_api(ctx, ENDPOINTS["search_resumes"], params=params) as (payload, info):
                ... tool body ...
        except GuardAbort as e:
            return e.payload

    What is reused verbatim from guarded_page, and why: the session lock
    and the identity re-check (logout()+login() can swap in a brand-new
    SessionInfo under the same session id while this call is queued —
    presence alone is not identity, see guarded_page's docstring); the
    GuardAbort/SessionUnavailable/ToolAbort hierarchy and the
    `except GuardAbort as e: return e.payload` call-site convention; and
    the Cloudflare body screen, run BEFORE any shape inspection, in the
    same relative position guarded_page runs it.

    What does NOT transfer: check_session_expired is not called here — its
    URL half cannot fire (redirects are not followed) and its HTTP-status
    half maps 403 to a single "blocked" wording that would pre-empt the
    clearance-vs-block distinction below. browser/api_client.classify()'s
    own Error Handling rows cover both statuses instead.

    before_request keeps before_goto's contract exactly: runs inside the
    lock, after the throttle gate, and may raise ToolAbort to abort
    without issuing a request.

    `body` is forwarded to fetch() unchanged, for a POST endpoint's request
    body. The method/body mismatch check below (a body handed to a GET
    endpoint, or a POST endpoint called with body=None) is deliberately a
    CALL-TIME check here, not a construction-time one on Endpoint: the body
    does not exist until a caller supplies it, so no __post_init__ could
    ever see it. It is also deliberately placed ahead of the `try` that
    wraps fetch() below, not inside fetch() itself: fetch() runs inside
    guarded_api's own broad `except Exception`, which converts anything
    raised there into ERROR_API_REQUEST_FAILED ("可能是逾時或網路問題，
    請稍後再試") — reporting a caller's own bug as a transient network blip
    to an Agent whose reasonable next move is to retry.

    Cookies are read from the live BrowserContext on every call, inside the
    held lock — not snapshotted into a longer-lived jar. Not for freshness
    (after login the browser never navigates again, so nothing rotates
    them) but because a snapshot would be a second copy of state that
    already has one owner (the BrowserContext), and this project has
    twice been bitten by exactly that pattern — one fact living in two
    places with only one kept up to date.
    """
    app = ctx.request_context.lifespan_context
    info = await resolve_session(ctx)
    if not info:
        raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

    session_id = get_session_id(ctx)
    async with info.lock:
        # Re-check presence AND identity — see guarded_page's identical
        # check above for why presence alone (is_logged_in) is not enough.
        if app.session_pool.get_session(session_id) is not info:
            raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

        throttle_result = await enforce_throttle(
            info.throttle,
            max_requests_per_hour=app.config.max_requests_per_hour,
            max_inline_wait_seconds=app.config.max_inline_wait_seconds,
            activity_streak_limit_minutes=app.config.activity_streak_limit_minutes,
            rest_duration_minutes=app.config.rest_duration_minutes,
            min_call_interval_seconds=app.config.min_call_interval_seconds,
        )
        if throttle_result is not None:
            raise ToolAbort(throttle_result, kind="throttled")

        if before_request is not None:
            await before_request(info)

        if endpoint.method == "POST" and body is None:
            raise ToolAbort(
                _error_internal_config(f"{endpoint.key} 是 POST 端點但未帶 body"),
                kind="internal_config",
            )
        if endpoint.method != "POST" and body is not None:
            raise ToolAbort(
                _error_internal_config(f"{endpoint.key} 不是 POST 端點卻帶了 body"),
                kind="internal_config",
            )

        cookies = await info.browser_context.cookies()
        cookie_header = select_cookies_for_host(cookies, hostname_for(endpoint))

        # note_request runs in `finally`, not after a bare `await fetch(...)`
        # line: a timeout or connection error raises out of the `try` below
        # and skips anything placed after it, which would leave the
        # rolling-window volume count reading zero on exactly the calls
        # most worth counting — 104 refusing or timing out under load is
        # the condition the volume cap exists to react to, not to miss.
        # attach_request_counter cannot see this traffic at all (it is a
        # browser-navigation listener), so this is the only place any
        # aiohttp request — successful or not — is ever counted.
        try:
            raw = await fetch(endpoint, cookie_header=cookie_header, params=params, body=body)
        except Exception as exc:
            log.error("guarded_api: request to %s failed: %s", endpoint.key, exc)
            # A timeout/connection error says nothing about whether the
            # session itself is usable — the next call may succeed outright
            # — so this is ToolAbort, not SessionUnavailable: the design
            # treats transport failure as transient, and SessionUnavailable's
            # contract (a future session-recovery handler may react to it,
            # e.g. by clearing cookies) must not misfire on a network blip
            # that has nothing to do with session health.
            raise ToolAbort(ERROR_API_REQUEST_FAILED, kind="transport")
        finally:
            note_request(info.throttle)

        # Must run before any shape inspection, same ordering rule as
        # guarded_page: a challenge page has no measured shape resembling
        # either family's success OR any of classify()'s named failures,
        # so it must be screened out first, not fall through into one of
        # those and be reported as something else.
        is_challenge, ray_id = _detect_cloudflare_challenge(raw.body)
        if is_challenge:
            log.warning(
                "guarded_api: Cloudflare challenge detected for session %s calling %s (Ray ID: %s)",
                session_id, endpoint.key, ray_id or "unknown",
            )
            raise SessionUnavailable(ERROR_CHALLENGE, kind="challenge")

        # A redirect (not followed — see fetch()'s docstring) is handled
        # here, ahead of classify(), only for the auth-host check, which
        # needs matches_auth_host — a dependency classify() deliberately
        # does not carry (it depends on nothing beyond the endpoint
        # declaration). classify() separately checks the same redirect's
        # Location for the company-switch marker string, so a redirect
        # that is neither an auth host NOR that marker still falls through
        # to classify()'s empty-body non_json branch and is reported
        # loudly rather than silently.
        if raw.location is not None:
            hostname = urlparse(raw.location).hostname or ""
            if matches_auth_host(hostname):
                log.warning("guarded_api: session %s redirected to auth host at %s", session_id, endpoint.key)
                # An expiry signal, not a transport failure — must declare
                # "expired", not fall in with the transport kind above.
                raise SessionUnavailable(ERROR_EXPIRED, kind="expired")

        verdict = classify(endpoint, raw)
        if not verdict.ok:
            if verdict.kind == "blocked":
                payload = (
                    ERROR_BLOCKED_API_AFTER_SUCCESS if info.has_succeeded_api_call
                    else ERROR_BLOCKED_API_FIRST_CALL
                )
                log.warning("guarded_api: request blocked (403) for session %s calling %s", session_id, endpoint.key)
                raise SessionUnavailable(payload, kind="blocked")
            payload, abort_cls = _api_error_for_kind(verdict.kind, endpoint, verdict.detail)
            log.warning(
                "guarded_api: %s failed kind=%s detail=%s", endpoint.key, verdict.kind, verdict.detail,
            )
            # The classifier's own kind, passed through verbatim — never
            # re-mapped here, so a kind classify() has never been taught
            # about still reaches send_message's ambiguous fallthrough
            # rather than silently landing on whatever this line happened
            # to default to.
            raise abort_cls(payload, kind=verdict.kind)

        info.has_succeeded_api_call = True
        yield verdict.payload, info
