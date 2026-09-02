from __future__ import annotations

import logging
import re
import time

import pytest

from mcp104.browser.throttle import (
    ThrottleState,
    enforce_throttle,
    evaluate,
    is_countable_request,
    note_request,
)

# Shared knobs for evaluate()/enforce_throttle() calls below — deliberately
# not the module's real defaults, so a future default change doesn't
# silently reinterpret what these tests are asserting.
_MIN_CALL_INTERVAL_SECONDS = 6.0
_MAX_REQUESTS_PER_HOUR = 500
_MAX_INLINE_WAIT_SECONDS = 20
_ACTIVITY_STREAK_LIMIT_SECONDS = 20 * 60
_REST_DURATION_SECONDS = 180


def _evaluate(state, *, now):
    return evaluate(
        state,
        now=now,
        min_call_interval_seconds=_MIN_CALL_INTERVAL_SECONDS,
        max_requests_per_hour=_MAX_REQUESTS_PER_HOUR,
        max_inline_wait_seconds=_MAX_INLINE_WAIT_SECONDS,
        activity_streak_limit_seconds=_ACTIVITY_STREAK_LIMIT_SECONDS,
        rest_duration_seconds=_REST_DURATION_SECONDS,
    )


async def _enforce(state, *, now_fn):
    return await enforce_throttle(
        state,
        min_call_interval_seconds=_MIN_CALL_INTERVAL_SECONDS,
        max_requests_per_hour=_MAX_REQUESTS_PER_HOUR,
        max_inline_wait_seconds=_MAX_INLINE_WAIT_SECONDS,
        activity_streak_limit_minutes=_ACTIVITY_STREAK_LIMIT_SECONDS / 60,
        rest_duration_minutes=_REST_DURATION_SECONDS / 60,
        now_fn=now_fn,
    )


def _numbers_in(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+\.?\d*", text)]


# ── Budget: exhaustion and rolling-window recovery ──────────────────────
# (Mechanism unchanged by this feature — only the rng-carrying signature
# drifted. Kept as regression coverage for the retained volume-cap logic.)

def test_budget_exhausted_returns_retry_after_not_exception_not_silent_proceed():
    t0 = 1_000_000.0
    state = ThrottleState()
    for _ in range(_MAX_REQUESTS_PER_HOUR):
        state.request_timestamps.append(t0)

    decision = _evaluate(state, now=t0 + 10)

    assert decision.proceed is False
    assert decision.reason == "budget"
    assert decision.wait_seconds > 0


def test_budget_rolling_window_drops_stale_entries_and_call_proceeds():
    # Same state object across a clock advance, proving the prune actually
    # mutates state rather than being recomputed fresh each call.
    t0 = 1_000_000.0
    state = ThrottleState()
    for _ in range(_MAX_REQUESTS_PER_HOUR):
        state.request_timestamps.append(t0)

    deferred = _evaluate(state, now=t0 + 10)
    assert deferred.proceed is False
    assert deferred.reason == "budget"

    # All entries are now older than the rolling hour window.
    proceeded = _evaluate(state, now=t0 + 3601)
    assert proceeded.proceed is True
    assert len(state.request_timestamps) == 0


# ── Mandatory rest after sustained continuous activity ───────────────────
# (Mechanism unchanged by this feature — kept as regression coverage.)

def test_rest_triggers_after_20_minutes_of_continuous_activity():
    now = 1_000_000.0
    state = ThrottleState(
        last_call_finished_at=now - 10,
        last_action_at=now - 10,  # recent — well under the 2-min gap-reset threshold
        activity_streak_start=now - _ACTIVITY_STREAK_LIMIT_SECONDS,
    )

    decision = _evaluate(state, now=now)

    assert decision.proceed is False
    assert decision.reason == "rest"
    assert decision.wait_seconds == pytest.approx(_REST_DURATION_SECONDS)
    assert state.resting_until == pytest.approx(now + _REST_DURATION_SECONDS)


def test_two_minute_gap_mid_streak_resets_it_and_call_proceeds():
    now = 1_000_000.0
    state = ThrottleState(
        last_call_finished_at=now - 130,
        last_action_at=now - 130,  # >= 120s gap -> resets the streak
        activity_streak_start=now - _ACTIVITY_STREAK_LIMIT_SECONDS,  # would rest if NOT reset
    )

    decision = _evaluate(state, now=now)

    assert decision.proceed is True


# ── T-37 (R10.1): a call inside the minimum interval waits inline ───────
# and returns a normal result — the caller never sees an error for this.

def test_call_inside_the_minimum_interval_proceeds_and_waits_the_remainder():
    now = 1_000_000.0
    elapsed_since_last_call = 2.0
    state = ThrottleState(last_call_finished_at=now - elapsed_since_last_call)

    decision = _evaluate(state, now=now)

    assert decision.proceed is True
    assert decision.wait_seconds == pytest.approx(
        _MIN_CALL_INTERVAL_SECONDS - elapsed_since_last_call
    )


@pytest.mark.asyncio
async def test_enforce_throttle_sleeps_inline_for_a_sub_floor_wait_and_returns_none(
    monkeypatch,
):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", fake_sleep)

    elapsed_since_last_call = 2.0
    state = ThrottleState(last_call_finished_at=1000.0)
    result = await _enforce(
        state, now_fn=lambda: 1000.0 + elapsed_since_last_call
    )

    assert result is None  # a normal result — the caller never sees this wait
    assert sleep_calls == [
        pytest.approx(_MIN_CALL_INTERVAL_SECONDS - elapsed_since_last_call)
    ]


# ── T-38 (R10.2, R10.3): volume cap and forced rest each refuse ─────────
# with a retry-after error, and never sleep the wait away inline.

@pytest.mark.asyncio
async def test_volume_cap_reached_returns_retry_after_without_sleeping(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", fake_sleep)

    t0 = 1_000_000.0
    state = ThrottleState()
    for _ in range(_MAX_REQUESTS_PER_HOUR):
        state.request_timestamps.append(t0)

    result = await _enforce(state, now_fn=lambda: t0 + 10)

    assert result is not None
    assert "retry_after_seconds" in result
    assert result["retry_after_seconds"] > 0
    assert sleep_calls == []  # the cap is never slept away inline


@pytest.mark.asyncio
async def test_forced_rest_returns_retry_after_without_sleeping(monkeypatch):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", fake_sleep)

    now = 1_000_000.0
    state = ThrottleState(
        last_call_finished_at=now - 10,
        last_action_at=now - 10,
        activity_streak_start=now - _ACTIVITY_STREAK_LIMIT_SECONDS,
    )

    result = await _enforce(state, now_fn=lambda: now)

    assert result is not None
    assert "retry_after_seconds" in result
    assert result["retry_after_seconds"] == pytest.approx(_REST_DURATION_SECONDS)
    assert sleep_calls == []  # the rest is never slept away inline


# ── T-39 (R10.4): an API call is counted toward the rolling window ──────

def test_a_call_counted_via_note_request_can_fill_the_rolling_window():
    # Real wall-clock time, so this doesn't depend on whether note_request
    # draws its own timestamp from an injected clock or from real time.
    real_now = time.time()
    state = ThrottleState()
    for _ in range(_MAX_REQUESTS_PER_HOUR - 1):
        state.request_timestamps.append(real_now)

    one_short_of_the_cap = _evaluate(state, now=real_now)
    assert one_short_of_the_cap.proceed is True

    note_request(state)

    at_the_cap = _evaluate(state, now=real_now)
    assert at_the_cap.proceed is False
    assert at_the_cap.reason == "budget"


# ── T-40 (R10.5): the observed inter-call interval is logged ────────────
# note_request has no clock-injection parameter, so the interval is driven
# by seeding ThrottleState's own timestamp field directly — the same
# public-construction idiom every other test in this file already uses
# (e.g. `ThrottleState(last_call_finished_at=1000.0)` above).

def test_observed_inter_call_interval_is_logged(caplog):
    caplog.set_level(logging.DEBUG)

    observed_interval = 42.0
    state = ThrottleState(last_request_logged_at=time.time() - observed_interval)

    note_request(state)

    logged_numbers = [
        n for record in caplog.records for n in _numbers_in(record.getMessage())
    ]
    assert any(abs(n - observed_interval) < 0.5 for n in logged_numbers), (
        f"expected the observed ~{observed_interval}s inter-call interval to "
        f"appear in a log message; got: {[r.getMessage() for r in caplog.records]}"
    )


# ── T-53 (note_request, interface): exactly one timestamp per call ──────

def test_note_request_appends_exactly_one_timestamp_per_call():
    state = ThrottleState()
    assert len(state.request_timestamps) == 0

    note_request(state)
    assert len(state.request_timestamps) == 1

    note_request(state)
    note_request(state)
    assert len(state.request_timestamps) == 3


# ── T-60 (R10.6): an early retry earns the same refusal ─────────────────

def test_early_retry_after_forced_rest_receives_the_same_refusal():
    now = 1_000_000.0
    state = ThrottleState(
        last_call_finished_at=now - 10,
        last_action_at=now - 10,
        activity_streak_start=now - _ACTIVITY_STREAK_LIMIT_SECONDS,
    )

    first = _evaluate(state, now=now)
    assert first.proceed is False
    assert first.reason == "rest"

    # Retries well before the instructed rest duration has elapsed.
    retry_now = now + 5
    second = _evaluate(state, now=retry_now)

    assert second.proceed is False
    assert second.reason == "rest"


# ── Request counting: host classification ────────────────────────────────
# (Unrelated to the pacing mechanism this feature replaces — unchanged.)

def test_is_countable_request_counts_104_hosts_including_uts_beacon():
    assert is_countable_request("https://vip.104.com.tw/search/searchResult") is True
    # uts.104.com.tw beacons measured at 38% of observed image traffic — 104's
    # own host, must count even though it looks analytics-shaped.
    assert is_countable_request("https://uts.104.com.tw/beacon.gif") is True


def test_is_countable_request_excludes_third_party_hosts():
    assert is_countable_request("https://www.googletagmanager.com/gtm.js") is False
    assert is_countable_request("https://www.google-analytics.com/collect") is False
    assert is_countable_request("https://connect.facebook.net/sdk.js") is False
    assert is_countable_request("https://static.hotjar.com/c/hotjar.js") is False
    assert is_countable_request("https://stats.doubleclick.net/r") is False


def test_is_countable_request_excludes_unrelated_hosts():
    assert is_countable_request("https://example.com/anything") is False
