from __future__ import annotations

import subprocess
import types
from pathlib import Path

import pytest

from mcp104.browser.stealth import launch_browser

REPO_ROOT = Path(__file__).resolve().parents[1]


def _git_grep_functional_hits(pattern: str, *paths: str) -> dict[str, list[str]]:
    """git grep for `pattern` under `paths`, returning only lines that are
    not pure comment lines (leading '#' after stripping whitespace) —
    scoped to launch parameters / runtime dependencies / process spawns,
    not to prose that happens to mention the same words. Keyed by
    repo-relative path (forward slashes) -> list of matched line text."""
    result = subprocess.run(
        ["git", "grep", "-nI", "-E", pattern, "--", *paths],
        cwd=REPO_ROOT, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    hits: dict[str, list[str]] = {}
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        # Format is "path:lineno:content"
        try:
            path, _, content = line.split(":", 2)
        except ValueError:
            continue
        if content.strip().startswith("#"):
            continue
        hits.setdefault(path.replace("\\", "/"), []).append(content)
    return hits


# =========================================================================
# T-53 (stealth.launch_browser, interface): launch args declare a full
# Chromium build and carry no display-server parameters.
# =========================================================================

@pytest.mark.asyncio
async def test_t053_launch_args_declare_full_chromium_and_exclude_display_params(
    monkeypatch,
):
    launch_calls: list[dict] = []

    class _FakeBrowser:
        async def close(self):
            pass

    class _FakeChromium:
        async def launch(self, **kwargs):
            launch_calls.append(kwargs)
            return _FakeBrowser()

    class _FakePlaywrightContextManager:
        async def start(self):
            return types.SimpleNamespace(chromium=_FakeChromium())

        async def __aenter__(self):
            return types.SimpleNamespace(chromium=_FakeChromium())

        async def __aexit__(self, *exc):
            return False

    fake_pw = types.SimpleNamespace(chromium=_FakeChromium())

    async def fake_get_playwright():
        return fake_pw

    # get_playwright() is documented (Design C3) as its own reusable
    # interface, and it turned out to cache its result at module scope
    # (a stale cached fake from another test silently answered this one
    # otherwise) — bypass that entirely by replacing get_playwright()
    # itself rather than the async_playwright() constructor it wraps.
    import mcp104.browser.stealth as stealth_mod
    monkeypatch.setattr(stealth_mod, "get_playwright", fake_get_playwright)

    await launch_browser()

    assert len(launch_calls) == 1, "launch_browser must call chromium.launch() exactly once"
    kwargs = launch_calls[0]

    # A full (non-headless-shell) Chromium build is explicitly declared.
    assert kwargs.get("channel") == "chromium"

    # No display-server parameters survive into the launch call.
    env = kwargs.get("env") or {}
    assert "DISPLAY" not in env
    args = kwargs.get("args", [])
    assert not any("display" in str(a).lower() for a in args)


# =========================================================================
# T-39 (R8.3): the "browser binary not found" error names the cause and
# the cache location, and — the reason this case exists — must NOT
# contain any install command line (no line has ever been verified to
# work for a uvx-installed user, per Design C3).
# =========================================================================

@pytest.mark.asyncio
async def test_t039_missing_browser_error_names_cause_and_location_without_a_command(
    monkeypatch,
):
    # Environment-dependent triggering (pointing PLAYWRIGHT_BROWSERS_PATH
    # at an empty dir) proved unreliable on this machine: patchright's
    # "chromium" channel resolved a browser regardless (channel installs
    # can be found outside the ms-playwright cache). Instead, drive the
    # real failure path with the exact real-world error text Playwright
    # raises for a missing executable — verified real text, not a guess —
    # so this test checks that launch_browser LAUNDERS that message
    # (strips the install command it contains) rather than merely
    # asserting on an environment condition this box doesn't reproduce.
    real_playwright_error = (
        "BrowserType.launch: Executable doesn't exist at "
        "C:\\Users\\someone\\AppData\\Local\\ms-playwright\\chromium-1091\\"
        "chrome-win\\chrome.exe\n"
        "\u2554\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2557\n"
        "\u2551 Looks like Playwright Test or Playwright was just installed or updated. \u2551\n"
        "\u2551 Please run the following command to download new browsers:    \u2551\n"
        "\u2551                                                                \u2551\n"
        "\u2551     playwright install                                        \u2551\n"
        "\u2551                                                                \u2551\n"
        "\u255a\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u2550\u255d\n"
    )

    class _FakeChromium:
        async def launch(self, **kwargs):
            raise Exception(real_playwright_error)

    class _FakePlaywrightContextManager:
        async def start(self):
            return types.SimpleNamespace(chromium=_FakeChromium())

        async def __aenter__(self):
            return types.SimpleNamespace(chromium=_FakeChromium())

        async def __aexit__(self, *exc):
            return False

    def fake_async_playwright():
        return _FakePlaywrightContextManager()

    import patchright.async_api as patchright_async_api

    monkeypatch.setattr(patchright_async_api, "async_playwright", fake_async_playwright)
    import mcp104.browser.stealth as stealth_mod
    if hasattr(stealth_mod, "async_playwright"):
        monkeypatch.setattr(stealth_mod, "async_playwright", fake_async_playwright)

    with pytest.raises(Exception) as exc_info:
        await launch_browser()

    message = str(exc_info.value)

    # Must say where the cache lives and which env var controls it.
    assert "ms-playwright" in message
    assert "PLAYWRIGHT_BROWSERS_PATH" in message

    # Must NOT hand the caller a command line to run — the half this
    # case exists to guard, per Design C3 / Error Scenario 8.
    lowered = message.lower()
    for forbidden in (
        "pip install", "playwright install", "patchright install",
        "npx ", "uvx ", "python -m patchright",
    ):
        assert forbidden not in lowered, (
            f"error message must not contain an install command, found "
            f"{forbidden!r} in: {message}"
        )


# =========================================================================
# T-78 (R1.3): the login path has no display-server dependency anywhere —
# not in launch parameters, not in spawned processes, not in the runtime
# dependency set. Scoped to functional code, not comments/prose that
# merely mention the retired noVNC design (a documentation-freshness
# concern, not a code dependency).
# =========================================================================

def test_t078_login_path_has_no_display_server_dependency():
    login_path_files = [
        "src/mcp104/browser/stealth.py",
        "src/mcp104/browser/cdp_stream.py",
        "src/mcp104/tools/auth.py",
        "src/mcp104/main.py",
    ]
    pattern = r"Xvfb|x11vnc|novnc|noVNC|DISPLAY|xvfb-run"
    hits = _git_grep_functional_hits(pattern, *login_path_files)
    assert hits == {}, f"display-server reference found on the login path: {hits}"


# =========================================================================
# T-113 (R3.6): outside the login path, nothing in src/mcp104/ touches a
# browser page — an explicit, file-named allowlist.
# =========================================================================

_ALLOWED_PAGE_ACCESS_FILES = {
    "src/mcp104/browser/stealth.py",
    "src/mcp104/browser/cdp_stream.py",
    "src/mcp104/tools/auth.py",
}


def test_t113_no_browser_page_access_outside_the_login_path_allowlist():
    # Deliberately scoped to actual page/browser operations, not import
    # statements — a TYPE_CHECKING-only `from patchright... import` for
    # type annotations is not "operating on a page" (Test Approach: the
    # whole suite must not require a browser to be installed, which this
    # kind of guarded import exists to make possible).
    pattern = r"\.goto\(|\.new_page\(|\.new_context\(|new_cdp_session\(|\.screencast"
    hits = _git_grep_functional_hits(pattern, "src/mcp104")
    offending = set(hits) - _ALLOWED_PAGE_ACCESS_FILES
    assert offending == set(), (
        f"browser/page access found outside the login-path allowlist "
        f"({sorted(_ALLOWED_PAGE_ACCESS_FILES)}): "
        f"{ {k: v for k, v in hits.items() if k in offending} }"
    )
