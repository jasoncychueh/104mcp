import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from aiohttp import web
from patchright.async_api import Browser

from mcp.server.fastmcp import FastMCP

from mcp104.config import get_config, Config
from mcp104.db.database import Database
from mcp104.browser.stealth import launch_browser
from mcp104.browser.session import SessionPool
from mcp104.browser.vnc import VncManager
from mcp104.web.auth_server import create_auth_app
from mcp104.tools.auth import register_auth_tools
from mcp104.tools.discovery import register_discovery_tools
from mcp104.tools.search import register_search_tools
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.status import register_status_tools

log = logging.getLogger("104-mcp.main")

STALE_CHECK_INTERVAL = 300  # 5 minutes
STALE_SESSION_TIMEOUT = 30  # minutes
WATCHER_SHUTDOWN_TIMEOUT = 15.0  # seconds — see _shutdown_globals


@dataclass
class AppContext:
    config: Config
    db: Database
    browser: Browser            # shared headless browser for active sessions
    session_pool: SessionPool
    vnc_manager: VncManager
    _pending_browsers: dict     # token → (browser, context, page) for login in progress
    _watcher_tasks: dict        # token → asyncio.Task, strong ref to _watch_for_login tasks


# ── Global singletons (initialized once at app startup) ──────────────
_app_ctx: AppContext | None = None
_cleanup_task: asyncio.Task | None = None
_auth_runner: web.AppRunner | None = None


async def _stale_session_cleaner(session_pool: SessionPool):
    while True:
        await asyncio.sleep(STALE_CHECK_INTERVAL)
        await session_pool.cleanup_stale(max_idle_minutes=STALE_SESSION_TIMEOUT)


async def _init_globals():
    """Initialize global resources once."""
    global _app_ctx, _cleanup_task, _auth_runner

    config = get_config()
    db = Database(config.db_path)
    await db.init()

    browser = await launch_browser(headless=True)
    session_pool = SessionPool()
    vnc_manager = VncManager()

    # Start auth web server on :8080 for noVNC login pages
    auth_app = create_auth_app(vnc_manager)
    _auth_runner = web.AppRunner(auth_app)
    await _auth_runner.setup()
    site = web.TCPSite(_auth_runner, "0.0.0.0", 8080)
    await site.start()

    # Start stale session cleanup
    _cleanup_task = asyncio.create_task(_stale_session_cleaner(session_pool))

    _app_ctx = AppContext(
        config=config,
        db=db,
        browser=browser,
        session_pool=session_pool,
        vnc_manager=vnc_manager,
        _pending_browsers={},
        _watcher_tasks={},
    )


async def _shutdown_globals():
    """Cleanup global resources."""
    global _app_ctx, _cleanup_task, _auth_runner
    if _cleanup_task:
        _cleanup_task.cancel()
        try:
            await _cleanup_task
        except asyncio.CancelledError:
            pass
    if _app_ctx:
        # Cancel any in-flight login watchers BEFORE closing the pending
        # browsers/VNC stacks they operate on. Without this, each watcher
        # is left to asyncio.run()'s generic teardown, which fires AFTER
        # the loop below has already closed their browsers and VNC
        # stacks — so each watcher then runs its own cleanup against
        # already-closed resources (confusing "failed to close" log noise
        # for objects that were closed on purpose). Cancelling first lets
        # each watcher's own CancelledError handler (_abandon_pending_login)
        # tear itself down cleanly; the loop below only has to handle
        # whatever a watcher didn't (there normally shouldn't be any).
        watcher_tasks = list(_app_ctx._watcher_tasks.values())
        for task in watcher_tasks:
            task.cancel()
        if watcher_tasks:
            # Bounded, not unbounded: a cancelled watcher's own cleanup
            # closes a headed Chromium (tools/auth.py's
            # _abandon_pending_login), which — per CLAUDE.md's known-issue
            # #1 — can hang if that browser already died silently under
            # /dev/shm pressure. An unbounded gather here would let one
            # stuck watcher hang the entire shutdown until Docker SIGKILLs
            # the container.
            try:
                await asyncio.wait_for(
                    asyncio.gather(*watcher_tasks, return_exceptions=True),
                    timeout=WATCHER_SHUTDOWN_TIMEOUT,
                )
            except asyncio.TimeoutError:
                log.error(
                    "Shutdown: %d watcher task(s) did not finish within %.0fs of "
                    "cancellation — proceeding with the rest of shutdown regardless",
                    len(watcher_tasks), WATCHER_SHUTDOWN_TIMEOUT,
                )
        _app_ctx._watcher_tasks.clear()

        # Each pending (not-yet-completed) login owns its own non-headless
        # Browser + BrowserContext, separate from the shared headless
        # browser closed below. Without this, a login in progress at
        # shutdown time leaks an orphaned headed Chromium across restarts.
        for token, (browser, context, _page) in list(_app_ctx._pending_browsers.items()):
            try:
                await context.close()
            except Exception:
                log.exception("Shutdown: failed closing pending login context for token %s", token[:8])
            try:
                await browser.close()
            except Exception:
                log.exception("Shutdown: failed closing pending login browser for token %s", token[:8])
        _app_ctx._pending_browsers.clear()

        await _app_ctx.vnc_manager.stop_all()
        await _app_ctx.session_pool.cleanup_all()
        await _app_ctx.browser.close()
        await _app_ctx.db.close()
    if _auth_runner:
        await _auth_runner.cleanup()


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Per-session lifespan: just yield the shared global context."""
    assert _app_ctx is not None, "Global resources not initialized"
    yield _app_ctx


mcp = FastMCP(
    "104-mcp-server",
    lifespan=app_lifespan,
    host="0.0.0.0",
    # No port= here: mcp.run() is never called (see main() below), so a
    # port on this constructor would be dead config duplicating uvicorn's
    # own port=8081 — the actual port in effect.
)

register_auth_tools(mcp)
register_search_tools(mcp)
register_messaging_tools(mcp)
register_status_tools(mcp)
register_discovery_tools(mcp)


async def main():
    await _init_globals()
    try:
        # mcp.run() blocks; we need to call it in a way that uses the current loop.
        # Use the underlying ASGI app with uvicorn directly.
        import uvicorn
        config = uvicorn.Config(
            mcp.streamable_http_app(),
            host="0.0.0.0",
            port=8081,
            log_level="info",
        )
        server = uvicorn.Server(config)
        await server.serve()
    finally:
        await _shutdown_globals()


def run() -> None:
    """Sync entry point for the `mcp104` console script (`[project.scripts]` in
    pyproject.toml) — `main()` is `async def`, and a console script needs a plain
    callable, so this is the wrapper `ENTRYPOINT ["mcp104"]` in the Dockerfile
    actually invokes.
    """
    asyncio.run(main())


if __name__ == "__main__":
    run()
