"""Shared client identity — the User-Agent and Accept-Language presented by
BOTH the login browser (browser/stealth.py's create_stealth_context) and the
plain HTTP client that issues authenticated requests after login
(browser/api_client.py). One definition, referenced from both places, so the
two clients sharing one Cloudflare bot-clearance cookie never present
contradictory identities.

Deliberately imports nothing — this is what keeps the module importable in
a browser-free environment. browser/stealth.py cannot host these constants
itself: it imports patchright at module level, and any module that imports
IT would then fail everywhere patchright isn't installed (e.g. this
project's own test suite). See structure.md's TYPE_CHECKING rule for why
that matters project-wide.

Sending a User-Agent is measured necessary: dropping it gets HTTP 403 from
Cloudflare (docs/104-site-facts.md §6b.6 — "拿掉 user-agent 會得到 403"). Sending
a matching Accept-Language is [INF] — never independently shown necessary;
it is sent only so the two clients don't disagree about locale under the
same clearance cookie.
"""

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)

ACCEPT_LANGUAGE = "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
