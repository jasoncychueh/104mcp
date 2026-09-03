"""Endpoint declarations and HTTP execution for 104's JSON API.

Answers: which host and path does each read tool call, what does its
response envelope look like when it succeeds or fails, and how is a
request actually issued. Deliberately does NOT decide what a failure means
to the caller (tools/helpers.py's `_issue_one` — the per-request unit
shared by `guarded_api` and `guarded_sequence` — owns turning a Verdict
into the payload an MCP tool returns) and does NOT know about sessions, the
throttle, or the browser context that owns the cookies (browser/session.py
and tools/helpers.py own those).

Every endpoint is a complete declaration — see the Endpoint dataclass below
for the full field list, no field of which has a default (see that class's
own docstring for why). Each property below has, at least once, been
assumed global before a live measurement showed it varies per endpoint,
and every one of those wrong assumptions failed silently rather than
raising:

- host:  hitting the wrong host returns HTTP 404 carrying a marketing HTML
  page (docs/104-site-facts.md §6b.6-pre) — nothing about that response
  says "wrong host". `/vipapi/*`, `/bc-comm/*`, `/job-api/*` live on
  auth.vip.104.com.tw; `/api/*` exists on both hosts.
- family: the two response shapes fail invisibly in OPPOSITE directions if
  collapsed into one check (see classify()'s docstring below).
- extra_headers: three of the four family-A routes (search, the
  résumé-count route, match) answer HTTP 200 with their own ACCESS_DENIED
  status when no Referer is sent; only recommend tolerates its absence —
  a per-endpoint requirement, not a site-wide one, and not simply "the one
  route someone happened to check first" (docs/104-site-facts.md §6b.3h,
  corrected 2026-08-14 — see the comment above ENDPOINTS for how the
  earlier, narrower conclusion arose).
- method: a construction-time whitelist ({"GET", "POST"}) is the only
  thing standing between "an endpoint declaration" and "an endpoint that
  can delete a real conversation" — docs/104-site-facts.md §6b.9-2 records
  two DELETE routes on the same messaging API surface this module talks
  to, and the whitelist makes them unconstructible rather than merely
  undocumented.

No field of `Endpoint` has a default. That is the mechanism that makes
"forgot to declare a property" a construction-time TypeError instead of a
silent None — a property added later inherits the same treatment without
this paragraph needing to be revisited.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Sequence
from urllib.parse import quote, urlencode

import aiohttp

from mcp104.browser.fingerprint import ACCEPT_LANGUAGE, USER_AGENT

# ── Host resolution ──────────────────────────────────────────────────────
#
# "vip" / "auth" are the two tokens Endpoint.host is allowed to carry — see
# the module docstring for why hitting the wrong one of these two actual
# hostnames fails silently. [M docs/104-site-facts.md §6b.6-pre]
_HOST_NAMES = {
    "vip": "vip.104.com.tw",
    "auth": "auth.vip.104.com.tw",
}

# Site-root Referer, sufficient for every route that checks it at all —
# the check is coarse-grained (station-level, not page-accurate), so one
# constant value serves all of them. [M §6b.3h: "只給站台根目錄就夠"]
REFERER_SITE_ROOT = "https://vip.104.com.tw/"

# The only two HTTP methods any Endpoint may declare — enforced in
# Endpoint.__post_init__, not merely documented. This is what makes the two
# DELETE routes recorded in docs/104-site-facts.md §6b.9-2
# (deleteMessage/deleteMessageList) unconstructible: a caller cannot build
# an Endpoint("DELETE", ...) at all, which is a stronger guarantee than a
# comment asking people not to add them.
_ALLOWED_METHODS = frozenset({"GET", "POST"})

# The only three values Endpoint.family may declare — enforced in
# Endpoint.__post_init__, the same construction-time whitelist as
# _ALLOWED_METHODS above. "A" and "B" dispatch classify() to the two JSON
# envelope shapes; "opaque" names a route measured to answer outside
# either envelope (currently only logout_session) and gets its own
# explicit branch in classify() rather than falling through into either
# family's shape-specific parsing.
_ALLOWED_FAMILIES = frozenset({"A", "B", "opaque"})

# 15 seconds because that was page.goto's own navigation timeout on the
# now-removed page-navigation guard (guarded_page) this value was carried
# over from without reason to diverge, and it is kept unchanged here for
# the same reason: combined with the interval floor in browser/throttle.py
# (MIN_CALL_INTERVAL_SECONDS), a call's worst case inside the session lock
# is the floor plus this timeout, which must stay under the MCP client's
# own default request timeout — a client that gives up while 104 is still
# answering reports a failure that did not happen (measured once already,
# on read_messages, before MAX_INLINE_WAIT_SECONDS existed).
FETCH_TIMEOUT_SECONDS = 15.0

# 3xx statuses fetch() reports via RawResponse.location rather than
# following — see fetch()'s docstring for why redirects are never followed.
_REDIRECT_STATUSES = frozenset({301, 302, 303, 307, 308})

# The company-switch redirect target. Its presence anywhere in a response
# body means "session expired", regardless of whether it arrived inside a
# JSON envelope's `url` field or inside an HTML <script> redirect — the two
# measured expiry shapes share only this string. [M §6b.3e, §6b.3i]
EXPIRY_MARKER = "/company/status/switchCompany"


@dataclass(frozen=True)
class FamilyBShape:
    """What must be present under a family-B response's `data` for an HTTP
    200 to count as healthy — the structural floor beneath the HTTP status
    that classify() checks for family B. The two measured family-B
    endpoints do NOT share a shape: get_resume_detail's `data` is a
    mapping containing a `resume` key (plus its own nested `error`, null
    when healthy); list_jobs' `data` IS the row list directly, with no
    further inner key and no nested `error` field to check.
    [M research/captures/resume_correct_host.json, reco_match_bodies.json]

    No field has a default: a family-B endpoint built without one fails at
    Endpoint construction (see Endpoint.__post_init__ below), which is
    earlier and louder than failing the first time classify() happens to
    see that endpoint's response. `sibling_keys` follows the same
    discipline for the same reason — every FamilyBShape(...) construction
    site under src/, tests/, and research/probes/ must state its sibling
    set explicitly, even when that set is empty (`()`).

    `sibling_keys` names top-level envelope keys that ride ALONGSIDE
    `data`/`metadata` — not under either of them — and that classify()
    should copy into the Verdict's payload when they are present. The
    motivating case is `failed`, the outbound-contact event/message
    routes' own per-recipient failure list [M docs/104-site-facts.md
    §8.13/§8.17]: it is a THIRD top-level key, not a `data` or `metadata`
    field, so neither the is_list branch nor the inner_key branch below
    ever sees it. Declaring "data" or "metadata" here is a construction
    error (__post_init__) — those two keys already have their own,
    single, extraction path (the is_list/inner_key branches), and letting
    them ALSO ride in sibling_keys would give one envelope key two
    different ways to reach the tool layer, which is exactly the kind of
    duplicate-source-of-truth this module's construction-time checks exist
    to rule out before a test ever has to catch it at runtime.
    """

    is_list: bool
    inner_key: str | None  # required key under `data` when is_list is False; ignored (must be None) when is_list is True
    sibling_keys: tuple[str, ...]  # top-level envelope keys (besides data/metadata) to copy into the payload when present and the verdict is ok — never "data" or "metadata" (see class docstring)

    def __post_init__(self) -> None:
        for reserved in ("data", "metadata"):
            if reserved in self.sibling_keys:
                raise ValueError(
                    f"FamilyBShape.sibling_keys must not contain {reserved!r} "
                    "— it already has its own extraction path"
                )


@dataclass(frozen=True)
class Endpoint:
    """One entry of ENDPOINTS. No field below has a default — every call
    site that builds one must supply every field explicitly, which is the
    point (see module docstring). `family_b_shape` is no exception: it is
    validated against `family` in __post_init__ rather than left to
    whatever classify() happens to fall back on for an endpoint it was
    never told about. `method` is validated the same way, against
    _ALLOWED_METHODS.

    `extra_headers` is a name/value pair sequence, not a `frozenset[str]` of
    names: `fetch()` emits every declared pair on the wire VERBATIM, so what
    an endpoint declares is exactly what it sends and a test can assert
    that directly — a name-only set could declare a header this module has
    no value source for, which would read as enforcement and silently send
    nothing (the exact "filter that looks applied and does nothing" shape
    this project keeps re-discovering). Tuple-of-pairs, not a dict: same
    reason build_url's own `params` is a sequence, not a dict — ordering is
    preserved and a frozen dataclass need not worry about dict hashability.
    """

    key: str
    host: str  # "vip" | "auth"
    path: str  # may contain `{name}` placeholders filled from build_url's params
    method: str  # "GET" | "POST" — see _ALLOWED_METHODS
    family: str  # "A" | "B" — dispatches classify(); "opaque" names a route measured to answer outside either JSON envelope (e.g. logout_session) — classify() is never reached for it in practice, see that endpoint's own comment
    extra_headers: tuple[tuple[str, str], ...]  # NAME/VALUE pairs sent verbatim, beyond the always-sent baseline (User-Agent, Accept-Language, Cookie)
    family_b_shape: FamilyBShape | None  # required (non-None) iff family == "B" — enforced below, not merely documented
    throttle_gated: bool  # whether this route passes through enforce_throttle's judgment gate (tools/helpers.py's guarded_api/guarded_sequence) — every row must answer this explicitly; the sole False today is logout_session (see ENDPOINTS below for why it qualifies for the exemption)

    def __post_init__(self) -> None:
        if self.method not in _ALLOWED_METHODS:
            raise ValueError(f"Endpoint {self.key!r}: method must be one of {sorted(_ALLOWED_METHODS)}, got {self.method!r}")
        if self.family not in _ALLOWED_FAMILIES:
            raise ValueError(f"Endpoint {self.key!r}: family must be one of {sorted(_ALLOWED_FAMILIES)}, got {self.family!r}")
        if self.family == "B" and self.family_b_shape is None:
            raise ValueError(f"Endpoint {self.key!r}: family 'B' requires family_b_shape")
        if self.family != "B" and self.family_b_shape is not None:
            raise ValueError(f"Endpoint {self.key!r}: family_b_shape is only meaningful for family 'B'")


# Tool-facing name -> Endpoint. Keys match the five read tools' own names
# (tools/search.py, a later phase) so a caller never has to know the
# underlying path.
#
# Family assignment: [M §6b.3e "但是：信封有兩種"] — search / recommend / match
# all carry {status, message, result, url} (family A); résumé detail and
# the job list carry {data, metadata} with no status key at all (family B).
#
# Referer requirement, one call per route per header configuration
# [M §6b.3h, corrected 2026-08-14 — an earlier pass here read only
# `getSearchRsNum`, whose two prior calls had both already carried a
# Referer by accident, and never actually called `searchResult` without
# one; that produced the wrong "only match needs it" conclusion this
# comment used to state]:
#
#   route                          | no Referer     | with Referer
#   /api/search/searchResult       | ACCESS_DENIED  | SUCCESS
#   /api/search/getSearchRsNum     | ACCESS_DENIED  | SUCCESS (not one of our 5 tools' endpoints; measured for completeness)
#   /api/recommend/resumeListAll   | SUCCESS         | SUCCESS (identical either way)
#   /api/match/matchResult         | ACCESS_DENIED  | SUCCESS
#
# Three of four require it; recommend is measured harmless either way, on
# a single call each direction — the same evidence weight the earlier,
# wrong "only match needs it" conclusion rested on. All THREE of these
# vip-host family-A routes therefore declare Referer here, uniformly,
# rather than carrying the (thinly-evidenced) exception for recommend:
# declaring the same value on every endpoint is still a per-endpoint,
# individually-stated declaration — this table has no entry that
# defaults or is inherited — it is simply a table whose three measured
# values happen to agree, and it removes a configuration state (Referer
# omitted) that produces the exact silent-looking failure this table
# exists to prevent (HTTP 200 + ACCESS_DENIED) on a route where nothing
# but time separates "measured harmless" from "measured harmless so far".
#
# NOT extended to get_resume_detail / list_jobs below: those are a
# different host (auth.vip.104.com.tw) that this measurement never
# touched, so leaving their extra_headers empty is "not measured",
# not "measured unnecessary" — an inference this project's own standing
# rule (never extend a measurement past what it covers) forbids drawing
# on their behalf. See docs/104-site-facts.md §6b.3h for the note
# recording this implementation chose per-route declaration on the
# measured host over sending Referer to literally every request.
ENDPOINTS: dict[str, Endpoint] = {
    "search_resumes": Endpoint(
        key="search_resumes",
        host="vip",
        path="/api/search/searchResult",
        method="GET",
        family="A",
        extra_headers=(("Referer", REFERER_SITE_ROOT),),
        family_b_shape=None,
        throttle_gated=True,
    ),
    "list_recommended_resumes": Endpoint(
        key="list_recommended_resumes",
        host="vip",
        path="/api/recommend/resumeListAll",
        method="GET",
        family="A",
        extra_headers=(("Referer", REFERER_SITE_ROOT),),
        family_b_shape=None,
        throttle_gated=True,
    ),
    "list_matched_resumes": Endpoint(
        key="list_matched_resumes",
        host="vip",
        path="/api/match/matchResult",
        method="GET",
        family="A",
        extra_headers=(("Referer", REFERER_SITE_ROOT),),
        family_b_shape=None,
        throttle_gated=True,
    ),
    "get_resume_detail": Endpoint(
        key="get_resume_detail",
        host="auth",
        path="/vipapi/resume/search/{idno}",
        method="GET",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=False, inner_key="resume", sibling_keys=()),
        throttle_gated=True,
    ),
    "list_jobs": Endpoint(
        key="list_jobs",
        host="auth",
        path="/job-api/jobs/recommend",
        method="GET",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=()),
        throttle_gated=True,
    ),
    # ── Messaging (§6b.7-§6b.9): all three on auth.vip.104.com.tw, all
    # family B ({data, metadata}). extra_headers=() on all three is
    # deliberately "not measured", not "measured unnecessary" — the
    # Referer measurement above covered vip.104.com.tw only and was never
    # extended to this host, the same reasoning that already keeps
    # get_resume_detail/list_jobs's extra_headers empty. If a live call
    # ever returns an auth-shaped or denied response, the remedy is a
    # per-header, per-route measurement (research/probes/
    # probe_referer_per_route.py's pattern), never adding headers until it
    # works.
    "read_messages": Endpoint(
        key="read_messages",
        host="auth",
        path="/bc-comm/message/all-stream",
        method="POST",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=()),
        throttle_gated=True,
    ),
    "get_conversation": Endpoint(
        key="get_conversation",
        host="auth",
        path="/bc-comm/message/{job_no}-{p_id}",
        method="GET",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=()),
        throttle_gated=True,
    ),
    # family_b_shape is [INF], not [M]: only 104's own front-end reading
    # response.data[0].messageId[0] [C §6b.9-2] says `data` is a list — the
    # reject-and-read measurement [M §6b.9-4] could only trigger failures,
    # never reach a success envelope. See tools/messaging.py's
    # _send_verdict for how a shape surprise here is prevented from turning
    # a real send into a reported failure: a "malformed" Verdict lands on
    # the AMBIGUOUS side of that mapping, so it reports "unconfirmed"
    # rather than "failed".
    "send_message": Endpoint(
        key="send_message",
        host="auth",
        path="/bc-comm/message/{job_no}-{p_id}",
        method="POST",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=("failed",)),
        throttle_gated=True,
    ),
    # ── Outbound contact (§8.13-§8.19): four routes newly declared for
    # send_inquiry / list_templates, all on auth.vip.104.com.tw, all
    # family B. extra_headers=() on all four for the same "not measured,
    # not measured unnecessary" reason as the three messaging routes
    # above — this table has never extended the vip-only Referer
    # measurement to this host. get_template (GET
    # /bc-comm/template/{id}) is deliberately NOT declared here: nothing
    # in this project calls it (list_templates' rows already carry the
    # full template body; send_inquiry sends template_id verbatim without
    # looking it up) and an endpoint with no call site is a standing
    # invitation for the next reader to assume one exists.
    "list_templates": Endpoint(
        key="list_templates",
        host="auth",
        path="/bc-comm/template",
        method="GET",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=()),
        throttle_gated=True,
    ),
    # resolve_candidate_idno: the reverse bridge send_inquiry's first
    # sub-request uses to turn the pId the caller supplied into the idNo
    # the event route's wire body actually wants [M §8.14-2 #3]. inner_key
    # ="idNo" is a real (true-valued) structural floor, not decoration:
    # this request exists FOR that one field — losing it here, at the
    # transport layer, is earlier and louder than a tool-level `if` a few
    # frames up the stack. [M §8.15] measured its 404 shape: an
    # unrecognised pId answers {"code": "00004", "message": "找不到對應資源",
    # "detail": []} — the same family-B {code, message, detail: []} shape
    # _classify_family_b already dispatches by HTTP STATUS, not by code —
    # deliberately: message/info's 404 for "conversation doesn't exist
    # yet" is a different code (00207) and an unrecognised template id is
    # a third (00802, §8.15) — three different codes for the same status,
    # which is exactly why nothing here keys off any of them.
    "resolve_candidate_idno": Endpoint(
        key="resolve_candidate_idno",
        host="auth",
        path="/bc-comm/message/resume/{job_no}-{p_id}",
        method="GET",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=False, inner_key="idNo", sibling_keys=()),
        throttle_gated=True,
    ),
    # event_last_info: send_inquiry's second sub-request, taken only for
    # data.emailCC. inner_key=None (not "emailCC"): _classify_family_b's
    # inner_key check is a TRUTHY check, and the measured shape for
    # emailCC can legitimately be an empty list — treating that as a
    # missing-key failure would misclassify a healthy, empty-CC response
    # as malformed. `data` still has to be an object; that much is
    # unconditional in _classify_family_b regardless of inner_key.
    "event_last_info": Endpoint(
        key="event_last_info",
        host="auth",
        path="/bc-comm/event/last-info",
        method="GET",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=False, inner_key=None, sibling_keys=()),
        throttle_gated=True,
    ),
    # send_willingness_event: send_inquiry's third and only send-bearing
    # sub-request — the one that reaches _send_verdict. sibling_keys=
    # ("failed",) for the same reason as send_message above: `failed` is
    # a third top-level envelope key riding alongside `data`/`metadata`,
    # measured on this exact route [§8.13/§8.17], and the send-outcome
    # classification in tools/messaging.py needs to see it (present vs.
    # absent, not merely truthy) to tell "confirmed" apart from
    # "unconfirmed".
    "send_willingness_event": Endpoint(
        key="send_willingness_event",
        host="auth",
        path="/bc-comm/event/willingness",
        method="POST",
        family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=("failed",)),
        throttle_gated=True,
    ),
    # ── Restore verification ─────────────────────────────────────────────
    #
    # verify_session: an authenticated no-op call used only to prove a
    # restored cookie jar is still alive. Route and shape are the same
    # station as search_resumes (vip.104.com.tw, family A, needs Referer)
    # — this is the résumé-COUNT route, not a résumé read, and it is
    # measured to cost zero of the daily résumé-browsing quota
    # [M §6b.3g-inline / §8.8-1]. The ten parameters the browser itself
    # sends on this route are measured unnecessary: an unparameterised
    # call gets the identical HTTP 200 + parseable JSON + family-A
    # SUCCESS as the fully-parameterised one, under the same cookie jar
    # [M §8.8-1]. Referer is NOT part of that "nothing needed" finding —
    # omitting it still gets ACCESS_DENIED on this route like the other
    # three vip family-A routes [M §6b.3h] — so it stays declared.
    "verify_session": Endpoint(
        key="verify_session",
        host="vip",
        path="/api/search/getSearchRsNum",
        method="GET",
        family="A",
        extra_headers=(("Referer", REFERER_SITE_ROOT),),
        family_b_shape=None,
        throttle_gated=True,
    ),
    # logout_session: the one best-effort, fire-and-forget server-side
    # logout request. Measured [M §8.8-4], not assumed: `GET` gets a
    # 302 (empty body) to boidc.104.com.tw, whose chain ends on an HTML
    # error page; `POST` (empty body) is 404 HTML. Neither is a JSON
    # envelope of either family, hence family="opaque" — a value
    # classify() never actually dispatches on here, because `_issue_one`'s
    # auth-host redirect check (tools/helpers.py's per-request unit shared
    # by guarded_api and guarded_sequence) intercepts this route's 302
    # (Location -> boidc.104.com.tw) before classify() is ever called, and always has
    # in every measured run of this route. `method="GET"` per the same
    # measurement (`POST` is 404, not accepted). Post-hoc verification
    # (three send styles, one shared session) found the vip application
    # session (its/ithp) unaffected in all three — this call has no
    # observed effect on vip.104.com.tw, so its purpose is to send the
    # attempt and record that we tried, not to reach a confirmed
    # server-side logout. throttle_gated=False: the sole exemption from
    # enforce_throttle's judgment gate — note_request still counts this
    # request (the gate, not the ledger, is what's waived). This route
    # qualifies for the exemption on its own properties: one tool call
    # issues exactly one request, it is never retried automatically, and
    # refusing it would leave 104's side in a state the operator does not
    # expect (logged out there, still treated as logged in here).
    "logout_session": Endpoint(
        key="logout_session",
        host="vip",
        path="/oidc/logout",
        method="GET",
        family="opaque",
        extra_headers=(),
        family_b_shape=None,
        throttle_gated=False,
    ),
    # Never declared, deliberately — an endpoint absent from ENDPOINTS
    # cannot be called at all, which is the enforcement (the method
    # whitelist above covers the two DELETE routes a second way):
    #   - POST .../acknowledgement (已讀回報) — the account holder's ruling
    #     is that no tool ever sends this (see CLAUDE.md); this comment is
    #     meant to be the only place this route's name appears anywhere
    #     under src/ (tests/test_messaging.py sweeps for that).
    #   - DELETE .../{msgId} and DELETE .../{jobNo}-{pId} — delete real
    #     conversation history; unconstructible anyway per _ALLOWED_METHODS.
    #   - GET /bc-comm/event/{jobNo}-{pId} — does NOT enumerate a
    #     conversation's events (measured: 1 of 4 event-bearing messages
    #     returned, selection criterion unmeasured, §6b.10-1) and nothing
    #     needs it: a message's own `event` field already carries the
    #     complete, identically-shaped object.
}


def hostname_for(endpoint: Endpoint) -> str:
    """The actual FQDN for `endpoint.host`. Pure; exists so callers outside
    this module (tools/helpers.py's cookie selection) never have to know
    the "vip"/"auth" token mapping themselves — one place decides it."""
    return _HOST_NAMES[endpoint.host]


def build_url(endpoint: Endpoint, params: Sequence[tuple[str, str]] | None = None) -> str:
    """Pure. Build the full request URL for `endpoint`.

    `params` is an ORDERED sequence of (key, value) pairs, not a dict —
    tools/filters.py's encode_filters() (a later phase) emits filters this
    way specifically to preserve wire submission order and support
    repeated keys (the `edu[]`-style array encoding), both of which a dict
    would silently collapse.

    Any `{name}` placeholder in endpoint.path (currently only
    get_resume_detail's `{idno}`) is filled from the matching entry in
    `params` and does NOT also appear in the query string; every other
    entry becomes a literal query parameter, in the order given, so a
    repeated key produces a repeated query parameter rather than being
    silently deduplicated.
    """
    host = hostname_for(endpoint)
    items = list(params or [])
    placeholder_names = set(re.findall(r"\{(\w+)\}", endpoint.path))

    path_values: dict[str, str] = {}
    query_items: list[tuple[str, str]] = []
    for key, value in items:
        if key in placeholder_names and key not in path_values:
            path_values[key] = value
        else:
            query_items.append((key, value))

    missing = placeholder_names - path_values.keys()
    if missing:
        raise ValueError(
            f"build_url: endpoint {endpoint.key!r} path requires "
            f"{sorted(missing)}, not supplied in params"
        )

    path = endpoint.path.format(**{k: quote(str(v), safe="") for k, v in path_values.items()})
    url = f"https://{host}{path}"
    if query_items:
        url += "?" + urlencode(query_items)
    return url


def select_cookies_for_host(cookies: Sequence[dict], host: str) -> str:
    """Pure. Build a `Cookie:` header value from BrowserContext.cookies()
    dicts (each carrying at least "name", "value", "domain"), including
    only those whose `domain` covers `host`.

    The leading dot on `domain` is load-bearing, not cosmetic: Chromium
    (and so Playwright/patchright's cookies(), which has no separate
    host-only flag — see patchright._impl._api_structures.Cookie) reports
    a HOST-ONLY cookie's domain as the bare hostname with no leading dot,
    and a cookie carrying an explicit Domain attribute (subdomain-wide) as
    that domain WITH a leading dot, regardless of how the Set-Cookie
    header itself was written. A host-only cookie therefore matches ONLY
    an exact hostname; a leading-dot cookie matches that domain and any
    subdomain. Collapsing the distinction (stripping the dot before
    comparing) would let a cookie scoped host-only to vip.104.com.tw leak
    into a request to auth.vip.104.com.tw — a different host under the
    same "*.104.com.tw" strings-look-similar family. This is why
    `_issue_one` (tools/helpers.py's per-request unit shared by
    guarded_api and guarded_sequence) must re-read cookies per request
    rather than cache a single header string across both hosts it may
    need to call: which cookies apply differs by host, not just by
    session.
    """
    parts = []
    for cookie in cookies:
        domain = cookie.get("domain", "")
        if domain.startswith("."):
            bare = domain[1:]
            matches = host == bare or host.endswith("." + bare)
        else:
            matches = host == domain
        if matches:
            parts.append(f"{cookie['name']}={cookie['value']}")
    return "; ".join(parts)


@dataclass(frozen=True)
class RawResponse:
    """What fetch() hands to classify() — everything classify() needs and
    nothing it has to fetch itself, which is what keeps classify() pure and
    testable against captured fixtures with no HTTP involved.

    `parsed_json` is a best-effort parse of `body` performed WITHOUT regard
    to the declared Content-Type — the API serves JSON under
    `text/html` on at least one measured path (docs/104-site-facts.md
    §6b.3e), so trusting the header would reject a real JSON body. `body`
    is kept as raw text alongside the parse result because two of the
    failure scenarios (the Cloudflare challenge screen and the HTML-script
    expiry redirect) are never valid JSON and must still be inspectable as
    text.
    """

    status: int
    location: str | None  # Location header, only when status is a 3xx we did not follow
    content_type: str | None  # declared header value, verbatim — used only for error messages, never for parse decisions
    body: str
    parsed_json: object | None  # dict | list | None


async def fetch(
    endpoint: Endpoint,
    *,
    cookie_header: str,
    params: Sequence[tuple[str, str]] | None = None,
    body: dict | None = None,
) -> RawResponse:
    """Issue exactly one HTTP request for `endpoint` — GET or POST per
    `endpoint.method`. Redirects are NOT followed: following one into an
    HTML login page and parsing it as JSON is exactly the silent
    substitution this whole design exists to prevent. The unfollowed 3xx
    body is empty, so the redirect's Location header (read by
    `_issue_one`, not here — see that function's docstring for why the
    auth-host check does not belong in this module) is surfaced via
    RawResponse.location instead.

    `body` is only meaningful for a POST endpoint — the method/body
    mismatch check (a body handed to a GET endpoint, or a POST endpoint
    called with body=None) lives in tools/helpers.py's `_issue_one` (the
    per-request unit shared by guarded_api and guarded_sequence), ahead
    of the `try` that wraps this call, not here: it is a call-time check
    (the body does not exist at Endpoint construction, so no
    __post_init__ can see it) and it must not be swallowed by
    `_issue_one`'s broad `except Exception` around fetch(), which would
    report a caller bug as a transient network failure.

    `Content-Type: application/json` is never declared in
    `endpoint.extra_headers` — aiohttp sets it as a consequence of
    `json=body` on a POST, so declaring it would both duplicate a fact the
    method already determines and imply a measurement (of what the SERVER
    requires) that was never taken.
    """
    url = build_url(endpoint, params)
    headers = {
        "User-Agent": USER_AGENT,
        "Accept-Language": ACCEPT_LANGUAGE,
        "Cookie": cookie_header,
    }
    for name, value in endpoint.extra_headers:
        headers[name] = value

    timeout = aiohttp.ClientTimeout(total=FETCH_TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        if endpoint.method == "POST":
            request_cm = session.post(url, headers=headers, json=body, allow_redirects=False)
        else:
            request_cm = session.get(url, headers=headers, allow_redirects=False)
        async with request_cm as response:
            status = response.status
            content_type = response.headers.get("Content-Type")
            location = response.headers.get("Location") if status in _REDIRECT_STATUSES else None
            body_bytes = await response.read()

    # A NEW local, body_text — never rebinding the `body` PARAMETER (the
    # request body) to hold the response text. One slot, two meanings is a
    # shape this project avoids elsewhere for the same reason: it is
    # harmless today only because nothing after this point still needs the
    # request body, which is exactly the kind of fact a future edit can
    # quietly stop being true without anyone noticing the rename would
    # then be load-bearing.
    body_text = body_bytes.decode("utf-8", errors="replace")
    parsed_json = None
    if body_text:
        try:
            parsed_json = json.loads(body_text)
        except json.JSONDecodeError:
            parsed_json = None

    return RawResponse(
        status=status,
        location=location,
        content_type=content_type,
        body=body_text,
        parsed_json=parsed_json,
    )


@dataclass(frozen=True)
class Verdict:
    """classify()'s return value. `ok=True` carries the extracted payload —
    family A's `result`, family B's `data` AND `metadata` together as
    `{"data": ..., "metadata": ...}` (not `data` alone: `metadata` carries
    the server's own row total and page number on the job routes, and
    discarding it would force whichever tool reads this payload to invent
    figures 104 already supplied — deciding which half of a response
    matters is not this module's job, only extracting it correctly is) —
    and nothing else. `ok=False` carries a
    `kind` naming which failure condition fired plus a human-readable
    `detail` (typically 104's own message), and NO payload — `_issue_one`
    (tools/helpers.py's per-request unit shared by guarded_api and
    guarded_sequence) reads `kind` to pick the abort class and the
    Chinese error text, never `detail` alone, so a condition this module
    has not been taught about cannot silently fall through as success.
    """

    ok: bool
    payload: object = None
    kind: str = ""
    detail: str = ""


def classify(endpoint: Endpoint, raw: RawResponse) -> Verdict:
    """Pure. Decide success or failure for one response, driven entirely by
    `endpoint.family` — never inferred from the body's own shape.

    A single predicate cannot serve both families, and the two natural
    ways to write one fail invisibly in OPPOSITE directions
    (docs/104-site-facts.md §6b.3e): testing `status != "SUCCESS"` rejects
    every healthy family-B response (family B has no `status` key at all);
    defaulting the key to success admits every family-B error. Dispatching
    on the endpoint's DECLARED family, rather than probing the body for a
    `status` key, is what keeps the two checks from ever being swapped by
    accident.
    """
    # Session expired. Checked as a plain substring BEFORE any JSON/HTML
    # branch and before either family's own logic, because the measured
    # expiry shapes share ONLY this string — one carries it in an
    # envelope's `url` field, one inside an HTML redirect script, and a
    # real unfollowed 3xx carries it in the `Location` header instead
    # (this site has been observed changing which of these it uses for
    # the same underlying signal). Checking `raw.location` in addition to
    # `raw.body` is what keeps a redirect target on vip.104.com.tw itself
    # (not an auth host, so the hostname-based check elsewhere never sees
    # it) from falling through to a generic non-JSON failure — the
    # unfollowed redirect body is empty, so `raw.body` alone would never
    # catch this shape. Keying on the string rather than the carrying
    # format means a fourth shape in any format is still recognised.
    # [M §6b.3e, §6b.3i]
    if EXPIRY_MARKER in raw.body or (raw.location is not None and EXPIRY_MARKER in raw.location):
        return Verdict(False, kind="expired")

    # A bot block (measured cause: expired clearance cookie OR active bot
    # detection) is the same HTTP status on both families, and the two
    # causes need opposite remedies — `_issue_one`'s caller decides the
    # wording (first-call vs. after-a-prior-success on this session);
    # classify() only names the condition.
    if raw.status == 403:
        return Verdict(False, kind="blocked")

    if endpoint.family == "A":
        return _classify_family_a(raw)
    if endpoint.family == "opaque":
        # In practice this branch is never reached at all: `_issue_one`'s
        # auth-host redirect check intercepts logout_session's 302 before
        # classify() is ever called, and the EXPIRY_MARKER check above
        # catches the same redirect's Location a second, independent way.
        # It is declared anyway because classify() is called directly in
        # tests and must not crash on a response shape its own Endpoint
        # table admits — an opaque endpoint carries no family_b_shape
        # (Endpoint.__post_init__ refuses one), so routing it into
        # _classify_family_b would read that shape as None and blow up on
        # the first attribute access. This route does not parse the
        # response body at all: any status that reached this point without
        # tripping the expiry or 403 checks above counts as the guard
        # having let the request through, not as a claim that logout
        # succeeded.
        return Verdict(True, payload={})
    return _classify_family_b(endpoint, raw)


def _classify_family_a(raw: RawResponse) -> Verdict:
    if raw.parsed_json is None:
        # 404 + a body that never parsed as JSON at all is the wrong-host
        # marketing page; anything else non-JSON is reported as such.
        if raw.status == 404:
            return Verdict(False, kind="wrong_host", detail=raw.content_type or "")
        return Verdict(False, kind="non_json", detail=raw.content_type or "")
    if not isinstance(raw.parsed_json, dict):
        return Verdict(False, kind="malformed", detail="envelope is not an object")

    body = raw.parsed_json
    status = body.get("status")
    message = body.get("message") or ""

    # White-list, not black-list: only the documented success value is
    # accepted. Family A's status vocabulary is open — three of the five
    # observed values appeared after this check was first written
    # (docs/104-site-facts.md §6b.3e, §6b.3g, §6b.3h) — so "not SUCCESS"
    # is the only safe test; "not a known failure" would admit whatever
    # comes next.
    if status == "SUCCESS":
        return Verdict(True, payload=body.get("result"))

    # HTTP status disambiguates the SAME "not SUCCESS" outcome into
    # different scenarios with different remedies — a closed job (404) is
    # actionable ("choose another job"), a missing parameter (400) is a
    # caller bug, and the ordinary case is 200 with a non-SUCCESS status.
    if raw.status == 404:
        return Verdict(False, kind="not_found", detail=message)
    if raw.status == 400:
        return Verdict(False, kind="missing_param", detail=message)
    if status == "ACCESS_DENIED":
        # HTTP 200 with this status is measured to mean a header the route
        # requires (currently only Referer) was not sent — a client
        # configuration fault, not a site condition. [M §6b.3h]
        return Verdict(False, kind="header_fault", detail=message)

    return Verdict(False, kind="unrecognised_status", detail=f"{status}: {message}".strip(": "))


def _family_b_error_detail(parsed_json: object) -> str:
    """Pure. Recover 104's own per-field explanation from a family-B error
    envelope, never paraphrasing it.

    Tries `error`, then `message` (the base line — a family-B envelope
    carries at most one of these), then appends `detail`'s
    `{field: [msg, ...]}` flattened into `field: msg; field: msg` — the
    third error envelope §6b.9-4 measured (`{"code": "00005", "message":
    "帶入內容驗證失敗", "detail": {"content": [...], "file": [...]}}`) has no
    `error` key at all, so reading `error` alone (what this classifier did
    before this function existed) silently threw away everything 104 had
    actually said. Words are kept verbatim — 104's own detail messages are
    partly English, and rewording them would be this project inventing an
    error message rather than reporting one.
    """
    if not isinstance(parsed_json, dict):
        return ""
    base = str(parsed_json.get("error") or parsed_json.get("message") or "")
    detail = parsed_json.get("detail")
    if isinstance(detail, dict):
        parts = []
        for field, msgs in detail.items():
            if isinstance(msgs, list):
                msg_text = "; ".join(str(m) for m in msgs)
            else:
                msg_text = str(msgs)
            parts.append(f"{field}: {msg_text}")
        detail_text = "; ".join(parts)
        if detail_text:
            return f"{base}（{detail_text}）" if base else detail_text
    return base


def _family_b_payload(shape: FamilyBShape, body: dict, data: object) -> dict:
    """Build an ok-verdict family-B payload: `data`/`metadata` plus any of
    `shape.sibling_keys` present in `body`. A sibling key that is absent
    from `body` is left OUT of the payload entirely, never filled with
    `None` — "104 didn't send this key" and "104 sent null" are different
    facts, and the tool layer's own return-shape decision (e.g. `failed`
    present-but-absent driving the confirmed/ambiguous split) reads
    absence itself as a signal. Only called on the ok=True path — a failed verdict never
    carries a payload at all, so there is nothing to copy siblings onto.
    """
    payload: dict = {"data": data, "metadata": body.get("metadata")}
    for key in shape.sibling_keys:
        if key in body:
            payload[key] = body[key]
    return payload


def _classify_family_b(endpoint: Endpoint, raw: RawResponse) -> Verdict:
    # HTTP status is the floor for family B — structural checks below are
    # additional, never a substitute for it. Family B's own authentication
    # failure lives in the HTTP status (measured: 401), not the envelope —
    # unlike family A, where it lives in the envelope at HTTP 200.
    if raw.status == 401:
        return Verdict(False, kind="expired")
    if raw.status == 404:
        if isinstance(raw.parsed_json, dict):
            return Verdict(False, kind="not_found", detail=_family_b_error_detail(raw.parsed_json))
        return Verdict(False, kind="wrong_host", detail=raw.content_type or "")
    if raw.status == 400:
        # A third error envelope, neither family A nor family B's own
        # success shape: {"code": ..., "message": ..., "detail": {field:
        # [msg, ...]}} [M §6b.9-4]. Its own kind (never folded into family
        # A's missing_param — that kind means a required PAIRED parameter
        # was omitted; this is body-validation failure, strictly broader:
        # the measured instance happens to be a required field, but an
        # unmeasured content-length limit would route through this same
        # branch, and "缺少必要參數" would then describe a message that was
        # too long).
        return Verdict(False, kind="validation", detail=_family_b_error_detail(raw.parsed_json))
    if raw.status != 200:
        if isinstance(raw.parsed_json, dict):
            detail = str(raw.parsed_json.get("error") or "")
        else:
            detail = ""
        return Verdict(False, kind="unrecognised_status", detail=f"HTTP {raw.status}: {detail}".rstrip(": "))

    if raw.parsed_json is None:
        return Verdict(False, kind="non_json", detail=raw.content_type or "")
    if not isinstance(raw.parsed_json, dict):
        return Verdict(False, kind="malformed", detail="body is not an object")

    body = raw.parsed_json
    if "data" not in body:
        return Verdict(False, kind="malformed", detail="missing data")
    data = body["data"]

    # Guaranteed non-None: classify() only calls this function for
    # family=="B" (family=="opaque" is handled by its own branch there,
    # before this function is ever reached), and Endpoint.__post_init__
    # refuses to construct a family-B endpoint without a family_b_shape —
    # so there is no per-call default to fall back on here.
    shape = endpoint.family_b_shape
    if shape.is_list:
        if not isinstance(data, list):
            return Verdict(False, kind="malformed", detail="data is not a list")
        # `data` AND `metadata`, not `data` alone — `metadata` (row total,
        # page number on the job routes) is 104's own figures and belongs
        # to whichever tool reads this payload, not something this
        # transport layer discards on the caller's behalf. See Verdict's
        # docstring.
        return Verdict(True, payload=_family_b_payload(shape, body, data))

    if not isinstance(data, dict):
        return Verdict(False, kind="malformed", detail="data is not an object")
    inner_key = shape.inner_key
    if inner_key and not data.get(inner_key):
        return Verdict(False, kind="malformed", detail=f"missing {inner_key}")
    if data.get("error"):
        return Verdict(False, kind="malformed", detail=str(data.get("error")))
    return Verdict(True, payload=_family_b_payload(shape, body, data))
