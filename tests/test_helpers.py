from __future__ import annotations


import json
import logging

import pytest

from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.browser.throttle import ThrottleAbort
from mcp104.config import get_config
from mcp104.browser.api_client import ENDPOINTS, RawResponse
from tests.conftest import _SeqFetchSpy
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
                            body_bytes=b'{"data": [], "metadata": {}}',
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
    text = json.dumps(body, ensure_ascii=False)
    return RawResponse(status=status, location=None, content_type=content_type,
                        body=text, body_bytes=text.encode("utf-8"), parsed_json=body)


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
            body_bytes=b"",
            parsed_json=None,
        )
    else:
        third = RawResponse(
            status=200, location=None, content_type="text/html; charset=utf-8",
            body="this body cannot be parsed as JSON at all",
            body_bytes=b"",
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


# =========================================================================
# The asset path through the shared guard: throttle accounting, the five
# misuse shapes, every kind classify_asset can emit reaching the Agent with
# the right payload AND abort class, fetch_asset's redirect policy, and the
# rule that no asset URL or cookie value may appear in a return value or a
# log line.
#
# Every byte here is synthetic. No real photo, attachment, filename, name,
# phone or e-mail appears anywhere in this file.
# =========================================================================

from mcp104.browser.api_client import (  # noqa: E402
    ASSET_ROUTES,
    EXPIRY_MARKER,
    ASSET_MAX_BYTES,
)
from mcp104.tools.helpers import (  # noqa: E402
    ERROR_ASSET_EMPTY_BODY,
    ERROR_BLOCKED_API_AFTER_SUCCESS,
    ERROR_ASSET_NOT_AUTHENTICATED,
    ERROR_ASSET_TOO_LARGE,
    ERROR_EXPIRED,
    _api_error_for_kind,
    _asset_error_for_kind,
    _error_unrecognised_status,
)

_PHOTO_ROUTE = ASSET_ROUTES["candidate_photo"]
_ATTACH_ROUTE = ASSET_ROUTES["resume_attachment"]
_PHOTO_URL = "https://asset.vip.104.com.tw/download/webHeadShot?v=SYNTHETIC-TOKEN-AAA"
_ATTACH_URL = "https://asset.vip.104.com.tw/download/resumeAttach/SYNTHETIC-TOKEN-BBB"

_SYNTH_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF" + b"\x00" * 32
_NOT_AUTH_HTML = (
    '<html><head><script>location.href="https://vip.104.com.tw/";</script>'
    "</head><body></body></html>"
)


class _AssetSeqFetchSpy:
    """Like conftest._SeqFetchSpy, but it stands in for BOTH transports:
    guarded_sequence's JSON sub-request goes through `fetch` and its asset
    sub-request through `fetch_asset`, which have different signatures. One
    script, consumed in call order, so a test can assert exactly how many
    requests were issued and in what order."""

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[tuple[str, object]] = []

    def _next(self):
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item

    async def fetch(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append(("json", endpoint.key))
        return self._next()

    async def fetch_asset(self, route, url, *, cookie_header):
        self.calls.append(("asset", route.key))
        return self._next()


def _patch_both_transports(monkeypatch, spy) -> None:
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy.fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy.fetch, raising=False)
    monkeypatch.setattr("mcp104.browser.api_client.fetch_asset", spy.fetch_asset)
    monkeypatch.setattr("mcp104.tools.helpers.fetch_asset", spy.fetch_asset, raising=False)


def _asset_resp(status=200, *, body_bytes=b"", body=None, content_type=None, location=None):
    """A RawResponse shaped as fetch_asset produces one — `body` is the text
    projection, empty once the bytes are a recognised file family."""
    if body is None:
        body = "" if body_bytes[:3] == b"\xff\xd8\xff" else body_bytes[:4096].decode("utf-8", errors="replace")
    return RawResponse(status=status, location=location, content_type=content_type,
                       body=body, body_bytes=body_bytes, parsed_json=None)


_DETAIL_OK = _resp({"data": {"resume": {"personalPic": _PHOTO_URL}}, "metadata": {}})


# -- throttle accounting --------------------------------------------------

@pytest.mark.asyncio
async def test_an_asset_subrequest_is_counted_in_the_rolling_window(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    _patch_both_transports(
        monkeypatch,
        _AssetSeqFetchSpy([_DETAIL_OK, _asset_resp(body_bytes=_SYNTH_JPEG)]),
    )
    before = len(info.throttle.request_timestamps)

    async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
        await request(ENDPOINTS["get_resume_detail"], params=[("idno", "1234567890123")])
        await request(_PHOTO_ROUTE, asset_url=_PHOTO_URL)

    assert len(info.throttle.request_timestamps) - before == 2


@pytest.mark.asyncio
async def test_a_failed_asset_subrequest_is_still_counted(monkeypatch):
    # note_request runs in `finally`, so a timeout counts too — the volume
    # cap must not go blind on exactly the calls worth counting.
    ctx, info = _new_api_ctx_and_session()
    _patch_both_transports(
        monkeypatch,
        _AssetSeqFetchSpy([_DETAIL_OK, TimeoutError("synthetic timeout")]),
    )
    before = len(info.throttle.request_timestamps)

    with pytest.raises(GuardAbort):
        async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
            await request(ENDPOINTS["get_resume_detail"], params=[("idno", "1234567890123")])
            await request(_PHOTO_ROUTE, asset_url=_PHOTO_URL)

    assert len(info.throttle.request_timestamps) - before == 2


@pytest.mark.asyncio
async def test_a_window_with_too_few_slots_stops_the_whole_sequence_before_any_request(monkeypatch):
    # slots_needed is an ADMISSION judgment, not a reservation held for the
    # duration: with only one slot left, a two-request sequence is refused
    # up front and no request goes out at all.
    ctx, info = _new_api_ctx_and_session()
    spy = _AssetSeqFetchSpy([])
    _patch_both_transports(monkeypatch, spy)
    monkeypatch.setattr(
        "mcp104.tools.helpers.enforce_throttle",
        _fake_throttle_rejection(),
    )

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
            await request(ENDPOINTS["get_resume_detail"], params=[("idno", "1234567890123")])

    assert exc_info.value.kind == "throttled"
    assert "retry_after_seconds" in exc_info.value.payload
    assert spy.calls == []


def _fake_throttle_rejection():
    async def fake_enforce_throttle(*args, **kwargs):
        return ThrottleAbort(
            kind="throttled",
            payload={"error": "SYNTHETIC 節流", "retry_after_seconds": 42},
            detail="",
        )
    return fake_enforce_throttle


# -- the 403 wording split is the SHARED code, reached from the asset path --

@pytest.mark.asyncio
async def test_asset_success_sets_has_succeeded_and_403_then_uses_the_after_success_wording(monkeypatch):
    ctx, info = _new_api_ctx_and_session()
    assert info.has_succeeded_api_call is False
    _patch_both_transports(
        monkeypatch,
        _AssetSeqFetchSpy([
            _DETAIL_OK,
            _asset_resp(body_bytes=_SYNTH_JPEG),
            _asset_resp(status=403, body_bytes=b"denied"),
        ]),
    )

    async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
        await request(ENDPOINTS["get_resume_detail"], params=[("idno", "1234567890123")])
        await request(_PHOTO_ROUTE, asset_url=_PHOTO_URL)
    assert info.has_succeeded_api_call is True

    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
            await request(_ATTACH_ROUTE, asset_url=_ATTACH_URL)
    assert exc_info.value.kind == "blocked"
    assert exc_info.value.payload is ERROR_BLOCKED_API_AFTER_SUCCESS


@pytest.mark.asyncio
async def test_an_asset_response_redirecting_to_an_auth_host_is_expired(monkeypatch):
    ctx, _info = _new_api_ctx_and_session()
    _patch_both_transports(
        monkeypatch,
        _AssetSeqFetchSpy([
            _DETAIL_OK,
            _asset_resp(status=302, location="https://bsignin.104.com.tw/login"),
        ]),
    )

    with pytest.raises(SessionUnavailable) as exc_info:
        async with guarded_sequence(ctx, slots_needed=2) as (request, _info2):
            await request(ENDPOINTS["get_resume_detail"], params=[("idno", "1234567890123")])
            await request(_PHOTO_ROUTE, asset_url=_PHOTO_URL)

    assert exc_info.value.kind == "expired"
    assert exc_info.value.payload is ERROR_EXPIRED


# -- the five misuse shapes -----------------------------------------------

@pytest.mark.asyncio
@pytest.mark.parametrize("kwargs,target", [
    ({}, "asset"),                                   # AssetRoute without asset_url
    ({"asset_url": _PHOTO_URL}, "json"),             # Endpoint WITH asset_url
    ({"asset_url": _PHOTO_URL, "params": []}, "asset"),
    ({"asset_url": _PHOTO_URL, "body": {"x": 1}}, "asset"),
    ({"asset_url": _PHOTO_URL, "pick_data": ("x",)}, "asset"),
])
async def test_each_request_misuse_is_a_caller_bug_not_a_network_blip(monkeypatch, kwargs, target):
    ctx, _info = _new_api_ctx_and_session()
    spy = _AssetSeqFetchSpy([])  # nothing may be issued
    _patch_both_transports(monkeypatch, spy)
    endpoint = _PHOTO_ROUTE if target == "asset" else ENDPOINTS["get_resume_detail"]

    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_sequence(ctx, slots_needed=1) as (request, _info2):
            await request(endpoint, **kwargs)

    assert exc_info.value.kind == "internal_config"
    assert exc_info.value.payload != ERROR_API_REQUEST_FAILED
    assert "程式問題" in exc_info.value.payload["error"]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_handing_an_asset_route_to_guarded_api_is_a_guardabort_not_an_attributeerror():
    # AssetRoute has no throttle_gated (deliberately). Without this check
    # guarded_api would raise AttributeError, which is NOT a GuardAbort and
    # so escapes every tool's `except GuardAbort` as an unhandled exception.
    ctx, _info = _new_api_ctx_and_session()
    with pytest.raises(GuardAbort) as exc_info:
        async with guarded_api(ctx, _PHOTO_ROUTE):
            pass  # pragma: no cover
    assert isinstance(exc_info.value, ToolAbort)
    assert exc_info.value.kind == "internal_config"
    assert "guarded_sequence" in exc_info.value.payload["error"]


@pytest.mark.asyncio
async def test_the_guarded_api_refusal_precedes_session_resolution():
    # With no session at all, a caller bug must still read as a caller bug —
    # not as ERROR_NOT_LOGGED_IN, which would disguise it as a login problem.
    ctx = FakeCtx(SessionPool())  # no session activated
    with pytest.raises(ToolAbort) as exc_info:
        async with guarded_api(ctx, _PHOTO_ROUTE):
            pass  # pragma: no cover
    assert exc_info.value.kind == "internal_config"
    assert exc_info.value.payload["error"] != "請先呼叫 login()"


# -- every kind classify_asset can emit, delivered to the Agent -----------

async def _issue_asset_and_capture(monkeypatch, response):
    """Drive one asset sub-request through the real guard and return the
    GuardAbort it raised (or None on success)."""
    ctx, _info = _new_api_ctx_and_session()
    _patch_both_transports(monkeypatch, _AssetSeqFetchSpy([response]))
    try:
        async with guarded_sequence(ctx, slots_needed=1) as (request, _info2):
            await request(_ATTACH_ROUTE, asset_url=_ATTACH_URL)
    except GuardAbort as e:
        return e
    return None


@pytest.mark.asyncio
async def test_asset_not_authenticated_reaches_the_agent_as_session_unavailable(monkeypatch):
    e = await _issue_asset_and_capture(
        monkeypatch, _asset_resp(body_bytes=_NOT_AUTH_HTML.encode("utf-8"),
                                 content_type="text/html; charset=utf-8"))
    assert isinstance(e, SessionUnavailable)
    assert e.kind == "asset_not_authenticated"
    assert e.payload is ERROR_ASSET_NOT_AUTHENTICATED
    # The one sentence that stops the Agent drawing the wrong conclusion.
    assert "這不代表這位候選人沒有這個檔案" in e.payload["error"]


@pytest.mark.asyncio
async def test_asset_too_large_reaches_the_agent_as_tool_abort(monkeypatch):
    oversized = _SYNTH_JPEG + b"\x00" * (ASSET_MAX_BYTES + 1 - len(_SYNTH_JPEG))
    e = await _issue_asset_and_capture(monkeypatch, _asset_resp(body_bytes=oversized))
    assert type(e) is ToolAbort
    assert e.kind == "asset_too_large"
    assert e.payload is ERROR_ASSET_TOO_LARGE
    # It must not claim nothing was downloaded: 32 MB crossed the wire, a
    # throttle slot was spent and the résumé-detail request already went out.
    assert "沒有寫入任何檔案" in e.payload["error"]


@pytest.mark.asyncio
async def test_asset_empty_body_reaches_the_agent_without_claiming_eight_bytes(monkeypatch):
    e = await _issue_asset_and_capture(monkeypatch, _asset_resp(body_bytes=b""))
    assert type(e) is ToolAbort
    assert e.kind == "asset_empty_body"
    assert e.payload is ERROR_ASSET_EMPTY_BODY
    assert "8" not in e.payload["error"]
    assert "簽名" not in e.payload["error"]


@pytest.mark.asyncio
async def test_asset_unknown_format_carries_the_signature_and_asks_for_a_report(monkeypatch):
    junk = bytes(range(8, 40))
    e = await _issue_asset_and_capture(
        monkeypatch, _asset_resp(body_bytes=junk, content_type="application/octet-stream"))
    assert type(e) is ToolAbort
    assert e.kind == "asset_unknown_format"
    assert junk[:8].hex() in e.payload["error"]
    assert "application/octet-stream" in e.payload["error"]
    assert "請回報" in e.payload["error"]


@pytest.mark.asyncio
async def test_expired_on_the_asset_path_delegates_to_the_shared_table(monkeypatch):
    # Triggered through the prologue's LOCATION half specifically: that is
    # the one end-to-end path proving the redirect policy, the shared
    # prologue and the delegation all hold at once. Getting it wrong would
    # hand the Agent "未知的內部錯誤（expired）" as a ToolAbort.
    e = await _issue_asset_and_capture(
        monkeypatch, _asset_resp(status=302, location="https://vip.104.com.tw" + EXPIRY_MARKER))
    assert isinstance(e, SessionUnavailable)
    assert e.kind == "expired"
    assert e.payload is ERROR_EXPIRED
    assert "已過期" in e.payload["error"]


@pytest.mark.asyncio
async def test_unrecognised_status_on_the_asset_path_delegates_to_the_shared_table(monkeypatch):
    e = await _issue_asset_and_capture(monkeypatch, _asset_resp(status=500, body_bytes=b"oops"))
    assert type(e) is ToolAbort
    assert e.kind == "unrecognised_status"
    assert e.payload == _error_unrecognised_status("HTTP 500")


@pytest.mark.parametrize("kind,detail", [
    ("expired", ""),
    ("unrecognised_status", "HTTP 500"),
    ("wrong_host", ""),
    ("header_fault", ""),
    ("not_found", "SYNTHETIC"),
    ("missing_param", "SYNTHETIC"),
    ("malformed", "SYNTHETIC"),
    ("non_json", "text/html"),
    ("validation", "SYNTHETIC"),
    ("a-kind-nobody-has-ever-taught-us", "SYNTHETIC"),
])
def test_every_non_asset_kind_gets_the_same_payload_and_abort_class_as_the_json_path(kind, detail):
    # Delegation, asserted as delegation: the asset table does not restate
    # these, it hands them to the same function. The final "unknown kind"
    # line therefore exists exactly once, which the last parameter checks.
    asset_payload, asset_cls = _asset_error_for_kind(kind, _ATTACH_ROUTE, detail)
    json_payload, json_cls = _api_error_for_kind(kind, _ATTACH_ROUTE, detail)
    assert asset_payload == json_payload
    assert asset_cls is json_cls


def test_a_kind_nobody_taught_us_still_lands_on_the_single_last_line():
    payload, cls = _asset_error_for_kind("no-such-kind", _ATTACH_ROUTE, "")
    assert payload == {"error": "未知的內部錯誤（no-such-kind）"}
    assert cls is ToolAbort


# -- fetch_asset's own redirect policy and text projection ----------------
#
# These two properties are the PRECONDITION for the expiry cases above: if
# fetch_asset followed redirects, or never filled `location`, those tests
# would pass against a synthetic RawResponse no real path could produce.

class _FakeAiohttpResponse:
    """A response whose body arrives in SEVERAL chunks, each one shorter
    than what the reader asked for.

    That shape is the point of the double, not an incidental detail. Real
    `StreamReader.read(n)` with n >= 0 does not return n bytes: it waits
    until some data is buffered and hands back whatever is there, capped
    at n. A double that returns `self._body[:n]` in one call answers every
    question the same way whether the code under test loops or issues a
    single read — so it cannot fail on the very bug it is meant to pin.
    """

    chunk_size = 8

    def __init__(self, status, headers, body_bytes):
        self.status = status
        self.headers = headers
        self.content = self
        self._body = body_bytes
        self._pos = 0
        self.read_calls = 0
        self.asked_for: list[int] = []

    async def read(self, n):
        self.read_calls += 1
        self.asked_for.append(n)
        chunk = self._body[self._pos:self._pos + min(n, self.chunk_size)]
        self._pos += len(chunk)
        return chunk

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class _FakeAiohttpSession:
    """Records what fetch_asset asked aiohttp for, so allow_redirects and the
    timeout shape can be asserted rather than assumed.

    Everything it records is per-INSTANCE. Class attributes would persist
    across tests, so a test that never opened a session could read the
    previous test's headers and pass.
    """

    def __init__(self, response):
        self._response = response
        self.last_get_kwargs: dict = {}
        self.last_timeout = None
        self.sessions_opened = 0

    def __call__(self, *, timeout=None):
        self.sessions_opened += 1
        self.last_timeout = timeout
        return self

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def get(self, url, *, headers, allow_redirects):
        self.last_get_kwargs = {
            "url": url, "headers": headers, "allow_redirects": allow_redirects,
        }
        return self._response


def _install_fake_aiohttp(monkeypatch, api_mod, session):
    """Swap the aiohttp name in the module UNDER TEST, not an attribute of
    the real third-party module. monkeypatch would restore either, but only
    this one leaves aiohttp itself untouched for anything else in the run.
    """
    import types

    monkeypatch.setattr(
        api_mod,
        "aiohttp",
        types.SimpleNamespace(
            ClientSession=session,
            ClientTimeout=api_mod.aiohttp.ClientTimeout,
        ),
    )
    return session


@pytest.mark.asyncio
async def test_fetch_asset_does_not_follow_redirects_and_reports_location(monkeypatch):
    import mcp104.browser.api_client as api_mod

    response = _FakeAiohttpResponse(
        302, {"Location": "https://vip.104.com.tw" + EXPIRY_MARKER, "Content-Type": "text/html"}, b"")
    session = _install_fake_aiohttp(monkeypatch, api_mod, _FakeAiohttpSession(response))

    raw = await api_mod.fetch_asset(_ATTACH_ROUTE, _ATTACH_URL, cookie_header="its=SYNTHETIC-COOKIE")

    assert session.last_get_kwargs["allow_redirects"] is False
    assert raw.location == "https://vip.104.com.tw" + EXPIRY_MARKER
    assert raw.parsed_json is None


@pytest.mark.asyncio
async def test_fetch_asset_leaves_location_none_on_a_200_and_empties_body_for_a_known_file(monkeypatch):
    import mcp104.browser.api_client as api_mod

    response = _FakeAiohttpResponse(200, {"Content-Type": "image/gif"}, _SYNTH_JPEG)
    _install_fake_aiohttp(monkeypatch, api_mod, _FakeAiohttpSession(response))

    raw = await api_mod.fetch_asset(_PHOTO_ROUTE, _PHOTO_URL, cookie_header="its=SYNTHETIC-COOKIE")

    assert raw.location is None
    # Delivered in several short chunks (see _FakeAiohttpResponse): the
    # whole body must still arrive.
    assert response.read_calls > 1
    assert raw.body_bytes == _SYNTH_JPEG
    # The text projection is empty because the magic hit — NOT because the
    # body was empty. This is what stops the Cloudflare/expiry scans from
    # searching a binary. The declared Content-Type is a lie on this route
    # and is carried verbatim for error messages only.
    assert raw.body == ""
    assert raw.content_type == "image/gif"


@pytest.mark.asyncio
async def test_fetch_asset_projects_a_non_file_response_to_text(monkeypatch):
    import mcp104.browser.api_client as api_mod

    html = _NOT_AUTH_HTML.encode("utf-8")
    response = _FakeAiohttpResponse(200, {"Content-Type": "text/html"}, html)
    _install_fake_aiohttp(monkeypatch, api_mod, _FakeAiohttpSession(response))

    raw = await api_mod.fetch_asset(_ATTACH_ROUTE, _ATTACH_URL, cookie_header="")
    assert raw.body == _NOT_AUTH_HTML
    assert raw.body_bytes == html


@pytest.mark.asyncio
async def test_fetch_asset_accumulates_a_body_delivered_in_several_short_reads(monkeypatch):
    """The bound is a ceiling, not a stopping point: a body under the cap
    must arrive WHOLE even though every read returns fewer bytes than the
    reader asked for. A single read() would land a truncated file whose
    magic bytes (at offset 0) still classify it as a valid asset.
    """
    import mcp104.browser.api_client as api_mod

    body = _SYNTH_JPEG + bytes(range(256)) * 4
    response = _FakeAiohttpResponse(200, {"Content-Type": "application/pdf"}, body)
    _install_fake_aiohttp(monkeypatch, api_mod, _FakeAiohttpSession(response))

    raw = await api_mod.fetch_asset(_ATTACH_ROUTE, _ATTACH_URL, cookie_header="")

    assert raw.body_bytes == body
    assert response.read_calls > 1
    # Every ask is for what is still missing, never for a fixed slice.
    assert response.asked_for[0] == ASSET_MAX_BYTES + 1


@pytest.mark.asyncio
async def test_fetch_asset_reads_at_most_the_cap_plus_one_byte(monkeypatch):
    import mcp104.browser.api_client as api_mod

    class _Huge(_FakeAiohttpResponse):
        # A never-ending body, handed over a megabyte at a time — still
        # always LESS than the reader asks for, so the cap can only be
        # reached by looping.
        chunk_size = 1024 * 1024

        async def read(self, n):
            self.read_calls += 1
            self.asked_for.append(n)
            return b"\x00" * min(n, self.chunk_size)

    response = _Huge(200, {"Content-Type": "application/pdf"}, b"")
    _install_fake_aiohttp(monkeypatch, api_mod, _FakeAiohttpSession(response))

    raw = await api_mod.fetch_asset(_ATTACH_ROUTE, _ATTACH_URL, cookie_header="")
    # The one extra byte IS the over-limit evidence; no separate field
    # carries it, and classify_asset compares against the same constant.
    assert len(raw.body_bytes) == ASSET_MAX_BYTES + 1
    assert response.read_calls > 1
    assert response.asked_for[0] == ASSET_MAX_BYTES + 1
    # It stops the moment it holds the evidence rather than draining a
    # body already known to be over the limit.
    assert sum(min(n, _Huge.chunk_size) for n in response.asked_for) == ASSET_MAX_BYTES + 1


@pytest.mark.asyncio
async def test_fetch_asset_sends_cookies_on_both_routes_and_no_referer(monkeypatch):
    import mcp104.browser.api_client as api_mod

    for route, url in ((_PHOTO_ROUTE, _PHOTO_URL), (_ATTACH_ROUTE, _ATTACH_URL)):
        response = _FakeAiohttpResponse(200, {}, _SYNTH_JPEG)
        session = _install_fake_aiohttp(monkeypatch, api_mod, _FakeAiohttpSession(response))
        await api_mod.fetch_asset(route, url, cookie_header="its=SYNTHETIC-COOKIE")
        headers = session.last_get_kwargs["headers"]
        # Sent on BOTH routes regardless of cookie_required: that is what a
        # real browser does, and withholding them would manufacture a
        # request shape no human produces.
        assert headers["Cookie"] == "its=SYNTHETIC-COOKIE"
        # Measured unnecessary on both routes, so never sent.
        assert "Referer" not in headers


@pytest.mark.asyncio
async def test_a_poisoned_binary_still_succeeds_after_issue_ones_challenge_scan(monkeypatch):
    """The false-positive defence, asserted where the scan actually runs.

    `_detect_cloudflare_challenge(raw.body)` and the expiry substring check
    live in `_issue_one`, BEFORE classify — so a test that only calls
    classify_asset can never reach them. This drives a JPEG whose bytes
    happen to contain both markers through the real fetch_asset and the
    real `_issue_one`: the magic hit empties the text projection, so
    neither scan can fire on binary noise and the call succeeds.
    """
    import mcp104.browser.api_client as api_mod

    poisoned = _SYNTH_JPEG + "正在執行安全驗證".encode("utf-8") + EXPIRY_MARKER.encode("utf-8")
    ctx, _info = _new_api_ctx_and_session()

    # Only the JSON transport is a spy; fetch_asset stays REAL so the text
    # projection under test is the one production computes.
    spy = _AssetSeqFetchSpy([_DETAIL_OK])
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy.fetch)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy.fetch, raising=False)
    _install_fake_aiohttp(
        monkeypatch, api_mod,
        _FakeAiohttpSession(_FakeAiohttpResponse(200, {"Content-Type": "image/gif"}, poisoned)),
    )

    async with guarded_sequence(ctx, slots_needed=2) as (request, _info2):
        await request(ENDPOINTS["get_resume_detail"], params=[("idno", "1234567890123")])
        payload = await request(_PHOTO_ROUTE, asset_url=_PHOTO_URL)

    # No SessionUnavailable(kind="challenge"), no "expired" — a success.
    assert payload["format"] == "jpeg"
    assert payload["body_bytes"] == poisoned


# -- no asset URL and no cookie value, in a return value OR a log line ----

@pytest.mark.asyncio
async def test_a_refused_asset_url_never_leaks_the_token_into_the_payload_or_the_log(monkeypatch, caplog):
    ctx, info = _new_api_ctx_and_session()
    info.cookies = [{"name": "its", "value": "SYNTHETIC-COOKIE", "domain": ".104.com.tw"}]
    spy = _AssetSeqFetchSpy([])
    _patch_both_transports(monkeypatch, spy)
    bad_url = "https://evil.example/download/resumeAttach/SYNTHETIC-SECRET-TOKEN?v=ALSO-SECRET"

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(ToolAbort) as exc_info:
            async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
                await request(_ATTACH_ROUTE, asset_url=bad_url)

    error = exc_info.value.payload["error"]
    # The return value is part of the contract here: _error_internal_config
    # inlines its detail into a string the Agent reads.
    for forbidden in ("SYNTHETIC-SECRET-TOKEN", "ALSO-SECRET", "?v=", "/download/", bad_url):
        assert forbidden not in error
        assert forbidden not in caplog.text
    # What it MAY carry: the route key, which check failed, and — because
    # this is the host check — the hostname.
    assert "resume_attachment" in error
    assert "evil.example" in error
    assert "SYNTHETIC-COOKIE" not in caplog.text
    assert spy.calls == []


@pytest.mark.asyncio
async def test_a_failing_asset_request_never_logs_the_url_or_the_cookie(monkeypatch, caplog):
    ctx, info = _new_api_ctx_and_session()
    info.cookies = [{"name": "its", "value": "SYNTHETIC-COOKIE", "domain": ".104.com.tw"}]
    _patch_both_transports(
        monkeypatch,
        _AssetSeqFetchSpy([_asset_resp(status=500, body_bytes=b"oops")]),
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(GuardAbort):
            async with guarded_sequence(ctx, slots_needed=1) as (request, _info):
                await request(_ATTACH_ROUTE, asset_url=_ATTACH_URL)

    for forbidden in ("SYNTHETIC-TOKEN-BBB", _ATTACH_URL, "SYNTHETIC-COOKIE"):
        assert forbidden not in caplog.text
