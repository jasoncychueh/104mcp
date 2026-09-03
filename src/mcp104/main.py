import asyncio
import logging
import os
import sys
import threading
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field

from mcp.server.fastmcp import FastMCP

from mcp104.browser.session import SessionPool
from mcp104.browser.stealth import selftest_browser, stop_playwright
from mcp104.browser.throttle import compact_state_file
from mcp104.config import Config, get_config
from mcp104.db.database import Database
from mcp104.tools.auth import (
    PendingLoginResources,
    _finalize_pending_login,
    register_auth_tools,
)
from mcp104.tools.discovery import register_discovery_tools
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from mcp104.tools.status import register_status_tools
from mcp104.web.auth_server import AuthEndpoint, resolve_auth_binding

log = logging.getLogger("104-mcp.main")


async def _stop_playwright_quietly() -> None:
    """Shared teardown step for both process-exit paths (the argv
    self-check and the normal server shutdown): stop the shared Playwright
    driver started by browser/stealth.py's get_playwright(). Idempotent on
    the stealth module's own side; swallows and logs any exception so a
    failure here never turns a clean shutdown into a non-zero exit."""
    try:
        await stop_playwright()
    except Exception:
        log.exception("Failed to stop the shared Playwright driver during shutdown")

# The argv self-check path: a fixed route that takes no destination
# argument (nothing here is configurable, so there's no knob to point at an
# attacker's endpoint), launches a real browser, navigates nowhere but
# about:blank, closes it, and exits 0 — all without writing a single byte
# to stdout. It exists so a subprocess can prove the driver pipeline
# actually starts on a host with no display server, without going through
# login() (which would leave a live Chromium and a 900s watcher running
# past the end of the test).
SELFTEST_FLAG = "--selftest-browser-stdout"


def configure_logging() -> None:
    """Point the root logger's handler at stderr and set its level, before
    any other initialization runs. Without an explicit handler, Python's
    lastResort handler silently drops every INFO-level message and stdout
    stays clean only by accident — this fixes both at once.

    Clears any handler already on the root logger first: importing this
    module runs the module-level `mcp = FastMCP(...)` statement below, and
    constructing it runs the `mcp` package's own logging.basicConfig() as a
    side effect, which attaches a handler of its own choosing before this
    function ever runs. Clearing that inherited handler here — rather than
    only adding to it — guarantees exactly one handler is in effect and
    that it targets stderr, regardless of import-time ordering."""
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
    """Startup sequence, fixed order: get_config() → create the per-user
    data directory → Database.init(account_label) → compact_state_file().
    resolve_auth_binding() is also called here, during config resolution,
    so a half-set auth binding pair fails at startup instead of producing a
    login_url that silently connects to nothing.

    Any failure here propagates to the caller, which is responsible for the
    stderr/non-zero-exit/clean-stdout contract — this function does not
    touch stdout or sys.exit itself.

    Every failure path below releases what it already opened before
    re-raising: db.init() failing (e.g. SharedDataDirectoryError from the
    account-label isolation check) still leaves aiosqlite's own connection
    open with a live, non-daemon worker thread — without closing it here,
    that thread keeps the interpreter alive past main()'s sys.exit(1), and a
    stdio MCP client sees a hang/timeout instead of the startup error.
    """
    global _app_ctx

    config = get_config()
    resolve_auth_binding(config)

    config.data_dir.mkdir(parents=True, exist_ok=True)

    db = Database(config.db_path)
    try:
        await db.init(config.account_label)
    except Exception:
        await db.close()
        raise

    compact_state_file(config.throttle_state_path)

    _app_ctx = AppContext(
        config=config,
        db=db,
        session_pool=SessionPool(),
    )


async def _shutdown_globals() -> None:
    """Cleanup global resources. Every still-pending login is torn down
    through _finalize_pending_login, which itself cancels that login's
    watcher (with a bound, so a stuck watcher cannot hang shutdown
    indefinitely) before closing the browser resources it operates on."""
    global _app_ctx
    if _app_ctx is None:
        return

    # Tear down every remaining pending login through the same
    # _finalize_pending_login path _watch_for_login and logout() already
    # use, rather than hand-rolling a partial teardown here — a hand-rolled
    # loop that only closes context/browser skips stopping the CDP stream
    # and never discards the pool's pending registration for that token.
    # _finalize_pending_login is idempotent and bounded (it cancels the
    # token's own watcher with its own timeout before touching any browser
    # resource), and it also detaches the ephemeral-port auth listener once
    # the last pending login is gone — this loop does not need to repeat
    # any of that itself.
    for token in list(_app_ctx._pending_logins.keys()):
        try:
            await _finalize_pending_login(_app_ctx, token, "process shutdown")
        except Exception:
            # One pending login's teardown must not strand the rest of this
            # loop (their browsers) or the steps after it (session_pool
            # cleanup, db close, auth_site release, stop_playwright) — log
            # and keep going rather than let a single raise abort shutdown.
            log.exception(
                "shutdown: finalize of pending login %s failed, continuing", token
            )

    _app_ctx.session_pool.cleanup_all()
    await _app_ctx.db.close()

    # Fixed-port form: the listener is held open until process shutdown
    # (this process owns it for its whole lifetime, unlike the ephemeral
    # form below). The ephemeral-port form has already detached this field
    # back to None once _pending_logins went empty, so it is None here and
    # this is skipped — an unconditional await ctx.auth_site.close() would
    # AttributeError on that (the most common) case, and idempotency on
    # close() does not help because there is no handle to call it on in
    # the first place.
    if _app_ctx.auth_site is not None:
        endpoint = _app_ctx.auth_site
        _app_ctx.auth_site = None
        await endpoint.close()

    # Last step: every Browser/BrowserContext this process opened has
    # already been closed by this point (pooled sessions no longer hold one
    # at all; the only source was each pending login's own resources, torn
    # down above via _finalize_pending_login), so it is now safe to stop
    # the shared Playwright driver they ran on top of.
    await _stop_playwright_quietly()


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
        finally:
            await _stop_playwright_quietly()
        return

    try:
        await _init_globals()
    except Exception as exc:
        log.error("104-mcp 啟動失敗：%s", exc)
        # sys.exit(1) below relies on every resource _init_globals opened
        # having already been released (see that function's own docstring)
        # so no non-daemon thread is left holding the interpreter open at
        # shutdown — that is the normal, clean path, and SystemExit
        # propagating out of main()/run() is what tests observe as the
        # process's exit code. The daemon watchdog thread started just
        # before it is a last-resort fallback only, for a future leak this
        # fix doesn't anticipate: a non-daemon thread blocks interpreter
        # shutdown *after* SystemExit has already unwound every frame, so
        # sys.exit(1) itself has no opportunity to notice or react to that —
        # only a separate thread racing the shutdown can force it via
        # os._exit(1), which skips the thread-join Python normally does on
        # exit. Being a daemon thread, the watchdog never itself holds the
        # process open when shutdown succeeds normally within the grace
        # period, so it costs nothing on the clean path. Flush the logging
        # handlers first so the error above is never lost to a hard exit.
        for handler in logging.getLogger().handlers:
            try:
                handler.flush()
            except Exception:
                pass

        def _force_exit_if_still_alive() -> None:
            time.sleep(5)
            os._exit(1)

        watchdog = threading.Thread(
            target=_force_exit_if_still_alive, daemon=True, name="startup-failure-exit-watchdog"
        )
        watchdog.start()

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
