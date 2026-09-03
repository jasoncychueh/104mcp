"""stdio-cdp-rearchitecture design.md Testing Strategy §C5 — login view page and
local endpoints (`src/mcp104/web/auth_server.py`).

Written BLIND to auth_server.py's implementation (spec-tester Mode 1 rule): every
assertion below comes from design.md's declared Interfaces/Data Models for §C5
(`create_auth_app`, `start_auth_site`, `AuthEndpoint`, `resolve_auth_binding`,
`Binding`), the §Architecture lifecycle state table (the render-precedence rule for
the four view-page screens, the token-rejection byte-identity rule), and
requirements.md R1.10/R1.11/R1.13/R1.15/R10.1-3 — never from reading auth_server.py
itself. `mcp104.browser.cdp_stream` (§C4, already implemented, explicitly readable
per this dispatch) is used only to model the shape `get_admissible_stream` must
return (`state` property, `add_viewer`/`remove_viewer`/`refresh_for_new_viewer`/
`dispatch_input`), via a hand-rolled `FakeStream` — no real page/CDP session is
created.

Cases: T-7, T-46, T-62, T-63, T-64, T-65, T-85, T-92, T-102, T-105, T-117.

Scope note on T-7/T-46/T-105 (the four-screen distinguishability trio): which of
the four screens the client actually renders for a given connection (the
render-precedence rule) is not in this suite's automated coverage — client-side
rendering logic runs as inline JS shipped inside the served HTML page, and this
suite never executes it (no browser). That is Phase 5's manual visual acceptance
checklist item 2, a real human looking at the page, not a case in this file. What
these three cases pin down instead is the server-observable half of the contract
the client-side rendering logic depends on: all four screen copy strings exist,
pairwise distinct as full sentences (not just by shared prefix); the two live
`state` values ("waiting"/"completed") are distinct and delivered over the
WebSocket, with no third "ended"-shaped value ever appearing online; and the two
close-time screens' own server-side drivers — a connection that never saw
"completed" gets its socket closed by the server when the source ends (not left
dangling), a connection that DID see "completed" gets that message delivered
strictly before its own socket close.
"""
from __future__ import annotations

import asyncio
import logging
import socket

import aiohttp
import pytest
from aiohttp.test_utils import TestClient, TestServer

from mcp104.config import get_config
from mcp104.web.auth_server import (
    create_auth_app,
    resolve_auth_binding,
    start_auth_site,
)


# ── Fakes ─────────────────────────────────────────────────────────────────

class FakeStream:
    """Stands in for browser.cdp_stream.CdpLoginStream (§C4, already
    implemented) at the shape get_admissible_stream is declared to hand back:
    a `state` property plus add_viewer/remove_viewer/refresh_for_new_viewer/
    dispatch_input. No real page or CDP session — auth_server.py only ever
    needs this shape."""

    def __init__(self, state: str = "waiting"):
        self._state = state
        self.viewers: list = []
        self.dispatched: list = []
        self.refresh_count = 0

    @property
    def state(self) -> str:
        return self._state

    def set_completed(self) -> None:
        self._state = "completed"

    def add_viewer(self, sink) -> None:
        self.viewers.append(sink)

    def remove_viewer(self, sink) -> None:
        if sink in self.viewers:
            self.viewers.remove(sink)

    async def refresh_for_new_viewer(self) -> None:
        """Real CdpLoginStream restarts the screencast here, which produces a
        new frame; frame fan-out re-asserts whatever state was last read
        (§C4). Mimicked by pushing one frame+state pair to every registered
        sink (per Mode-2 correction: the view page never gets a bare `state`
        push on connect — it rides along with a frame)."""
        self.refresh_count += 1
        await self.push_state_frame()

    async def dispatch_input(self, event: dict) -> None:
        self.dispatched.append(event)

    async def push_state_frame(self) -> None:
        """Test-only hook standing in for "the next screencast frame arrives"
        — used to simulate the reassertion §C4 describes happening on every
        frame during `settling`, since this fake has no real screencast
        loop to drive it automatically."""
        for sink in list(self.viewers):
            await sink.send_json({"type": "frame", "data": "", "metadata": {}})
            await sink.send_json({"type": "state", "value": self._state})

    async def stop(self) -> None:
        """Models the one documented effect of the real CdpLoginStream.stop()
        that this suite can observe without a browser (§C4/§Data Models: no
        online value ever carries "closed" — the only server-observable
        signal that the source ended is the socket itself closing): it
        closes every registered viewer sink. auth_server.py registers the
        live WebSocketResponse as that sink via add_viewer, so calling this
        closes the real WS connection from the server side, exactly as the
        real stream's stop() does via `await sink.close()` on each viewer."""
        self._state = "closed"
        for sink in list(self.viewers):
            await sink.close()
        self.viewers.clear()


def _stream_lookup(streams: dict):
    """token -> CdpLoginStream | None, matching get_admissible_stream's declared
    shape (design.md §C5)."""

    def get_admissible_stream(token: str):
        return streams.get(token)

    return get_admissible_stream


@pytest.fixture(autouse=True)
def _auth_env(monkeypatch, tmp_path):
    """get_config() requires an identity value and a writable data dir
    (mcp104/config.py, already implemented, explicitly readable). Neither is
    what any of these cases are about, so it's set once here rather than per
    test."""
    monkeypatch.setenv("MCP104_ACCOUNT", "test-account")
    monkeypatch.setenv("MCP104_DATA_DIR", str(tmp_path))
    monkeypatch.delenv("MCP104_AUTH_BIND_PORT", raising=False)
    monkeypatch.delenv("MCP104_AUTH_BASE_URL", raising=False)


# ── T-62 — resolve_auth_binding: the two legal configurations, and the two
#    half-set configurations that must fail loudly instead of falling back ──

def test_t062_resolve_auth_binding_both_set_uses_them(monkeypatch):
    monkeypatch.setenv("MCP104_AUTH_BIND_PORT", "18081")
    monkeypatch.setenv("MCP104_AUTH_BASE_URL", "https://example.internal:9443")
    config = get_config()
    binding = resolve_auth_binding(config)
    assert binding.host == "127.0.0.1"
    assert binding.port == 18081


def test_t062_resolve_auth_binding_both_unset_binds_localhost_ephemeral(monkeypatch):
    config = get_config()
    binding = resolve_auth_binding(config)
    assert binding.host == "127.0.0.1"
    assert binding.port is None


def test_t062_resolve_auth_binding_only_port_set_fails_loudly(monkeypatch):
    monkeypatch.setenv("MCP104_AUTH_BIND_PORT", "18081")
    config = get_config()
    with pytest.raises(Exception):
        resolve_auth_binding(config)


def test_t062_resolve_auth_binding_only_base_url_set_fails_loudly(monkeypatch):
    monkeypatch.setenv("MCP104_AUTH_BASE_URL", "https://example.internal:9443")
    config = get_config()
    with pytest.raises(Exception):
        resolve_auth_binding(config)


# ── T-63 — start_auth_site: the returned address already carries the real
#    port before the caller could have learned it any other way ───────────

@pytest.mark.asyncio
async def test_t063_start_auth_site_returned_address_already_has_the_real_port():
    config = get_config()
    app = create_auth_app(_stream_lookup({}))
    endpoint = await start_auth_site(app, config)
    try:
        assert endpoint.port > 0
        assert str(endpoint.port) in endpoint.base_url
        # The listener is already accepting on exactly the reported port —
        # not a placeholder value discovered after the fact.
        reader, writer = await asyncio.open_connection("127.0.0.1", endpoint.port)
        writer.close()
        await writer.wait_closed()
    finally:
        await endpoint.close()


# ── T-64 — create_auth_app: both routes exist; an unknown token 404s on both ──

@pytest.mark.asyncio
async def test_t064_create_auth_app_view_and_ws_routes_404_for_unknown_token():
    app = create_auth_app(_stream_lookup({}))
    async with TestClient(TestServer(app)) as client:
        view_resp = await client.get("/auth/never-issued-token")
        assert view_resp.status == 404

        ws_resp = await client.get("/auth/never-issued-token/ws")
        assert ws_resp.status == 404


# ── T-65 — access log is off ────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t065_auth_site_access_log_is_disabled(caplog):
    config = get_config()
    app = create_auth_app(_stream_lookup({}))
    endpoint = await start_auth_site(app, config)
    try:
        with caplog.at_level(logging.DEBUG, logger="aiohttp.access"):
            reader, writer = await asyncio.open_connection("127.0.0.1", endpoint.port)
            writer.write(
                b"GET /auth/whatever HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\nConnection: close\r\n\r\n"
            )
            await writer.drain()
            await reader.read()
            writer.close()
            await writer.wait_closed()
        assert not any(r.name == "aiohttp.access" for r in caplog.records)
    finally:
        await endpoint.close()


# ── T-85 — R1.13: once a login is finished or abandoned, both routes for
#    that token get "not found" ────────────────────────────────────────

@pytest.mark.asyncio
async def test_t085_finished_or_abandoned_token_is_not_found_on_both_routes():
    # From auth_server's own vantage point, "finished" and "abandoned" both
    # collapse into "get_admissible_stream no longer returns anything for
    # this token" — that boundary is exactly what §C5 says the route layer
    # is allowed to know (admission is closed either way; the pool layer
    # above owns which of the two happened).
    app = create_auth_app(_stream_lookup({}))
    async with TestClient(TestServer(app)) as client:
        view_resp = await client.get("/auth/once-had-a-login")
        assert view_resp.status == 404
        ws_resp = await client.get("/auth/once-had-a-login/ws")
        assert ws_resp.status == 404


# ── T-92 — R1.15: three causes x two routes = six byte-identical responses,
#    Date header excluded, body length in scope ────────────────────────

@pytest.mark.asyncio
async def test_t092_rejected_tokens_are_byte_identical_across_causes_and_routes():
    app = create_auth_app(_stream_lookup({}))
    tokens = ["never-existed", "already-completed", "already-abandoned"]
    responses = []
    async with TestClient(TestServer(app)) as client:
        for token in tokens:
            for suffix in ("", "/ws"):
                resp = await client.get(f"/auth/{token}{suffix}")
                body = await resp.read()
                headers = {k: v for k, v in resp.headers.items() if k != "Date"}
                responses.append((resp.status, resp.reason, body, headers))

    first = responses[0]
    for other in responses[1:]:
        assert other == first, "rejected-token responses must be byte-identical " \
            "across all three causes and both routes (Date header excluded)"
    assert len(first[2]) > 0, "body length is explicitly in the comparison scope"


# ── T-102 — a configured fixed port already in use fails the login() call
#    cleanly, without leaking resources; the ephemeral path is unaffected ──

@pytest.mark.asyncio
async def test_t102_fixed_port_already_bound_fails_without_leaking_resources(monkeypatch):
    occupier = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    occupier.bind(("127.0.0.1", 0))
    occupier.listen(1)
    port = occupier.getsockname()[1]
    try:
        monkeypatch.setenv("MCP104_AUTH_BIND_PORT", str(port))
        monkeypatch.setenv("MCP104_AUTH_BASE_URL", f"http://127.0.0.1:{port}")
        config = get_config()
        app = create_auth_app(_stream_lookup({}))
        with pytest.raises(Exception):
            await start_auth_site(app, config)
    finally:
        occupier.close()

    # The ephemeral-port path is a separate config; a fixed-port failure above
    # must not have left any global state that breaks it.
    monkeypatch.delenv("MCP104_AUTH_BIND_PORT", raising=False)
    monkeypatch.delenv("MCP104_AUTH_BASE_URL", raising=False)
    config2 = get_config()
    app2 = create_auth_app(_stream_lookup({}))
    endpoint2 = await start_auth_site(app2, config2)
    try:
        assert endpoint2.port > 0
    finally:
        await endpoint2.close()


# ── T-117 — AuthEndpoint.close(): the listener really releases, idempotently ──

@pytest.mark.asyncio
async def test_t117_close_releases_the_port_for_a_new_bind():
    config = get_config()
    app = create_auth_app(_stream_lookup({}))
    endpoint = await start_auth_site(app, config)
    port = endpoint.port
    await endpoint.close()

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        probe.bind(("127.0.0.1", port))
    finally:
        probe.close()


@pytest.mark.asyncio
async def test_t117_close_is_idempotent_and_never_raises():
    config = get_config()
    app = create_auth_app(_stream_lookup({}))
    endpoint = await start_auth_site(app, config)
    await endpoint.close()
    await endpoint.close()  # a second close() must be a no-op, not an error


# ── shared helper: pull the first "state" message off a WS connection,
#    skipping any "frame" messages that ride along with it (Mode-2
#    correction: state is reasserted alongside frames, not sent bare) ──────

async def _receive_state(ws, timeout=5):
    for _ in range(10):
        msg = await ws.receive(timeout=timeout)
        payload = msg.json()
        if payload.get("type") == "state":
            return payload
    raise AssertionError("no 'state' message arrived within 10 frames")


_SCREEN_1_WAITING = "請在下方畫面中完成 104 登入。"
_SCREEN_2_COMPLETED_CONNECTED = "登入已完成，這段期間不需要做任何事，頁面即將自動關閉。"
_SCREEN_3_COMPLETED_THEN_CLOSED = "登入已完成，可以關閉本頁。"
_SCREEN_4_NEVER_COMPLETED_CLOSED = "連線中斷，請重新呼叫 login()。"

_ONLINE_STATE_VOCABULARY = {"waiting", "completed"}


# ── T-7 ─ the view page's four screens are pairwise distinguishable, and
#    the two ways of driving them (online `state` value vs. socket close)
#    are separate mechanisms. Which screen the client actually RENDERS for
#    a given connection (the render-precedence rule) is client-side JS this
#    suite never executes (no browser) -- that is Phase 5's manual visual
#    acceptance checklist item 2, not an automated case. This case only
#    pins the server-observable half: the four copy strings all exist,
#    pairwise differ as full sentences, and are driven by two disjoint
#    mechanisms (state pushes vs. socket close) with no third
#    "ended"-like online value ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_t007_four_screens_are_pairwise_distinguishable():
    streams = {"waiting-token": FakeStream(state="waiting")}
    app = create_auth_app(_stream_lookup(streams))

    async with TestClient(TestServer(app)) as client:
        # Screens 1 vs 2: driven by the online `state` value while the
        # connection is alive.
        async with client.ws_connect("/auth/waiting-token/ws") as ws:
            waiting_payload = await _receive_state(ws)
        assert waiting_payload["type"] == "state"
        assert waiting_payload["value"] == "waiting"

        streams["completed-token"] = FakeStream(state="completed")
        async with client.ws_connect("/auth/completed-token/ws") as ws:
            completed_payload = await _receive_state(ws)
        assert completed_payload["type"] == "state"
        assert completed_payload["value"] == "completed"

        assert waiting_payload["value"] != completed_payload["value"]

        # Negative: the online vocabulary is exactly {"waiting", "completed"}
        # -- no "closed"/"ended"/"disconnected"-shaped value is ever pushed.
        observed_values = {waiting_payload["value"], completed_payload["value"]}
        assert observed_values <= _ONLINE_STATE_VOCABULARY
        assert "closed" not in observed_values
        assert "ended" not in observed_values

        # Screens 3 vs 4 are driven by socket close, not an online value.
        # The four screen copy strings must all exist, verbatim, in the
        # served page, and must be pairwise distinct as full sentences,
        # not merely by their shared completion-notice prefix (screens 2
        # and 3 both start with it).
        page_resp = await client.get("/auth/waiting-token")
        page_text = await page_resp.text()

    screens = [
        _SCREEN_1_WAITING,
        _SCREEN_2_COMPLETED_CONNECTED,
        _SCREEN_3_COMPLETED_THEN_CLOSED,
        _SCREEN_4_NEVER_COMPLETED_CLOSED,
    ]
    for screen_text in screens:
        assert screen_text in page_text, f"screen copy missing from served page: {screen_text!r}"
    for i in range(len(screens)):
        for j in range(i + 1, len(screens)):
            assert screens[i] != screens[j], (
                f"screens {i + 1} and {j + 1} must be distinct full sentences: "
                f"{screens[i]!r} == {screens[j]!r}"
            )


# ── T-46 ─ "stream stopped" and "just no new frames" are two different
#    events on the server side. (c) the disconnect copy is present in the
#    served page for the never-completed precondition -- the CLIENT-side
#    choice of which copy actually renders is out of scope here (client JS,
#    no browser in this suite; Phase 5 manual visual acceptance item 2) ───

@pytest.mark.asyncio
async def test_t046_never_completed_connection_carries_disconnect_copy_precondition():
    streams = {"stalled-token": FakeStream(state="waiting")}
    stream = streams["stalled-token"]
    app = create_auth_app(_stream_lookup(streams))

    async with TestClient(TestServer(app)) as client:
        received = []
        async with client.ws_connect("/auth/stalled-token/ws") as ws:
            received.append(await _receive_state(ws))

            # (b) frames stop but the source is still alive (stream.stop()
            # is NOT called) -- the socket must NOT close. A "close on any
            # pause in frames" implementation would turn an ordinary lull
            # into a spurious disconnect.
            await asyncio.sleep(0.2)
            assert ws.closed is False

            # (a) now the source actually ends -- the socket must close,
            # positively (an event actually fires), not merely "the
            # connection is left dangling and we happen to exit the
            # context manager afterward".
            await stream.stop()
            close_msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert close_msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
            assert ws.closed is True

        # The connection closed having never seen "completed" -- this is
        # the precondition T-46 requires, distinct from T-105's case.
        assert all(m["value"] != "completed" for m in received)

        page_resp = await client.get("/auth/stalled-token")
        page_text = await page_resp.text()
    # (c) The disconnect copy the client would render for exactly this
    # precondition must be present, verbatim, in the served page.
    assert _SCREEN_4_NEVER_COMPLETED_CLOSED in page_text


# ── T-105 ─ a successful login leaves a different trace online than an
#    abandoned one: whether the completion message arrived before close.
#    Which screen the client then renders from that trace is out of scope
#    here (client JS, no browser; Phase 5 manual visual acceptance item 2);
#    (b) "close happens after the settle window elapses" is pinned by
#    T-8/T-120, not repeated here ───────────────────────────────────

@pytest.mark.asyncio
async def test_t105_successful_login_shows_completion_not_disconnect():
    stream = FakeStream(state="waiting")
    streams = {"success-token": stream}
    app = create_auth_app(_stream_lookup(streams))

    async with TestClient(TestServer(app)) as client:
        received = []
        async with client.ws_connect("/auth/success-token/ws") as ws:
            received.append(await _receive_state(ws))
            assert received[0]["value"] == "waiting"

            stream.set_completed()
            await stream.push_state_frame()  # next screencast frame reasserts state
            received.append(await _receive_state(ws))

            assert received[-1]["type"] == "state"
            assert received[-1]["value"] == "completed"
            # Distinct precondition from T-46: this connection DID see
            # "completed" before closing.
            assert any(m["value"] == "completed" for m in received)

            # (a) the completion message's delivery is ordered strictly
            # before the socket close -- proven by reading the NEXT message
            # off this same live connection after the completed state and
            # observing it is the close frame, not by code-sequencing alone
            # (a message pushed after close has no receiver to prove it
            # reached).
            await stream.stop()
            close_msg = await asyncio.wait_for(ws.receive(), timeout=5)
            assert close_msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)

        page_resp = await client.get("/auth/success-token")
        page_text = await page_resp.text()
    assert _SCREEN_3_COMPLETED_THEN_CLOSED in page_text

    # (c) Negative control, from the opposite precondition: a connection
    # that never receives "completed" must have delivered NO "completed"
    # message before its own close -- this is T-105's own reverse case
    # (distinct from T-46's copy-string focus), proving the ordering
    # guarantee above does not leak into the un-completed path.
    never_completed_stream = FakeStream(state="waiting")
    streams["never-completed-token"] = never_completed_stream
    async with TestClient(TestServer(app)) as client:
        received2 = []
        async with client.ws_connect("/auth/never-completed-token/ws") as ws:
            received2.append(await _receive_state(ws))
            await never_completed_stream.stop()
            close_msg2 = await asyncio.wait_for(ws.receive(), timeout=5)
            assert close_msg2.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED)
        assert all(m["value"] != "completed" for m in received2)
