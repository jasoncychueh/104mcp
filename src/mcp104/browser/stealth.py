from patchright.async_api import async_playwright, Browser, BrowserContext

from mcp104.browser.fingerprint import ACCEPT_LANGUAGE, USER_AGENT


_playwright = None


async def get_playwright():
    global _playwright
    if _playwright is None:
        _playwright = await async_playwright().start()
    return _playwright


async def launch_browser(headless: bool = True, display: str | None = None) -> Browser:
    """Launch a stealth patchright Chromium browser.

    Args:
        headless: If False, requires DISPLAY env var (Xvfb).
        display: X11 display (e.g. ":99"). Only used when headless=False.
    """
    import os
    env = None
    if not headless and display:
        env = {**os.environ, "DISPLAY": display}

    pw = await get_playwright()
    browser = await pw.chromium.launch(
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
            "--disable-dev-shm-usage",
        ],
        env=env if not headless else None,
    )
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
