import asyncio
import logging
import secrets
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP

from mcp104.browser.session import (
    SessionInfo, PendingLogin,
    check_login_liveness, save_cookies, load_cookies, clear_cookies,
)
from mcp104.browser.stealth import launch_browser, create_stealth_context
from mcp104.tools.helpers import get_session_id

LOGIN_URL = "https://bsignin.104.com.tw/login"
# How long the background watcher waits for the user to finish logging in —
# app.config.login_timeout_seconds (LOGIN_TIMEOUT_SECONDS env var, see
# config.py). Kept as app config rather than a module constant so it lives
# in the same place as every other env-derived value (auth_base_url,
# max_daily_messages) instead of a one-off os.getenv scattered here.

# 104 使用 ORY Hydra 做 OIDC，登入鏈為
# bsignin → boidc(OAuth2+PKCE) → bsignin/mfa → bsignin/product → vip.104.com.tw/rms/index。
# MFA（validation_type: unreliable_device）在容器內每次都會觸發，因為每次登入都是
# 全新的瀏覽器設定檔、沒有裝置指紋歷史 —— 所以「靜默重新登入」在設計上不可能，
# 一定要真人在 noVNC 完成。詳見 docs/104-site-facts.md。

log = logging.getLogger("104-mcp.auth")

# app-session cookies that actually survive the transfer into the headless
# context (its/ithp, 24h). PHPSESSID is session-only and does not — cookie
# presence there would be a false positive for "logged in".
VIP_SESSION_COOKIE_NAMES = ("its", "ithp")
COOKIE_POLL_INTERVAL = 1.0  # seconds
WATCHER_CANCEL_TIMEOUT = 10.0  # seconds — see _abandon_pending_login


def _has_vip_session_cookie(cookies: list[dict]) -> bool:
    for c in cookies:
        if c.get("domain") not in (".vip.104.com.tw", "vip.104.com.tw"):
            continue
        if c.get("name") in VIP_SESSION_COOKIE_NAMES:
            return True
    return False


async def _abandon_pending_login(app, token: str, reason: str):
    """Close everything associated with a pending (not-yet-completed) login
    and clear its bookkeeping. Idempotent — every step tolerates the
    resource already being gone/closed, so this is safe to call from
    multiple exit paths (timeout, crash, cancellation, or a fresh login()
    superseding a stale one) without double-cleanup blowing up.

    Cancels the watcher task FIRST and awaits it (bounded — see below)
    before touching any browser/VNC resource. Without this, a caller other
    than the watcher itself (e.g. login()'s stale-pending cleanup) could
    race the still-running watcher: the watcher might be sitting in the
    window between its own context.close() and activate() — having already
    captured a login the user genuinely just completed — and abandoning
    around it would make activate() return False (its `_pending` entry
    gone), so the watcher would discard a perfectly good headless_context.
    Cancelling and awaiting first means the two can never run concurrently.

    The await is bounded by WATCHER_CANCEL_TIMEOUT, not unbounded. A
    cancelled watcher's own handler runs `await context.close()` on a
    headed Chromium; per CLAUDE.md's known-issue #1 that browser can die
    silently under /dev/shm pressure, and closing a context whose CDP
    connection is already dead can hang. Without a timeout here, login()
    (which calls this synchronously via the stale-pending cleanup) would
    hang past the Agent's own tool-call timeout while the server coroutine
    keeps waiting — the cleanup this function exists to guarantee must not
    itself become unboundable on a browser that's already misbehaving.

    Guards against the watcher cancelling itself: this function is also
    called FROM WITHIN _watch_for_login's own timeout/exception handlers,
    where `app._watcher_tasks[token]` IS the task currently executing this
    code — asyncio raises RuntimeError on a task awaiting itself, so that
    case skips straight to the resource cleanup below instead.
    """
    log.warning("Abandoning pending login for token %s: %s", token[:8], reason)

    task = app._watcher_tasks.pop(token, None)
    current = asyncio.current_task()
    if task is not None and task is not current and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=WATCHER_CANCEL_TIMEOUT)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            log.error(
                "Watcher task for token %s did not finish within %.0fs of cancellation "
                "(likely a hung browser close) — proceeding with cleanup regardless; "
                "the watcher task itself is left to finish or die on its own",
                token[:8], WATCHER_CANCEL_TIMEOUT,
            )
        except Exception:
            log.exception("Watcher task for token %s raised while being cancelled", token[:8])

    browser_info = app._pending_browsers.pop(token, None)
    app.session_pool.discard_pending(token)
    if browser_info:
        browser, context, _page = browser_info
        try:
            await context.close()
        except Exception:
            log.exception("Failed closing context for abandoned login %s", token[:8])
        try:
            await browser.close()
        except Exception:
            log.exception("Failed closing browser for abandoned login %s", token[:8])
    try:
        await app.vnc_manager.stop(token)
    except Exception:
        log.exception("Failed stopping VNC for abandoned login %s", token[:8])


def _on_watcher_done(app, token: str, task: asyncio.Task):
    """Done-callback for the watcher task. Its only job is to log an
    unexpected crash — _watch_for_login's own try/except already runs
    cleanup for every exit path, so this does not duplicate that."""
    app._watcher_tasks.pop(token, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Login watcher for token %s ended with an exception: %s", token[:8], exc)


async def _watch_for_login(app, token: str):
    """Background task: detect login completion by watching for the main
    frame to actually settle on vip.104.com.tw, then confirming the
    app-session cookies have arrived.

    The old trigger fired on the first response merely mentioning
    "vip.104.com.tw" or "/rms/" anywhere in its URL — which can happen
    mid-chain before the OAuth exchange finishes — and snapshotted cookies
    before a usable vip.104.com.tw session existed. That is the root cause
    documented in docs/104-site-facts.md: the committed cookies.json had no
    vip.104.com.tw cookies at all. Exact-hostname frame navigation plus a
    cookie-presence poll (not a one-shot check) fixes both halves of that.

    Every exit path — timeout, cancellation, or an unexpected crash (e.g. a
    /dev/shm crash mid-poll, or create_stealth_context/add_cookies failing
    after the source context is already closed) — runs _abandon_pending_login
    exactly once via the try/except below, so _pending_browsers/_pending
    never leak and the VNC stack is always torn down.
    """
    pending = app.session_pool.get_pending(token)
    if not pending:
        return

    browser_info = app._pending_browsers.get(token)
    if not browser_info:
        return

    browser, context, page = browser_info
    succeeded = False

    def on_frame_navigated(frame):
        if frame == page.main_frame:
            hostname = urlparse(frame.url).hostname or ""
            if hostname == "vip.104.com.tw":
                log.info("Main frame settled on vip.104.com.tw: %s", frame.url)
                login_detected.set()

    login_detected = asyncio.Event()

    try:
        loop = asyncio.get_event_loop()
        overall_deadline = loop.time() + app.config.login_timeout_seconds

        page.on("framenavigated", on_frame_navigated)
        try:
            remaining = max(0.0, overall_deadline - loop.time())
            await asyncio.wait_for(login_detected.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            await _abandon_pending_login(app, token, "timed out waiting for vip.104.com.tw navigation")
            return
        finally:
            page.remove_listener("framenavigated", on_frame_navigated)

        # Check if already completed by a manual check_login call
        if not app.session_pool.get_pending(token):
            return

        # Frame settling on vip.104.com.tw is necessary but not sufficient —
        # the app-session cookies can lag slightly behind the navigation.
        # Poll for them within what remains of the login budget rather than
        # snapshotting once with a fallback proceed, which would just delay
        # the same bug.
        cookies = await context.cookies()
        while not _has_vip_session_cookie(cookies):
            if not app.session_pool.get_pending(token):
                return
            if loop.time() >= overall_deadline:
                await _abandon_pending_login(app, token, "vip.104.com.tw session cookie never appeared")
                return
            await asyncio.sleep(COOKIE_POLL_INTERVAL)
            cookies = await context.cookies()

        log.info("Auto-completing login for token %s", token[:8])
        save_cookies(cookies)

        await context.close()
        await browser.close()
        app._pending_browsers.pop(token, None)

        headless_context = await create_stealth_context(app.browser)
        await headless_context.add_cookies(cookies)

        session_id = pending.mcp_session_id
        activated = app.session_pool.activate(token, SessionInfo(
            browser_context=headless_context,
        ))
        if not activated:
            # _pending was already consumed by a concurrent caller — this
            # context was never registered in the pool, so close it here
            # rather than leak it.
            await headless_context.close()

        # The login itself is complete (registered, or correctly discarded
        # as superseded) as of here — set this BEFORE the VNC teardown
        # below, not after. Otherwise a failure in vnc_manager.stop alone
        # would leave `succeeded` False and mislabel a login that actually
        # completed as "unexpected exception in watcher" in the except
        # block's log line.
        succeeded = True

        await app.vnc_manager.stop(token)
        app._watcher_tasks.pop(token, None)
        log.info("Login auto-completed, VNC closed for session %s", session_id)

    except asyncio.CancelledError:
        if not succeeded:
            await _abandon_pending_login(app, token, "watcher task cancelled")
        raise
    except Exception:
        log.exception("Login watcher for token %s crashed", token[:8])
        if not succeeded:
            await _abandon_pending_login(app, token, "unexpected exception in watcher")


def register_auth_tools(mcp: FastMCP):

    @mcp.tool()
    async def login(ctx: Context) -> dict:
        """啟動 104 人力銀行登入流程。

        會自動嘗試恢復已存的 cookies。若無 cookies，啟動 VNC 登入並回傳 login_url。
        使用者在 VNC 完成登入後，系統會自動偵測並完成 session 建立，不需手動呼叫 check_login。
        """
        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)

        if app.session_pool.is_logged_in(session_id):
            # A dead in-memory session must not be reported healthy forever
            # — that made the Agent loop on every subsequent tool call. But
            # only a DEFINITE "logged_out" verdict may destroy it; a mere
            # timeout/interstitial ("indeterminate") must not, since MFA
            # makes recovery a human-required VNC cycle (see module note).
            info = app.session_pool.get_session(session_id)
            async with info.lock:
                liveness = await check_login_liveness(info.browser_context)
            if liveness == "alive":
                return {"status": "already_logged_in"}
            if liveness == "indeterminate":
                log.warning(
                    "Liveness check for %s was indeterminate; leaving the session intact", session_id
                )
                return {
                    "status": "unknown",
                    "error": "無法確認登入狀態（可能是逾時或暫時性錯誤），請稍後再呼叫一次 login()",
                }
            log.info("In-memory session for %s is definitely logged out; clearing and re-logging in", session_id)
            clear_cookies()
            await app.session_pool.remove(session_id)
            # fall through to the VNC flow below
        else:
            cookies = load_cookies()
            if cookies:
                headless_ctx = await create_stealth_context(app.browser)
                await headless_ctx.add_cookies(cookies)
                liveness = await check_login_liveness(headless_ctx)
                if liveness == "alive":
                    app.session_pool.activate_direct(session_id, SessionInfo(
                        browser_context=headless_ctx,
                    ))
                    return {"status": "restored"}
                if liveness == "indeterminate":
                    # Don't delete cookies we can't yet prove are dead —
                    # close this throwaway context (never registered) and
                    # ask the Agent to retry rather than force a full MFA
                    # re-login on a hiccup.
                    await headless_ctx.close()
                    log.warning(
                        "Liveness check while restoring cookies for %s was indeterminate; not clearing cookies.json",
                        session_id,
                    )
                    return {
                        "status": "unknown",
                        "error": "無法確認登入狀態（可能是逾時或暫時性錯誤），請稍後再呼叫一次 login()",
                    }
                # liveness == "logged_out": definitely dead cookies
                await headless_ctx.close()
                clear_cookies()
                # fall through to the VNC flow below

        # A stale pending login for this same MCP session (e.g. the Agent
        # called login() again without finishing a previous VNC flow) would
        # otherwise stack a second Xvfb + x11vnc + headed Chromium on top —
        # the documented shm_size crash condition — and only be reclaimed
        # after its 5-minute timeout. Clean it up before starting a new one.
        for stale_token in app.session_pool.find_pending_tokens_for_session(session_id):
            await _abandon_pending_login(app, stale_token, "superseded by a new login() call")

        # VNC login flow
        token = secrets.token_urlsafe(32)
        vnc_session = await app.vnc_manager.start(token)

        browser = await launch_browser(headless=False, display=vnc_session.display)
        context = await create_stealth_context(browser)
        page = await context.new_page()
        await page.goto(LOGIN_URL)

        app.session_pool.add_pending(token, PendingLogin(
            display=vnc_session.display,
            mcp_session_id=session_id,
        ))
        app._pending_browsers[token] = (browser, context, page)

        # Start background watcher — auto-completes login when cookies
        # detected. Keep a strong reference (an unreferenced Task can be
        # garbage-collected mid-run) and log any crash that escapes the
        # watcher's own try/except.
        watcher_task = asyncio.create_task(_watch_for_login(app, token))
        app._watcher_tasks[token] = watcher_task
        watcher_task.add_done_callback(lambda t, tok=token: _on_watcher_done(app, tok, t))

        login_url = f"{app.config.auth_base_url}/auth/{token}"
        return {"login_url": login_url, "token": token}

    @mcp.tool()
    async def check_login(token: str, ctx: Context) -> dict:
        """檢查登入是否完成（advisory：背景 watcher 會自動偵測完成並生效，
        通常不需手動呼叫此工具）。

        Args:
            token: login() 回傳的 token。
        """
        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)

        # Already completed (by background watcher or previous call)
        if app.session_pool.is_logged_in(session_id):
            return {"status": "success"}

        pending = app.session_pool.get_pending(token)
        if not pending:
            return {"status": "failed", "error": "無效的 token 或登入已逾時"}

        # Background watcher hasn't completed yet
        return {"status": "pending", "message": "登入偵測中，使用者完成登入後會自動生效"}

    @mcp.tool()
    async def logout(ctx: Context) -> dict:
        """登出 104 人力銀行，清理瀏覽器 session 和已存的 cookies。"""
        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)
        clear_cookies()
        await app.session_pool.remove(session_id)
        return {"success": True}
