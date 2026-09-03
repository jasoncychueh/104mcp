from __future__ import annotations


import json
import logging

import pytest

from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.browser.throttle import ThrottleAbort
from mcp104.config import get_config
from mcp104.browser.api_client import ENDPOINTS, RawResponse
from mcp104.tools.helpers import (
    ERROR_API_REQUEST_FAILED,
    GuardAbort,
    SessionUnavailable,
    ToolAbort,
    _error_malformed,
    get_session_id,
    guarded_api,
    guarded_sequence,
)


class FakeSessionObj:
    """Stand-in for mcp's ServerSession — a plain object with a __dict__,
    which is exactly what get_session_id relies on being able to stamp."""
    pass


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeApp:
    def __init__(self, session_pool: SessionPool):
        self.session_pool = session_pool
        self.config = get_config()  # real defaults — nothing in this file sets throttle env vars


class FakeCtx:
    def __init__(self, session_pool: SessionPool):
        self.session = FakeSessionObj()
        self.request_context = FakeRequestContext(FakeApp(session_pool))


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """Neutralizes browser/throttle.py's request-pacing floor for every
    test in this file, which isn't exercising throttle behaviour itself.

    Patches mcp104.browser.throttle._sleep — NOT
    mcp104.browser.throttle.asyncio.sleep directly. That looks module-scoped
    but isn't: throttle.py's
    `import asyncio` binds the SAME shared module object everyone else
    imports, so patching its attribute would mutate asyncio for the WHOLE
    process — including this file's own ensure_future-based tests, which
    rely on a real asyncio.sleep(0.01) to let a concurrently-scheduled
    task run at all. _sleep is a throttle.py-local indirection that
    exists specifically so patching stays scoped to calls made through it.

    Previously this fixture also patched `browser.throttle._rng` with a
    `_FixedShortRng` stand-in, to steer the old drawn-delay distribution's
    random branch selection. The pacing rework (design.md Components §7)
    replaced that distribution with a deterministic interval floor —
    `MIN_CALL_INTERVAL_SECONDS` minus elapsed time, no randomness
    involved — so `_rng` no longer exists on browser.throttle at all.
    Patching a since-removed attribute is exactly what made every test in
    this file error at setup (tasks.md "Existing tests this feature
    invalidates"); the fix is simply that there is nothing left to patch
    for randomness, only the sleep itself.
    """
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


def test_get_session_id_distinct_objects_never_collide():
    pool = SessionPool()
    ctx1 = FakeCtx(pool)
    ctx2 = FakeCtx(pool)
    assert get_session_id(ctx1) != get_session_id(ctx2)


def test_get_session_id_stable_across_repeated_calls():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    first = get_session_id(ctx)
    second = get_session_id(ctx)
    assert first == second


# ── guarded_api: body forwarding, the method/body mismatch check (§1/§2) ────────────

def _new_api_ctx_and_session():
    pool = SessionPool()
    ctx = FakeCtx(pool)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight from SessionInfo.cookies now —
    # there is no BrowserContext to fake a `.cookies()` call against
    # post-login (§C7).
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return ctx, info


@pytest.mark.asyncio
async def test_guarded_api_forwards_body_to_fetch_unchanged(monkeypatch):
    ctx, _info = _new_api_ctx_and_session()
    calls = []

    async def fake_fetch(endpoint, *, cookie_header, params=None, body=None):
        calls.append(body)
        return RawResponse(status=200, location=None, content_type="application/json",
                            body='{"data": [], "metadata": {}}',
                            parsed_json={"data": [], "metadata": {}})

    monkeypatch.setattr("mcp104.browser.api_client.fetch", fake_fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", fake_fetch, raising=False)

    sent_body = {"content": "hello"}
    async with guarded_api(ctx, ENDPOINTS["send_message"], params=[], body=sent_body) as (_payload, _info2):
        pass

    assert calls == [sent_body]


@pytest.mark.asyncio
async def test_guarded_api_post_endpoint_without_body_is_internal_config_error():
    ctx, _info = _new_api_ctx_and_session()

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["send_message"], params=[], body=None):
            pass  # pragma: no cover

    assert exc_info.value.kind == "internal_config"
    # Must NOT read as a transient network problem (Error Handling §2) — a
    # caller bug reported as "retry later" is exactly what this distinction
    # exists to prevent.
    assert exc_info.value.payload != ERROR_API_REQUEST_FAILED
    assert "內部設定錯誤" in exc_info.value.payload["error"]


@pytest.mark.asyncio
async def test_guarded_api_get_endpoint_with_body_is_internal_config_error():
    ctx, _info = _new_api_ctx_and_session()

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["list_jobs"], params=[], body={"unexpected": True}):
            pass  # pragma: no cover

    assert exc_info.value.kind == "internal_config"
    assert exc_info.value.payload != ERROR_API_REQUEST_FAILED


@pytest.mark.asyncio
async def test_guarded_api_no_session_at_all_raises_not_logged_in_kind():
    # Round I1 Bug A: this test's old name ("...raise_sites_carry_the_
    # classifiers_own_kind") claimed coverage of every guarded_api raise
    # path while asserting only this one — payload wording and `.kind` are
    # independent (the redirect-to-auth-host site proved that: its payload
    # says "已過期" regardless of which kind is attached), so a test that
    # oversells its own scope is worse than a narrow one honestly named.
    # The other guarded_api kinds are pinned where they are actually
    # produced, each against a live path, not a hand-built exception:
    #   - "expired" (auth-host redirect) —
    #     test_api_client.py::test_guarded_api_does_not_follow_redirect_and_reports_auth_host_redirect_as_expired
    #   - "throttled" —
    #     test_api_client.py::test_guarded_api_throttled_rejection_carries_throttled_kind
    #   - "internal_config" (method/body mismatch) — this file's
    #     test_guarded_api_post_endpoint_without_body_is_internal_config_error /
    #     test_guarded_api_get_endpoint_with_body_is_internal_config_error
    #   - "transport", "challenge", "blocked", and the classifier's own
    #     kinds (e.g. "validation") — test_api_client.py's family-A/B
    #     Error Handling suite and tests/test_messaging.py's send_message
    #     taxonomy tests, which drive them through the real classify().
    ctx = FakeCtx(SessionPool())  # no session activated at all
    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["list_jobs"], params=[]):
            pass  # pragma: no cover
    assert exc_info.value.kind == "not_logged_in"


# ── guarded_sequence / _issue_one ─────────────────────────────────────────────
#
# All fixtures below build synthetic three-sub-request scripts against the
# real endpoints declared for send_inquiry, already delivered earlier:
# resolve_candidate_idno -> event_last_info -> send_willingness_event.
# Only the guard is exercised here -- no tools/messaging.py code runs, per
# this file's scope (these cases all anchor to helpers.py's own public
# interfaces, not to send_inquiry's behavior).

def _resp(body: dict, status: int = 200, content_type: str = "application/json; charset=utf-8") -> RawResponse:
    return RawResponse(status=status, location=None, content_type=content_type,
                        body=json.dumps(body, ensure_ascii=False), parsed_json=body)


class _SeqFetchSpy:
    """Drives guarded_sequence's N sub-requests with pre-scripted outcomes,
    consumed strictly in order. A scripted item that is a BaseException
    instance is raised instead of returned -- the same shape a real
    transport timeout takes when it escapes fetch() (guarded_api's
    existing except-Exception around fetch() is what turns this into a
    ToolAbort(kind="transport"), so raising here exercises that same path,
    not a hand-built exception at the guard boundary).
    """

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[tuple[object, object, object]] = []

    async def __call__(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append((endpoint, params, body))
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


def _patch_fetch(monkeypatch, spy) -> None:
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)


_RESOLVE_OK = _resp({"data": {"idNo": "SYNTH-ID-0001"}, "metadata": {}})
_LAST_INFO_OK = _resp({"data": {"emailCC": []}, "metadata": {}})
_SEND_OK = _resp({"data": [{"pId": "399022"}], "metadata": {}, "failed": []})


@pytest.mark.asyncio
async def test_T55_guarded_sequence_locks_gates_and_resolves_session_exactly_once(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    _patch_fetch(monkeypatch, _SeqFetchSpy([_RESOLVE_OK, _LAST_INFO_OK, _SEND_OK]))

    import mcp104.tools.helpers as helpers_mod
    real_enforce_throttle = helpers_mod.enforce_throttle
    real_resolve_session = helpers_mod.resolve_session
    throttle_calls: list[int] = []
    resolve_calls: list[int] = []

    async def spy_enforce_throttle(*args, **kwargs):
        throttle_calls.append(1)
        return await real_enforce_throttle(*args, **kwargs)

    async def spy_resolve_session(*args, **kwargs):
        resolve_calls.append(1)
        return await real_resolve_session(*args, **kwargs)

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", spy_enforce_throttle)
    monkeypatch.setattr("mcp104.tools.helpers.resolve_session", spy_resolve_session)

    before_first_calls: list[object] = []

    async def before_first(info_arg):
        before_first_calls.append(info_arg)

    async with guarded_sequence(ctx, slots_needed=3, before_first=before_first) as (request, _info):
        await request(ENDPOINTS["resolve_candidate_idno"], params=[])
        await request(ENDPOINTS["event_last_info"], params=[])
        await request(ENDPOINTS["send_willingness_event"], params=[], body={"content": "x"})

    # request() yields payloads, and the (request, info) tuple is only
    # handed out once by the context manager -- "once" is therefore only
    # observable through these three call counters, exactly as Test
    # Approach's "整段在同一個 lock 內不寫成測試" note says.
    assert len(throttle_calls) == 1
    assert len(resolve_calls) == 1
    assert len(before_first_calls) == 1


@pytest.mark.asyncio
async def test_T56_guarded_sequence_aborts_and_skips_remaining_requests_on_subrequest_failure(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    not_found = _resp({"code": "00004", "message": "找不到對應資源", "detail": []}, status=404)
    spy = _SeqFetchSpy([_RESOLVE_OK, not_found])  # no third item: it must never be consumed
    _patch_fetch(monkeypatch, spy)

    with pytest.raises(GuardAbort):
        async with guarded_sequence(ctx, slots_needed=3) as (request, _info):
            await request(ENDPOINTS["resolve_candidate_idno"], params=[])
            await request(ENDPOINTS["event_last_info"], params=[])
            await request(ENDPOINTS["send_willingness_event"], params=[], body={"content": "x"})

    assert len(spy.calls) == 2
    assert info.lock.locked() is False


@pytest.mark.asyncio
async def test_T57_request_projection_none_means_unprojected_and_empty_tuple_means_nothing_kept(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    envelope = _resp({
        "data": {"idNo": "SYNTH-ID-0001", "userName": "SYNTHETIC-NAME"},
        "metadata": {"quota": 299},
    })

    # (a) pick_data=("idNo",), pick_metadata=() -- only idNo survives, and
    # metadata's content is entirely gone, not merely unread.
    _patch_fetch(monkeypatch, _SeqFetchSpy([envelope]))
    async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
        projected = await request(
            ENDPOINTS["resolve_candidate_idno"], params=[],
            pick_data=("idNo",), pick_metadata=(),
        )
    assert projected["data"] == {"idNo": "SYNTH-ID-0001"}
    assert projected["metadata"] == {}

    # (b) same script, no pick_* at all (default None) -- full envelope,
    # same as guarded_api's existing behaviour today.
    _patch_fetch(monkeypatch, _SeqFetchSpy([envelope]))
    async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
        unprojected = await request(ENDPOINTS["resolve_candidate_idno"], params=[])
    assert unprojected["data"]["userName"] == "SYNTHETIC-NAME"
    assert unprojected["metadata"]["quota"] == 299

    # (c) same script via guarded_api -- identical "no projection" result.
    _patch_fetch(monkeypatch, _SeqFetchSpy([envelope]))
    async with guarded_api(ctx, ENDPOINTS["resolve_candidate_idno"], params=[]) as (via_guarded_api, _info2):
        pass
    assert via_guarded_api["data"]["userName"] == "SYNTHETIC-NAME"
    assert via_guarded_api["metadata"]["quota"] == 299


@pytest.mark.asyncio
async def test_T58_projection_on_is_list_endpoint_is_internal_config_error_before_fetch(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    spy = _SeqFetchSpy([])  # fetch must never be reached
    _patch_fetch(monkeypatch, spy)

    # list_templates is is_list=True -- projecting a keyed subset of `data`
    # makes no sense against a list, per the error-handling table's rejection row.
    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
            await request(ENDPOINTS["list_templates"], params=[], pick_data=("x",))

    assert exc_info.value.kind == "internal_config"
    # Must read as a caller bug, not a transient network problem -- the
    # check sits ahead of the try{} wrapping fetch().
    assert exc_info.value.payload != ERROR_API_REQUEST_FAILED
    assert "程式問題" in exc_info.value.payload["error"]
    assert len(spy.calls) == 0


@pytest.mark.asyncio
async def test_T59_projection_missing_key_raises_malformed_using_the_same_error_builder(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    # event_last_info has inner_key=None, so classify()'s own structural
    # floor does NOT already reject a response missing "emailCC" -- this
    # isolates request()'s own projection-level presence check (a second,
    # distinct guard from resolve_candidate_idno's inner_key="idNo", which
    # classify() would already have caught first).
    envelope = _resp({"data": {"someOtherField": 1}, "metadata": {}})
    _patch_fetch(monkeypatch, _SeqFetchSpy([envelope]))

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
            await request(
                ENDPOINTS["event_last_info"], params=[],
                pick_data=("emailCC",), pick_metadata=(),
            )

    assert exc_info.value.kind == "malformed"
    # Same builder as classify()'s own inner_key floor (_error_malformed) --
    # asserted by template shape rather than a guessed exact detail string,
    # since the implementer's own wording for "which key" is not specified
    # by design.md beyond "uses _error_malformed".
    err = exc_info.value.payload["error"]
    assert err.startswith("104 回應結構異常（")
    assert err.endswith("），可能是介面已變更，請回報")
    assert "emailCC" in err
    # Falls straight into the existing except-GuardAbort handler -- no new
    # handler needed, which is exactly why it's ToolAbort and not a
    # dedicated MalformedResponseError.
    assert _error_malformed("emailCC")["error"].startswith("104 回應結構異常（")


@pytest.mark.asyncio
async def test_T60_note_request_counts_every_subrequest_including_failure_and_timeout(monkeypatch):
    ctx, info = _new_api_ctx_and_session()

    import mcp104.tools.helpers as helpers_mod
    real_note_request = helpers_mod.note_request
    counted: list[int] = []

    def spy_note_request(*args, **kwargs):
        counted.append(1)
        return real_note_request(*args, **kwargs)

    monkeypatch.setattr("mcp104.tools.helpers.note_request", spy_note_request)

    # success
    _patch_fetch(monkeypatch, _SeqFetchSpy([_RESOLVE_OK]))
    async with guarded_api(ctx, ENDPOINTS["resolve_candidate_idno"], params=[]) as (_p, _i):
        pass

    # failure (404 not_found -- a real classify() outcome, not a hand-built one)
    not_found = _resp({"code": "00004", "message": "找不到對應資源", "detail": []}, status=404)
    _patch_fetch(monkeypatch, _SeqFetchSpy([not_found]))
    with pytest.raises(GuardAbort):
        async with guarded_api(ctx, ENDPOINTS["resolve_candidate_idno"], params=[]):
            pass  # pragma: no cover

    # timeout -- an exception escaping fetch(), same as a real transport timeout
    _patch_fetch(monkeypatch, _SeqFetchSpy([TimeoutError("synthetic timeout")]))
    with pytest.raises(GuardAbort):
        async with guarded_api(ctx, ENDPOINTS["resolve_candidate_idno"], params=[]):
            pass  # pragma: no cover

    # note_request runs in `finally` regardless of outcome (every sub-request
    # counts toward the rate limit the same way) -- the same discipline logout_session's
    # throttle_gated=False / note_request-always split already established,
    # generalised here to the sub-requests sharing this same _issue_one.
    assert len(counted) == 3


@pytest.mark.asyncio
async def test_T61_guarded_api_before_request_hook_still_runs_after_gate_before_fetch(monkeypatch):
    # Regression pin for "guarded_api routed through _issue_one changes no
    # observable behaviour": the existing single-request callers (all
    # eight prior tools plus send_message/list_templates) get here via
    # guarded_api unchanged, and its documented hook position -- gate,
    # then before_request, then fetch -- must still hold once the shared
    # _issue_one exists.
    ctx, info = _new_api_ctx_and_session()
    spy = _SeqFetchSpy([_RESOLVE_OK])
    _patch_fetch(monkeypatch, spy)

    hook_calls: list[object] = []

    async def hook(info_arg):
        hook_calls.append(info_arg)
        raise ToolAbort({"success": False, "error": "SYNTHETIC daily cap hit"}, kind="daily_cap")

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["resolve_candidate_idno"], params=[], before_request=hook):
            pass  # pragma: no cover

    assert exc_info.value.kind == "daily_cap"
    assert len(hook_calls) == 1
    assert hook_calls[0] is info
    # The hook fired (so the throttle gate had already passed -- a rejected
    # gate would abort before before_request ever ran) yet fetch() was
    # never reached -- pinning the hook's position squarely between the two.
    assert len(spy.calls) == 0


@pytest.mark.asyncio
async def test_T62_guarded_sequence_forwards_slots_needed_to_enforce_throttle_unmodified(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    captured: list[int] = []

    async def fake_enforce_throttle(*args, **kwargs):
        captured.append(kwargs["slots_needed"])
        return None  # let every entry through -- only the forwarded value matters here

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", fake_enforce_throttle)

    for n in (2, 5):
        _patch_fetch(monkeypatch, _SeqFetchSpy([]))  # no sub-request is issued in this test
        async with guarded_sequence(ctx, slots_needed=n) as (request, _info):
            pass

    assert captured == [2, 5]


@pytest.mark.asyncio
async def test_T66_slots_needed_misconfiguration_reaches_agent_as_internal_config_not_retry_after(monkeypatch):
    # Continuation of test_throttle.py's enforce_throttle case: this pins
    # what the GUARD does with that ThrottleAbort(kind="internal_config")
    # once it crosses into tools/helpers.py -- the existing internal_config
    # wiring (guarded_api's `abort.kind == "throttled"` branch) already
    # handles this without any new code, so this test needs no
    # guarded_sequence at all.
    ctx, info = _new_api_ctx_and_session()
    abort = ThrottleAbort(
        kind="internal_config", payload=None,
        detail="slots_needed=5 exceeds max_requests_per_hour=3",
    )

    async def fake_enforce_throttle(*args, **kwargs):
        return abort

    monkeypatch.setattr("mcp104.tools.helpers.enforce_throttle", fake_enforce_throttle)

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, ENDPOINTS["resolve_candidate_idno"], params=[]):
            pass  # pragma: no cover

    assert exc_info.value.kind == "internal_config"
    assert "retry_after_seconds" not in exc_info.value.payload
    assert "程式問題" in exc_info.value.payload["error"]


@pytest.mark.asyncio
@pytest.mark.parametrize("failure_kind", ["timeout", "challenge", "unclassifiable"])
async def test_T81_guarded_sequence_never_logs_body_or_cookie_on_any_last_subrequest_failure(
    monkeypatch, caplog, failure_kind,
):
    ctx, info = _new_api_ctx_and_session()
    info.cookies = [{"name": "its", "value": "SYNTHETIC-COOKIE", "domain": ".104.com.tw"}]

    if failure_kind == "timeout":
        third: object = TimeoutError("synthetic timeout")
    elif failure_kind == "challenge":
        third = RawResponse(
            status=200, location=None, content_type="text/html; charset=utf-8",
            body="正在執行安全驗證 Ray ID: SYNTH123 Performance and Security by Cloudflare",
            parsed_json=None,
        )
    else:
        third = RawResponse(
            status=200, location=None, content_type="text/html; charset=utf-8",
            body="this body cannot be parsed as JSON at all",
            parsed_json=None,
        )

    _patch_fetch(monkeypatch, _SeqFetchSpy([_RESOLVE_OK, _LAST_INFO_OK, third]))

    send_body = {
        "content": "SYNTHETIC-LETTER-BODY",
        "emailCC": ["cc-a@example.invalid"],
        "templateId": "SYNTHETIC-TEMPLATE",
    }

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GuardAbort):
            async with guarded_sequence(ctx, slots_needed=3) as (request, _info):
                await request(ENDPOINTS["resolve_candidate_idno"], params=[])
                await request(ENDPOINTS["event_last_info"], params=[])
                await request(ENDPOINTS["send_willingness_event"], params=[], body=send_body)

    log_text = caplog.text
    for forbidden in ("SYNTHETIC-LETTER-BODY", "cc-a@example.invalid", "SYNTHETIC-TEMPLATE", "SYNTHETIC-COOKIE"):
        assert forbidden not in log_text, f"{forbidden!r} leaked into logs for failure_kind={failure_kind!r}"
