"""Transport/guard tests: Endpoint, classify, guarded_api, and the auth-host predicate.

Written BLIND to browser/api_client.py's and tools/helpers.py's *logic*: every
assertion below is derived from the declared public interfaces plus
docs/104-site-facts.md's measured facts, never from reading what a function
decides. Structural mismatches that surfaced only as bare `TypeError`s on the first
run (module-level pure dataclasses that are public interfaces — the
`Endpoint`/`RawResponse`/`Verdict` field names, and `guarded_api`'s parameter types)
were resolved via `inspect.signature()` on the already-implemented functions rather
than by guessing repeatedly against the live system or by opening the module's
source — see this module's completion report for the exact call made and why that
line was judged not to cross into reading decision logic. Nothing below was adjusted
after seeing which body shapes a function accepts or rejects.

Cases: T-19, T-20, T-21, T-22, T-23, T-24, T-25, T-44, T-45, T-46, T-54, T-55, T-56, T-57.

Seams: the pure decision functions are never mocked; the HTTP transport is the
seam that is substituted. `fetch` is monkeypatched to return canned `RawResponse`s
built from tests/fixtures/.
There is no BrowserContext seam left post-login — `guarded_api` reads
credentials straight off `SessionInfo.cookies`, so a session under test is built
by handing that field a cookie list directly. `classify`, `select_cookies_for_host`
and `matches_auth_host` are exercised directly and are never mocked.
"""
from __future__ import annotations

import asyncio
import json

import pytest

from tests.conftest import load_fixture
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



# ── Fixture / fake-transport plumbing ────────────────────────────────────


def _raw(status: int, content_type: str, body: str, parsed_json, location: str | None = None) -> RawResponse:
    return RawResponse(
        status=status,
        location=location,
        content_type=content_type,
        body=body,
        body_bytes=body.encode("utf-8"),
        parsed_json=parsed_json,
    )


def _raw_from_wrapper(fixture_name: str) -> RawResponse:
    d = load_fixture(fixture_name)
    return _raw(d["http_status"], d["content_type"], d["body_text"], d["body_json"])


def _raw_from_bare_json(fixture_name: str, http_status: int = 200) -> RawResponse:
    body_json = load_fixture(fixture_name)
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
    pacing rework replaced the drawn-delay distribution
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
    # there is no BrowserContext left to fake post-login.
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return pool, ctx, info


def _endpoints_matching(*, family: str | None = None, host: str | None = None,
                         path_contains: str | None = None, method: str | None = None):
    """Look endpoints up by declared property rather than by guessing the
    ENDPOINTS dict's key strings — the keys are not part of the public
    contract, so matching on `.path`/`.family`/
    `.host` (all explicitly declared) is the only
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
        if method is not None and ep.method != method:
            continue
        out.append((key, ep))
    return out


def _headers_lower(ep: Endpoint) -> set[str]:
    # T-44 is an ORACLE, not a fixture: it cross-checks every declared endpoint
    # against docs/104-site-facts.md §6b.3h's independently-built reference table
    # (the docs->declaration direction). Repaired to read the NAME out of each
    # declared (name, value) pair after `required_headers` (a frozenset of bare
    # names) became `extra_headers` (name/value pairs) — the property this
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
    # The company job list (list_jobs) moved to the apply route;
    # /job-api/jobs/apply carries applyCount60 and the same 21-job set in the same order.
    ("/job-api/jobs/apply", "auth", "B", None, False),
    # Three more auth-host routes, none measured for Referer.
    ("/api/apply/applyDetail", "auth", "A", None, False),
    ("/vipapi/search/match-number", "auth", "B", None, False),
    ("/vipapi/job/over-seven-day-job", "auth", "B", None, False),
    # The dated job list and the job editor's load request: /api/job/ paths on the auth
    # host (the vip host's /api/* paths are different routes), neither measured for Referer.
    ("/api/job/jobList", "auth", "A", None, False),
    ("/api/job/jobUpdate", "auth", "A", None, False),
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
    # Every row of the table is a GET route; over-seven-day-job also has a PUT
    # endpoint on the same path, so the lookup names the method.
    for path_sub, host, family, measured, declared in _REFERENCE_ENDPOINTS:
        matches = _endpoints_matching(path_contains=path_sub, method="GET")
        assert len(matches) == 1, (
            f"expected exactly one declared GET endpoint whose path contains "
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


# ── T-61 (interface): the three applicants-and-job-reads routes, by their ENDPOINTS keys ──
#
# The reference table above carries host, family and the Referer declaration only; method
# and throttle_gated are what T-61 also states, so they are checked here, by key.

def test_list_applicants_match_number_and_refreshable_jobs_are_get_auth_and_throttle_gated():
    expected = {
        "list_applicants": ("/api/apply/applyDetail", "A"),
        "match_number": ("/vipapi/search/match-number", "B"),
        "list_refreshable_jobs": ("/vipapi/job/over-seven-day-job", "B"),
    }
    for key, (path, family) in expected.items():
        ep = ENDPOINTS[key]
        assert ep.path == path, f"{key}: expected path {path!r}, got {ep.path!r}"
        assert ep.host == "auth", f"{key}: expected host 'auth', got {ep.host!r}"
        assert ep.family == family, f"{key}: expected family {family!r}, got {ep.family!r}"
        assert ep.method == "GET", f"{key}: expected method GET, got {ep.method!r}"
        assert ep.throttle_gated is True, f"{key}: expected throttle_gated=True"


def test_refresh_jobs_is_the_put_side_of_the_refreshable_list_path():
    ep = ENDPOINTS["refresh_jobs"]
    assert ep.host == "auth"
    assert ep.method == "PUT"
    assert ep.family == "B"
    assert ep.throttle_gated is True
    assert ep.path == ENDPOINTS["list_refreshable_jobs"].path


def test_job_list_and_get_job_detail_are_family_a_gets_on_the_auth_host():
    for key, path in (("job_list", "/api/job/jobList"), ("get_job_detail", "/api/job/jobUpdate")):
        ep = ENDPOINTS[key]
        assert (ep.host, ep.path, ep.method, ep.family) == ("auth", path, "GET", "A"), key
        assert ep.extra_headers == (), key
        assert ep.throttle_gated is True, key


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
    family_a_body = load_fixture("zero_row_search.json")          # status=SUCCESS, top-level data=null
    family_b_body = load_fixture("resume_unrestricted.json")      # {data:{...}, metadata:{...}}, no "status"

    raw_a = _raw(200, "application/json; charset=utf-8",
                 json.dumps(family_a_body, ensure_ascii=False), family_a_body)
    raw_b = _raw(200, "application/json; charset=utf-8",
                 json.dumps(family_b_body, ensure_ascii=False), family_b_body)

    ep_a = Endpoint(key="t46_probe_a", host="vip", path="/api/probe", method="GET", body_encoding=None, family="A",
                     extra_headers=(), family_b_shape=None, throttle_gated=True)
    # inner_key="resume", data_kind="object" mirrors resume_unrestricted.json's
    # real shape ({"data": {"resume": {...}, ...}, "metadata": {...}}) and the
    # real get_resume_detail endpoint's own declared shape.
    ep_b = Endpoint(key="t46_probe_b", host="vip", path="/api/probe", method="GET", body_encoding=None, family="B",
                     extra_headers=(),
                     family_b_shape=FamilyBShape(data_kind="object", inner_key="resume", sibling_keys=()), throttle_gated=True)

    # Correct family declared for each body: both succeed.
    assert classify(ep_a, raw_a).ok is True
    assert classify(ep_b, raw_b).ok is True

    # Same two bodies, family swapped: family selection must flip the outcome.
    # If classify() sniffed the body shape instead of reading endpoint.family,
    # these two would classify identically to the pair above.
    verdict_a_declared_b = classify(ep_b, raw_a)
    assert verdict_a_declared_b.ok is False, (
        "family-A body's top-level `data` is null, which fails family B's "
        "'data present and a mapping' floor — accepting it "
        "means family wasn't actually taken from the endpoint declaration"
    )

    verdict_b_declared_a = classify(ep_a, raw_b)
    assert verdict_b_declared_a.ok is False, (
        "family-B body carries no 'status' key at all, so family A's "
        "status==SUCCESS check must reject it — admitting it here is exactly "
        "the 'default the key to success' failure mode, "
        "and would mean every family-B error is silently accepted too"
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
    # (it had only been verified by hand), so it is exactly the
    # kind of construction-time guard that quietly becomes optional if a
    # later refactor drops the check and nothing goes red.
    with pytest.raises(ValueError):
        Endpoint(key="probe_b_missing_shape", host="vip", path="/api/probe", method="GET", body_encoding=None, family="B",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)

    with pytest.raises(ValueError):
        Endpoint(key="probe_a_with_shape", host="vip", path="/api/probe", method="GET", body_encoding=None, family="A",
                 extra_headers=(),
                 family_b_shape=FamilyBShape(data_kind="object", inner_key="resume", sibling_keys=()), throttle_gated=True)


def test_opaque_family_endpoint_classifies_a_bare_redirect_as_ok():
    # Family "non_json" was renamed to "opaque" (Verdict.kind for
    # the non-JSON *failure* case is unaffected — it stays "non_json", see
    # the neighbouring family-A non-JSON case above). An "opaque" endpoint,
    # declared the same shape logout_session uses (family_b_shape=None,
    # throttle_gated=False), must classify a plain 302 with no body and a
    # non-JSON content-type as success without ever touching family B's
    # data_kind/inner_key shape logic (there is none to touch: family_b_shape
    # is None for this endpoint).
    ep = Endpoint(key="probe_opaque", host="vip", path="/api/probe", method="POST", body_encoding="json", family="opaque",
                  extra_headers=(), family_b_shape=None, throttle_gated=False)
    raw = _raw(302, "text/html", "", None, location="https://vip.104.com.tw/rms/index")

    verdict = classify(ep, raw)

    assert verdict.ok is True


def test_endpoint_construction_rejects_unknown_family():
    # Only "A", "B" and "opaque" are valid `family` values. "C"
    # (never a real family) and "non_json" (the old, pre-rename spelling)
    # must both be rejected at construction, alongside the existing method
    # whitelist case's ValueError-on-construction pattern.
    with pytest.raises(ValueError):
        Endpoint(key="probe_family_c", host="vip", path="/api/probe", method="GET", body_encoding=None, family="C",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)

    with pytest.raises(ValueError):
        Endpoint(key="probe_family_old_name", host="vip", path="/api/probe", method="GET", body_encoding=None, family="non_json",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)


# ── T-56 (interface): matches_auth_host ──────────────────────────────────

def test_auth_vip_host_is_not_classified_as_auth_host():
    # auth.vip.104.com.tw is a working host, not an authentication host, and
    # must return false.
    assert matches_auth_host("auth.vip.104.com.tw") is False
    assert matches_auth_host("vip.104.com.tw") is False
    # Positive control — docs/104-site-facts.md line 28 and tests/test_session.py:
    # these ARE the login/auth hosts, so the predicate isn't just always-false.
    assert matches_auth_host("bsignin.104.com.tw") is True
    assert matches_auth_host("boidc.104.com.tw") is True


# ── T-19..T-25, T-57 (behavior/e2e): guarded_api's error mapping ─────────
# All routed through guarded_api (not classify() directly): guarded_api maps
# every failure scenario to its own abort payload by raising GuardAbort
# subclasses, and T-19's claim ("no results key") is about that final
# tool-facing payload, not classify()'s raw Verdict. GuardAbort's
# `except GuardAbort as e: return e.payload` convention is reused as-is and
# confirmed by tests/test_helpers.py.

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

    # An unrecognised status is reported with its status and message verbatim.
    text = json.dumps(exc_info.value.payload, ensure_ascii=False)
    assert "ANOTHER_NOVEL_STATUS_7c21" in text
    assert "唯一可辨識字串XYZ789" in text


@pytest.mark.asyncio
async def test_family_b_healthy_body_accepted_and_family_b_error_rejected(monkeypatch):
    # T-21's complementary pair: a healthy family-B
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
    # (expired-session detection), which is exactly what the check must key on.
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
    # page, no longer exists; the fixture below is this file's
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
    # synthesized as an HTTP 404 with an HTML body, which resembles nothing
    # about "wrong host".
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
    # — logout()+login() replacing the pool entry with a
    # brand-new SessionInfo while a second caller is still parked on the old
    # entry's lock. The guarded_page-era sibling of this test
    # (tests/test_helpers.py::test_guarded_page_rejects_when_pool_entry_replaced_while_queued)
    # no longer exists — guarded_page itself is gone — so this is
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
            body_bytes=b"",
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
    # Payload and kind are independent: the wording alone does
    # not prove this raise site declared "expired" rather than "transport" —
    # this is the site that must NOT fall in with the transport kind raised
    # earlier in guarded_api for a plain fetch() exception.
    assert exc_info.value.kind == "expired"


@pytest.mark.asyncio
async def test_guarded_api_throttled_rejection_carries_throttled_kind(monkeypatch):
    _pool, ctx, _info = _new_ctx_and_session()

    # enforce_throttle's contract: a rejection is a ThrottleAbort, not
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

    # /job-api/jobs/apply is the company job list endpoint — the "list tool" this case names.
    _job_list_key, job_list_ep = _endpoints_matching(path_contains="/job-api/jobs/apply")[0]

    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, job_list_ep, params=[]):
            pass  # pragma: no cover — must never be reached

    assert "results" not in exc_info.value.payload
    assert "已過期" in exc_info.value.payload.get("error", "")


# ── Endpoint.method (no default), fetch()'s POST dispatch, extra_headers ────────

def test_endpoint_method_has_no_default_and_is_required():
    with pytest.raises(TypeError):
        Endpoint(key="probe_no_method", host="vip", path="/api/probe", family="A",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)


@pytest.mark.parametrize("method", ["PATCH"])
def test_endpoint_construction_rejects_patch(method):
    with pytest.raises(ValueError):
        Endpoint(key="probe_bad_method", host="auth", path="/bc-comm/message/{job_no}-{p_id}",
                 method=method, body_encoding=None, family="B", extra_headers=(),
                 family_b_shape=FamilyBShape(data_kind="list", inner_key=None, sibling_keys=()), throttle_gated=True)


def test_endpoint_construction_accepts_put():
    ep = Endpoint(key="probe_put", host="auth", path="/api/probe", method="PUT", body_encoding="json", family="B",
                  extra_headers=(),
                  family_b_shape=FamilyBShape(data_kind="object", inner_key=None, sibling_keys=()),
                  throttle_gated=True)
    assert ep.method == "PUT"


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

    def put(self, url, *, headers, json, allow_redirects):
        _FakeAiohttpSession.calls.append(("PUT", url, dict(headers), json))
        return _FakeAiohttpResponse()


@pytest.fixture(autouse=True)
def _reset_fake_aiohttp_calls():
    _FakeAiohttpSession.calls = []
    yield


@pytest.mark.asyncio
async def test_fetch_dispatches_get_post_or_put_per_endpoint_method(monkeypatch):
    from mcp104.browser import api_client as api_client_mod
    monkeypatch.setattr(api_client_mod.aiohttp, "ClientSession", _FakeAiohttpSession)

    _key, get_ep = _endpoints_matching(path_contains="/job-api/jobs/apply")[0]
    await api_client_mod.fetch(get_ep, cookie_header="c=1", params=[])

    _key, post_ep = _endpoints_matching(path_contains="/bc-comm/message/all-stream")[0]
    await api_client_mod.fetch(post_ep, cookie_header="c=1", params=[], body={"content": "x"})

    await api_client_mod.fetch(
        api_client_mod.ENDPOINTS["refresh_jobs"], cookie_header="c=1",
        body={"jobs": [{"jobNo": "1"}]},
    )

    assert _FakeAiohttpSession.calls[0][0] == "GET"
    assert _FakeAiohttpSession.calls[0][3] is None
    assert _FakeAiohttpSession.calls[1][0] == "POST"
    assert _FakeAiohttpSession.calls[1][3] == {"content": "x"}
    assert _FakeAiohttpSession.calls[2][0] == "PUT"
    assert _FakeAiohttpSession.calls[2][1].endswith("/vipapi/job/over-seven-day-job")
    assert _FakeAiohttpSession.calls[2][3] == {"jobs": [{"jobNo": "1"}]}


@pytest.mark.asyncio
async def test_fetch_emits_every_declared_extra_header_verbatim_and_nothing_undeclared(monkeypatch):
    from mcp104.browser import api_client as api_client_mod
    monkeypatch.setattr(api_client_mod.aiohttp, "ClientSession", _FakeAiohttpSession)

    declared = Endpoint(key="probe_headers", host="vip", path="/api/probe", method="GET", body_encoding=None,
                         family="A", extra_headers=(("X-Test-Header", "test-value"),),
                         family_b_shape=None, throttle_gated=True)
    await api_client_mod.fetch(declared, cookie_header="c=1", params=[])

    _method, _url, headers, _body = _FakeAiohttpSession.calls[0]
    assert headers.get("X-Test-Header") == "test-value"

    undeclared = Endpoint(key="probe_no_headers", host="vip", path="/api/probe", method="GET", body_encoding=None,
                           family="A", extra_headers=(), family_b_shape=None, throttle_gated=True)
    await api_client_mod.fetch(undeclared, cookie_header="c=1", params=[])
    _method, _url, headers2, _body = _FakeAiohttpSession.calls[1]
    assert "Referer" not in headers2, "a name not declared in extra_headers must not be sent"
    assert "X-Test-Header" not in headers2


# ── build_url: two placeholders in one hyphen-joined path segment ──────────────────

def test_build_url_fills_two_placeholders_in_one_hyphen_joined_segment():
    from mcp104.browser.api_client import build_url
    _key, ep = _endpoints_matching(path_contains="/bc-comm/message/{job_no}-{p_id}")[0]
    url = build_url(ep, [
        ("job_no", "12355016"), ("p_id", "0577442"),
        ("page", "1"), ("perPage", "100"), ("sort", "ASC"),
    ])
    assert url == (
        "https://auth.vip.104.com.tw/bc-comm/message/12355016-0577442"
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


# ── Constructions: all four pre-existing Endpoint(...) sites gain method= and
# the renamed header field — a construction failure here is the no-default rule
# doing its job at the exact call sites the plan enumerates. ────────────────────────

def test_all_declared_endpoints_construct_without_error():
    # ENDPOINTS itself is built at module import time — reaching this line at all
    # already proves every entry constructs cleanly under the required fields.
    # Asserted by key, not by count, so a feature that adds routes does not touch
    # this test. get_template is deliberately NOT declared - see the test below.
    for key in (
        "verify_session", "logout_session", "search_resumes", "get_resume_detail",
        "send_message", "list_jobs",
        "resolve_candidate_idno", "event_last_info", "send_willingness_event",
        "send_interview_event", "send_thank_event", "send_inquire_event", "send_exam_event",
        "list_applicants", "match_number", "list_refreshable_jobs",
        "refresh_jobs", "job_list", "get_job_detail",
    ):
        assert key in ENDPOINTS, f"{key!r} is not declared in ENDPOINTS"


# =========================================================================
# T-67 (behavior) - the four message-event endpoints are declared
# exactly as the expected table states: host="auth", family="B", method,
# path, and throttle_gated=True, item by item.
# =========================================================================

def _endpoint_by_exact_path(path: str, method: str):
    matches = [ep for ep in ENDPOINTS.values() if ep.path == path and ep.method == method]
    assert len(matches) == 1, (
        f"expected exactly one declared {method} endpoint with path == {path!r}, "
        f"found {matches!r}"
    )
    return matches[0]


# (path, method, expect_list, inner_key, sibling_keys) - the expected table verbatim.
_MESSAGING_ENDPOINTS = [
    ("/bc-comm/template", "GET", True, None, ()),
    ("/bc-comm/message/resume/{job_no}-{p_id}", "GET", False, "idNo", ()),
    ("/bc-comm/event/last-info", "GET", False, None, ()),
    ("/bc-comm/event/willingness", "POST", True, None, ("failed",)),
]


def test_the_four_new_c7_endpoints_are_declared_exactly_per_the_table():
    for path, method, expect_list, inner_key, sibling_keys in _MESSAGING_ENDPOINTS:
        ep = _endpoint_by_exact_path(path, method)
        assert ep.host == "auth", f"{path}: expected host=auth, got {ep.host!r}"
        assert ep.family == "B", f"{path}: expected family=B, got {ep.family!r}"
        assert ep.method == method, f"{path}: expected method={method!r}, got {ep.method!r}"
        assert ep.throttle_gated is True, f"{path}: expected throttle_gated=True"
        assert ep.family_b_shape is not None, f"{path}: expected a family_b_shape"
        assert (ep.family_b_shape.data_kind == "list") == expect_list, (
            f"{path}: expected expect_list={expect_list!r}, got data_kind={ep.family_b_shape.data_kind!r}"
        )
        assert ep.family_b_shape.inner_key == inner_key, (
            f"{path}: expected inner_key={inner_key!r}, got {ep.family_b_shape.inner_key!r}"
        )
        assert tuple(ep.family_b_shape.sibling_keys) == sibling_keys, (
            f"{path}: expected sibling_keys={sibling_keys!r}, "
            f"got {ep.family_b_shape.sibling_keys!r}"
        )


# =========================================================================
# T-69 (api_client.classify, interface) - sibling_keys copy semantics:
# declared+present -> copied into payload; declared+absent -> absent from
# payload (never None); never-declared top-level key -> never copied.
# =========================================================================

def test_classify_copies_declared_sibling_keys_only_when_present_and_never_undeclared_ones():
    ep = Endpoint(
        key="t69_probe", host="auth", path="/api/probe", method="POST", body_encoding="json", family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(data_kind="list", inner_key=None, sibling_keys=("failed",)),
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
# already have their own extraction path (data_kind/inner_key), so letting
# them in as siblings too would give each a second route into payload.
# =========================================================================

def test_family_b_shape_rejects_data_or_metadata_as_a_sibling_key():
    with pytest.raises(Exception):
        FamilyBShape(data_kind="list", inner_key=None, sibling_keys=("data",))

    with pytest.raises(Exception):
        FamilyBShape(data_kind="list", inner_key=None, sibling_keys=("metadata",))


# =========================================================================
# T-71 (behavior) - resolve_candidate_idno's response body missing
# idNo is judged malformed by classify()'s inner_key floor, at the
# transport layer, before any tool-level code ever sees it.
# =========================================================================

def test_resolve_candidate_idno_response_missing_idno_classifies_as_malformed():
    ep = Endpoint(
        key="t71_probe_resolve_candidate_idno", host="auth",
        path="/bc-comm/message/resume/{job_no}-{p_id}", method="GET", body_encoding=None, family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(data_kind="object", inner_key="idNo", sibling_keys=()),
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
        key="t72_probe", host="auth", path="/api/probe", method="POST", body_encoding="json", family="B",
        extra_headers=(),
        family_b_shape=FamilyBShape(data_kind="list", inner_key=None, sibling_keys=("failed",)),
        throttle_gated=True,
    )
    body = {"error": "SYNTHETIC-NOT-FOUND", "failed": ["SYNTHETIC-SHOULD-NOT-LEAK"]}
    raw = _raw(404, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)

    verdict = classify(ep, raw)

    assert verdict.ok is False
    assert verdict.payload is None


# =========================================================================
# The asset host: ASSET_ROUTES, the two closed tables, validate_asset_url,
# the shared classify prologue, and classify_asset's decision order.
# Every byte below is synthetic — hand-built file headers, never a real
# photo, attachment or filename.
# =========================================================================

from mcp104.browser.api_client import (  # noqa: E402
    ASSET_MAX_BYTES,
    ASSET_ROUTES,
    AssetRoute,
    EXPIRY_MARKER,
    _classify_prologue,
    classify_asset,
    detect_magic,
    validate_asset_url,
)

# Minimal, synthetic file headers. Each is the real signature plus filler —
# enough for magic detection, and containing nothing from any real file.
_SYNTH_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
_SYNTH_PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
_SYNTH_PDF = b"%PDF-1.4\n% synthetic\n" + b"\x00" * 32
_SYNTH_GIF = b"GIF89a" + b"\x00" * 32
_SYNTH_ZIP = b"PK\x03\x04" + b"\x00" * 32
# The measured not-authenticated page: HTTP 200, ~500 bytes of HTML holding
# one <script> that sets location.href. Synthesised to that shape.
_NOT_AUTH_HTML = (
    '<html><head><script>location.href="https://vip.104.com.tw/";</script>'
    "</head><body></body></html>"
)

_PHOTO_ROUTE = ASSET_ROUTES["candidate_photo"]
_ATTACH_ROUTE = ASSET_ROUTES["resume_attachment"]
_PHOTO_URL = "https://asset.vip.104.com.tw/download/webHeadShot?v=SYNTHETIC%2FTOKEN%3D"
_ATTACH_URL = "https://asset.vip.104.com.tw/download/resumeAttach/SYNTHETICTOKEN"


def _asset_raw(status=200, *, body_bytes=b"", body=None, content_type=None, location=None):
    """A RawResponse shaped the way fetch_asset produces one: `body` is the
    TEXT PROJECTION — empty once the bytes are a known file family."""
    if body is None:
        body = "" if detect_magic(body_bytes) is not None else body_bytes[:4096].decode("utf-8", errors="replace")
    return RawResponse(status=status, location=location, content_type=content_type,
                       body=body, body_bytes=body_bytes, parsed_json=None)


def test_asset_routes_table_is_declared_and_self_consistent():
    assert len(ASSET_ROUTES) == 2
    for key, route in ASSET_ROUTES.items():
        assert route.key == key
        assert route.host == "asset"
        assert isinstance(route.accepted_magic, tuple) and route.accepted_magic


def test_asset_routes_declare_the_measured_fields_verbatim():
    assert _PHOTO_ROUTE.path_prefix == "/download/webHeadShot"
    assert _PHOTO_ROUTE.cookie_required is False
    assert _PHOTO_ROUTE.referer_required is False
    assert _PHOTO_ROUTE.accepted_magic == ("jpeg", "png")

    assert _ATTACH_ROUTE.path_prefix == "/download/resumeAttach/"
    assert _ATTACH_ROUTE.cookie_required is True
    assert _ATTACH_ROUTE.referer_required is False
    assert _ATTACH_ROUTE.accepted_magic == ("jpeg", "pdf", "png")


def test_the_two_declaration_tables_are_each_closed():
    # An Endpoint may not address the file host...
    with pytest.raises(ValueError):
        Endpoint(key="x", host="asset", path="/p", method="GET", body_encoding=None, family="A",
                 extra_headers=(), family_b_shape=None, throttle_gated=True)
    # ...and an AssetRoute may not address a JSON host.
    with pytest.raises(ValueError):
        AssetRoute(key="x", host="auth", path_prefix="/p", cookie_required=True,
                   referer_required=False, accepted_magic=("pdf",))


# -- validate_asset_url ---------------------------------------------------

def test_validate_asset_url_accepts_a_real_asset_url_and_keeps_the_query_verbatim():
    assert validate_asset_url(_PHOTO_ROUTE, _PHOTO_URL) is None
    # The token is percent-encoded already; nothing here re-encodes or
    # rebuilds the URL, so what was validated is what gets fetched.
    assert "%2FTOKEN%3D" in _PHOTO_URL
    assert validate_asset_url(_ATTACH_ROUTE, _ATTACH_URL) is None


@pytest.mark.parametrize("url,expected_check,expected_hostname", [
    ("http://asset.vip.104.com.tw/download/webHeadShot?v=x", "scheme", None),
    ("https://vip.104.com.tw/download/webHeadShot?v=x", "host", "vip.104.com.tw"),
    # An endswith-based check would let this through; an exact match must not.
    ("https://asset.vip.104.com.tw.evil.example/download/webHeadShot?v=x",
     "host", "asset.vip.104.com.tw.evil.example"),
    ("https://asset.vip.104.com.tw/download/somethingElse?v=x", "path", None),
])
def test_validate_asset_url_refuses_each_wrong_shape(url, expected_check, expected_hostname):
    problem = validate_asset_url(_PHOTO_ROUTE, url)
    assert problem is not None
    assert problem.check == expected_check
    assert problem.hostname == expected_hostname


def test_validate_asset_url_problem_never_carries_the_url_itself():
    problem = validate_asset_url(_PHOTO_ROUTE, "https://evil.example/download/webHeadShot?v=SECRETTOKEN")
    assert "SECRETTOKEN" not in repr(problem)
    assert "/download" not in repr(problem)


# -- the shared prologue --------------------------------------------------

@pytest.mark.parametrize("raw,expected_kind", [
    (RawResponse(status=200, location=None, content_type="text/html", body="<a>" + EXPIRY_MARKER + "</a>",
                 body_bytes=b"", parsed_json=None), "expired"),
    (RawResponse(status=302, location="https://vip.104.com.tw" + EXPIRY_MARKER, content_type="text/html",
                 body="", body_bytes=b"", parsed_json=None), "expired"),
    (RawResponse(status=403, location=None, content_type="text/html", body="denied",
                 body_bytes=b"denied", parsed_json=None), "blocked"),
])
def test_classify_and_classify_asset_share_one_prologue(raw, expected_kind):
    # The same two checks, in the same order, reached from both entry
    # points — one function, not two copies that agree today.
    assert _classify_prologue(raw).kind == expected_kind
    assert classify(ENDPOINTS["search_resumes"], raw).kind == expected_kind
    assert classify_asset(_PHOTO_ROUTE, raw).kind == expected_kind


def test_classify_prologue_returns_none_when_neither_check_fires():
    assert _classify_prologue(_asset_raw(body_bytes=_SYNTH_JPEG)) is None


def test_asset_expiry_is_found_in_the_location_header_with_an_empty_body():
    # The Location half of the expiry check: an unfollowed 3xx to the
    # company-switch page is NOT an auth host and carries an empty body, so
    # neither the hostname check nor a body scan would see it.
    raw = _asset_raw(status=302, location="https://vip.104.com.tw" + EXPIRY_MARKER)
    assert classify_asset(_ATTACH_ROUTE, raw).kind == "expired"


# -- classify_asset's decision order --------------------------------------

@pytest.mark.parametrize("status", [401, 404, 500])
def test_any_non_200_is_unrecognised_status_not_a_body_based_verdict(status):
    # Deliberately NOT asset_not_authenticated and NOT asset_unknown_format:
    # a 404 carrying HTML must not be described as "not signed in", nor have
    # its first 8 bytes printed as a format signature. Also a deliberate
    # divergence from _classify_family_b, which maps 401 to expired.
    raw = _asset_raw(status=status, body_bytes=_NOT_AUTH_HTML.encode("utf-8"))
    verdict = classify_asset(_ATTACH_ROUTE, raw)
    assert verdict.ok is False
    assert verdict.kind == "unrecognised_status"
    assert str(status) in verdict.detail


def test_403_is_blocked_on_the_asset_path_too():
    raw = _asset_raw(status=403, body_bytes=b"denied")
    assert classify_asset(_ATTACH_ROUTE, raw).kind == "blocked"


@pytest.mark.parametrize("route,body_bytes,expected_format", [
    (_PHOTO_ROUTE, _SYNTH_JPEG, "jpeg"),
    (_PHOTO_ROUTE, _SYNTH_PNG, "png"),
    (_ATTACH_ROUTE, _SYNTH_JPEG, "jpeg"),
    (_ATTACH_ROUTE, _SYNTH_PNG, "png"),
    (_ATTACH_ROUTE, _SYNTH_PDF, "pdf"),
])
def test_whitelisted_magic_succeeds_and_carries_format_plus_bytes(route, body_bytes, expected_format):
    verdict = classify_asset(route, _asset_raw(body_bytes=body_bytes))
    assert verdict.ok is True
    assert verdict.payload == {"format": expected_format, "body_bytes": body_bytes}


def test_pdf_is_not_accepted_on_the_photo_route():
    # accepted_magic is per route, not per host.
    assert classify_asset(_PHOTO_ROUTE, _asset_raw(body_bytes=_SYNTH_PDF)).kind == "asset_unknown_format"


def test_gif_is_refused_even_when_104_declares_image_gif():
    raw = _asset_raw(body_bytes=_SYNTH_GIF, content_type="image/gif")
    verdict = classify_asset(_PHOTO_ROUTE, raw)
    assert verdict.kind == "asset_unknown_format"


def test_pk_signature_is_refused_there_is_no_zip_whitelist():
    verdict = classify_asset(_ATTACH_ROUTE, _asset_raw(body_bytes=_SYNTH_ZIP))
    assert verdict.kind == "asset_unknown_format"
    assert verdict.detail.startswith(_SYNTH_ZIP[:8].hex())


def test_the_measured_not_authenticated_html_is_its_own_kind():
    raw = _asset_raw(body_bytes=_NOT_AUTH_HTML.encode("utf-8"), content_type="text/html; charset=utf-8")
    assert classify_asset(_ATTACH_ROUTE, raw).kind == "asset_not_authenticated"


def test_empty_body_is_its_own_kind_and_offers_no_signature():
    verdict = classify_asset(_ATTACH_ROUTE, _asset_raw(body_bytes=b""))
    assert verdict.kind == "asset_empty_body"
    # There are zero bytes; nothing downstream may claim to show eight.
    assert verdict.detail == ""


def test_unknown_bytes_carry_eight_hex_bytes_and_the_declared_content_type():
    junk = bytes(range(8, 40))
    verdict = classify_asset(_ATTACH_ROUTE, _asset_raw(body_bytes=junk, content_type="application/octet-stream"))
    assert verdict.kind == "asset_unknown_format"
    signature, _, content_type = verdict.detail.partition("|")
    assert signature == junk[:8].hex()
    assert content_type == "application/octet-stream"


def test_the_size_check_runs_before_the_magic_check():
    # Over the limit, but starting with a perfectly valid JPEG signature:
    # size must win, or an oversized file gets accepted and written.
    oversized = _SYNTH_JPEG + b"\x00" * (ASSET_MAX_BYTES + 1 - len(_SYNTH_JPEG))
    assert len(oversized) == ASSET_MAX_BYTES + 1
    assert classify_asset(_PHOTO_ROUTE, _asset_raw(body_bytes=oversized)).kind == "asset_too_large"


def test_exactly_the_limit_is_not_too_large():
    at_limit = _SYNTH_PDF + b"\x00" * (ASSET_MAX_BYTES - len(_SYNTH_PDF))
    assert classify_asset(_ATTACH_ROUTE, _asset_raw(body_bytes=at_limit)).ok is True


def test_detect_magic_has_one_table_shared_by_fetch_asset_and_classify_asset():
    # fetch_asset decides RawResponse.body from detect_magic; classify_asset
    # decides success from the same call. Same bytes, same conclusion — the
    # projection half is asserted against the real fetch_asset in
    # tests/test_helpers.py (this file has no HTTP seam to drive it with).
    for body_bytes, expected in ((_SYNTH_JPEG, "jpeg"), (_SYNTH_PNG, "png"),
                                 (_SYNTH_PDF, "pdf"), (_SYNTH_GIF, None), (b"", None)):
        assert detect_magic(body_bytes) == expected
        verdict = classify_asset(_ATTACH_ROUTE, _asset_raw(body_bytes=body_bytes))
        # A route accepting jpeg/pdf/png succeeds on exactly the bytes
        # detect_magic named, and on nothing else.
        assert verdict.ok is (expected in _ATTACH_ROUTE.accepted_magic)


def test_a_binary_containing_marker_strings_is_still_a_successful_fetch():
    # Half of the false-positive defence: the text projection a poisoned
    # JPEG produces, and classify's verdict on it. The scans those markers
    # would trip (`_detect_cloudflare_challenge`, the expiry substring) run
    # in _issue_one, ahead of classify, so they are unreachable from here —
    # tests/test_helpers.py drives the same bytes through guarded_sequence
    # to cover that half end to end.
    poisoned = _SYNTH_JPEG + "正在執行安全驗證".encode("utf-8") + EXPIRY_MARKER.encode("utf-8")
    raw = _asset_raw(body_bytes=poisoned)
    assert raw.body == ""
    assert _classify_prologue(raw) is None
    verdict = classify_asset(_PHOTO_ROUTE, raw)
    assert verdict.ok is True
    assert verdict.payload["format"] == "jpeg"
# =========================================================================
# T-57 (ENDPOINTS, interface) - the four event-route endpoints are declared
# like send_willingness_event, and `failed` rides along as a sibling key.
# =========================================================================

_EVENT_ROUTE_ENDPOINTS = [
    ("send_interview_event", "/bc-comm/event/interview"),
    ("send_thank_event", "/bc-comm/event/thank"),
    ("send_inquire_event", "/bc-comm/event/inquire"),
    ("send_exam_event", "/bc-comm/event/exam"),
]


@pytest.mark.parametrize("key, path", _EVENT_ROUTE_ENDPOINTS)
def test_event_route_endpoint_is_declared_like_willingness(key, path):
    ep = ENDPOINTS[key]
    assert ep.key == key
    assert ep.path == path
    assert ep.host == "auth"
    assert ep.method == "POST"
    assert ep.family == "B"
    assert ep.throttle_gated is True
    assert ep.family_b_shape is not None
    assert ep.family_b_shape.data_kind == "list"
    assert ep.family_b_shape.inner_key is None
    assert tuple(ep.family_b_shape.sibling_keys) == ("failed",)


@pytest.mark.parametrize("key", [k for k, _p in _EVENT_ROUTE_ENDPOINTS])
def test_event_route_failed_is_present_empty_or_absent_on_the_ok_payload(key):
    ep = ENDPOINTS[key]
    base = {"data": [{"pId": "073748", "streamId": "073748_12355016"}], "metadata": {}}

    def _verdict(body):
        return classify(ep, _raw(200, "application/json; charset=utf-8",
                                 json.dumps(body, ensure_ascii=False), body))

    present = _verdict({**base, "failed": [{"x": 1}]})
    assert present.ok is True
    assert present.payload["failed"] == [{"x": 1}]

    empty = _verdict({**base, "failed": []})
    assert empty.ok is True
    assert empty.payload["failed"] == []

    absent = _verdict(dict(base))
    assert absent.ok is True
    assert "failed" not in absent.payload


# ── applicants-and-job-reads: T-56, T-57, T-58 ────────────────────────────────────
#
# The company-switch envelope and the family-A non-SUCCESS shapes, through the real
# classify() with nothing mocked. Named by behaviour; the case IDs sit in the comments
# only, because this file already carries T-nn names from an earlier cycle.

_COMPANY_SWITCH_FIXTURE = "failure_applicants_company_switch.json"
_BACKSLASH_SLASH = chr(92) + "/"  # the JSON escape for "/", as it sits in a raw body
_COMPANY_SWITCH_ENDPOINTS = ("list_applicants", "search_resumes")


def test_company_switch_envelope_with_json_escaped_url_is_expired_on_both_family_a_endpoints():
    # T-56
    d = load_fixture(_COMPANY_SWITCH_FIXTURE)
    # Precondition: the fixture must really carry the JSON-escaped slashes. Without them
    # this test would pass without exercising the escape at all.
    assert d["http_status"] == 200
    assert d["content_type"].startswith("text/html")
    assert "COMPANY_SWITCH" in d["body_text"]
    assert (_BACKSLASH_SLASH + "company" + _BACKSLASH_SLASH + "status") in d["body_text"]

    raw = _raw_from_wrapper(_COMPANY_SWITCH_FIXTURE)
    for key in _COMPANY_SWITCH_ENDPOINTS:
        verdict = classify(ENDPOINTS[key], raw)
        assert verdict.ok is False, f"{key}: a logged-out COMPANY_SWITCH envelope must be an error"
        assert verdict.kind == "expired", (
            f"{key}: a logged-out COMPANY_SWITCH envelope must read as an expired session, "
            f"got kind={verdict.kind!r}"
        )


def test_company_switch_envelope_with_unescaped_url_is_expired_and_success_bodies_stay_success():
    # T-57
    d = load_fixture(_COMPANY_SWITCH_FIXTURE)
    body_json = d["body_json"]
    assert body_json["status"] == "COMPANY_SWITCH"
    # The same envelope, serialised without the JSON escape on "/".
    unescaped_text = json.dumps(body_json, ensure_ascii=False)
    assert "/company/status/switchCompany" in unescaped_text
    assert _BACKSLASH_SLASH not in unescaped_text
    raw = _raw(d["http_status"], d["content_type"], unescaped_text, body_json)
    for key in _COMPANY_SWITCH_ENDPOINTS:
        verdict = classify(ENDPOINTS[key], raw)
        assert verdict.kind == "expired", (
            f"{key}: an unescaped COMPANY_SWITCH envelope must read as expired, "
            f"got kind={verdict.kind!r}"
        )

    # The other half of the same invariant: a real status=SUCCESS body is still a success
    # on both endpoints, so the widened expiry check does not swallow good responses.
    for key, fixture in (
        ("list_applicants", "rows_applicants.json"),
        ("search_resumes", "rows_search.json"),
    ):
        assert load_fixture(fixture)["status"] == "SUCCESS"  # precondition: the body is a success envelope
        verdict = classify(ENDPOINTS[key], _raw_from_bare_json(fixture))
        assert verdict.ok is True, (
            f"{key}: a status=SUCCESS body must remain a success, got kind={verdict.kind!r}"
        )


@pytest.mark.parametrize("key", _COMPANY_SWITCH_ENDPOINTS)
def test_family_a_401_without_a_status_key_is_unrecognised_and_its_detail_names_the_http_code(key):
    # T-58
    assert ENDPOINTS[key].family == "A"  # precondition: the endpoint under test is family A
    body = {"error": "unauthenticated"}
    raw = _raw(401, "application/json; charset=utf-8", json.dumps(body), body)
    verdict = classify(ENDPOINTS[key], raw)
    assert verdict.ok is False
    assert verdict.kind == "unrecognised_status", (
        f"{key}: a family-A body with no status key must be unrecognised_status, "
        f"got kind={verdict.kind!r}"
    )
    assert "401" in verdict.detail, f"{key}: detail must carry the HTTP code: {verdict.detail!r}"
    assert "None" not in verdict.detail, f"{key}: detail must not fall back to a literal None"


# ── job-switch-delete: T-55..T-58, T-75 ────────────────────────────────────────
# Written against the declared interfaces only: the seven declarations
# are read by key, the Endpoint/FamilyBShape keywords (body_encoding,
# data_kind) are used by their public names, and the envelope family is
# exercised through classify() with nothing mocked across it.


def _jobswitch_probe(**overrides):
    fields = dict(key="jobswitch_probe", host="auth", path="/job-api/probe", method="POST",
                  family="B", body_encoding="json", extra_headers=(),
                  family_b_shape=FamilyBShape(data_kind="any", inner_key=None, sibling_keys=()),
                  throttle_gated=True)
    fields.update(overrides)
    return Endpoint(**fields)


def _jobswitch_envelope_probe(**overrides):
    fields = dict(key="jobswitch_envelope_probe", host="auth", path="/api/probe", method="POST",
                  family="envelope", body_encoding="form", extra_headers=(),
                  family_b_shape=None, throttle_gated=True)
    fields.update(overrides)
    return Endpoint(**fields)


def _jobswitch_json_raw(status: int, obj) -> RawResponse:
    text = json.dumps(obj, ensure_ascii=False)
    return _raw(status, "application/json; charset=utf-8", text, obj)


# T-55 (interface): Endpoint refuses a body-method/encoding mismatch at construction.

def test_endpoint_refuses_post_without_a_body_encoding():
    with pytest.raises(ValueError):
        _jobswitch_probe(method="POST", body_encoding=None)


def test_endpoint_refuses_get_declared_with_a_json_body_encoding():
    with pytest.raises(ValueError):
        _jobswitch_probe(method="GET", body_encoding="json")


def test_endpoint_refuses_an_unknown_body_encoding():
    with pytest.raises(ValueError):
        _jobswitch_probe(body_encoding="xml")


def test_envelope_family_endpoint_with_a_form_body_constructs():
    ep = _jobswitch_envelope_probe()
    assert ep.family == "envelope"
    assert ep.body_encoding == "form"


def test_endpoint_body_encoding_has_no_default():
    with pytest.raises(TypeError):
        Endpoint(key="jobswitch_no_encoding", host="auth", path="/api/probe", method="GET",
                 family="A", extra_headers=(), family_b_shape=None, throttle_gated=True)


# T-56 (interface): fetch() sends a form body as urlencoded pairs (duplicates kept,
# in order) and a json body as JSON, observed by a real local HTTP server.
#
# The only seam is the URL builder: fetch() asks build_url for the request URL, and that
# URL is pointed at the local test server's address, path and query untouched. aiohttp is
# not patched. The request bytes, headers and body encoding are what fetch() really put on
# the wire.

@pytest.mark.asyncio
async def test_fetch_sends_form_bodies_as_urlencoded_pairs_and_json_bodies_as_json(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from urllib.parse import parse_qsl, urlsplit, urlunsplit
    from mcp104.browser import api_client as api_client_mod

    received: list[dict] = []

    async def handler(request):
        raw = await request.read()
        received.append({"content_type": request.content_type, "raw": raw.decode("utf-8")})
        return web.json_response({"data": True, "metadata": {}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)

    async with TestServer(app) as server:
        base = urlsplit(f"http://{server.host}:{server.port}")
        real_build_url = api_client_mod.build_url

        def _build_url_on_local_server(*args, **kwargs):
            parts = urlsplit(real_build_url(*args, **kwargs))
            return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))

        monkeypatch.setattr(api_client_mod, "build_url", _build_url_on_local_server)

        pairs = [("jobNos[]", "1000001"), ("jobNos[]", "1000002"), ("type", "multi")]
        form_ep = _jobswitch_envelope_probe(key="jobswitch_form", path="/t56/form")
        json_ep = _jobswitch_probe(key="jobswitch_json", path="/t56/json")

        await api_client_mod.fetch(form_ep, cookie_header="c=1", params=[], body=pairs)
        await api_client_mod.fetch(json_ep, cookie_header="c=1", params=[], body={"content": "x"})

    form_rec, json_rec = received
    assert form_rec["content_type"] == "application/x-www-form-urlencoded"
    assert parse_qsl(form_rec["raw"], keep_blank_values=True) == pairs
    assert json_rec["content_type"] == "application/json"
    assert json.loads(json_rec["raw"]) == {"content": "x"}


# T-57 (interface): classify() on the envelope family.

_COMPANY_SWITCH_RAW = '{"status": "COMPANY_SWITCH", "url": "https://vip.104.com.tw/company/status/switchCompany"}'
_COMPANY_SWITCH_ESCAPED = ('{"status": "COMPANY_SWITCH", '
                           '"url": "https:\\/\\/vip.104.com.tw\\/company\\/status\\/switchCompany"}')


def test_envelope_200_object_is_ok_and_its_payload_is_the_whole_object():
    body = {"status": "SUCCESS", "message": "openAll", "code": "", "result": {"x": 1},
            "data": None, "url": ""}
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(200, body))
    assert verdict.ok is True
    assert verdict.payload == body


def test_envelope_200_with_access_denied_status_is_still_ok():
    body = {"status": "ACCESS_DENIED", "message": "openAll"}
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(200, body))
    assert verdict.ok is True


def test_envelope_401_is_expired():
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(401, {"message": "x"}))
    assert verdict.ok is False
    assert verdict.kind == "expired"


def test_envelope_404_json_object_is_not_found():
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(404, {"message": "x"}))
    assert verdict.kind == "not_found"


def test_envelope_404_non_json_is_wrong_host():
    raw = _raw(404, "text/html; charset=utf-8", "<html>SYNTH-404</html>", None)
    verdict = classify(_jobswitch_envelope_probe(), raw)
    assert verdict.kind == "wrong_host"


def test_envelope_400_is_validation_with_the_body_message_as_detail():
    verdict = classify(_jobswitch_envelope_probe(),
                       _jobswitch_json_raw(400, {"status": "USER_ERROR", "message": "SYNTH-400-MESSAGE"}))
    assert verdict.kind == "validation"
    assert verdict.detail == "SYNTH-400-MESSAGE"


def test_envelope_500_is_unrecognised_status():
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(500, {"message": "SYNTH-500"}))
    assert verdict.kind == "unrecognised_status"


def test_envelope_200_non_json_is_non_json():
    raw = _raw(200, "text/html; charset=utf-8", "<html>SYNTH-NON-JSON</html>", None)
    verdict = classify(_jobswitch_envelope_probe(), raw)
    assert verdict.kind == "non_json"


def test_envelope_200_json_array_is_malformed():
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(200, [1, 2]))
    assert verdict.kind == "malformed"


@pytest.mark.parametrize("text", [_COMPANY_SWITCH_RAW, _COMPANY_SWITCH_ESCAPED],
                         ids=["unescaped-switch-url", "json-escaped-switch-url"])
def test_envelope_company_switch_marker_is_expired_in_either_escaping(text):
    raw = _raw(200, "application/json; charset=utf-8", text, json.loads(text))
    verdict = classify(_jobswitch_envelope_probe(), raw)
    assert verdict.kind == "expired"


def test_envelope_403_is_blocked():
    verdict = classify(_jobswitch_envelope_probe(), _jobswitch_json_raw(403, {"message": "x"}))
    assert verdict.kind == "blocked"


# T-58 (interface): the seven new declarations, read by key.

_JOBSWITCH_REFERER_HEADERS = (("Referer", "https://vip.104.com.tw/"), ("X-Requested-With", "XMLHttpRequest"))

_JOBSWITCH_DECLARATIONS = {
    # key: (method, path, family, data_kind, body_encoding, extra_headers)
    "close_job_precheck": ("POST", "/vip-job/close/pre-check", "B", "object", "json", ()),
    "close_jobs": ("POST", "/job-api/jobs/close", "B", "any", "json", ()),
    "job_refund_points": ("GET", "/commodity-job-sort/refund-points/jobs", "B", "list", None, ()),
    "open_jobs": ("POST", "/api/job/openJob", "envelope", None, "form", _JOBSWITCH_REFERER_HEADERS),
    "job_more": ("GET", "/api/job/more", "A", None, None, ()),
    "company_identity": ("GET", "/vipapi/company/profile/identity", "B", "object", None, ()),
    "delete_job": ("POST", "/api/job/jobDelete", "envelope", None, "form", _JOBSWITCH_REFERER_HEADERS),
}


@pytest.mark.parametrize("key", sorted(_JOBSWITCH_DECLARATIONS))
def test_job_switch_declaration_matches_the_design_table(key):
    method, path, family, data_kind, body_encoding, extra_headers = _JOBSWITCH_DECLARATIONS[key]
    ep = ENDPOINTS[key]
    assert ep.host == "auth"
    assert ep.method == method
    assert ep.path == path
    assert ep.family == family
    assert ep.throttle_gated is True
    if data_kind is not None:
        assert ep.family_b_shape.data_kind == data_kind
    assert ep.body_encoding == body_encoding
    assert set(ep.extra_headers) == set(extra_headers)


def test_every_declared_endpoint_has_a_body_encoding_that_matches_its_method():
    for key, ep in ENDPOINTS.items():
        if ep.method in ("POST", "PUT"):
            assert ep.body_encoding in ("json", "form"), key
        else:
            assert ep.body_encoding is None, key


# T-75 (interface): family B classification of error bodies and of data_kind="any".

def test_family_b_400_error_object_is_validation_with_its_message_as_detail():
    body = {"error": {"code": 57, "message": "SYNTH-X", "details": {}}}
    verdict = classify(_jobswitch_probe(), _jobswitch_json_raw(400, body))
    assert verdict.kind == "validation"
    assert verdict.detail == "SYNTH-X"


def test_family_b_500_with_the_same_error_object_is_unrecognised_and_keeps_the_text():
    body = {"error": {"code": 57, "message": "SYNTH-X", "details": {}}}
    verdict = classify(_jobswitch_probe(), _jobswitch_json_raw(500, body))
    assert verdict.kind == "unrecognised_status"
    assert "SYNTH-X" in verdict.detail


def test_family_b_any_success_keeps_the_metadata_beside_a_boolean_data():
    body = {"data": True, "metadata": {"lastJob": True}}
    verdict = classify(_jobswitch_probe(), _jobswitch_json_raw(200, body))
    assert verdict.ok is True
    assert verdict.payload["metadata"]["lastJob"] is True


def test_family_b_string_error_keeps_its_existing_detail():
    body = {"error": "SYNTH-NOT-FOUND-TEXT"}
    verdict = classify(_jobswitch_probe(), _jobswitch_json_raw(404, body))
    assert verdict.kind == "not_found"
    assert verdict.detail == "SYNTH-NOT-FOUND-TEXT"


# ── template-management: T-68..T-73 ────────────────────────────────────────────────
# Written against the declared interfaces: the six template declarations are read
# by key from ENDPOINTS, Endpoint and FamilyBShape are built with their public
# keywords, and classify() is called with nothing mocked across it.


_TEMPLATE_DECLARATIONS = {
    # key: (method, path, data_kind, body_encoding); host auth, family B, throttled, no extra headers
    "list_template_types": ("GET", "/bc-comm/template-type", "list", None),
    "get_template": ("GET", "/bc-comm/template/{template_id}", "object", None),
    "create_template": ("POST", "/bc-comm/template", "any", "json"),
    "update_template": ("PUT", "/bc-comm/template/{template_id}", "bool", "json"),
    "template_delete_check": ("GET", "/bc-comm/template/delete-check/{template_id}", "bool", None),
    "delete_template": ("DELETE", "/bc-comm/template/{template_id}", "any", None),
}


# T-68 (interface): the six new template declarations, read key by key.

@pytest.mark.parametrize("key", sorted(_TEMPLATE_DECLARATIONS))
def test_T68_template_declaration_matches_the_design_table(key):
    method, path, data_kind, body_encoding = _TEMPLATE_DECLARATIONS[key]
    ep = ENDPOINTS[key]
    assert ep.host == "auth"
    assert ep.family == "B"
    assert ep.throttle_gated is True
    assert ep.method == method
    assert ep.path == path
    assert ep.family_b_shape.data_kind == data_kind
    assert ep.body_encoding == body_encoding
    assert tuple(ep.extra_headers) == ()
    assert tuple(ep.family_b_shape.sibling_keys) == ()


# T-69 (interface): the only DELETE declaration is the template deletion; no message route declares DELETE.

def test_T69_only_the_template_deletion_declares_delete():
    delete_keys = {key for key, ep in ENDPOINTS.items() if ep.method == "DELETE"}
    assert delete_keys == {"delete_template"}


def test_T69_no_message_route_declares_delete():
    assert not any(
        ep.path.startswith("/bc-comm/message") and ep.method == "DELETE" for ep in ENDPOINTS.values()
    )


# T-70 (interface): PATCH is still refused at construction; DELETE constructs.

def _template_probe(**overrides):
    fields = dict(key="t70_probe", host="auth", path="/bc-comm/template/{template_id}", method="DELETE",
                  body_encoding=None, family="B", extra_headers=(),
                  family_b_shape=FamilyBShape(data_kind="any", inner_key=None, sibling_keys=()),
                  throttle_gated=True)
    fields.update(overrides)
    return Endpoint(**fields)


def test_T70_patch_is_refused_at_construction():
    with pytest.raises(ValueError):
        _template_probe(method="PATCH")


def test_T70_delete_constructs_without_a_body_encoding():
    ep = _template_probe(method="DELETE")
    assert ep.method == "DELETE"
    assert ep.body_encoding is None


# T-72 (interface): data_kind has four values and no default; inner_key goes only with "object".

def test_T72_data_kind_has_no_default():
    with pytest.raises(TypeError):
        FamilyBShape(inner_key=None, sibling_keys=())


def test_T72_an_unknown_data_kind_is_refused():
    with pytest.raises(ValueError):
        FamilyBShape(data_kind="dict", inner_key=None, sibling_keys=())


@pytest.mark.parametrize("data_kind", ["list", "bool", "any"])
def test_T72_inner_key_is_refused_with_any_data_kind_but_object(data_kind):
    with pytest.raises(ValueError):
        FamilyBShape(data_kind=data_kind, inner_key="resume", sibling_keys=())


@pytest.mark.parametrize("data_kind", ["list", "object", "bool", "any"])
def test_T72_each_of_the_four_data_kinds_constructs(data_kind):
    shape = FamilyBShape(data_kind=data_kind, inner_key=None, sibling_keys=())
    assert shape.data_kind == data_kind


def test_T72_inner_key_constructs_with_object():
    shape = FamilyBShape(data_kind="object", inner_key="resume", sibling_keys=())
    assert shape.inner_key == "resume"


# T-73 (interface): classify() on each data_kind; "bool" and "any" are the new kinds.

def _kind_probe(data_kind):
    return Endpoint(key="t73_probe", host="auth", path="/bc-comm/template/probe", method="GET",
                    body_encoding=None, family="B", extra_headers=(),
                    family_b_shape=FamilyBShape(data_kind=data_kind, inner_key=None, sibling_keys=()),
                    throttle_gated=True)


@pytest.mark.parametrize("data", [True, False])
def test_T73_bool_kind_accepts_either_boolean_data(data):
    verdict = classify(_kind_probe("bool"), _jobswitch_json_raw(200, {"data": data, "metadata": {}}))
    assert verdict.ok is True


@pytest.mark.parametrize("data", [{}, "true"])
def test_T73_bool_kind_refuses_data_that_is_not_a_boolean(data):
    verdict = classify(_kind_probe("bool"), _jobswitch_json_raw(200, {"data": data, "metadata": {}}))
    assert verdict.kind == "malformed"


def test_T73_any_kind_accepts_null_data_and_refuses_a_missing_data_key():
    ok = classify(_kind_probe("any"), _jobswitch_json_raw(200, {"data": None, "metadata": {}}))
    assert ok.ok is True
    missing = classify(_kind_probe("any"), _jobswitch_json_raw(200, {"metadata": {}}))
    assert missing.kind == "malformed"


def test_T73_list_and_object_kinds_keep_their_shape_rules():
    assert classify(_kind_probe("list"), _jobswitch_json_raw(200, {"data": [], "metadata": {}})).ok is True
    assert classify(_kind_probe("list"), _jobswitch_json_raw(200, {"data": {}, "metadata": {}})).kind == "malformed"
    assert classify(_kind_probe("object"), _jobswitch_json_raw(200, {"data": {"a": 1}, "metadata": {}})).ok is True
    assert classify(_kind_probe("object"), _jobswitch_json_raw(200, {"data": [1], "metadata": {}})).kind == "malformed"


# ── job-edit-create: T-56, T-57, T-58 ──────────────────────────────────────────────
# The four declarations are read by key; ENDPOINTS is never counted. Written from
# the declared interfaces only. The text_check rows follow classify()'s text_check rules.

_JOB_EDIT_REFERER = "https://vip.104.com.tw/"
_JOB_EDIT_WRITE_HEADERS = {"Referer": _JOB_EDIT_REFERER, "X-Requested-With": "XMLHttpRequest"}


# T-56 (interface): the four job-edit declarations, read by key.

def test_job_edit_declarations_carry_the_designed_host_method_path_family_and_headers():
    expected = {
        "job_insert": ("GET", "/api/job/jobInsert", "A", None, {}),
        "forbidden_words_check": ("POST", "/job-api/forbidden-words/check", "text_check", "json", {}),
        "update_job": ("POST", "/api/job/updateJob", "envelope", "form", _JOB_EDIT_WRITE_HEADERS),
        "new_job": ("POST", "/api/job/newJob", "envelope", "form", _JOB_EDIT_WRITE_HEADERS),
    }
    for key, (method, path, family, body_encoding, headers) in expected.items():
        ep = ENDPOINTS[key]
        assert ep.host == "auth", key
        assert ep.method == method, key
        assert ep.path == path, key
        assert ep.family == family, key
        assert ep.body_encoding == body_encoding, key
        assert dict(ep.extra_headers) == headers, key
        assert ep.throttle_gated is True, key


# T-57 (interface): classify() on the text_check family hands the HTTP 400 verdict to the tool.

def test_text_check_family_keeps_the_http_400_verdict_and_guards_every_other_shape():
    ep = ENDPOINTS["forbidden_words_check"]
    json_ct = "application/json; charset=utf-8"
    ok_texts = ["", "SYNTH-HIT", "", "", "", "", ""]
    obj_400 = {"status": "FAILED", "message": "SYNTH-REJECTED"}
    not_found = {"status": "DATA_NOT_FOUND"}
    envelope = {"data": ok_texts, "metadata": {}}
    rows = [
        ("200 data envelope", _raw(200, json_ct, json.dumps(envelope), envelope),
         "ok", {"http_status": 200, "texts": ok_texts}),
        ("400 data envelope", _raw(400, json_ct, json.dumps(envelope), envelope),
         "ok", {"http_status": 400, "texts": ok_texts}),
        ("200 bare string array", _raw(200, json_ct, json.dumps(ok_texts), ok_texts),
         "malformed", None),
        ("400 bare string array", _raw(400, json_ct, json.dumps(ok_texts), ok_texts),
         "malformed", None),
        ("200 data holding a number", _raw(200, json_ct, json.dumps({"data": ["", 1]}), {"data": ["", 1]}),
         "malformed", None),
        ("400 object", _raw(400, json_ct, json.dumps(obj_400), obj_400), "validation", None),
        ("200 object", _raw(200, json_ct, json.dumps({"x": 1}), {"x": 1}), "malformed", None),
        ("200 non-JSON", _raw(200, "text/html", "<html>SYNTH</html>", None), "non_json", None),
        ("401", _raw(401, json_ct, '{"message": "x"}', {"message": "x"}), "expired", None),
        ("403", _raw(403, "text/html", "<html>SYNTH forbidden</html>", None), "blocked", None),
        ("404 JSON", _raw(404, json_ct, json.dumps(not_found), not_found), "not_found", None),
        ("404 non-JSON", _raw(404, "text/html", "<html>SYNTH missing</html>", None),
         "wrong_host", None),
        ("500", _raw(500, "text/html", "<html>SYNTH boom</html>", None), "unrecognised_status", None),
        ("company switch marker", _raw(200, json_ct, _COMPANY_SWITCH_RAW,
                                       json.loads(_COMPANY_SWITCH_RAW)), "expired", None),
        ("company switch marker, JSON-escaped",
         _raw(200, json_ct, _COMPANY_SWITCH_ESCAPED, json.loads(_COMPANY_SWITCH_ESCAPED)),
         "expired", None),
    ]
    for label, raw, expected_kind, expected_payload in rows:
        verdict = classify(ep, raw)
        if expected_kind == "ok":
            assert verdict.ok is True, label
            assert verdict.payload == expected_payload, f"{label}: payload {verdict.payload!r}"
        else:
            assert verdict.ok is False, label
            assert verdict.kind == expected_kind, f"{label}: got kind {verdict.kind!r}"


# T-58 (interface): fetch() sends a form body with repeated bracket keys, in order, to a real
# local HTTP server. build_url is redirected to that server; the probe path is not a 104 route.

@pytest.mark.asyncio
async def test_form_body_keeps_repeated_bracket_keys_in_order_and_the_values_intact(monkeypatch):
    from aiohttp import web
    from aiohttp.test_utils import TestServer
    from urllib.parse import parse_qsl, urlsplit, urlunsplit
    from mcp104.browser import api_client as api_client_mod

    received: list[dict] = []

    async def handler(request):
        raw = await request.read()
        received.append({"content_type": request.content_type, "raw": raw.decode("utf-8")})
        return web.json_response({"status": "SUCCESS", "message": "", "result": {}})

    app = web.Application()
    app.router.add_route("*", "/{tail:.*}", handler)
    pairs = [("a[]", "1"), ("a[]", "2"), ("b", "x y")]
    form_ep = _jobswitch_envelope_probe(key="t58_form_probe", path="/t58/probe")

    async with TestServer(app) as server:
        base = urlsplit(f"http://{server.host}:{server.port}")
        real_build_url = api_client_mod.build_url

        def _build_url_on_local_server(*args, **kwargs):
            parts = urlsplit(real_build_url(*args, **kwargs))
            return urlunsplit((base.scheme, base.netloc, parts.path, parts.query, parts.fragment))

        monkeypatch.setattr(api_client_mod, "build_url", _build_url_on_local_server)
        await api_client_mod.fetch(form_ep, cookie_header="c=1", params=[], body=pairs)

    assert len(received) == 1
    record = received[0]
    assert record["content_type"] == "application/x-www-form-urlencoded"
    decoded = parse_qsl(record["raw"], keep_blank_values=True)
    assert [value for key, value in decoded if key == "a[]"] == ["1", "2"]
    assert [value for key, value in decoded if key == "b"] == ["x y"]
    assert decoded == pairs
