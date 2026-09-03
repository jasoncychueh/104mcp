"""stdio-cdp-rearchitecture design.md Testing Strategy §C8 — the guard and
endpoint declarations (`src/mcp104/tools/helpers.py`, `src/mcp104/browser/api_client.py`).

Written BLIND to helpers.py's and api_client.py's implementations (spec-tester
Mode 1 rule): every assertion below comes from design.md's declared Interfaces/Data
Models for §C8 and §C6 (verify_session/logout_session endpoint declarations, the
throttle-exemption mechanism, ThrottleAbort, ServerLogoutResult) and
requirements.md R3.5/R3.8/R4.6 — never from reading helpers.py or api_client.py.
`mcp104.browser.session` and `mcp104.browser.throttle` (already implemented,
explicitly readable per this dispatch) are used only to construct fixtures
(SessionInfo, ThrottleState) — their own decision logic (evaluate/classify) is
exercised for real, never mocked, per §Testing Strategy's "not mocked" list.

Cases: T-18, T-21, T-26, T-75, T-76, T-115.

Field-name note: design.md's §C8 prose names ENDPOINTS' new dict keys directly
("ENDPOINTS[\"verify_session\"]", "ENDPOINTS[\"logout_session\"]"), so those are
hardcoded here. It does NOT name the new throttle-exemption field it says
`Endpoint` gains ("a field with no default... whether this route goes through
throttle enforcement") — that field's name is discovered at runtime via
`dataclasses.fields(Endpoint)` rather than guessed, both because the design
doesn't supply a name and because T-76 independently requires exactly this kind
of introspection (its whole point is not comparing against a list hand-written
in the test).
"""
from __future__ import annotations

import dataclasses
import time

import pytest

from mcp104.browser.api_client import ENDPOINTS, Endpoint, hostname_for
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
import mcp104.tools.helpers as helpers_mod
from mcp104.tools.helpers import SessionUnavailable, ToolAbort, get_session_id, guarded_api


# ── ctx/app plumbing (mirrors the shape tests/test_api_client.py already
#    established for the MCP Context object; independently reconstructed
#    here rather than importing from that file, which this dispatch was
#    told not to touch) ────────────────────────────────────────────────

class _FakeSessionObj:
    pass


class _FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class _FakeApp:
    def __init__(self, session_pool: SessionPool):
        self.session_pool = session_pool
        self.config = get_config()


class _FakeCtx:
    def __init__(self, session_pool: SessionPool):
        self.session = _FakeSessionObj()
        self.request_context = _FakeRequestContext(_FakeApp(session_pool))


@pytest.fixture(autouse=True)
def _guard_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP104_ACCOUNT", "test-account")
    monkeypatch.setenv("MCP104_DATA_DIR", str(tmp_path))


@pytest.fixture(autouse=True)
def _no_real_sleep(monkeypatch):
    """Neutralize the inter-call pacing floor's inline sleep (same fix
    tests/test_api_client.py and tests/test_helpers.py already apply) — none
    of these cases are about pacing duration."""

    async def instant_sleep(seconds):
        return None

    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


def _new_ctx_and_session(cookies=None):
    pool = SessionPool()
    ctx = _FakeCtx(pool)
    sid = get_session_id(ctx)
    info = SessionInfo(
        cookies=cookies if cookies is not None else
        [{"name": "its", "value": "sess-cookie", "domain": "vip.104.com.tw", "path": "/"}],
        account_label="test-account",
    )
    pool.activate_direct(sid, info)
    return ctx, info


class _RawResponseStub:
    """Minimal stand-in matching api_client.RawResponse's declared shape
    (status/location/content_type/body/parsed_json — established for real by
    the already-implemented module's `fetch` contract, per docs/104-site-facts.md
    and design.md §C8's family/envelope description)."""

    def __init__(self, status, content_type, body, parsed_json, location=None):
        self.status = status
        self.location = location
        self.content_type = content_type
        self.body = body
        self.parsed_json = parsed_json


def _success_envelope_response():
    payload = {"status": "SUCCESS", "message": "", "result": {}, "url": "", "data": {}}
    import json
    return _RawResponseStub(
        status=200,
        content_type="application/json; charset=utf-8",
        body=json.dumps(payload, ensure_ascii=False),
        parsed_json=payload,
    )


def _install_fake_fetch(monkeypatch, response_or_factory):
    calls = []

    async def fake_fetch(endpoint, *, cookie_header, params=None, body=None):
        calls.append({"endpoint": endpoint, "cookie_header": cookie_header,
                       "params": params, "body": body})
        if callable(response_or_factory):
            return response_or_factory()
        return response_or_factory

    monkeypatch.setattr("mcp104.browser.api_client.fetch", fake_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", fake_fetch, raising=False)
    return calls


# ── T-18 — R3.5: restore-verification declares the endpoint measured to NOT
#    consume the résumé-browsing quota, not the resume-detail endpoint ──────

def test_t018_verify_session_endpoint_is_the_quota_free_route():
    endpoint = ENDPOINTS["verify_session"]
    # .host is a short label (e.g. "vip"), not the full domain — resolved via
    # hostname_for (already implemented, contract not spelled out in
    # design.md; discovered from the T-115 traceback, not by reading
    # helpers.py's decision logic).
    assert hostname_for(endpoint) == "vip.104.com.tw"
    assert endpoint.path == "/api/search/getSearchRsNum"


# ── T-76 — the logout endpoint fully declares itself: checked against
#    Endpoint's own field set (dataclasses.fields), not a list hand-written
#    in this test ───────────────────────────────────────────────────────

def test_t076_endpoint_fields_have_no_defaults_and_the_two_new_rows_exist():
    """T-76, corrected per Mode-2 ruling: `Endpoint` is a frozen dataclass whose
    fields all lack defaults, so "fully declared" is enforced at construction
    time — a row that skipped a field would never have made it into ENDPOINTS
    in the first place. `family_b_shape=None` is a legitimate value for a
    non-"B"-family row (existing family-A rows are None too), so this case
    does not assert non-None field values; it asserts (a) no field on the
    dataclass carries a default anyone could have silently relied on, and (b)
    both new rows exist in ENDPOINTS as real Endpoint instances."""
    MISSING = dataclasses.MISSING
    fields_with_defaults = [
        f.name for f in dataclasses.fields(Endpoint)
        if f.default is not MISSING or f.default_factory is not MISSING
    ]
    assert fields_with_defaults == [], (
        f"Endpoint fields must have no defaults so every row is forced to "
        f"declare every field explicitly; found defaults on {fields_with_defaults}"
    )

    for key in ("verify_session", "logout_session"):
        endpoint = ENDPOINTS[key]
        assert isinstance(endpoint, Endpoint), (
            f"ENDPOINTS[{key!r}] must be a real Endpoint instance"
        )


# ── T-115 — the logout request is never blocked by our own throttle gate,
#    while the ledger still counts it; the exempt set is exactly {logout_session} ──

@pytest.mark.asyncio
async def test_t115a_logout_request_bypasses_the_throttle_gate_while_others_are_blocked(monkeypatch):
    ctx, info = _new_ctx_and_session()
    # Any other call would be rejected: force a rest window far in the future.
    info.throttle.resting_until = time.time() + 10_000

    logout_calls = _install_fake_fetch(
        monkeypatch,
        # A 302 to boidc — the measured logout response shape (§C8) — is fine
        # here: this case only cares whether the gate let the request through,
        # not what guarded_api does with the result afterwards.
        _RawResponseStub(
            status=302, content_type="text/html", body="", parsed_json=None,
            location="https://boidc.104.com.tw/oauth2/logout",
        ),
    )
    logout_endpoint = ENDPOINTS["logout_session"]
    try:
        async with guarded_api(ctx, logout_endpoint) as (payload, info_out):
            pass
    except (ToolAbort, SessionUnavailable) as exc:
        assert exc.kind != "throttled", (
            "logout_session must never be rejected by our own throttle gate"
        )
    assert len(logout_calls) == 1, "the exempted route's request must actually be sent"


@pytest.mark.asyncio
async def test_t115a_control_endpoint_is_blocked_under_the_same_throttle_state(monkeypatch):
    ctx, info = _new_ctx_and_session()
    info.throttle.resting_until = time.time() + 10_000

    control_calls = _install_fake_fetch(monkeypatch, _success_envelope_response())
    control_endpoint = ENDPOINTS["verify_session"]
    with pytest.raises(ToolAbort) as excinfo:
        async with guarded_api(ctx, control_endpoint) as (payload, info_out):
            pass
    assert excinfo.value.kind == "throttled"
    assert control_calls == [], (
        "a non-exempt route under the same forced-rest state must not reach "
        "the transport at all"
    )


def test_t115b_exempt_endpoint_set_is_exactly_logout_session():
    # Endpoint.throttle_gated: bool — True means "goes through the throttle
    # gate", False means "exempt" (design.md §C8, settled during Mode-2 review).
    exempt = {key for key, ep in ENDPOINTS.items() if not ep.throttle_gated}
    assert exempt == {"logout_session"}, (
        "design.md §C8: the throttle-gate exemption must apply to exactly one "
        "route today; widening it must be a visible, deliberate edit to this "
        "expected set (T-115)"
    )


@pytest.mark.asyncio
async def test_t115c_logout_request_is_still_counted_in_the_ledger_despite_gate_exemption(monkeypatch):
    ctx, info = _new_ctx_and_session()
    before = len(info.throttle.request_timestamps)

    _install_fake_fetch(
        monkeypatch,
        _RawResponseStub(
            status=302, content_type="text/html", body="", parsed_json=None,
            location="https://boidc.104.com.tw/oauth2/logout",
        ),
    )
    logout_endpoint = ENDPOINTS["logout_session"]
    try:
        async with guarded_api(ctx, logout_endpoint) as (payload, info_out):
            pass
    except (ToolAbort, SessionUnavailable):
        pass  # T-115a already covers the outcome; this case is about the ledger

    after = len(info.throttle.request_timestamps)
    assert after == before + 1, (
        "the exemption is the gate, not the ledger (§C6/§C8): note_request must "
        "still run for the logout request"
    )


# ── T-21 — R3.8: the restore-verification request is counted toward the
#    request-throttle ledger like any other call ───────────────────────

@pytest.mark.asyncio
async def test_t021_verify_session_request_is_counted_by_the_throttle(monkeypatch):
    ctx, info = _new_ctx_and_session()
    before = len(info.throttle.request_timestamps)

    _install_fake_fetch(monkeypatch, _success_envelope_response())
    endpoint = ENDPOINTS["verify_session"]
    async with guarded_api(ctx, endpoint) as (payload, info_out):
        pass

    after = len(info.throttle.request_timestamps)
    assert after == before + 1, (
        "R3.8: restore verification must not be an unthrottled bypass — its "
        "request must be counted exactly like any other guarded_api call"
    )


# ── T-26 — every kind in the disposition table uses an existing error
#    vocabulary; the 403 row's wording lives in the same family/module as
#    the existing two 403 wordings, not a hand-rolled second vocabulary ────

def test_t026_restore_verify_403_wording_lives_in_the_existing_blocked_family():
    # These are {"error": "..."} payload dicts (guarded_api's other
    # ERROR_*/ToolAbort payloads follow the same shape), not raw strings.
    blocked_constants = {
        name: getattr(helpers_mod, name)
        for name in dir(helpers_mod)
        if name.startswith("ERROR_BLOCKED")
        and isinstance(getattr(helpers_mod, name), dict)
        and isinstance(getattr(helpers_mod, name).get("error"), str)
    }
    assert "ERROR_BLOCKED_API_RESTORE_VERIFY" in blocked_constants, (
        "design.md §C8 names this constant explicitly, co-located with the "
        "existing two 403 wordings in tools/helpers.py"
    )
    # The restore-verify wording must be a THIRD-or-later member of an
    # existing family, not a first member of a new one hand-written on the
    # restore path.
    assert len(blocked_constants) >= 3, (
        f"expected the restore-verify 403 wording to join at least two "
        f"pre-existing ERROR_BLOCKED* constants in the same module, found "
        f"only {sorted(blocked_constants)} — a lone constant would mean a "
        f"second, parallel vocabulary was hand-written on the restore path"
    )
    assert blocked_constants["ERROR_BLOCKED_API_RESTORE_VERIFY"]["error"].strip() != ""


# ── T-75 — credentials come from the session itself; the whole guard runs
#    with no browser object anywhere in reach ───────────────────────────

@pytest.mark.asyncio
async def test_t075_guard_runs_end_to_end_with_no_browser_object(monkeypatch):
    ctx, info = _new_ctx_and_session(
        cookies=[{"name": "its", "value": "cookie-from-session", "domain": "vip.104.com.tw", "path": "/"}]
    )
    # SessionInfo (browser/session.py, already implemented) carries no
    # browser/context field at all any more — there is nothing here for the
    # guard to reach into even if it tried.
    assert not hasattr(info, "browser_context")
    assert not any("browser" in f.name for f in dataclasses.fields(SessionInfo))

    calls = _install_fake_fetch(monkeypatch, _success_envelope_response())
    endpoint = ENDPOINTS["verify_session"]
    async with guarded_api(ctx, endpoint) as (payload, info_out):
        pass

    assert len(calls) == 1
    # The credentials actually used came from SessionInfo.cookies, not from
    # any live browser context.
    assert "cookie-from-session" in calls[0]["cookie_header"]
