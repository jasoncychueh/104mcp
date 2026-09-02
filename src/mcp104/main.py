import asyncio
import logging
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP

from mcp104.browser.session import SessionPool
from mcp104.browser.stealth import selftest_browser
from mcp104.browser.throttle import compact_state_file
from mcp104.config import Config, get_config
from mcp104.db.database import Database
from mcp104.tools.auth import PendingLoginResources, register_auth_tools
from mcp104.tools.discovery import register_discovery_tools
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from mcp104.tools.status import register_status_tools
from mcp104.web.auth_server import AuthEndpoint, resolve_auth_binding

log = logging.getLogger("104-mcp.main")

WATCHER_SHUTDOWN_TIMEOUT = 15.0  # seconds — see _shutdown_globals

# The argv self-check path (design.md §C1): a fixed route that takes no
# destination argument (nothing here is configurable, so there's no knob to
# point at an attacker's endpoint), launches a real browser, navigates
# nowhere but about:blank, closes it, and exits 0 — all without writing a
# single byte to stdout. It exists so a subprocess (T-32) can prove the
# driver pipeline actually starts on a host with no display server, without
# going through login() (which would leave a live Chromium and a 900s
# watcher running past the end of the test).
SELFTEST_FLAG = "--selftest-browser-stdout"


def configure_logging() -> None:
    """Point the root logger's handler at stderr and set its level, before
    any other initialization runs. Without an explicit handler, Python's
    lastResort handler silently drops every INFO-level message and stdout
    stays clean only by accident — this fixes both at once (design.md §C1,
    Requirements 7.1-7.3).

    Clears any handler already on the root logger first: constructing the
    module-level `mcp = FastMCP(...)` below runs the `mcp` package's own
    logging.basicConfig() as a side effect, and that call is a no-op on a
    second invocation unless the root logger has zero handlers — so calling
    this defensively first, and again clearing here, keeps exactly one
    handler in effect and guarantees it targets stderr regardless of
    import-time ordering."""
    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.setLevel(logging.INFO)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    )
    root.addHandler(handler)


@dataclass
class AppContext:
    config: Config
    db: Database
    session_pool: SessionPool
    auth_site: AuthEndpoint | None = None
    logout_epoch: int = 0
    _pending_logins: dict[str, PendingLoginResources] = field(default_factory=dict)
    _finished_logins: dict[str, str] = field(default_factory=dict)
    _watcher_tasks: dict[str, asyncio.Task] = field(default_factory=dict)


# ── Global singleton (initialized once at process startup) ───────────
_app_ctx: AppContext | None = None


async def _init_globals() -> None:
    """Startup sequence, fixed order (design.md §C1): get_config() → create
    the per-user data directory → Database.init(account_label) →
    compact_state_file(). resolve_auth_binding() is also called here, during
    config resolution, so a half-set auth binding pair fails at startup
    instead of producing a login_url that silently connects to nothing.

    Any failure here propagates to the caller, which is responsible for the
    stderr/non-zero-exit/clean-stdout contract (§Error Handling scenario 7)
    — this function does not touch stdout or sys.exit itself.
    """
    global _app_ctx

    config = get_config()
    resolve_auth_binding(config)

    config.data_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    await db.init(config.account_label)

    compact_state_file(config.throttle_state_path)

    _app_ctx = AppContext(
        config=config,
        db=db,
        session_pool=SessionPool(),
    )


async def _shutdown_globals() -> None:
    """Cleanup global resources. Cancels in-flight login watchers before
    tearing down the pending-login resources they operate on, with a time
    bound so a stuck watcher cannot hang shutdown indefinitely."""
    global _app_ctx
    if _app_ctx is None:
        return

    # Cancel any in-flight login watchers BEFORE closing the pending-login
    # browsers they operate on. Without this, each watcher is left to
    # asyncio.run()'s generic teardown, which fires AFTER the loop below has
    # already closed their browsers — so each watcher then runs its own
    # cleanup against already-closed resources (confusing "failed to close"
    # log noise for objects that were closed on purpose). Cancelling first
    # lets each watcher's own CancelledError handler tear itself down
    # cleanly; the loop below only has to handle whatever a watcher didn't
    # (there normally shouldn't be any).
    watcher_tasks = list(_app_ctx._watcher_tasks.values())
    for task in watcher_tasks:
        task.cancel()
    if watcher_tasks:
        # Bounded, not unbounded: a cancelled watcher's own cleanup closes a
        # headed Chromium, which — per CLAUDE.md's known-issue #1 — can hang
        # if that browser already died silently under /dev/shm pressure. An
        # unbounded gather here would let one stuck watcher hang the entire
        # shutdown.
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
    # Browser + BrowserContext. Without this, a login in progress at
    # shutdown time leaks an orphaned headed Chromium across restarts.
    for token, resource in list(_app_ctx._pending_logins.items()):
        try:
            await resource.context.close()
        except Exception:
            log.exception(
                "Shutdown: failed closing pending login context for token %s",
                token[:8],
            )
        try:
            await resource.browser.close()
        except Exception:
            log.exception(
                "Shutdown: failed closing pending login browser for token %s",
                token[:8],
            )
    _app_ctx._pending_logins.clear()

    _app_ctx.session_pool.cleanup_all()
    await _app_ctx.db.close()

    # Fixed-port form: the listener is held open until process shutdown
    # (§C1's asymmetric ownership rule). The ephemeral-port form has
    # already detached this field back to None once _pending_logins went
    # empty, so it is None here and this is skipped — an unconditional
    # await ctx.auth_site.close() would AttributeError on that (the most
    # common) case, and idempotency on close() does not help because there
    # is no handle to call it on in the first place.
    if _app_ctx.auth_site is not None:
        endpoint = _app_ctx.auth_site
        _app_ctx.auth_site = None
        await endpoint.close()


@asynccontextmanager
async def app_lifespan(server: FastMCP) -> AsyncIterator[AppContext]:
    """Per-session lifespan: just yield the shared global context."""
    assert _app_ctx is not None, "Global resources not initialized"
    yield _app_ctx


mcp = FastMCP("104-mcp-server", lifespan=app_lifespan)

register_auth_tools(mcp)
register_search_tools(mcp)
register_messaging_tools(mcp)
register_status_tools(mcp)
register_discovery_tools(mcp)


async def main() -> None:
    configure_logging()

    if len(sys.argv) > 1 and sys.argv[1] == SELFTEST_FLAG:
        try:
            await selftest_browser()
        except Exception as exc:
            log.error("自檢路徑失敗：%s", exc)
            sys.exit(1)
        return

    try:
        await _init_globals()
    except Exception as exc:
        log.error("104-mcp 啟動失敗：%s", exc)
        sys.exit(1)

    try:
        await mcp.run_stdio_async()
    finally:
        await _shutdown_globals()


def run() -> None:
    """Sync entry point for the `mcp104` console script (`[project.scripts]`
    in pyproject.toml) — `main()` is `async def`, and a console script needs
    a plain callable, so this is the wrapper the entry point actually
    invokes.
    """
    asyncio.run(main())


if __name__ == "__main__":
    run()
