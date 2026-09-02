from __future__ import annotations

import ast
import asyncio
import base64
import inspect
import logging
from types import SimpleNamespace

import pytest

from mcp104.browser.cdp_stream import (
    CdpLoginStream,
    cdp_modifiers,
    key_events_for,
    mouse_event_for,
    page_coords,
    wheel_event_for,
)
import mcp104.browser.cdp_stream as cdp_stream_module

# ---------------------------------------------------------------------------
# Blindness note (see spec-tester dispatch): this file is written against
# design.md §C4 / §Architecture (生命週期狀態表) / §Data Models only. It does
# NOT read src/mcp104/browser/cdp_stream.py or research/probes/probe_headless_cdp.py.
# Every CDP method/event name used below (Page.startScreencast,
# Page.stopScreencast, Page.screencastFrame, Page.screencastFrameAck,
# Input.dispatchKeyEvent, Input.dispatchMouseEvent) is the literal Chrome
# DevTools Protocol name referenced directly in design.md's prose, not a guess
# at this module's internals.
# ---------------------------------------------------------------------------


# --- Fakes: shaped per design.md's own description of the CDP session/page
# call shape (`send(method, params)`), plus a minimal aiohttp-WebSocketResponse
# -like sink (`closed` attribute + `async send_str(str)`), as instructed for
# cases whose concrete type the design doesn't name. ---------------------------


class FakeCdpSession:
    # design.md §C4 constraint 2 & the self-heal section state, as fact, that
    # real Chromium emits a fresh screencastFrame the instant
    # Page.startScreencast is (re)issued. This fake reproduces exactly that
    # documented protocol behavior so a stop/start watchdog restart is
    # observable at the sink -- it is not a guess about this module's
    # internals, it is CDP's own contract as design.md states it.
    def __init__(self):
        self.sent: list[tuple[str, dict | None]] = []
        self._handlers: dict[str, object] = {}
        self.fail_methods: set[str] = set()
        self._auto_frame_seq = 10_000

    def on(self, event, handler):
        self._handlers[event] = handler

    def get_handler(self, event):
        return self._handlers[event]

    async def send(self, method, params=None):
        self.sent.append((method, params))
        if method in self.fail_methods:
            raise RuntimeError(f"cdp send failed: {method}")
        if method == "Page.startScreencast":
            handler = self._handlers.get("Page.screencastFrame")
            if handler is not None:
                self._auto_frame_seq += 1
                handler(
                    {
                        "data": base64.b64encode(b"auto-frame").decode(),
                        "metadata": {"deviceWidth": 100, "deviceHeight": 100},
                        "sessionId": self._auto_frame_seq,
                    }
                )
        return {}


class FakePage:
    def __init__(self, session: FakeCdpSession):
        self._session = session

        async def _new_cdp_session(page):
            return self._session

        self.context = SimpleNamespace(new_cdp_session=_new_cdp_session)


class FakeSink:
    def __init__(self, fail: bool = False):
        self.closed = False
        self.sent: list[str] = []
        self.fail = fail

    async def send_str(self, data: str) -> None:
        if self.fail:
            raise RuntimeError("send_str failed")
        self.sent.append(data)


class FlakySink:
    """Fails its first `fail_times` sends, then succeeds -- used to prove a
    later message was NOT delivered by the call that is expected to fail."""

    def __init__(self, fail_times: int = 1):
        self.closed = False
        self.sent: list[str] = []
        self._remaining_fails = fail_times

    async def send_str(self, data: str) -> None:
        if self._remaining_fails > 0:
            self._remaining_fails -= 1
            raise RuntimeError("send_str failed")
        self.sent.append(data)


def _messages(sink) -> list[dict]:
    import json

    return [json.loads(s) for s in sink.sent]


async def _drain():
    # The CDP event emitter contract (`session.on(event, handler)`) requires a
    # plain sync handler; async ack/fanout work must be scheduled as a task.
    # Yield control repeatedly so any such scheduled task actually runs before
    # we assert on its effects.
    for _ in range(50):
        await asyncio.sleep(0)


def _deliver_frame(session: FakeCdpSession, session_id: int, data: bytes = b"x"):
    handler = session.get_handler("Page.screencastFrame")
    return handler(
        {
            "data": base64.b64encode(data).decode(),
            "metadata": {"deviceWidth": 100, "deviceHeight": 100},
            "sessionId": session_id,
        }
    )


def _cdp_calls(session: FakeCdpSession, method: str, since: int = 0):
    return [params for m, params in session.sent[since:] if m == method]


PRINTABLE_KEY_EVENT = {
    "type": "keydown",
    "key": "p",
    "shiftKey": False,
    "ctrlKey": False,
    "altKey": False,
    "metaKey": False,
}


def _mouse_event(event_name: str, **overrides):
    base = {
        "type": "mouse",
        "event": event_name,
        "offset_x": 10.0,
        "offset_y": 10.0,
        "rect_w": 100.0,
        "rect_h": 100.0,
        "device_width": 100.0,
        "device_height": 100.0,
        "button": -1,
        "clickCount": 0,
        "deltaX": 0.0,
        "deltaY": 0.0,
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Module-level pure functions
# ---------------------------------------------------------------------------


def test_t054_modifiers_map_to_cdp_bit_values():
    # Values per the CDP Input.dispatchKeyEvent/dispatchMouseEvent "modifiers"
    # bitmask convention (Alt=1, Control=2, Meta/Command=4, Shift=8) -- an
    # external protocol constant, not this project's private choice.
    assert cdp_modifiers(shift=False, ctrl=False, alt=False, meta=False) == 0
    assert cdp_modifiers(shift=True, ctrl=False, alt=False, meta=False) == 8
    assert cdp_modifiers(shift=False, ctrl=True, alt=False, meta=False) == 2
    assert cdp_modifiers(shift=False, ctrl=False, alt=True, meta=False) == 1
    assert cdp_modifiers(shift=False, ctrl=False, alt=False, meta=True) == 4
    assert cdp_modifiers(shift=True, ctrl=True, alt=False, meta=False) == 10
    assert cdp_modifiers(shift=True, ctrl=True, alt=True, meta=True) == 15


def test_t055_printable_key_produces_rawkeydown_char_keyup_sequence():
    # key_events_for() converts ONE incoming ws event at a time (its signature
    # is key_events_for(event) -> list[dict]). The browser sends keydown and
    # keyup as two separate events; the down->char->up triplet is the
    # concatenation of the two calls, not the output of a single call.
    keydown_event = dict(PRINTABLE_KEY_EVENT, type="keydown")
    keyup_event = dict(PRINTABLE_KEY_EVENT, type="keyup")

    events = key_events_for(keydown_event) + key_events_for(keyup_event)

    assert [e.get("type") for e in events] == ["rawKeyDown", "char", "keyUp"]
    assert not events[0].get("text")
    assert events[1].get("text") == "p"
    assert not events[2].get("text")


def test_t055_named_key_carries_windows_virtual_key_code():
    event = {
        "type": "keydown",
        "key": "Enter",
        "shiftKey": False,
        "ctrlKey": False,
        "altKey": False,
        "metaKey": False,
    }
    events = key_events_for(event)
    assert events, "a named key with a known VK mapping must produce events"
    # VK_RETURN = 0x0D, per the public Win32 virtual-key table CDP itself uses.
    assert all(e.get("windowsVirtualKeyCode") == 13 for e in events)


def test_t055_unknown_key_returns_empty_sequence():
    event = {
        "type": "keydown",
        "key": "UnknownKeyNameThatDoesNotExist",
        "shiftKey": False,
        "ctrlKey": False,
        "altKey": False,
        "metaKey": False,
    }
    assert key_events_for(event) == []


def test_t056_three_pointer_event_kinds_convert_correctly():
    pressed = mouse_event_for(_mouse_event("mousePressed", button=0, clickCount=1))
    released = mouse_event_for(_mouse_event("mouseReleased", button=2, clickCount=1))
    moved = mouse_event_for(_mouse_event("mouseMoved", button=1))
    assert pressed["type"] == "mousePressed" and pressed["button"] == "left"
    assert released["type"] == "mouseReleased" and released["button"] == "right"
    assert moved["type"] == "mouseMoved" and moved["button"] == "middle"


def test_t056_unknown_button_code_maps_to_none_not_left():
    event = _mouse_event("mouseMoved", button=99)
    result = mouse_event_for(event)
    assert result["button"] == "none"


def test_t056_no_button_held_maps_to_none():
    event = _mouse_event("mouseMoved", button=-1)
    result = mouse_event_for(event)
    assert result["button"] == "none"


def test_t056_unrecognized_event_kind_returns_none():
    assert mouse_event_for(_mouse_event("somethingElseEntirely")) is None


def test_t057_wheel_event_carries_both_delta_axes():
    event = _mouse_event("mouseWheel", deltaX=12.5, deltaY=-7.25)
    result = wheel_event_for(event)
    assert result is not None
    assert result["deltaX"] == pytest.approx(12.5)
    assert result["deltaY"] == pytest.approx(-7.25)


def test_t090_scales_offset_when_display_and_page_sizes_differ():
    # display 100x50, page (device) 200x100 -> 2x scale on both axes
    result = page_coords(
        offset_x=25.0, offset_y=10.0, rect_w=100.0, rect_h=50.0,
        device_width=200.0, device_height=100.0,
    )
    assert result == pytest.approx((50.0, 20.0))


@pytest.mark.parametrize(
    "kwargs",
    [
        dict(offset_x=10.0, offset_y=10.0, rect_w=0.0, rect_h=50.0, device_width=200.0, device_height=100.0),
        dict(offset_x=10.0, offset_y=10.0, rect_w=100.0, rect_h=0.0, device_width=200.0, device_height=100.0),
        dict(offset_x=10.0, offset_y=10.0, rect_w=100.0, rect_h=50.0, device_width=0.0, device_height=100.0),
        dict(offset_x=10.0, offset_y=10.0, rect_w=100.0, rect_h=50.0, device_width=200.0, device_height=0.0),
        dict(offset_x=10.0, offset_y=10.0, rect_w=-1.0, rect_h=50.0, device_width=200.0, device_height=100.0),
    ],
)
def test_t090_missing_or_nonpositive_geometry_returns_none_never_a_guess(kwargs):
    assert page_coords(**kwargs) is None


def test_t090_zero_offset_is_legal_top_left_pixel():
    result = page_coords(
        offset_x=0.0, offset_y=0.0, rect_w=100.0, rect_h=50.0,
        device_width=200.0, device_height=100.0,
    )
    assert result == pytest.approx((0.0, 0.0))


# ---------------------------------------------------------------------------
# CdpLoginStream — frame path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t058_every_frame_is_acked_and_ack_failure_does_not_halt_stream():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    assert "Page.startScreencast" in [m for m, _ in session.sent]

    _deliver_frame(session, session_id=1)
    await _drain()
    acked_ids = [p.get("sessionId") for p in _cdp_calls(session, "Page.screencastFrameAck")]
    assert 1 in acked_ids

    session.fail_methods.add("Page.screencastFrameAck")
    _deliver_frame(session, session_id=2)
    await _drain()
    session.fail_methods.discard("Page.screencastFrameAck")

    _deliver_frame(session, session_id=3)
    await _drain()
    acked_ids = [p.get("sessionId") for p in _cdp_calls(session, "Page.screencastFrameAck")]
    # A failed ack must not stop later frames from being processed/acked.
    assert 3 in acked_ids

    await stream.stop()


@pytest.mark.asyncio
async def test_t059_refresh_for_new_viewer_restarts_screencast():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    before = len(session.sent)

    sink = FakeSink()
    stream.add_viewer(sink)
    assert session.sent[before:] == [], "add_viewer is synchronous and must not touch CDP"

    await stream.refresh_for_new_viewer()
    new_methods = [m for m, _ in session.sent[before:]]
    assert "Page.stopScreencast" in new_methods
    assert "Page.startScreencast" in new_methods
    assert new_methods.index("Page.stopScreencast") < new_methods.index("Page.startScreencast")

    await stream.stop()
    # Safe to call after stop(): state is already "closed", so it must be a no-op.
    after_stop = len(session.sent)
    await stream.refresh_for_new_viewer()
    assert session.sent[after_stop:] == []


@pytest.mark.asyncio
async def test_t060_stop_is_idempotent_and_drains_in_flight_ack_before_returning():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()

    ack_started = asyncio.Event()
    ack_may_finish = asyncio.Event()
    ack_completed = {"value": False}
    real_send = session.send

    async def slow_send(method, params=None):
        if method == "Page.screencastFrameAck":
            ack_started.set()
            await ack_may_finish.wait()
            ack_completed["value"] = True
        return await real_send(method, params)

    session.send = slow_send

    _deliver_frame(session, session_id=1)
    await asyncio.wait_for(ack_started.wait(), timeout=2)

    stop_task = asyncio.create_task(stream.stop())
    await asyncio.sleep(0)
    assert not stop_task.done(), "stop() must wait for the in-flight ack to finish"

    ack_may_finish.set()
    await asyncio.wait_for(stop_task, timeout=2)
    assert ack_completed["value"] is True

    # Idempotent: repeated calls after the first must not raise.
    await stream.stop()
    await stream.stop()


@pytest.mark.asyncio
async def test_t061_one_failing_viewer_does_not_break_fanout_to_others():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()

    bad_sink = FakeSink(fail=True)
    good_sink = FakeSink()
    stream.add_viewer(bad_sink)
    stream.add_viewer(good_sink)

    _deliver_frame(session, session_id=1)
    await _drain()

    assert good_sink.sent, "a failing sink must not prevent delivery to other viewers"

    # The stream itself must still be usable after a sink failure.
    _deliver_frame(session, session_id=2)
    await _drain()
    assert len(good_sink.sent) >= 2

    stream.remove_viewer(bad_sink)
    stream.remove_viewer(good_sink)
    await stream.stop()


@pytest.mark.asyncio
async def test_t009a_no_frame_files_written_to_workdir_or_data_dir(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    data_dir = tmp_path / "data"
    data_dir.mkdir()

    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    sink = FakeSink()
    stream.add_viewer(sink)

    for i in range(5):
        _deliver_frame(session, session_id=i, data=f"frame-{i}".encode())
    await _drain()
    await stream.stop()

    files = [p for p in tmp_path.rglob("*") if p.is_file()]
    assert files == []


def _find_function_defs(tree: ast.AST, names: set[str]) -> list[ast.AST]:
    """Every function/method def anywhere in the module whose name is in
    `names` -- not scoped to a class, so this finds the screencast-frame
    handler (a method on CdpLoginStream) and dispatch_input (also a method)
    regardless of which class owns them."""
    return [
        node for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names
    ]


def test_t009b_frame_path_has_no_direct_file_write_or_frame_bearing_log_call():
    """R1.x / privacy: the screencast-frame handler (Page.screencastFrame's
    handler, whatever it is named) and dispatch_input must not write frame
    bytes to a file, nor log/print anything at all from within their own
    function bodies -- not just calls that happen to name a variable
    "frame"/"data"/"metadata". Scanning by function identity (found via the
    CDP event registration and the public dispatch_input method), not by
    guessing argument-name conventions, so a rename of a local variable
    can't hide a violation from this test, and can't produce a false
    positive either."""
    source = inspect.getsource(cdp_stream_module)
    tree = ast.parse(source)

    # Identify the screencast-frame handler by how it is wired up: the
    # callback passed to `self._cdp.on("Page.screencastFrame", <handler>)`.
    handler_names = {"dispatch_input"}
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "on"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and node.args[0].value == "Page.screencastFrame"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Attribute)
        ):
            handler_names.add(node.args[1].attr)

    assert len(handler_names) == 2, (
        f"expected to find exactly the screencastFrame handler plus "
        f"dispatch_input, found {handler_names!r} -- the registration call "
        f"shape this test looks for may have changed"
    )

    targets = _find_function_defs(tree, handler_names)
    assert len(targets) == len(handler_names), (
        f"could not locate a function def for every name in {handler_names!r}"
    )

    write_calls = []
    log_or_print_calls = []

    for fn in targets:
        for node in ast.walk(fn):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)

            if name == "open" and isinstance(func, ast.Name):
                write_calls.append((fn.name, node.lineno))
            if name in {"write", "writelines"} and isinstance(func, ast.Attribute):
                write_calls.append((fn.name, node.lineno))

            if name == "print" and isinstance(func, ast.Name):
                log_or_print_calls.append((fn.name, node.lineno))
            if isinstance(func, ast.Attribute) and func.attr in {
                "debug", "info", "warning", "error", "exception", "critical", "log",
            }:
                log_or_print_calls.append((fn.name, node.lineno))

    assert write_calls == [], f"direct file write(s) found in {write_calls}"
    assert log_or_print_calls == [], (
        f"log/print call(s) found inside the frame-handling or input-dispatch "
        f"function body at {log_or_print_calls} -- frame/keystroke data must "
        f"never reach a log sink from these functions"
    )


# ---------------------------------------------------------------------------
# CdpLoginStream — input path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t002_dispatched_printable_key_never_appears_in_diagnostic_output(caplog, capsys):
    caplog.set_level(logging.DEBUG)
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()

    secret = "Ω7qX"
    for ch in secret:
        event = dict(PRINTABLE_KEY_EVENT, key=ch)
        await stream.dispatch_input(event)

    log_text = "\n".join(r.getMessage() for r in caplog.records)
    out, err = capsys.readouterr()
    for ch in secret:
        assert ch not in log_text
        assert ch not in out
        assert ch not in err

    await stream.stop()


@pytest.mark.asyncio
async def test_t003_printable_key_inserts_character_exactly_once():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    before = len(session.sent)

    await stream.dispatch_input(PRINTABLE_KEY_EVENT)

    char_events = [
        p for p in _cdp_calls(session, "Input.dispatchKeyEvent", since=before)
        if (p or {}).get("type") == "char"
    ]
    assert len(char_events) == 1
    assert char_events[0].get("text") == "p"

    await stream.stop()


@pytest.mark.asyncio
async def test_t004_unmodified_move_is_applied_as_plain_move_not_drag():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    before = len(session.sent)

    event = _mouse_event("mouseMoved", button=-1)
    await stream.dispatch_input(event)

    mouse_calls = _cdp_calls(session, "Input.dispatchMouseEvent", since=before)
    assert len(mouse_calls) == 1
    assert mouse_calls[0].get("button") == "none"

    await stream.stop()


@pytest.mark.asyncio
async def test_t005_pointer_event_maps_to_page_coordinates_when_sizes_differ():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    before = len(session.sent)

    event = _mouse_event(
        "mousePressed", button=0, clickCount=1,
        offset_x=25.0, offset_y=10.0,
        rect_w=100.0, rect_h=50.0, device_width=200.0, device_height=100.0,
    )
    await stream.dispatch_input(event)

    mouse_calls = _cdp_calls(session, "Input.dispatchMouseEvent", since=before)
    assert len(mouse_calls) == 1
    assert mouse_calls[0].get("x") == pytest.approx(50.0)
    assert mouse_calls[0].get("y") == pytest.approx(20.0)

    await stream.stop()


@pytest.mark.asyncio
async def test_t010_wheel_event_reaches_the_controlled_page():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    before = len(session.sent)

    event = _mouse_event("mouseWheel", deltaX=0.0, deltaY=120.0)
    await stream.dispatch_input(event)

    wheel_calls = [
        p for p in _cdp_calls(session, "Input.dispatchMouseEvent", since=before)
        if (p or {}).get("type") == "mouseWheel"
    ]
    assert len(wheel_calls) == 1
    assert wheel_calls[0].get("deltaY") == pytest.approx(120.0)

    await stream.stop()


@pytest.mark.asyncio
async def test_t100_input_is_gated_by_completion_and_the_gate_is_push_independent():
    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()

    before = len(session.sent)
    await stream.dispatch_input(PRINTABLE_KEY_EVENT)
    assert _cdp_calls(session, "Input.dispatchKeyEvent", since=before), (
        "input sent while state == waiting must be applied"
    )

    stream.mark_completed()
    assert stream.state == "completed"

    after_completion = len(session.sent)
    await stream.dispatch_input(PRINTABLE_KEY_EVENT)
    assert _cdp_calls(session, "Input.dispatchKeyEvent", since=after_completion) == [], (
        "input sent after mark_completed() must not be applied"
    )

    # The gate must not depend on a successful push: no viewers registered,
    # and announce_completed()'s only viewer fails outright -- the gate stays
    # shut regardless.
    bad_sink = FakeSink(fail=True)
    stream.add_viewer(bad_sink)
    await stream.announce_completed()
    await _drain()

    after_announce = len(session.sent)
    await stream.dispatch_input(PRINTABLE_KEY_EVENT)
    assert _cdp_calls(session, "Input.dispatchKeyEvent", since=after_announce) == []

    await stream.stop()


# ---------------------------------------------------------------------------
# Self-healing stall recovery (R1.9). Per the coordinator's Mode 2 direction,
# §C4 now names the two watchdog constants used below:
#   WATCHDOG_IDLE_SECONDS  -- how long without a new frame counts as stalled
#   WATCHDOG_POLL_SECONDS  -- the watchdog's own polling interval
# Both are module-level attributes on mcp104.browser.cdp_stream, shortened
# here via monkeypatch so the tests don't depend on their (possibly large)
# production defaults.
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_t006_stalled_page_still_gets_a_frame_update(monkeypatch):
    monkeypatch.setattr(cdp_stream_module, "WATCHDOG_IDLE_SECONDS", 0.02)
    monkeypatch.setattr(cdp_stream_module, "WATCHDOG_POLL_SECONDS", 0.01)

    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    sink = FakeSink()
    stream.add_viewer(sink)
    await stream.start()
    await _drain()

    frames_before = len([m for m in _messages(sink) if m.get("type") == "frame"])
    assert frames_before >= 1, "sanity: the initial startup frame reached the viewer"

    # The page never redraws after this. Only the watchdog can produce a new
    # frame; wait (bounded, as a deadlock guard only -- correctness comes
    # from the shortened constants above, not from the timeout value) for
    # that new frame to reach the sink.
    await asyncio.wait_for(
        _wait_for_condition(
            lambda: len([m for m in _messages(sink) if m.get("type") == "frame"]) > frames_before
        ),
        timeout=5,
    )

    await stream.stop()


@pytest.mark.asyncio
async def test_t099_settling_period_still_refreshes_frames_and_reasserts_completed_per_frame(monkeypatch):
    monkeypatch.setattr(cdp_stream_module, "WATCHDOG_IDLE_SECONDS", 0.02)
    monkeypatch.setattr(cdp_stream_module, "WATCHDOG_POLL_SECONDS", 0.01)

    session = FakeCdpSession()
    stream = CdpLoginStream(FakePage(session))
    await stream.start()
    await _drain()

    # This sink fails exactly once -- the announce_completed() push -- then
    # succeeds. Any "completed" it receives afterwards can only have arrived
    # via the frame fan-out's reassert, never via a second announce.
    sink = FlakySink(fail_times=1)
    stream.add_viewer(sink)

    stream.mark_completed()
    await stream.announce_completed()
    await _drain()
    assert sink.sent == [], "sanity: the one announce_completed() push really failed"

    # No manual redraw from here on -- only the watchdog keeps the stream
    # alive during settling, per the lifecycle table's "既有串流：維持開啟"
    # entry for `settling`.
    await asyncio.wait_for(
        _wait_for_condition(
            lambda: any(
                m.get("type") == "state" and m.get("value") == "completed"
                for m in _messages(sink)
            )
        ),
        timeout=5,
    )

    # And it must not have been a one-shot: a second, later reassertion must
    # also arrive, proving the executor is the (repeating) frame fan-out and
    # not a single deferred retry of the failed announce.
    first_hit_count = len([
        m for m in _messages(sink) if m.get("type") == "state" and m.get("value") == "completed"
    ])
    await asyncio.wait_for(
        _wait_for_condition(
            lambda: len([
                m for m in _messages(sink) if m.get("type") == "state" and m.get("value") == "completed"
            ]) > first_hit_count
        ),
        timeout=5,
    )

    await stream.stop()


async def _wait_for_condition(predicate, poll_interval: float = 0.005):
    while not predicate():
        await asyncio.sleep(poll_interval)
