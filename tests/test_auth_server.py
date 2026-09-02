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

Scope note on T-7/T-46/T-105 (the four-screen distinguishability trio): the actual
*rendering* of the four screens is inline client-side JS shipped inside the served
HTML page, which this suite does not execute (no browser — that's T-32's job, and
it isn't in this file's case list). What is tested here is the server-observable
half of the contract that the client-side rendering logic depends on: (a) the two
live `state` values ("waiting"/"completed") are distinct and delivered over the
WebSocket, and (b) the two copy strings the design names for the two close-time
screens are present, verbatim, in the served page (design.md §C5: "登入已完成，可以
關閉本頁" / "連線中斷，請重新呼叫 login()") so that the client-side branch has both
messages available to choose between. Whether the client-side JS picks the right one
is out of this file's reach.
"""
from __future__ import annotations

import asyncio
import logging
import socket

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
    monkeypatch.setenv("MCP104_ACCOUNT_LABEL", "test-account")
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


# ── T-7 — the view page's four screens are pairwise distinguishable, and
#    the two ways of driving them (online `state` value vs. socket close)
#    are separate mechanisms ────────────────────────────────────────────

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

        # Screens 3 vs 4 are driven by socket close, not an online value —
        # design.md is explicit that no online value ever carries "closed".
        # The two close-time copy strings must both exist in the served page
        # for the client-side branch (which decides using "did I ever see
        # state=='completed' before I closed") to choose between.
        page_resp = await client.get("/auth/waiting-token")
        page_text = await page_resp.text()
    assert "登入已完成" in page_text and "可以關閉本頁" in page_text
    assert "連線中斷" in page_text and "login()" in page_text


# ── T-46 — a connection that never received "completed" shows the
#    disconnect copy on close; the precondition (never having received
#    "completed") is load-bearing ───────────────────────────────────────

@pytest.mark.asyncio
async def test_t046_never_completed_connection_carries_disconnect_copy_precondition():
    streams = {"stalled-token": FakeStream(state="waiting")}
    app = create_auth_app(_stream_lookup(streams))

    async with TestClient(TestServer(app)) as client:
        received = []
        async with client.ws_connect("/auth/stalled-token/ws") as ws:
            received.append(await _receive_state(ws))
        # The connection closed (context exit) having never seen "completed" —
        # this is the precondition T-46 requires, distinct from T-105's case.
        assert all(m["value"] != "completed" for m in received)

        page_resp = await client.get("/auth/stalled-token")
        page_text = await page_resp.text()
    # The disconnect copy the client renders for exactly this precondition
    # must be present and must be the string that names re-calling login().
    assert "連線中斷" in page_text
    assert "login()" in page_text


# ── T-105 — a successful login ends with the completion message, not the
#    disconnect message; renders while the connection is still alive ──────

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
        # Distinct precondition from T-46: this connection DID see "completed"
        # before closing.
        assert any(m["value"] == "completed" for m in received)

        page_resp = await client.get("/auth/success-token")
        page_text = await page_resp.text()
    assert "登入已完成" in page_text
