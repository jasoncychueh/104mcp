"""Phase 1 transport/guard tests — design.md Testing Strategy, Components §2-4, §8.

Written BLIND to browser/api_client.py's and tools/helpers.py's *logic* (spec-tester
Mode 1 rule): every assertion below is derived from design.md's declared interfaces
plus docs/104-site-facts.md's measured facts, never from reading what a function
decides. Structural mismatches that surfaced only as bare `TypeError`s on the first
run (module-level pure dataclasses this design declares as public interfaces — the
`Endpoint`/`RawResponse`/`Verdict` field names, and `guarded_api`'s parameter types)
were resolved via `inspect.signature()` on the already-implemented functions rather
than by guessing repeatedly against the live system or by opening the module's
source — see this module's completion report for the exact call made and why that
line was judged not to cross into reading decision logic. Nothing below was adjusted
after seeing which body shapes a function accepts or rejects.

Cases: T-19, T-20, T-21, T-22, T-23, T-24, T-25, T-44, T-45, T-46, T-54, T-55, T-56, T-57.

Seams substituted per Testing Strategy ("not mocked: the pure decision functions...
The HTTP transport and the browser context are the seams that are substituted"):
`fetch` is monkeypatched to return canned `RawResponse`s built from tests/fixtures/.
There is no BrowserContext seam left post-login (§C7) — `guarded_api` reads
credentials straight off `SessionInfo.cookies`, so a session under test is built
by handing that field a cookie list directly. `classify`, `select_cookies_for_host`
and `matches_auth_host` are exercised directly and are never mocked.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from mcp104.browser.api_client import (
    ENDPOINTS,
    Endpoint,
    FamilyBShape,
    RawResponse,
    classify,
    select_cookies_for_host,
)
from mcp104.browser.session import SessionInfo, SessionPool, matches_auth_host
from mcp104.browser.throttle import ThrottleAbort
from mcp104.config import get_config
from mcp104.tools.helpers import ERROR_CHALLENGE, GuardAbort, ToolAbort, get_session_id, guarded_api

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Fixture / fake-transport plumbing ────────────────────────────────────

def _load(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _raw(status: int, content_type: str, body: str, parsed_json, location: str | None = None) -> RawResponse:
    return RawResponse(
        status=status,
        location=location,
        content_type=content_type,
        body=body,
        parsed_json=parsed_json,
    )


def _raw_from_wrapper(fixture_name: str) -> RawResponse:
    d = _load(fixture_name)
    return _raw(d["http_status"], d["content_type"], d["body_text"], d["body_json"])


def _raw_from_bare_json(fixture_name: str, http_status: int = 200) -> RawResponse:
    body_json = _load(fixture_name)
    return _raw(http_status, "application/json; charset=utf-8",
                json.dumps(body_json, ensure_ascii=False), body_json)


class FakeSessionObj:
    pass


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeApp:
    def __init__(self, session_pool: SessionPool):
        self.session_pool = session_pool
        self.config = get_config()


class FakeCtx:
    def __init__(self, session_pool: SessionPool):
        self.session = FakeSessionObj()
        self.request_context = FakeRequestContext(FakeApp(session_pool))


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """Neutralizes the pacing floor's inline sleep for every test in this file —
    this file exercises the guard's lock/identity/error-mapping behaviour, not
    pacing (that's tests/test_throttle.py). Only `_sleep` needs patching: the
    pacing rework (design.md Components §7) replaced the drawn-delay distribution
    with a deterministic interval floor, which needs no randomness source, so
    there is no `_rng` to neutralize any more (see tests/test_helpers.py's
    matching fixture for the same fix applied there)."""
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


def _install_fake_fetch(monkeypatch, raw: RawResponse):
    async def fake_fetch(endpoint, *, cookie_header, params=None, body=None):
        return raw
    # guarded_api calls into mcp104.browser.api_client's module-level `fetch` by
    # attribute lookup, so patching the module attribute is enough. The
    # mcp104.tools.helpers alias is also patched, harmlessly (raising=False), in
    # case it was instead imported by name into tools/helpers.py's namespace.
    monkeypatch.setattr("mcp104.browser.api_client.fetch", fake_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", fake_fetch, raising=False)
    return fake_fetch


def _new_ctx_and_session():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight off SessionInfo.cookies now —
    # there is no BrowserContext left to fake post-login (§C7).
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return pool, ctx, info


def _endpoints_matching(*, family: str | None = None, host: str | None = None,
                         path_contains: str | None = None):
    """Look endpoints up by declared property rather than by guessing the
    ENDPOINTS dict's key strings — design.md never names those keys, only the
    five tools' own names (Components §9), so matching on `.path`/`.family`/
    `.host` (all explicitly declared, per Data Models) is the only
    blindness-safe way to find "the search endpoint" etc. Returns (key, Endpoint)
    pairs; guarded_api/classify take the Endpoint object itself, not the key."""
    out = []
    for key, ep in ENDPOINTS.items():
        if family is not None and ep.family != family:
            continue
        if host is not None and ep.host != host:
            continue
        if path_contains is not None and path_contains not in ep.path:
            continue
        out.append((key, ep))
    return out


def _headers_lower(ep: Endpoint) -> set[str]:
    # T-44 is an ORACLE, not a fixture: it cross-checks every declared endpoint
    # against docs/104-site-facts.md §6b.3h's independently-built reference table
    # (the docs->declaration direction). Repaired to read the NAME out of each
    # declared (name, value) pair after `required_headers` (a frozenset of bare
    # names) became `extra_headers` (name/value pairs, §1) — the property this
    # function tests is unchanged: a tuple-of-pairs is not made redundant by this
    # repair, since a name-only set can't tell a typo'd VALUE from a correct one,
    # which is exactly what T-8 (declaration->wire, below) exists to catch instead.
    return {name.lower() for name, _value in ep.extra_headers}


# ── T-44 (interface): ENDPOINTS matches the recorded table ──────────────
# Reference table built independently from docs/104-site-facts.md — NOT from
# browser/api_client.py, deliberately: the oracle must not move with the thing
# it guards (that property is what makes this table worth having; see the
# §6b.3h correction below, which is exactly the kind of drift it exists to
# catch).
#
# Two distinguishable facts, both from docs/104-site-facts.md §6b.3h, kept in
# two separate columns rather than collapsed into one — that conflation
# (measurement vs. policy) produced two of this round's High findings:
#
#   measured_requires_referer — §6b.3h's "重大更正" per-route measurement
#       table (one call per route per header configuration): search and
#       match genuinely required it, recommend genuinely succeeded without
#       it. None where the route was never measured for this at all (the two
#       auth.vip.104.com.tw endpoints).
#
#   declared_requires_referer — what ENDPOINTS actually states today, per
#       §6b.3h's "實作範圍的分歧記錄" note. Broader than the measurement on
#       vip.104.com.tw: all three family-A vip endpoints declare Referer,
#       because the implementation sends it unconditionally on that host
#       (harmless on all four routes measured, necessary on three) — three
#       *individually stated* identical values, not one value folded into an
#       unconditional baseline. Deliberately NOT extended to the two
#       auth.vip.104.com.tw endpoints: Referer was never measured on that
#       host, and generalizing "harmless on these four routes" to "harmless
#       on every host" is exactly the single-observation-to-family inference
#       §6b.3h's own correction was about. That both auth-host endpoints
#       declare False despite `measured=None` is the point being made
#       visible here, not an oversight.
_REFERENCE_ENDPOINTS = [
    # (path substring, host short-code, family, measured_requires_referer, declared_requires_referer)
    ("/api/search/searchResult", "vip", "A", True, True),
    ("/api/recommend/resumeListAll", "vip", "A", False, True),
    ("/api/match/matchResult", "vip", "A", True, True),
    ("/vipapi/resume/search", "auth", "B", None, False),
    # docs/104-site-facts.md line 921: "/job-api/jobs/recommend 才是公司職缺清單"
    # (this is the endpoint for the company job list, despite the "recommend"
    # in its path — the sibling /job-api/jobs/match returns a strict subset).
    ("/job-api/jobs/recommend", "auth", "B", None, False),
]


def test_declared_endpoints_match_recorded_host_family_and_referer_declaration():
    # One table drives every check for every declared endpoint, rather than
    # naming individual routes in separate assertions — a new endpoint, or a
    # changed per-endpoint requirement, has to be stated as a row here or it
    # is silently uncovered, instead of silently inheriting whatever a
    # hand-picked assertion happened to enumerate.
    #
    # Only `declared_requires_referer` is checked against ENDPOINTS — that is
    # the only one of the two columns that is actually a property of the
    # running code. `measured_requires_referer` is carried alongside for
    # provenance and shows up in the failure message, so a mismatch is
    # legible as "declaration drifted from policy" rather than silently
    # re-read as "measurement was wrong".
    for path_sub, host, family, measured, declared in _REFERENCE_ENDPOINTS:
        matches = _endpoints_matching(path_contains=path_sub)
        assert len(matches) == 1, (
            f"expected exactly one declared endpoint whose path contains "
            f"{path_sub!r}, found {matches!r}"
        )
        _key, ep = matches[0]
        assert ep.host == host, f"{path_sub}: expected host {host!r}, got {ep.host!r}"
        assert ep.family == family, f"{path_sub}: expected family {family!r}, got {ep.family!r}"

        has_referer = "referer" in _headers_lower(ep)
        assert has_referer == declared, (
            f"{path_sub}: docs/104-site-facts.md §6b.3h's declaration note says "
            f"ENDPOINTS should declare requires_referer={declared!r} "
            f"(§6b.3h measured requires_referer={measured!r} for this route), "
            f"but extra_headers say {has_referer!r} ({ep.extra_headers!r})"
        )


# ── T-45 (interface): select_cookies_for_host ────────────────────────────

def test_select_cookies_for_host_includes_only_matching_host_cookies():
    cookies = [
        {"name": "its", "value": "vip-token", "domain": "vip.104.com.tw", "path": "/"},
        {"name": "ithp", "value": "auth-token", "domain": "auth.vip.104.com.tw", "path": "/"},
    ]

    vip_header = select_cookies_for_host(cookies, "vip.104.com.tw")
    auth_header = select_cookies_for_host(cookies, "auth.vip.104.com.tw")

    assert "its=vip-token" in vip_header
    assert "ithp" not in vip_header

    assert "ithp=auth-token" in auth_header
    assert "its" not in auth_header


# ── T-46 (interface): classify reads endpoint.family, never the body ─────

def test_classify_family_selected_by_endpoint_declaration_not_body_shape():
    family_a_body = _load("zero_row_search.json")          # status=SUCCESS, top-level data=null
    family_b_body = _load("resume_unrestricted.json")      # {data:{...}, metadata:{...}}, no "status"

    raw_a = _raw(200, "application/json; charset=utf-8",
                 json.dumps(family_a_body, ensure_ascii=False), family_a_body)
    raw_b = _raw(200, "application/json; charset=utf-8",
                 json.dumps(family_b_body, ensure_ascii=False), family_b_body)

    ep_a = Endpoint(key="t46_probe_a", host="vip", path="/api/probe", method="GET", family="A",
                     extra_headers=(), family_b_shape=None, throttle_gated=True)
    # inner_key="resume", is_list=False mirrors resume_unrestricted.json's
    # real shape ({"data": {"resume": {...}, ...}, "metadata": {...}}) and the
    # real get_resume_detail endpoint's own declared shape.
    ep_b = Endpoint(key="t46_probe_b", host="vip", path="/api/probe", method="GET", family="B",
                     extra_headers=(),
                     family_b_shape=FamilyBShape(is_list=False, inner_key="resume", sibling_keys=()), throttle_gated=True)

    # Correct family declared for each body: both succeed.
    assert classify(ep_a, raw_a).ok is True
    assert classify(ep_b, raw_b).ok is True

    # Same two bodies, family swapped: family selection must flip the outcome.
    # If classify() sniffed the body shape instead of reading endpoint.family,
    # these two would classify identically to the pair above.
    verdict_a_declared_b = classify(ep_b, raw_a)
    assert verdict_a_declared_b.ok is False, (
        "family-A body's top-level `data` is null, which fails family B's "
        "'data present and a mapping' floor (Components §4) — accepting it "
        "means family wasn't actually taken from the endpoint declaration"
    )

    verdict_b_declared_a = classify(ep_a, raw_b)
    assert verdict_b_declared_a.ok is False, (
        "family-B body carries no 'status' key at all, so family A's "
        "status==SUCCESS check must reject it — admitting it here is exactly "
        "the 'default the key to success' failure mode Components §4 warns "
        "about, and would mean every family-B error is silently accepted too"
    )


def test_endpoint_construction_enforces_family_b_shape_invariant():
    # Not one of the 14 assigned cases — added per coordinator request
    # (Mode 2, 2026-08-14): `family_b_shape` moved out of a module-level
    # lookup (which defaulted an unknown key to "any dict under `data`
    # passes", with no inner-key check and no error-field check) and into
    # Endpoint's own __post_init__ validation, so a family-B endpoint with no
    # shape, or a family-A endpoint carrying one, now fails at construction —
    # at module import, when ENDPOINTS is built — rather than the first time
    # classify() happens to see it. That invariant had no test pinning it
    # (the implementer had only verified it by hand), so it is exactly the
    # kind of construction-time guard that quietly becomes optional if a
    # later refactor drops the check and nothing goes red.
    with pytest.raises(ValueError):
        Endpoint(key="probe_b_missing_shape", host="vip", path="/api/probe", method="GET", family="B",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)

    with pytest.raises(ValueError):
        Endpoint(key="probe_a_with_shape", host="vip", path="/api/probe", method="GET", family="A",
                 extra_headers=(),
                 family_b_shape=FamilyBShape(is_list=False, inner_key="resume", sibling_keys=()), throttle_gated=True)


def test_opaque_family_endpoint_classifies_a_bare_redirect_as_ok():
    # I2-B(a): family "non_json" was renamed to "opaque" (Verdict.kind for
    # the non-JSON *failure* case is unaffected — it stays "non_json", see
    # the neighbouring family-A non-JSON case above). An "opaque" endpoint,
    # declared the same shape logout_session uses (family_b_shape=None,
    # throttle_gated=False), must classify a plain 302 with no body and a
    # non-JSON content-type as success without ever touching family B's
    # is_list/inner_key shape logic (there is none to touch: family_b_shape
    # is None for this endpoint).
    ep = Endpoint(key="probe_opaque", host="vip", path="/api/probe", method="POST", family="opaque",
                  extra_headers=(), family_b_shape=None, throttle_gated=False)
    raw = _raw(302, "text/html", "", None, location="https://vip.104.com.tw/rms/index")

    verdict = classify(ep, raw)

    assert verdict.ok is True


def test_endpoint_construction_rejects_unknown_family():
    # I2-B(b): only "A", "B" and "opaque" are valid `family` values. "C"
    # (never a real family) and "non_json" (the old, pre-rename spelling)
    # must both be rejected at construction, alongside the existing method
    # whitelist case's ValueError-on-construction pattern.
    with pytest.raises(ValueError):
        Endpoint(key="probe_family_c", host="vip", path="/api/probe", method="GET", family="C",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)

    with pytest.raises(ValueError):
        Endpoint(key="probe_family_old_name", host="vip", path="/api/probe", method="GET", family="non_json",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)


# ── T-56 (interface): matches_auth_host ──────────────────────────────────

def test_auth_vip_host_is_not_classified_as_auth_host():
    # design.md Components §8: "auth.vip.104.com.tw is a working host, not an
    # authentication host... its docstring states that auth.vip.104.com.tw is
    # a working host and must return false."
    assert matches_auth_host("auth.vip.104.com.tw") is False
    assert matches_auth_host("vip.104.com.tw") is False
    # Positive control — docs/104-site-facts.md line 28 and tests/test_session.py:
    # these ARE the login/auth hosts, so the predicate isn't just always-false.
    assert matches_auth_host("bsignin.104.com.tw") is True
    assert matches_auth_host("boidc.104.com.tw") is True


# ── T-19..T-25, T-57 (behavior/e2e): guarded_api's Error Handling mapping ─
# All routed through guarded_api (not classify() directly): design.md
# Components §3 states guarded_api "Maps every Error Handling scenario to its
# own abort payload" via `raise GuardAbort subclasses`, and T-19's claim ("no
# results key") is about that final tool-facing payload, not classify()'s raw
# Verdict. GuardAbort's `except GuardAbort as e: return e.payload` convention
# is reused as-is per Components §3 and confirmed by tests/test_helpers.py.

@pytest.mark.asyncio
async def test_every_failure_fixture_produces_error_with_no_results_key(monkeypatch):
    cases = [
        ("failure_family_a_logged_out.json", "/api/search/searchResult"),
        ("failure_family_a_expired.json", "/api/search/searchResult"),
        ("failure_family_b_unauthenticated.json", None),  # family B — any family-B endpoint
        ("failure_access_denied.json", "/api/match/matchResult"),
        ("failure_data_not_found.json", "/api/match/matchResult"),
    ]
    for fixture_name, path_hint in cases:
        _pool, ctx, _info = _new_ctx_and_session()
        raw = _raw_from_wrapper(fixture_name)
        _install_fake_fetch(monkeypatch, raw)

        if path_hint:
            _key, ep = _endpoints_matching(path_contains=path_hint)[0]
        else:
            _key, ep = _endpoints_matching(family="B")[0]

        with pytest.raises(GuardAbort) as exc_info:
            async with guarded_api(ctx, ep, params=[]):
                pass  # pragma: no cover — must never be reached

        assert "results" not in exc_info.value.payload, (
            f"{fixture_name} leaked a 'results' key on failure: {exc_info.value.payload!r}"
        )
        assert "error" in exc_info.value.payload, f"{fixture_name} carried no 'error' key"


@pytest.mark.asyncio
async def test_family_a_unrecognised_status_including_novel_one_is_a_failure(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()
    novel_status_body = {
        "status": "TOTALLY_NOVEL_STATUS_NEVER_SEEN_9f3e",
        "message": "從未出現過的狀態訊息",
        "result": None,
        "url": "",
        "data": None,
    }
    raw = _raw(200, "application/json; charset=utf-8",
               json.dumps(novel_status_body, ensure_ascii=False), novel_status_body)
    _install_fake_fetch(monkeypatch, raw)
    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]

    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, ep, params=[]):
            pass  # pragma: no cover

    assert "results" not in exc_info.value.payload


@pytest.mark.asyncio
async def test_unrecognised_status_reported_verbatim_with_stop_instruction(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()
    novel_status_body = {
        "status": "ANOTHER_NOVEL_STATUS_7c21",
        "message": "唯一可辨識字串XYZ789",
        "result": None,
        "url": "",
        "data": None,
    }
    raw = _raw(200, "application/json; charset=utf-8",
               json.dumps(novel_status_body, ensure_ascii=False), novel_status_body)
    _install_fake_fetch(monkeypatch, raw)
    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]

    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, ep, params=[]):
            pass  # pragma: no cover

    # Error Handling scenario 9: "report the status and message verbatim".
    text = json.dumps(exc_info.value.payload, ensure_ascii=False)
    assert "ANOTHER_NOVEL_STATUS_7c21" in text
    assert "唯一可辨識字串XYZ789" in text


@pytest.mark.asyncio
async def test_family_b_healthy_body_accepted_and_family_b_error_rejected(monkeypatch):
    # T-21's complementary pair (design.md Components §4): a healthy family-B
    # body must not be rejected for lacking a `status` key, and a family-B
    # error body must not be admitted.
    _resume_key, resume_ep = _endpoints_matching(path_contains="/vipapi/resume/search")[0]

    _pool, ctx, _info = _new_ctx_and_session()
    healthy_raw = _raw_from_bare_json("resume_unrestricted.json")
    _install_fake_fetch(monkeypatch, healthy_raw)
    async with guarded_api(ctx, resume_ep, params=[]) as (payload, info):
        assert payload is not None
        assert info is not None

    _pool2, ctx2, _info2 = _new_ctx_and_session()
    error_raw = _raw_from_wrapper("failure_family_b_unauthenticated.json")
    _install_fake_fetch(monkeypatch, error_raw)
    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx2, resume_ep, params=[]):
            pass  # pragma: no cover

    assert "results" not in exc_info.value.payload


@pytest.mark.asyncio
async def test_both_family_a_expiry_bodies_report_expired_session(monkeypatch):
    # tests/fixtures/README.md: as re-measured, neither body parses as JSON
    # and the two are byte-different from each other — the only thing they
    # share is the /company/status/switchCompany redirect-target string
    # (Error Handling scenario 4), which is exactly what the check must key on.
    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]

    for fixture_name in ("failure_family_a_logged_out.json", "failure_family_a_expired.json"):
        _pool, ctx, _info = _new_ctx_and_session()
        raw = _raw_from_wrapper(fixture_name)
        _install_fake_fetch(monkeypatch, raw)

        with pytest.raises(GuardAbort) as exc_info:
            async with guarded_api(ctx, ep, params=[]):
                pass  # pragma: no cover

        assert "已過期" in exc_info.value.payload.get("error", ""), (
            f"{fixture_name} was not reported as an expired session: "
            f"{exc_info.value.payload!r}"
        )
        assert "results" not in exc_info.value.payload


@pytest.mark.asyncio
async def test_challenge_body_produces_stop_and_wait_error_not_empty_result(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()
    # Exact marker text proven to trip _detect_cloudflare_challenge — the ZH
    # challenge-page markers are pinned directly in
    # tests/test_helpers.py's _detect_cloudflare_challenge coverage
    # (guarded_page, which exercised this same detector against a live
    # page, no longer exists post-§C7; the fixture below is this file's
    # own carrier for that string).
    raw = _raw(
        200,
        "text/html; charset=utf-8",
        "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。",
        None,
    )
    _install_fake_fetch(monkeypatch, raw)
    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]

    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, ep, params=[]):
            pass  # pragma: no cover — must never be reached

    assert exc_info.value.payload == ERROR_CHALLENGE
    assert "results" not in exc_info.value.payload


@pytest.mark.asyncio
async def test_404_html_and_404_json_produce_different_errors(monkeypatch):
    # No committed fixture exists for the "wrong host" HTML-404 shape (only
    # the JSON-404 DATA_NOT_FOUND shape is captured, per tests/fixtures/README.md)
    # — see this module's completion report. The HTML body below is
    # synthesized to match Error Handling scenario 1's description ("HTTP 404
    # with an HTML body... resembles nothing about 'wrong host'").
    _key, ep = _endpoints_matching(path_contains="/api/match/matchResult")[0]

    _pool_json, ctx_json, _info_json = _new_ctx_and_session()
    json_raw = _raw_from_wrapper("failure_data_not_found.json")
    _install_fake_fetch(monkeypatch, json_raw)
    with pytest.raises(GuardAbort) as exc_json:
        async with guarded_api(ctx_json, ep, params=[]):
            pass  # pragma: no cover

    _pool_html, ctx_html, _info_html = _new_ctx_and_session()
    html_raw = _raw(404, "text/html; charset=utf-8",
                     "<html><body>104人力銀行 - 找不到頁面</body></html>", None)
    _install_fake_fetch(monkeypatch, html_raw)
    with pytest.raises(GuardAbort) as exc_html:
        async with guarded_api(ctx_html, ep, params=[]):
            pass  # pragma: no cover

    assert "results" not in exc_json.value.payload
    assert "results" not in exc_html.value.payload
    assert exc_json.value.payload != exc_html.value.payload, (
        "an HTML 404 (wrong host / config fault) and a JSON 404 "
        "(server-reported not-found) must not produce the same error"
    )
    # The server's own message from the JSON 404 fixture should surface —
    # data_not_found is a closed job, per its note field.
    json_text = json.dumps(exc_json.value.payload, ensure_ascii=False)
    assert "此職務已關閉" in json_text or "DATA_NOT_FOUND" in json_text


# ── T-54, T-55 (interface): guarded_api's lock, identity re-check, redirects ─

@pytest.mark.asyncio
async def test_guarded_api_holds_lock_and_rechecks_identity_after_acquiring(monkeypatch):
    # Same race guarded_api's own docstring describes for guarded_api itself
    # (Components §3/§C8) — logout()+login() replacing the pool entry with a
    # brand-new SessionInfo while a second caller is still parked on the old
    # entry's lock. The guarded_page-era sibling of this test
    # (tests/test_helpers.py::test_guarded_page_rejects_when_pool_entry_replaced_while_queued)
    # no longer exists post-§C7 — guarded_page itself is gone — so this is
    # now the sole test pinning that identity re-check for the API path.
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)
    info_old = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info_old)

    release_holder = asyncio.Event()
    healthy_raw = _raw_from_bare_json("zero_row_search.json")

    async def blocking_fetch(endpoint, *, cookie_header, params=None, body=None):
        await release_holder.wait()
        return healthy_raw

    monkeypatch.setattr("mcp104.browser.api_client.fetch", blocking_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", blocking_fetch, raising=False)

    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]
    result = {}

    async def holder():
        async with guarded_api(ctx, ep, params=[]):
            pass

    async def queued():
        try:
            async with guarded_api(ctx, ep, params=[]):
                result["proceeded"] = True
        except GuardAbort as e:
            result["error"] = e.payload

    holder_task = asyncio.ensure_future(holder())
    await asyncio.sleep(0.01)  # let holder resolve info_old, acquire its lock, block in fetch

    queued_task = asyncio.ensure_future(queued())
    await asyncio.sleep(0.01)  # let queued resolve info_old too, then park on its lock

    # Simulate logout()+login() completing while `queued` is still parked —
    # a brand-new SessionInfo (new lock) replaces the one `queued` resolved.
    pool.activate_direct(sid, SessionInfo(cookies=[], account_label="test@104.com"))

    release_holder.set()
    await asyncio.wait_for(asyncio.gather(holder_task, queued_task), timeout=5)

    assert "proceeded" not in result
    assert result.get("error") == {"error": "請先呼叫 login()"}


@pytest.mark.asyncio
async def test_guarded_api_does_not_follow_redirect_and_reports_auth_host_redirect_as_expired(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()
    calls = {"n": 0}

    async def redirecting_fetch(endpoint, *, cookie_header, params=None, body=None):
        calls["n"] += 1
        return RawResponse(
            status=302,
            location="https://bsignin.104.com.tw/login",
            content_type="text/html; charset=utf-8",
            body="",
            parsed_json=None,
        )

    monkeypatch.setattr("mcp104.browser.api_client.fetch", redirecting_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", redirecting_fetch, raising=False)

    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]

    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, ep, params=[]):
            pass  # pragma: no cover

    # Exactly one HTTP call — the 3xx is read, never chased into a second request.
    assert calls["n"] == 1
    assert "已過期" in exc_info.value.payload.get("error", "")
    # Payload and kind are independent (Round I1 Bug A): the wording alone does
    # not prove this raise site declared "expired" rather than "transport" —
    # this is the site that must NOT fall in with the transport kind raised
    # earlier in guarded_api for a plain fetch() exception.
    assert exc_info.value.kind == "expired"


@pytest.mark.asyncio
async def test_guarded_api_throttled_rejection_carries_throttled_kind(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()

    # enforce_throttle's contract (§C10): a rejection is a ThrottleAbort, not
    # a bare dict — guarded_api reads .kind/.payload off it directly.
    async def rejecting_throttle(*args, **kwargs):
        return ThrottleAbort(
            kind="throttled",
            payload={"error": "節流測試", "retry_after_seconds": 5},
            detail="",
        )

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", rejecting_throttle)
    _key, ep = _endpoints_matching(path_contains="/api/search/searchResult")[0]

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ep, params=[]):
            pass  # pragma: no cover

    assert exc_info.value.kind == "throttled"


# ── T-57 (e2e): logged-out session on a list tool ────────────────────────

@pytest.mark.asyncio
async def test_logged_out_session_on_list_tool_returns_expired_error_never_empty_result(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()
    raw = _raw_from_wrapper("failure_family_b_unauthenticated.json")
    _install_fake_fetch(monkeypatch, raw)

    # docs/104-site-facts.md line 921: /job-api/jobs/recommend is the company
    # job list endpoint — the "list tool" this case names.
    _job_list_key, job_list_ep = _endpoints_matching(path_contains="/job-api/jobs/recommend")[0]

    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, job_list_ep, params=[]):
            pass  # pragma: no cover — must never be reached

    assert "results" not in exc_info.value.payload
    assert "已過期" in exc_info.value.payload.get("error", "")


# ── §1: Endpoint.method (no default), fetch()'s POST dispatch, extra_headers ────────

def test_endpoint_method_has_no_default_and_is_required():
    with pytest.raises(TypeError):
        Endpoint(key="probe_no_method", host="vip", path="/api/probe", family="A",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)


def test_endpoint_construction_rejects_a_method_outside_get_or_post():
    with pytest.raises(ValueError):
        Endpoint(key="probe_bad_method", host="auth", path="/bc-comm/message/{job_no}-{p_id}",
                 method="DELETE", family="B", extra_headers=(),
                 family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=()), throttle_gated=True)


class _FakeAiohttpResponse:
    def __init__(self, status=200, headers=None, body=b'{"data": [], "metadata": {}}'):
        self.status = status
        self.headers = headers or {}
        self._body = body

    async def read(self):
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAiohttpSession:
    """Stand-in for aiohttp.ClientSession, capturing exactly what fetch() actually
    issues (method, url, headers, json body) — used to test fetch() itself for real,
    not through guarded_api's monkeypatched-away fetch."""

    calls: list[tuple[str, str, dict, object]] = []

    def __init__(self, timeout=None):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, *, headers, allow_redirects):
        _FakeAiohttpSession.calls.append(("GET", url, dict(headers), None))
        return _FakeAiohttpResponse()

    def post(self, url, *, headers, json, allow_redirects):
        _FakeAiohttpSession.calls.append(("POST", url, dict(headers), json))
        return _FakeAiohttpResponse()


@pytest.fixture(autouse=True)
def _reset_fake_aiohttp_calls():
    _FakeAiohttpSession.calls = []
    yield


@pytest.mark.asyncio
async def test_fetch_dispatches_get_or_post_per_endpoint_method(monkeypatch):
    from mcp104.browser import api_client as api_client_mod
    monkeypatch.setattr(api_client_mod.aiohttp, "ClientSession", _FakeAiohttpSession)

    _key, get_ep = _endpoints_matching(path_contains="/job-api/jobs/recommend")[0]
    await api_client_mod.fetch(get_ep, cookie_header="c=1", params=[])

    _key, post_ep = _endpoints_matching(path_contains="/bc-comm/message/all-stream")[0]
    await api_client_mod.fetch(post_ep, cookie_header="c=1", params=[], body={"content": "x"})

    assert _FakeAiohttpSession.calls[0][0] == "GET"
    assert _FakeAiohttpSession.calls[0][3] is None
    assert _FakeAiohttpSession.calls[1][0] == "POST"
    assert _FakeAiohttpSession.calls[1][3] == {"content": "x"}


@pytest.mark.asyncio
async def test_fetch_emits_every_declared_extra_header_verbatim_and_nothing_undeclared(monkeypatch):
    from mcp104.browser import api_client as api_client_mod
    monkeypatch.setattr(api_client_mod.aiohttp, "ClientSession", _FakeAiohttpSession)

    declared = Endpoint(key="probe_headers", host="vip", path="/api/probe", method="GET",
                         family="A", extra_headers=(("X-Test-Header", "test-value"),),
                         family_b_shape=None, throttle_gated=True)
    await api_client_mod.fetch(declared, cookie_header="c=1", params=[])

    _method, _url, headers, _body = _FakeAiohttpSession.calls[0]
    assert headers.get("X-Test-Header") == "test-value"

    undeclared = Endpoint(key="probe_no_headers", host="vip", path="/api/probe", method="GET",
                           family="A", extra_headers=(), family_b_shape=None, throttle_gated=True)
    await api_client_mod.fetch(undeclared, cookie_header="c=1", params=[])
    _method, _url, headers2, _body = _FakeAiohttpSession.calls[1]
    assert "Referer" not in headers2, "a name not declared in extra_headers must not be sent"
    assert "X-Test-Header" not in headers2


# ── build_url: two placeholders in one hyphen-joined path segment (§2) ──────────────

def test_build_url_fills_two_placeholders_in_one_hyphen_joined_segment():
    from mcp104.browser.api_client import build_url
    _key, ep = _endpoints_matching(path_contains="/bc-comm/message/{job_no}-{p_id}")[0]
    url = build_url(ep, [
        ("job_no", "12355016"), ("p_id", "7174595"),
        ("page", "1"), ("perPage", "100"), ("sort", "ASC"),
    ])
    assert url == (
        "https://auth.vip.104.com.tw/bc-comm/message/12355016-7174595"
        "?page=1&perPage=100&sort=ASC"
    )


def test_build_url_missing_placeholder_raises_naming_it():
    from mcp104.browser.api_client import build_url
    _key, ep = _endpoints_matching(path_contains="/bc-comm/message/{job_no}-{p_id}")[0]
    with pytest.raises(ValueError) as exc_info:
        build_url(ep, [("job_no", "12355016")])
    assert "p_id" in str(exc_info.value)


# ── §3b: family-B HTTP 400 -> validation kind, 104's own detail flattened ───────────

def test_classify_family_b_400_is_validation_kind_with_flattened_detail():
    raw = _raw_from_wrapper("failure_send_validation.json")
    _key, ep = _endpoints_matching(path_contains="/bc-comm/message/{job_no}-{p_id}", family="B")[0]

    verdict = classify(ep, raw)

    assert verdict.ok is False
    assert verdict.kind == "validation"
    assert "content" in verdict.detail
    assert "required" in verdict.detail.lower()
    assert "缺少必要參數" not in verdict.detail


def test_classify_family_b_404_with_only_error_key_still_behaves_as_before():
    body = {"error": "找不到資料"}
    raw = _raw(404, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)
    _key, ep = _endpoints_matching(path_contains="/vipapi/resume/search")[0]

    verdict = classify(ep, raw)

    assert verdict.ok is False
    assert verdict.kind == "not_found"
    assert verdict.detail == "找不到資料"


# ── §1 constructions: all four pre-existing Endpoint(...) sites gain method= and
# the renamed header field — a construction failure here is the no-default rule
# doing its job at the exact call sites the plan enumerates. ────────────────────────

def test_all_declared_endpoints_construct_without_error():
    # ENDPOINTS itself is built at module import time — reaching this line at all
    # already proves every entry constructs cleanly under the required fields.
    # Fourteen today: the ten pinned pre-this-round (five pre-existing + three
    # messaging endpoints + the two §C8 additions: verify_session,
    # logout_session) + this round's four new §C7 endpoints (list_templates,
    # resolve_candidate_idno, event_last_info, send_willingness_event).
    # get_template is deliberately NOT among them - see T-68.
    assert len(ENDPOINTS) == 14


# =========================================================================
# T-67 (R2.1, R3.1, behavior) - the four new C7 endpoints are declared
# exactly as the design's table states: host="auth", family="B", method,
# path, and throttle_gated=True, item by item.
# =========================================================================

def _endpoint_by_exact_path(path: str):
    matches = [ep for ep in ENDPOINTS.values() if ep.path == path]
    assert len(matches) == 1, (
        f"expected exactly one declared endpoint with path == {path!r}, "
        f"found {matches!r}"
    )
    return matches[0]


# (path, method, is_list, inner_key, sibling_keys) - C7's table verbatim.
_NEW_C7_ENDPOINTS = [
    ("/bc-comm/template", "GET", True, None, ()),
    ("/bc-comm/message/resume/{job_no}-{p_id}", "GET", False, "idNo", ()),
    ("/bc-comm/event/last-info", "GET", False, None, ()),
    ("/bc-comm/event/willingness", "POST", True, None, ("failed",)),
]


def test_the_four_new_c7_endpoints_are_declared_exactly_per_the_table():
    for path, method, is_list, inner_key, sibling_keys in _NEW_C7_ENDPOINTS:
        ep = _endpoint_by_exact_path(path)
        assert ep.host == "auth", f"{path}: expected host=auth, got {ep.host!r}"
        assert ep.family == "B", f"{path}: expected family=B, got {ep.family!r}"
        assert ep.method == method, f"{path}: expected method={method!r}, got {ep.method!r}"
        assert ep.throttle_gated is True, f"{path}: expected throttle_gated=True"
        assert ep.family_b_shape is not None, f"{path}: expected a family_b_shape"
        assert ep.family_b_shape.is_list == is_list, (
            f"{path}: expected is_list={is_list!r}, got {ep.family_b_shape.is_list!r}"
        )
        assert ep.family_b_shape.inner_key == inner_key, (
            f"{path}: expected inner_key={inner_key!r}, got {ep.family_b_shape.inner_key!r}"
        )
        assert tuple(ep.family_b_shape.sibling_keys) == sibling_keys, (
            f"{path}: expected sibling_keys={sibling_keys!r}, "
            f"got {ep.family_b_shape.sibling_keys!r}"
        )


# =========================================================================
# T-68 (R2.5, behavior) - get_template is deliberately NOT declared: C7
# says no caller ever needs a single-template fetch. Distinct from the
# housekeeping len(ENDPOINTS) == 14 update above.
# =========================================================================

def test_get_template_endpoint_is_not_declared():
    assert not any(
        ep.path == "/bc-comm/template/{id}" for ep in ENDPOINTS.values()
    ), "get_template (GET /bc-comm/template/{id}) must not be registered - C7"


# =========================================================================
# T-69 (api_client.classify, interface) - sibling_keys copy semantics:
# declared+present -> copied into payload; declared+absent -> absent from
# payload (never None); never-declared top-level key -> never copied.
# =========================================================================

def test_classify_copies_declared_sibling_keys_only_when_present_and_never_undeclared_ones():
    ep = Endpoint(
        key="t69_probe", host="auth", path="/api/probe", method="POST", family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=("failed",)),
        throttle_gated=True,
    )

    # Case 1: the declared sibling key ("failed") is present -> copied verbatim.
    body_present = {"data": [{"eventId": "SYNTHETIC-EVENT-1"}], "metadata": {}, "failed": ["x"]}
    raw_present = _raw(200, "application/json; charset=utf-8",
                        json.dumps(body_present, ensure_ascii=False), body_present)
    verdict_present = classify(ep, raw_present)
    assert verdict_present.ok is True
    assert verdict_present.payload is not None
    assert verdict_present.payload.get("failed") == ["x"]

    # Case 2: the declared sibling key is absent -> must not appear at all,
    # and in particular must never be synthesized as None.
    body_absent = {"data": [{"eventId": "SYNTHETIC-EVENT-2"}], "metadata": {}}
    raw_absent = _raw(200, "application/json; charset=utf-8",
                       json.dumps(body_absent, ensure_ascii=False), body_absent)
    verdict_absent = classify(ep, raw_absent)
    assert verdict_absent.ok is True
    assert verdict_absent.payload is not None
    assert "failed" not in verdict_absent.payload, (
        "an absent declared sibling key must be OMITTED from payload, not filled with None"
    )

    # Case 3: an undeclared top-level key ("extra") must never be copied,
    # even though it sits right alongside data/metadata in the envelope.
    body_undeclared = {
        "data": [{"eventId": "SYNTHETIC-EVENT-3"}], "metadata": {},
        "extra": "SYNTHETIC-SHOULD-NOT-LEAK",
    }
    raw_undeclared = _raw(200, "application/json; charset=utf-8",
                           json.dumps(body_undeclared, ensure_ascii=False), body_undeclared)
    verdict_undeclared = classify(ep, raw_undeclared)
    assert verdict_undeclared.ok is True
    assert verdict_undeclared.payload is not None
    assert "extra" not in verdict_undeclared.payload


# =========================================================================
# T-70 (api_client.FamilyBShape, interface) - declaring "data" or "metadata"
# as a sibling key is a __post_init__ construction error: those two keys
# already have their own extraction path (is_list/inner_key), so letting
# them in as siblings too would give each a second route into payload.
# =========================================================================

def test_family_b_shape_rejects_data_or_metadata_as_a_sibling_key():
    with pytest.raises(Exception):
        FamilyBShape(is_list=True, inner_key=None, sibling_keys=("data",))

    with pytest.raises(Exception):
        FamilyBShape(is_list=True, inner_key=None, sibling_keys=("metadata",))


# =========================================================================
# T-71 (R2.6, behavior) - resolve_candidate_idno's response body missing
# idNo is judged malformed by classify()'s inner_key floor, at the
# transport layer, before any tool-level code ever sees it.
# =========================================================================

def test_resolve_candidate_idno_response_missing_idno_classifies_as_malformed():
    ep = Endpoint(
        key="t71_probe_resolve_candidate_idno", host="auth",
        path="/bc-comm/message/resume/{job_no}-{p_id}", method="GET", family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=False, inner_key="idNo", sibling_keys=()),
        throttle_gated=True,
    )
    body = {"data": {}, "metadata": {}}  # data present, but idNo missing entirely
    raw = _raw(200, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)

    verdict = classify(ep, raw)

    assert verdict.ok is False
    assert verdict.kind == "malformed"


# =========================================================================
# T-72 (api_client.classify, interface) - a failed verdict (ok=False) never
# gets a payload assembled, even when the response body carries a declared
# sibling key.
# =========================================================================

def test_classify_does_not_assemble_payload_for_a_failed_verdict_even_with_a_sibling_key_present():
    ep = Endpoint(
        key="t72_probe", host="auth", path="/api/probe", method="POST", family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(is_list=True, inner_key=None, sibling_keys=("failed",)),
        throttle_gated=True,
    )
    body = {"error": "SYNTHETIC-NOT-FOUND", "failed": ["SYNTHETIC-SHOULD-NOT-LEAK"]}
    raw = _raw(404, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)

    verdict = classify(ep, raw)

    assert verdict.ok is False
    assert verdict.payload is None
