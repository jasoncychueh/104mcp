from __future__ import annotations

import logging
import re
from contextlib import asynccontextmanager
from functools import wraps
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, Sequence
from urllib.parse import urlparse
from uuid import uuid4

from mcp.server.fastmcp import Context

from mcp104.browser.api_client import (
    AssetRoute,
    Endpoint,
    classify,
    classify_asset,
    fetch,
    fetch_asset,
    hostname_for,
    select_cookies_for_host,
    validate_asset_url,
    ENDPOINTS,
)
from mcp104.browser.session import SessionInfo, load_cookies, load_identity, matches_auth_host, save_identity
from mcp104.browser.throttle import enforce_throttle, note_request
from mcp104.tools.discovery import _snake_case

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
# A third wording in the same 403 family, for restore verification only
# (tools/auth.py's verify_restored_session) — it names both remedies at
# once and says plainly it cannot tell which applies: a first API call on
# a freshly-restored session has no prior success on THIS run to compare
# against, so the first-call/after-success split above cannot be drawn
# here either. No guard code below reads this constant; it is placed here
# because this module is that wording family's one home, and it is
# assembled into RestoreVerdict.payload by verify_restored_session itself.
ERROR_BLOCKED_API_RESTORE_VERIFY = {
    "error": (
        "恢復驗證被 104 拒絕（HTTP 403），可能是機器人偵測，也可能是 Cloudflare 通關 "
        "cookie 已失效——目前無法分辨是哪一種。若稍後仍然失敗，才需要重新呼叫 login()；"
        "也可能只是暫時被擋，請稍後再試"
    )
}


# ── The asset host's own Agent-facing payloads ───────────────────────────
#
# The asset host answers HTTP 200 whether it served a file or refused to
# (docs/104-site-facts.md §8.23), so every one of these describes a 200.
ERROR_ASSET_NOT_AUTHENTICATED = {
    "error": (
        "104 的資產主機回傳了轉址頁而不是檔案，代表這次請求沒有被當成已登入。"
        "請重新呼叫 login() 之後再試。這不代表這位候選人沒有這個檔案，也不是「查無資料」"
        "——重試同一個呼叫、或改抓另一個候選人都不會改變結果。"
    )
}
ERROR_ASSET_TOO_LARGE = {
    "error": (
        "這個檔案超過本工具的 32 MB 上限。檔案已經傳輸了 32 MB 才被中止，這次呼叫已經"
        "用掉一個節流名額，前面那次履歷詳情請求也已經送出；沒有寫入任何檔案。"
        "重試會重複這些成本，而且結果會一樣。"
    )
}
ERROR_ASSET_EMPTY_BODY = {"error": "104 回了一個空的回應（HTTP 200、零位元組），沒有寫入任何檔案。"}


def _error_asset_unknown_format(detail: str) -> dict:
    """`detail` is classify_asset's own "<前 8 位元組十六進位>|<Content-Type>".

    The signature is in the message on purpose: this refusal is expected to
    be hit by an ordinary user (a zip/OOXML attachment is very likely to
    exist on 104 even though its bytes have never been measured), so the
    message has to be actionable — those 8 bytes are exactly the
    measurement that would let the whitelist grow. They are a format
    signature, not content.
    """
    signature, _, content_type = detail.partition("|")
    return {
        "error": (
            f"這個檔案的型別不在已知清單內（簽名 {signature or '未知'}，"
            f"104 宣稱的 Content-Type {content_type or '未提供'}），沒有寫入任何檔案，請回報。"
        )
    }


def _error_asset_url_wrong_host(route: AssetRoute, hostname: str | None) -> dict:
    """A `link` that does not live on the measured asset host.

    Deliberately NOT the generic "this is a program bug, please report it"
    wording the other two URL checks use: scheme and path-prefix failures
    really can only come from this project assembling something wrong, but
    a link on another host would most likely mean 104 changed its own data
    — and sending the operator off to audit our code for that is telling
    them the wrong thing. Names the hostname only: no path, no query
    string, because the query string is where the credential-bearing token
    lives.
    """
    return {
        "error": (
            f"104 給的資產網址（{route.key}）不在已量測的資產主機上"
            f"（{hostname or '無法解析主機名'}）。"
            "本工具只抓 104 自己在已量測資產主機上交出來的檔案，不會去抓其他主機上的網址。"
        )
    }


def _error_wrong_host(endpoint: Endpoint | AssetRoute) -> dict:
    return {"error": f"內部設定錯誤：{endpoint.key} 指向錯誤的主機（{endpoint.host}），這是程式問題，非站台狀況，請回報"}


def _error_header_fault(endpoint: Endpoint | AssetRoute) -> dict:
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


# ── Shared response shaping: key conversion + browse_limit ───────────────
#
# These three live here, not in tools/search.py, because more than one tool
# module needs them and this project has already ruled once on the shape
# that choice takes: MalformedResponseError's docstring below records that
# "tools/messaging.py imports a leading-underscore helper out of
# tools/search.py" was REJECTED in favour of moving the shared thing into
# this module and having each tool module import it back. Same precedent
# applied here — and it removes an existing duplicate on the way, since
# search.py and messaging.py each carried their own copy of the key
# conversion.


def convert_keys(value, *, string_transform=None):
    """Recursively convert every dict key at every depth via `_snake_case`;
    list elements are walked one by one; every non-string value (int, bool,
    None, ...) passes through completely unchanged.

    `string_transform`, when given, is applied to every string VALUE (never
    to a key). It exists because the two callers differ on exactly this one
    point and on nothing else: the résumé tools pass
    tools/search.py's `html_to_text` (candidates write several fields in
    104's rich-text editor), the messaging tools pass nothing. The reason it
    is a PARAMETER is that behavioural difference alone — the two callers
    need different answers for the same input, so the choice belongs to the
    caller and cannot be settled here. It is not a dependency-direction
    device: this module already imports `_snake_case` from tools/discovery.py
    two lines below, so a claim that helpers.py never imports a tool module
    would be false as written.

    `_snake_case` itself is imported from tools/discovery.py, never
    redefined: describe_result_fields() has to key its payload on the SAME
    delivered names this produces, and one shared function is the only way
    that cannot drift.
    """
    if isinstance(value, dict):
        return {_snake_case(k): convert_keys(v, string_transform=string_transform) for k, v in value.items()}
    if isinstance(value, list):
        return [convert_keys(v, string_transform=string_transform) for v in value]
    if string_transform is not None and isinstance(value, str):
        return string_transform(value)
    return value


# Warn at 90% of resumeMax (270 against the measured 300) — a specified figure, not an
# [INF] guess. Kept as a ratio rather than a hardcoded 270 so it tracks whatever
# resumeMax a given response actually carries, should it ever differ from the measured
# 300. This is a heads-up only, never a boundary this module enforces: 104's own
# enforcement at resumeMax, if any, has never been observed, and refusing on an
# unobserved boundary would be this tool's guess overriding the site's — see
# browse_limit_warning (reaching the maximum never refuses a call here).
_BROWSE_LIMIT_WARNING_RATIO = 0.9


def extract_browse_limit(container: dict) -> dict | None:
    """container.browseLimit -> {resume_max, on_that_day_count}, mechanically converted,
    or None when browseLimit is absent/not-a-dict — never a fabricated
    {resume_max: None, ...} shell, so a caller can tell "104 didn't report a quota this
    time" from "quota is unset" (104's own response carries no browseLimit key at all in
    that case, rather than one with null sub-fields). Shared by every tool whose route
    carries it: browseLimit sits at the same relative position (a sibling of the row
    container / of `resume`) on every one of them.

    The `convert_keys` walk here runs with NO `string_transform`, so quota
    values arrive raw — deliberately, and the same way for every caller.
    tools/search.py's own copy used to pass `html_to_text`, and for every
    measured value the two are indistinguishable (both quota fields are
    tagless, and the numeric-string coercion below then turns them into
    ints), which is why the untouched tests could not tell the difference.
    Raw is nonetheless the right answer rather than an accident: a quota is
    a number 104 reports, never a rich-text field a candidate typed, so
    running an HTML-to-text transform over it would be an unexplained
    no-op that invites the next reader to believe markup is expected here.
    Anything that IS rich text goes through the transform at the row/detail
    conversion, which is where the caller-specific choice belongs.
    """
    browse_limit = container.get("browseLimit")
    if not isinstance(browse_limit, dict):
        return None
    converted = convert_keys(browse_limit)
    # 104 自己的型別不一致（量到：resumeMax 是整數 300、onThatDayCount 是字串 "0"），
    # 純數字字串一律轉成 int，讓呼叫端拿到兩個同型別的數字。
    for key in ("resume_max", "on_that_day_count"):
        value = converted.get(key)
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            converted[key] = int(value.strip())
    return converted


def browse_limit_warning(browse_limit: dict | None) -> str | None:
    """A heads-up when today's browse count has reached the design-pinned 90% threshold
    of resumeMax (270/300 as measured) — see _BROWSE_LIMIT_WARNING_RATIO. Never refuses:
    104's own enforcement at the maximum has never been observed, so this tool does not
    guess a boundary the site itself has not been seen to enforce.
    """
    if not browse_limit:
        return None
    try:
        resume_max = int(browse_limit.get("resume_max"))
        on_that_day = int(browse_limit.get("on_that_day_count"))
    except (TypeError, ValueError):
        return None
    if resume_max <= 0 or on_that_day < resume_max * _BROWSE_LIMIT_WARNING_RATIO:
        return None
    return (
        f"今日已瀏覽 {on_that_day}/{resume_max} 筆履歷，已達提醒門檻（上限的 90%，"
        "依規格訂定，非猜測值）。本次呼叫不會因此被拒絕 —— 104 從未被觀察到在上限本身"
        "拒絕請求，本工具不會替 104 猜測一個未經觀察的邊界。"
    )


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


def _restore_session_from_cookie_file(app, session_id: str) -> bool:
    """行程重啟（或新連線）之後憑證檔還在時，直接把它掛成這個連線的 session，不要求
    Agent 再呼叫一次 login()——2026-09-04 實測：真人登入完成後行程重啟，下一個工具
    呼叫回「請先呼叫 login()」，使用者看到的是「我明明登入了它還說沒登入」。

    這裡刻意**不驗證**：接下來這個工具自己的那一次請求就是驗證——憑證過期時守衛會判成
    expired 並回報「已過期，請重新 login()」，login() 再負責清掉壞掉的憑證檔。"""
    cookies = load_cookies(app.config.cookies_path)
    if not cookies:
        return False
    app.session_pool.activate_direct(
        session_id,
        SessionInfo(cookies=cookies, account_label=load_identity(app.config.identity_path)),
    )
    log.info("Session restored from the credential file for a connection that had not called login()")
    return True


async def ensure_account_identity(ctx: Context) -> dict | None:
    """Make sure this connection's SessionInfo knows which 104 account it is
    (`account_label` = the login e-mail), which every row in the database is
    keyed on. Order: already known → nothing; cached in `account.json` next to
    cookies.json → load it; otherwise ask 104 once — `GET event/last-info`
    reports the signed-in operator as `metadata.userEmail` (measured 2026-09-04,
    equal to the account's login e-mail) — and cache it. That request goes
    through guarded_api like any other (throttled, session-checked), so the
    first tool call after a fresh login costs one extra request.

    Returns None when the identity is in place, or the error payload the tool
    should return as-is (session expired, throttled, malformed…). The e-mail is
    the operator's own; it is stored only in their data directory and never
    logged or returned to the agent."""
    app = ctx.request_context.lifespan_context
    info = app.session_pool.get_session(get_session_id(ctx))
    if info is None or info.account_label is not None:
        return None
    cached = load_identity(app.config.identity_path)
    if cached:
        info.account_label = cached
        return None
    try:
        async with guarded_api(ctx, ENDPOINTS["event_last_info"]) as (payload, live_info):
            metadata = payload.get("metadata")
            email = metadata.get("userEmail") if isinstance(metadata, dict) else None
            if not isinstance(email, str) or "@" not in email:
                return _error_malformed("event/last-info 的 metadata.userEmail 缺失或不是 e-mail")
            live_info.account_label = email
            save_identity(app.config.identity_path, email)
            return None
    except GuardAbort as e:
        return e.payload


def require_login(func):
    """Decorator: returns error if there is no usable login state — no session for
    this connection AND no credential file to restore one from (see
    _restore_session_from_cookie_file) — and otherwise makes sure the session's
    104 account identity is resolved before the tool body runs (see
    ensure_account_identity), so tool code can rely on info.account_label."""
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
            if not _restore_session_from_cookie_file(app, session_id):
                return ERROR_NOT_LOGGED_IN
        identity_error = await ensure_account_identity(ctx)
        if identity_error is not None:
            return identity_error
        return await func(*args, **kwargs)
    return wrapper


class GuardAbort(Exception):
    """Base for "abort the tool call and return this payload" signals
    raised from within guarded_api's locked region — either by the guard
    itself or by a before_request hook.
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
    expired, blocked). Raised only by guarded_api itself. Kept distinct
    from the base GuardAbort so that a future handler reacting
    specifically to session trouble (e.g. attempting cleanup) has
    something meaningful to catch — a before_request rejection like a
    daily-cap hit is NOT a session problem and must not raise this."""


class ToolAbort(GuardAbort):
    """A before_request hook (or other pre-request check) wants to abort
    the tool call for a reason that has nothing to do with session health
    — e.g. send_message's daily-cap check, or a throttle judgment-gate
    rejection. Using SessionUnavailable for this would be a lie: the class
    docstring above promises "the session cannot be used", which is false
    for a cap or throttle rejection, and a future session-recovery handler
    keyed on SessionUnavailable would misfire on it."""


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

    An implementation detail of guarded_api, not a general-purpose
    call-site helper: guarded_api calls it once to get the `info` it then
    validates by identity and locks. Tool code should not call this
    directly to get its own separate `info` — every tool that needs
    SessionInfo now gets it as guarded_api's yielded `(payload, info)`,
    which is guaranteed to be the SAME object whose identity was checked
    and whose lock is held (see guarded_api's docstring for why a
    separately-resolved `info` can go stale while queued on the lock).
    """
    app = ctx.request_context.lifespan_context
    session_id = get_session_id(ctx)
    return app.session_pool.get_session(session_id)


# Kind (from browser.api_client.Verdict) -> (payload builder, abort class).
# "expired"/"blocked" mean the SESSION itself is the problem
# (SessionUnavailable); everything else is a per-call
# problem the session may still be perfectly healthy for (ToolAbort) — a
# configuration bug (wrong_host/header_fault) or a shape/status this call's
# response didn't satisfy. "blocked" is special-cased separately in
# guarded_api itself because its payload depends on session history, not
# just the kind.
def _api_error_for_kind(kind: str, endpoint: Endpoint | AssetRoute, detail: str) -> tuple[dict, type[GuardAbort]]:
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


def _asset_error_for_kind(kind: str, route: AssetRoute, detail: str) -> tuple[dict, type[GuardAbort]]:
    """The asset path's counterpart to _api_error_for_kind, handling ONLY
    the four kinds classify_asset alone can emit and DELEGATING every other
    kind to that function.

    Delegation, not a second list. `_issue_one`'s failure path has exactly
    one dedicated branch of its own ("blocked"); every other kind — the
    shared `expired` and `unrecognised_status` included — leaves through
    whichever of these two tables the dispatch picked. A hand-written asset
    table therefore has to restate them, and an earlier draft that tried
    did lose `expired` — the one kind the whole redirect-policy/prologue
    chain exists to deliver, which would have reached the Agent as "未知的
    內部錯誤（expired）". Delegating instead gives the shared kinds a payload
    and abort class bit-identical to the JSON path's, and leaves the "nobody
    taught us this kind" last line living in exactly one place.

    `asset_not_authenticated` is a SessionUnavailable — same family as
    expired/challenge/blocked — but deliberately NOT `expired`: the vip
    session may well still be alive and merely unrecognised by the file
    host, and reporting "session 已過期" would send the next person reading
    the log to investigate an expiry that never happened.
    """
    if kind == "asset_not_authenticated":
        return ERROR_ASSET_NOT_AUTHENTICATED, SessionUnavailable
    if kind == "asset_too_large":
        return ERROR_ASSET_TOO_LARGE, ToolAbort
    if kind == "asset_empty_body":
        return ERROR_ASSET_EMPTY_BODY, ToolAbort
    if kind == "asset_unknown_format":
        return _error_asset_unknown_format(detail), ToolAbort
    return _api_error_for_kind(kind, route, detail)


def _error_internal_config(detail: str) -> dict:
    # Same "this is a code bug, not a site condition" wording family as
    # _error_wrong_host/_error_header_fault — raised ahead of fetch()'s own
    # try/except (see guarded_api below), never inside it, so it reaches
    # the Agent as what it is instead of being converted into
    # ERROR_API_REQUEST_FAILED ("可能是逾時或網路問題，請稍後再試") by the
    # broad `except Exception` that wraps the real transport call.
    return {"error": f"內部設定錯誤：{detail}，這是程式問題，請回報"}


async def _issue_one(
    info: SessionInfo,
    target: Endpoint | AssetRoute,
    params: Sequence[tuple[str, str]] | None,
    body: dict | None,
    *,
    session_id: str,
    throttle_state_path: Path,
    asset_url: str | None = None,
) -> object:
    """Issue exactly ONE HTTP request against `target` and return its
    payload, or raise a GuardAbort subclass. This is the one unit both
    guarded_api (one request per tool call) and guarded_sequence (N
    requests, one call each) run — an extra guard path ("多一條守衛路徑")
    is a real risk, and having both entry points call the SAME function,
    rather than two copies that happen to agree today, is the only
    mitigation that stays true after the next edit.

    call-shape check -> select cookie -> fetch (note_request in
    `finally`) -> Cloudflare challenge screen -> auth-host redirect check
    -> classify() -> info.has_succeeded_api_call = True -> return
    verdict.payload. Every step here is verbatim what guarded_api used to
    do inline; moving it here changes no observable behaviour.

    `target` is EITHER a JSON Endpoint or an AssetRoute (104's file host),
    and the two differ in exactly four places, all selected once at the top
    of this function: the call-shape check, which fetch runs, which
    classify runs, and which kind->payload table maps a failure. Everything
    between those four — cookie selection via hostname_for, note_request in
    `finally`, the broad except around the transport call, the Cloudflare
    scan, the auth-host redirect check, the 403 wording split, the success
    flag, the failure log line — is ONE piece of code serving both, not two
    that agree today. That is the whole reason there is no second
    per-request unit: the first attempt to enumerate those shared steps in
    prose had already lost two of them before any code existed.

    `asset_url` is 104's own URL and is required for (and only for) an
    AssetRoute. It never reaches a log line or a return value: it carries a
    credential-bearing token.

    Every failure-path log statement below names only the endpoint key,
    the HTTP status code (once a response exists to have one), and 104's
    own message (`verdict.detail`, already 104's text via
    browser.api_client._family_b_error_detail/_family_a's own message
    field) — never `body` (candidate content, event content, emailCC —
    see tools/messaging.py's send_inquiry) and never the Cookie header.
    That is this function's own property, not a discipline each call site
    has to remember: both guarded_api and guarded_sequence route every
    request through here, so a log line written once, here, is the whole
    guarantee.

    `body` is forwarded to fetch() unchanged. The call-shape checks below
    (a body handed to a GET endpoint, a POST endpoint called with
    body=None, an AssetRoute without its URL or carrying params/body, a URL
    that is not on the measured asset host) are deliberately CALL-TIME
    checks, not construction-time ones: none of those values exists until a
    caller supplies them. They are also deliberately placed ahead of the
    `try` that wraps the transport call below, which converts anything
    raised inside it into ERROR_API_REQUEST_FAILED ("可能是逾時或網路問題，
    請稍後再試") — reporting a caller's own bug as a transient network blip
    to an Agent whose reasonable next move is to retry.

    Cookies are read from `info.cookies` on every call — not from a
    browser object, because there is no browser object to read from after
    login completes (SessionInfo is the sole holder of credentials
    post-login; see browser/session.py). They are selected for the asset
    host the same way, through hostname_for, and they are sent on the asset
    routes regardless of what each route's measurement says it needs — see
    AssetRoute.cookie_required.
    """
    is_asset = isinstance(target, AssetRoute)

    # ── Dispatch point 1 of 4: the call-shape check ──────────────────────
    if is_asset:
        if asset_url is None:
            raise ToolAbort(
                _error_internal_config(f"{target.key} 是資產路由但未帶 asset_url"),
                kind="internal_config",
            )
        if params is not None or body is not None:
            raise ToolAbort(
                _error_internal_config(f"{target.key} 是資產路由，不接受 params 或 body"),
                kind="internal_config",
            )
        problem = validate_asset_url(target, asset_url)
        if problem is not None:
            # Neither the payload nor this log line may carry the URL, the
            # path or the query string: `?v=` is the token. Only the route
            # key, which check failed, and (host check only) the hostname.
            log.warning(
                "_issue_one: refused an asset URL for %s (failed check: %s)",
                target.key, problem.check,
            )
            if problem.check == "host":
                raise ToolAbort(
                    _error_asset_url_wrong_host(target, problem.hostname),
                    kind="internal_config",
                )
            raise ToolAbort(
                _error_internal_config(f"{target.key} 的資產網址未通過 {problem.check} 檢查"),
                kind="internal_config",
            )
    else:
        if asset_url is not None:
            raise ToolAbort(
                _error_internal_config(f"{target.key} 不是資產路由卻帶了 asset_url"),
                kind="internal_config",
            )
        if target.method == "POST" and body is None:
            raise ToolAbort(
                _error_internal_config(f"{target.key} 是 POST 端點但未帶 body"),
                kind="internal_config",
            )
        if target.method != "POST" and body is not None:
            raise ToolAbort(
                _error_internal_config(f"{target.key} 不是 POST 端點卻帶了 body"),
                kind="internal_config",
            )

    cookie_header = select_cookies_for_host(info.cookies, hostname_for(target))

    # note_request runs in `finally`, not after a bare `await fetch(...)`
    # line: a timeout or connection error raises out of the `try` below
    # and skips anything placed after it, which would leave the
    # rolling-window volume count reading zero on exactly the calls
    # most worth counting — 104 refusing or timing out under load is
    # the condition the volume cap exists to react to, not to miss.
    # this is the only place any aiohttp request — successful or not —
    # is ever counted. Runs unconditionally, regardless of
    # endpoint.throttle_gated: the gate may be waived, the ledger never
    # is — a route exempt from the judgment gate is still a real request
    # against 104 and still belongs in the rolling-window volume count
    # and the inter-call pacing anchor. Called once per sub-request in a
    # guarded_sequence burst, not once per tool call.
    try:
        # ── Dispatch point 2 of 4: which fetch ───────────────────────────
        if is_asset:
            raw = await fetch_asset(target, asset_url, cookie_header=cookie_header)
        else:
            raw = await fetch(target, cookie_header=cookie_header, params=params, body=body)
    except Exception as exc:
        log.error("_issue_one: request to %s failed: %s", target.key, exc)
        # A timeout/connection error says nothing about whether the
        # session itself is usable — the next call may succeed outright
        # — so this is ToolAbort, not SessionUnavailable: the design
        # treats transport failure as transient, and SessionUnavailable's
        # contract (a future session-recovery handler may react to it,
        # e.g. by clearing cookies) must not misfire on a network blip
        # that has nothing to do with session health.
        raise ToolAbort(ERROR_API_REQUEST_FAILED, kind="transport")
    finally:
        note_request(info.throttle, path=throttle_state_path)

    # Must run before any shape inspection: a challenge page has no
    # measured shape resembling either family's success OR any of
    # classify()'s named failures, so it must be screened out first,
    # not fall through into one of those and be reported as something
    # else.
    is_challenge, ray_id = _detect_cloudflare_challenge(raw.body)
    if is_challenge:
        log.warning(
            "_issue_one: Cloudflare challenge detected for session %s calling %s (status=%s, Ray ID: %s)",
            session_id, target.key, raw.status, ray_id or "unknown",
        )
        raise SessionUnavailable(ERROR_CHALLENGE, kind="challenge")

    # A redirect (not followed — see fetch()'s docstring) is handled
    # here, ahead of classify(), only for the auth-host check, which
    # needs matches_auth_host — a dependency classify() deliberately
    # does not carry (it depends on nothing beyond the endpoint
    # declaration). classify() separately checks the same redirect's
    # Location for the company-switch marker string, so a family A/B
    # redirect that is neither an auth host NOR that marker still
    # falls through to classify()'s empty-body "non_json" failure kind
    # and is reported loudly rather than silently. This does not
    # describe logout_session (family="opaque"): its only measured
    # redirect target is boidc.104.com.tw, an auth host, so this very
    # check — which runs before classify() in call order — always
    # intercepts it first; classify()'s own opaque branch (which
    # returns success unconditionally) is never reached in practice,
    # see that endpoint's own comment.
    if raw.location is not None:
        hostname = urlparse(raw.location).hostname or ""
        if matches_auth_host(hostname):
            log.warning(
                "_issue_one: session %s redirected to auth host at %s (status=%s)",
                session_id, target.key, raw.status,
            )
            # An expiry signal, not a transport failure — must declare
            # "expired", not fall in with the transport kind above.
            raise SessionUnavailable(ERROR_EXPIRED, kind="expired")

    # ── Dispatch point 3 of 4: which classify ────────────────────────────
    verdict = classify_asset(target, raw) if is_asset else classify(target, raw)
    if not verdict.ok:
        if verdict.kind == "blocked":
            payload = (
                ERROR_BLOCKED_API_AFTER_SUCCESS if info.has_succeeded_api_call
                else ERROR_BLOCKED_API_FIRST_CALL
            )
            log.warning(
                "_issue_one: request blocked (403) for session %s calling %s (status=%s)",
                session_id, target.key, raw.status,
            )
            raise SessionUnavailable(payload, kind="blocked")
        # ── Dispatch point 4 of 4: which kind -> payload + abort class ───
        # The asset table handles only its own four kinds and delegates
        # every other one back to _api_error_for_kind, so a shared kind
        # (expired, unrecognised_status, and whatever is added next) gets
        # a bit-identical payload on both paths and the "unknown kind"
        # last line stays single.
        error_payload, abort_cls = (
            _asset_error_for_kind(verdict.kind, target, verdict.detail) if is_asset
            else _api_error_for_kind(verdict.kind, target, verdict.detail)
        )
        log.warning(
            "_issue_one: %s failed status=%s kind=%s detail=%s",
            target.key, raw.status, verdict.kind, verdict.detail,
        )
        # The classifier's own kind, passed through verbatim — never
        # re-mapped here, so a kind classify() has never been taught
        # about still reaches send_message's ambiguous fallthrough
        # rather than silently landing on whatever this line happened
        # to default to.
        raise abort_cls(error_payload, kind=verdict.kind)

    info.has_succeeded_api_call = True
    return verdict.payload


def _project_field(which: str, obj: object, keys: tuple[str, ...] | None) -> dict:
    """Apply one half (`data` or `metadata`) of a guarded_sequence
    `request()` projection. `keys is None` is never passed here — the
    caller only calls this once it knows at least one of pick_data/
    pick_metadata was given (see _project_payload) — `()` and a non-empty
    tuple are the only two shapes this function ever sees.

    `()` means "keep nothing" and short-circuits before even asking
    whether `obj` is a dict — an empty pick is a valid, meaningful
    request regardless of what shape sits underneath ("()" means "keep
    nothing for this half"). A non-empty pick against a non-dict `obj` (metadata
    absent from the envelope entirely, e.g.) is treated the same as
    "named key missing" — there is nothing under `which` for any key to
    live in.
    """
    if not keys:
        return {}
    if not isinstance(obj, dict):
        raise ToolAbort(_error_malformed(f"{which} missing or not an object (cannot project {keys[0]})"), kind="malformed")
    projected: dict = {}
    for key in keys:
        if key not in obj:
            raise ToolAbort(_error_malformed(f"{which}.{key} missing"), kind="malformed")
        projected[key] = obj[key]
    return projected


def _project_payload(
    payload: dict,
    pick_data: tuple[str, ...] | None,
    pick_metadata: tuple[str, ...] | None,
) -> dict:
    """`None` for BOTH pick_data and pick_metadata means "no projection at
    all" — the caller never reaches this function in that case (see
    guarded_sequence's `request()`); this function only runs once at
    least one of them is not None, and each half is projected
    independently — a half whose own pick_* is `None` is left untouched
    (still whatever `payload` already carried for it), not wiped to `{}`;
    only a half whose pick_* is an explicit tuple (`()` included) is
    projected. Top-level keys other than `data`/`metadata` (a
    FamilyBShape's sibling_keys, e.g. `failed`) are carried through
    unfiltered — request()'s two pick_* parameters name only these two
    halves, deliberately (信封本來就是兩個並列的鍵 — the envelope is
    inherently two parallel keys), so there is no third parameter asking
    this function to touch anything else.
    """
    projected = dict(payload)
    if pick_data is not None:
        projected["data"] = _project_field("data", payload.get("data"), pick_data)
    if pick_metadata is not None:
        projected["metadata"] = _project_field("metadata", payload.get("metadata"), pick_metadata)
    return projected


@asynccontextmanager
async def guarded_api(
    ctx: Context,
    endpoint: Endpoint,
    *,
    params: Sequence[tuple[str, str]] | None = None,
    body: dict | None = None,
    before_request: Callable[[SessionInfo], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[object, SessionInfo]]:
    """Resolve the session, hold its lock for the whole region, issue
    exactly ONE HTTP request (no navigation, no settle window — an HTTP
    response has no client-side after-state the way a Vue SPA page does),
    and yield (payload, info).

    Every read/write tool (tools/search.py, tools/messaging.py) plus
    restore verification and server-side logout (tools/auth.py) use this
    identically:

        try:
            async with guarded_api(ctx, ENDPOINTS["search_resumes"], params=params) as (payload, info):
                ... tool body ...
        except GuardAbort as e:
            return e.payload

    check_session_expired is not called here — its URL half cannot fire
    (redirects are not followed) and its HTTP-status half maps 403 to a
    single "blocked" wording that would pre-empt the clearance-vs-block
    distinction below. browser/api_client.classify()'s own Error Handling
    rows cover both statuses instead.

    before_request runs inside the lock, after the throttle gate (when the
    endpoint declares one), and may raise ToolAbort to abort without
    issuing a request.

    The actual request/response handling (method/body check, cookie
    selection, fetch, challenge/redirect/classify) lives in `_issue_one`
    now, shared with guarded_sequence below — this function's own job is
    reduced to session resolution, the lock, the throttle gate, and the
    before_request hook, in that order, exactly as before (this refactor
    changes no observable behaviour of any of the existing call sites —
    signature and yield shape are unchanged).
    """
    # THE function's first statement, ahead of resolve_session: an
    # AssetRoute reaching here is a program bug, and it must be reported as
    # one. Two things would otherwise go wrong. Without the check at all it
    # would fall through to `endpoint.throttle_gated` below — an attribute
    # AssetRoute deliberately does not have (see its docstring) — raising
    # AttributeError, which is NOT a GuardAbort and so escapes every tool's
    # `except GuardAbort` as an unhandled exception. And placing the check
    # merely "before the lock" would still put it after resolve_session, so
    # a caller with no session would be told ERROR_NOT_LOGGED_IN instead —
    # a program bug disguised as a login problem, the same masking
    # _issue_one's call-shape checks are positioned to avoid.
    if isinstance(endpoint, AssetRoute):
        raise ToolAbort(
            _error_internal_config(
                f"{endpoint.key} 是資產路由，不能走 guarded_api——一次資產抓取一定是兩個"
                "請求（先讀履歷詳情取網址，再抓檔案），請改用 guarded_sequence"
            ),
            kind="internal_config",
        )

    app = ctx.request_context.lifespan_context
    info = await resolve_session(ctx)
    if not info:
        raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

    session_id = get_session_id(ctx)
    async with info.lock:
        # Re-check presence AND identity: logout()+login() can remove this
        # session and register a brand-new SessionInfo under the same
        # session_id while this call was queued waiting for `info.lock` —
        # a presence-only check (is_logged_in) would pass against that NEW
        # entry even though we hold the OLD entry's lock, silently
        # defeating the whole point of per-session locking.
        if app.session_pool.get_session(session_id) is not info:
            raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

        if endpoint.throttle_gated:
            abort = await enforce_throttle(
                info.throttle,
                path=app.config.throttle_state_path,
                max_requests_per_hour=app.config.max_requests_per_hour,
                max_inline_wait_seconds=app.config.max_inline_wait_seconds,
                activity_streak_limit_minutes=app.config.activity_streak_limit_minutes,
                rest_duration_minutes=app.config.rest_duration_minutes,
                min_call_interval_seconds=app.config.min_call_interval_seconds,
            )
            if abort is not None:
                # The abort's own `kind` is what reaches ToolAbort, never a
                # kind decided at this call site — a state-file read
                # failure must surface as "internal_config", not as
                # "throttled" with no retry_after_seconds pretending to be
                # an ordinary pace rejection (see ThrottleAbort's
                # docstring). Only "throttled" carries its own payload;
                # every other kind hands back nothing but `detail`, and
                # this is the one place authorised to turn that detail
                # into the same "code bug, not a site condition" wording
                # family as _error_wrong_host/_error_header_fault —
                # browser/throttle.py never imports tools/ and never
                # writes this Agent-facing prose itself.
                payload = abort.payload if abort.kind == "throttled" else _error_internal_config(abort.detail)
                raise ToolAbort(payload, kind=abort.kind)

        if before_request is not None:
            await before_request(info)

        payload = await _issue_one(
            info, endpoint, params, body,
            session_id=session_id,
            throttle_state_path=app.config.throttle_state_path,
        )
        yield payload, info


@asynccontextmanager
async def guarded_sequence(
    ctx: Context,
    *,
    slots_needed: int,
    before_first: Callable[[SessionInfo], Awaitable[None]] | None = None,
) -> AsyncIterator[tuple[Callable[..., Awaitable[object]], SessionInfo]]:
    """The multi-request counterpart to guarded_api — same lock, same
    throttle gate, same `_issue_one` per sub-request, but ONE lock hold
    and ONE throttle-gate check for the whole sequence rather than one
    each per sub-request. Used today by three callers
    (tools/messaging.py's send_inquiry, three sub-requests; and
    tools/resume_files.py's two asset tools, two sub-requests each), but
    nothing here is specific to any of them — the sequence length is
    entirely the caller's business (see `slots_needed` below).

        async with guarded_sequence(ctx, slots_needed=3) as (request, info):
            idno_payload = await request(ENDPOINTS["resolve_candidate_idno"], params=..., pick_data=("idNo",), pick_metadata=())
            ...

    `request()`'s first positional parameter takes EITHER an Endpoint or an
    AssetRoute; for an AssetRoute the URL 104 itself supplied is passed as
    the `asset_url=` keyword, and `params`/`body`/`pick_*` must all be
    omitted. Widening this parameter, rather than yielding a second
    request-like callable, is what keeps the yield shape — and therefore
    every existing call site and the worked example above — unchanged. The
    cost is that `request()` now has two mutually exclusive argument
    groups, ruled out by call-time checks rather than by the type system:
    an AssetRoute without `asset_url`, an Endpoint with one, an AssetRoute
    carrying params/body, and an AssetRoute carrying pick_* are all
    ToolAbort(kind="internal_config"). Handing an AssetRoute to guarded_api
    is a fifth, refused there.

        async with guarded_sequence(ctx, slots_needed=2) as (request, info):
            envelope = await request(ENDPOINTS["get_resume_detail"], params=[("idno", candidate_id)])
            asset = await request(ASSET_ROUTES["candidate_photo"], asset_url=url_from_104)

    `slots_needed` is forwarded to `enforce_throttle` VERBATIM — this
    function never inspects, rewrites, or defaults it beyond the type
    itself; how many requests a sequence needs is the calling tool's own
    fact, not something the guard should know or guess (helpers.py 裡不得
    出現 3 這個數字 — no literal 3 may appear in this file).

    `before_first` runs once, after the throttle gate and before the
    first sub-request — the sequence-level analogue of guarded_api's
    `before_request`. Per-sub-request checks (e.g. send_inquiry's daily-
    cap re-check ahead of its third sub-request) are NOT this parameter's
    job; they are the `before_request` argument to an individual
    `request(...)` call instead.

    A sub-request that fails raises straight out of `request()` — no
    `except` here catches it, so it propagates out of this
    asynccontextmanager, the `async with info.lock:` block exits (lock
    released), and no further sub-request is issued.

    `request()`'s pick_data/pick_metadata are the projection mechanism —
    see `_project_payload`/`_project_field` above and this file's own
    None-vs-() distinction described there: `None` (the default for both)
    means no projection at all, the SAME full envelope guarded_api has
    always yielded; a non-`None` value projects that half down to exactly
    the named keys, raising ToolAbort(kind="malformed") — sharing
    `_error_malformed`, classify()'s own inner_key floor's payload, so
    this needs no new handler at any call site's `except GuardAbort` —
    the instant a named key turns out absent. Projection is refused up
    front, before `_issue_one` (and therefore before fetch()'s own
    try/except) is ever reached, for an endpoint whose family_b_shape is
    `is_list=True` (its `data` is a list with no keys to pick from) or has
    no family_b_shape at all — ToolAbort(kind="internal_config"), the same
    "this is a program bug, please report it" family guarded_api already
    uses for a caller's own method/body mismatch.
    """
    app = ctx.request_context.lifespan_context
    info = await resolve_session(ctx)
    if not info:
        raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

    session_id = get_session_id(ctx)
    async with info.lock:
        # Same identity re-check as guarded_api, same reason: this call
        # may have queued on `info.lock` behind a logout()+login() pair
        # that replaced the pool entry under the same session_id.
        if app.session_pool.get_session(session_id) is not info:
            raise SessionUnavailable(ERROR_NOT_LOGGED_IN, kind="not_logged_in")

        # One throttle-gate check for the WHOLE sequence, not one per
        # sub-request — unconditional (not gated behind any single
        # sub-request's endpoint.throttle_gated) because a sequence
        # reserves slots for however many requests it is about to issue,
        # a fact that belongs to the caller, not to any one endpoint in
        # the burst.
        abort = await enforce_throttle(
            info.throttle,
            path=app.config.throttle_state_path,
            max_requests_per_hour=app.config.max_requests_per_hour,
            max_inline_wait_seconds=app.config.max_inline_wait_seconds,
            activity_streak_limit_minutes=app.config.activity_streak_limit_minutes,
            rest_duration_minutes=app.config.rest_duration_minutes,
            min_call_interval_seconds=app.config.min_call_interval_seconds,
            slots_needed=slots_needed,
        )
        if abort is not None:
            payload = abort.payload if abort.kind == "throttled" else _error_internal_config(abort.detail)
            raise ToolAbort(payload, kind=abort.kind)

        if before_first is not None:
            await before_first(info)

        async def request(
            endpoint: Endpoint | AssetRoute,
            *,
            params: Sequence[tuple[str, str]] | None = None,
            body: dict | None = None,
            asset_url: str | None = None,
            before_request: Callable[[SessionInfo], Awaitable[None]] | None = None,
            pick_data: tuple[str, ...] | None = None,
            pick_metadata: tuple[str, ...] | None = None,
        ) -> object:
            if pick_data is not None or pick_metadata is not None:
                if isinstance(endpoint, AssetRoute):
                    # Checked here rather than in _issue_one (which never
                    # sees pick_*), and ahead of the family_b_shape lookup
                    # below, which an AssetRoute has no attribute for. An
                    # asset response is bytes, not an envelope with halves
                    # to project.
                    raise ToolAbort(
                        _error_internal_config(
                            f"{endpoint.key} 是資產路由，不支援欄位投影（pick_data/pick_metadata）"
                        ),
                        kind="internal_config",
                    )
                shape = endpoint.family_b_shape
                if shape is None or shape.is_list:
                    raise ToolAbort(
                        _error_internal_config(
                            f"{endpoint.key} 不支援欄位投影（pick_data/pick_metadata 只能用在 "
                            "is_list=False 的端點）"
                        ),
                        kind="internal_config",
                    )

            if before_request is not None:
                await before_request(info)

            payload = await _issue_one(
                info, endpoint, params, body,
                session_id=session_id,
                throttle_state_path=app.config.throttle_state_path,
                asset_url=asset_url,
            )
            if pick_data is None and pick_metadata is None:
                return payload
            return _project_payload(payload, pick_data, pick_metadata)

        yield request, info
