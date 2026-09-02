"""無顯示層的瀏覽器啟動與收尾：`launch_browser` 開一顆 headless（或帶畫面，供 CDP
登入串流使用）的 stealth patchright Chromium；`create_stealth_context` 建立套用反偵測
設定（user agent、locale、timezone）的 context；`selftest_browser` 是
`--selftest-browser-stdout` 這條 argv 路徑的實作本體。`get_playwright`/`stop_playwright`
管理行程內唯一一份 `Playwright` 物件的生命週期——啟動與關閉的接線（何時呼叫
`stop_playwright`）屬於呼叫端（`main.py`），這個模組只提供函式。"""

import asyncio

from patchright.async_api import async_playwright, Browser, BrowserContext, Error as PatchrightError

from mcp104.browser.fingerprint import ACCEPT_LANGUAGE, USER_AGENT


_playwright = None
# Guards get_playwright()'s check-then-start against a concurrent caller
# racing it: without this, two coroutines can both observe _playwright is
# None and both call async_playwright().start(), leaking the first
# Playwright instance's driver process. The lock only protects the
# start-once decision, not every call — once _playwright is set, further
# calls return immediately without ever acquiring it.
#
# Created lazily, on first entry into get_playwright()/stop_playwright(),
# rather than at module import time. The lock object itself is created by
# a check-then-create on this module-level variable, same shape as the
# _playwright check-then-start it guards — but that inner check-then-create
# needs no lock of its own: under single-threaded asyncio, nothing can
# preempt between the `is None` check and the assignment except an `await`,
# and there is none in between, so no other coroutine can observe the
# half-initialized state.
_playwright_lock: asyncio.Lock | None = None

_MISSING_BROWSER_MARKER = "Executable doesn't exist"

_MISSING_BROWSER_MESSAGE = (
    "找不到 Chromium 執行檔。patchright 的瀏覽器快取是 user-level 的 "
    "ms-playwright 目錄，唯一會被讀取的環境變數是 PLAYWRIGHT_BROWSERS_PATH——"
    "任何一種方式在這台機器上裝過一次瀏覽器之後，這裡都會找得到。"
)


async def get_playwright():
    global _playwright, _playwright_lock
    if _playwright is None:
        if _playwright_lock is None:
            _playwright_lock = asyncio.Lock()
        async with _playwright_lock:
            # Re-check inside the lock: another caller may have finished
            # start()-ing between the check above and acquiring the lock.
            if _playwright is None:
                _playwright = await async_playwright().start()
    return _playwright


async def stop_playwright() -> None:
    """Stop the process-wide Playwright driver, if one was ever started.
    Idempotent: a second call after the first (or a call when
    `get_playwright` was never called at all) is a no-op. Callers are
    responsible for closing any Browser/BrowserContext they hold BEFORE
    calling this — stopping the driver out from under a still-open
    browser is not this function's job to prevent."""
    global _playwright, _playwright_lock
    if _playwright_lock is None:
        _playwright_lock = asyncio.Lock()
    async with _playwright_lock:
        if _playwright is not None:
            await _playwright.stop()
            _playwright = None


async def launch_browser(headless: bool = True) -> Browser:
    """Launch a stealth patchright Chromium browser.

    Args:
        headless: If False, launches with a visible window (still no
            display-server dependency — the CDP login stream carries the
            picture, not an X11 display).
    """
    pw = await get_playwright()
    try:
        browser = await pw.chromium.launch(
            headless=headless,
            channel="chromium",
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-dev-shm-usage",
            ],
        )
    except Exception as e:
        if _MISSING_BROWSER_MARKER in str(e):
            raise PatchrightError(_MISSING_BROWSER_MESSAGE) from e
        raise
    return browser


async def create_stealth_context(browser: Browser) -> BrowserContext:
    """Create a browser context with anti-detection settings."""
    context = await browser.new_context(
        viewport={"width": 1920, "height": 1080},
        locale="zh-TW",
        timezone_id="Asia/Taipei",
        # Shared with the plain HTTP client (browser/api_client.py) via
        # browser/fingerprint.py — one definition, so the browser and the
        # HTTP client never present contradictory identities under the same
        # bot-clearance cookie. See that module's docstring for provenance.
        user_agent=USER_AGENT,
        extra_http_headers={
            "Accept-Language": ACCEPT_LANGUAGE,
        },
    )
    return context


async def selftest_browser() -> None:
    """Body of the `--selftest-browser-stdout` argv path (see
    `main.py`'s SELFTEST_FLAG docstring for what the path must and must not
    do). Lives here rather than in main.py because it is the one caller
    outside the login path that legitimately needs to touch a browser page
    (page access is restricted to stealth.py, cdp_stream.py and
    tools/auth.py). Writes nothing to stdout/disk; failures propagate to
    the caller unchanged."""
    browser = await launch_browser(headless=True)
    try:
        context = await create_stealth_context(browser)
        try:
            page = await context.new_page()
            try:
                await page.goto("about:blank")
            finally:
                await page.close()
        finally:
            await context.close()
    finally:
        await browser.close()
