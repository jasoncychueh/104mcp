from patchright.async_api import async_playwright, Browser, BrowserContext, Error as PatchrightError

from mcp104.browser.fingerprint import ACCEPT_LANGUAGE, USER_AGENT


_playwright = None

_MISSING_BROWSER_MARKER = "Executable doesn't exist"

_MISSING_BROWSER_MESSAGE = (
    "找不到 Chromium 執行檔。patchright 的瀏覽器快取是 user-level 的 "
    "ms-playwright 目錄，唯一會被讀取的環境變數是 PLAYWRIGHT_BROWSERS_PATH——"
    "任何一種方式在這台機器上裝過一次瀏覽器之後，這裡都會找得到。"
)


async def get_playwright():
    global _playwright
    if _playwright is None:
        _playwright = await async_playwright().start()
    return _playwright


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
    (T-113 / R3.6 restrict page access to stealth.py, cdp_stream.py and
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
