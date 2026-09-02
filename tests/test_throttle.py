from __future__ import annotations

import logging
import os
import re
import time

import pytest

from mcp104.browser.throttle import (
    ThrottleAbort,
    ThrottleState,
    compact_state_file,
    enforce_throttle,
    evaluate,
    load_throttle_state,
    note_request,
)

# Shared knobs for evaluate()/enforce_throttle() calls below - deliberately
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


async def _enforce(state, *, now_fn, path, **overrides):
    kwargs = dict(
        min_call_interval_seconds=_MIN_CALL_INTERVAL_SECONDS,
        max_requests_per_hour=_MAX_REQUESTS_PER_HOUR,
        max_inline_wait_seconds=_MAX_INLINE_WAIT_SECONDS,
        activity_streak_limit_minutes=_ACTIVITY_STREAK_LIMIT_SECONDS / 60,
        rest_duration_minutes=_REST_DURATION_SECONDS / 60,
    )
    kwargs.update(overrides)
    return await enforce_throttle(state, path=path, now_fn=now_fn, **kwargs)


def _numbers_in(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"-?\d+\.?\d*", text)]


# -- Budget: exhaustion and rolling-window recovery ----------------------
# (Mechanism unchanged by this feature - only the rng-carrying signature
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


# -- Mandatory rest after sustained continuous activity -------------------
# (Mechanism unchanged by this feature - kept as regression coverage.)

def test_rest_triggers_after_20_minutes_of_continuous_activity():
    now = 1_000_000.0
    state = ThrottleState(
        last_call_finished_at=now - 10,
        last_action_at=now - 10,  # recent - well under the 2-min gap-reset threshold
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


# -- T-37 (R10.1): a call inside the minimum interval waits inline -------
# and returns a normal result - the caller never sees an error for this.

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
    monkeypatch, tmp_path,
):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", fake_sleep)

    elapsed_since_last_call = 2.0
    state = ThrottleState(last_call_finished_at=1000.0)
    result = await _enforce(
        state,
        now_fn=lambda: 1000.0 + elapsed_since_last_call,
        path=tmp_path / "throttle.log",
    )

    assert result is None  # a normal result - the caller never sees this wait
    assert sleep_calls == [
        pytest.approx(_MIN_CALL_INTERVAL_SECONDS - elapsed_since_last_call)
    ]


# -- T-38 (R10.2, R10.3): volume cap and forced rest each refuse ---------
# with a retry-after error, and never sleep the wait away inline.

@pytest.mark.asyncio
async def test_volume_cap_reached_returns_retry_after_without_sleeping(monkeypatch, tmp_path):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", fake_sleep)

    # request_timestamps is now sourced from the state FILE on every
    # enforce_throttle call (T-101/C10: file is the sole truth for this
    # field, replacing rather than merging with whatever is already in
    # memory) - so the budget has to be filled on disk, not just in the
    # in-memory ThrottleState, for enforce_throttle to see it.
    t0 = 1_000_000.0
    path = tmp_path / "throttle.log"
    path.write_text("\n".join(str(t0) for _ in range(_MAX_REQUESTS_PER_HOUR)) + "\n")
    state = ThrottleState()

    result = await _enforce(state, now_fn=lambda: t0 + 10, path=path)

    assert result is not None
    assert isinstance(result, ThrottleAbort)
    assert result.kind == "throttled"
    assert result.payload is not None
    assert "retry_after_seconds" in result.payload
    assert result.payload["retry_after_seconds"] > 0
    assert sleep_calls == []  # the cap is never slept away inline


@pytest.mark.asyncio
async def test_forced_rest_returns_retry_after_without_sleeping(monkeypatch, tmp_path):
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

    result = await _enforce(state, now_fn=lambda: now, path=tmp_path / "throttle.log")

    assert result is not None
    assert isinstance(result, ThrottleAbort)
    assert result.kind == "throttled"
    assert result.payload is not None
    assert result.payload["retry_after_seconds"] == pytest.approx(_REST_DURATION_SECONDS)
    assert sleep_calls == []  # the rest is never slept away inline


# -- T-39 (R10.4): an API call is counted toward the rolling window ------

def test_a_call_counted_via_note_request_can_fill_the_rolling_window(tmp_path):
    # Real wall-clock time, so this doesn't depend on whether note_request
    # draws its own timestamp from an injected clock or from real time.
    real_now = time.time()
    state = ThrottleState()
    for _ in range(_MAX_REQUESTS_PER_HOUR - 1):
        state.request_timestamps.append(real_now)

    one_short_of_the_cap = _evaluate(state, now=real_now)
    assert one_short_of_the_cap.proceed is True

    note_request(state, path=tmp_path / "throttle.log")

    at_the_cap = _evaluate(state, now=real_now)
    assert at_the_cap.proceed is False
    assert at_the_cap.reason == "budget"


# -- T-40 (R10.5): the observed inter-call interval is logged ------------
# note_request has no clock-injection parameter, so the interval is driven
# by seeding ThrottleState's own timestamp field directly - the same
# public-construction idiom every other test in this file already uses
# (e.g. ThrottleState(last_call_finished_at=1000.0) above).

def test_observed_inter_call_interval_is_logged(caplog, tmp_path):
    caplog.set_level(logging.DEBUG)

    observed_interval = 42.0
    state = ThrottleState(last_request_logged_at=time.time() - observed_interval)

    note_request(state, path=tmp_path / "throttle.log")

    logged_numbers = [
        n for record in caplog.records for n in _numbers_in(record.getMessage())
    ]
    assert any(abs(n - observed_interval) < 0.5 for n in logged_numbers), (
        f"expected the observed ~{observed_interval}s inter-call interval to "
        f"appear in a log message; got: {[r.getMessage() for r in caplog.records]}"
    )


# -- T-53 (note_request, interface): exactly one timestamp per call ------

def test_note_request_appends_exactly_one_timestamp_per_call(tmp_path):
    path = tmp_path / "throttle.log"
    state = ThrottleState()
    assert len(state.request_timestamps) == 0

    note_request(state, path=path)
    assert len(state.request_timestamps) == 1

    note_request(state, path=path)
    note_request(state, path=path)
    assert len(state.request_timestamps) == 3


# -- T-60 (R10.6): an early retry earns the same refusal ------------------

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


# =========================================================================
# T-119 - ThrottleAbort (interface): the __post_init__ mode invariant,
# both directions.
# =========================================================================

def test_throttled_kind_with_payload_and_empty_detail_constructs():
    abort = ThrottleAbort(kind="throttled", payload={"retry_after_seconds": 5}, detail="")
    assert abort.kind == "throttled"
    assert abort.payload == {"retry_after_seconds": 5}
    assert abort.detail == ""


def test_internal_config_kind_with_detail_and_no_payload_constructs():
    abort = ThrottleAbort(kind="internal_config", payload=None, detail="disk read failed")
    assert abort.kind == "internal_config"
    assert abort.payload is None
    assert abort.detail == "disk read failed"


def test_some_other_kind_with_detail_and_no_payload_also_constructs():
    # The rule is stated as "every OTHER kind", not "internal_config
    # specifically" - an implementation that only special-cases
    # internal_config would still pass the test above while failing this
    # one, which is exactly the hole this case exists to close.
    abort = ThrottleAbort(kind="expired", payload=None, detail="session expired")
    assert abort.kind == "expired"
    assert abort.payload is None
    assert abort.detail == "session expired"


def test_throttled_kind_without_payload_is_rejected_at_construction():
    # This is the case the design calls out as the reason T-119 exists: it
    # used to construct successfully under an "exactly one of the two is
    # real" rule, yet it would reach the agent as a throttle refusal with no
    # retry_after_seconds - indistinguishable from an internal error.
    with pytest.raises(Exception):
        ThrottleAbort(kind="throttled", payload=None, detail="")


def test_throttled_kind_with_both_payload_and_detail_is_rejected():
    with pytest.raises(Exception):
        ThrottleAbort(
            kind="throttled",
            payload={"retry_after_seconds": 5},
            detail="also has detail",
        )


def test_non_throttled_kind_with_neither_payload_nor_detail_is_rejected():
    with pytest.raises(Exception):
        ThrottleAbort(kind="internal_config", payload=None, detail="")


def test_non_throttled_kind_carrying_a_payload_is_rejected():
    with pytest.raises(Exception):
        ThrottleAbort(
            kind="internal_config",
            payload={"retry_after_seconds": 5},
            detail="disk read failed",
        )


# =========================================================================
# T-106 - load_throttle_state (interface)
# =========================================================================

def test_missing_file_returns_empty_state_not_error(tmp_path):
    path = tmp_path / "does-not-exist.log"
    state = load_throttle_state(path)
    assert list(state.request_timestamps) == []


def test_existing_unreadable_file_raises_not_returns_empty_state(tmp_path):
    # A directory at the state-file path can never be parsed as the
    # append-only timestamp log - this stands in for "exists but unreadable"
    # (permissions/disk failure) without needing OS-specific permission
    # tricks that don't port to Windows.
    path = tmp_path / "throttle.log"
    path.mkdir()
    with pytest.raises(Exception):
        load_throttle_state(path)


def test_unparseable_line_counts_as_one_request_not_skipped(tmp_path):
    path = tmp_path / "throttle.log"
    path.write_text("1000.0\nNOT-A-TIMESTAMP\n2000.0\n")

    state = load_throttle_state(path)

    # Three lines, three counted requests - a skip would leave only 2.
    assert len(list(state.request_timestamps)) == 3


def test_malformed_line_between_valid_lines_is_dated_by_the_following_timestamp(tmp_path):
    path = tmp_path / "throttle.log"
    path.write_text("1000.0\nGARBLED\n2000.0\n")

    state = load_throttle_state(path)
    timestamps = sorted(state.request_timestamps)

    # The garbled line is dated by the timestamp that follows it in the
    # file (2000.0), not the one before it (1000.0) - the design requires
    # "not earlier than the true value".
    assert timestamps == [pytest.approx(1000.0), pytest.approx(2000.0), pytest.approx(2000.0)]


def test_trailing_malformed_line_is_dated_by_file_mtime(tmp_path):
    path = tmp_path / "throttle.log"
    path.write_text("1000.0\nTRAILING-GARBAGE")
    before = time.time()

    state = load_throttle_state(path)

    timestamps = sorted(state.request_timestamps)
    assert len(timestamps) == 2
    # The trailing garbage line has no following timestamp to inherit, so
    # it must be dated by the file's own mtime - i.e. close to "now" for a
    # file that was just written.
    assert timestamps[-1] == pytest.approx(before, abs=30)


def test_timestamps_are_returned_sorted_oldest_to_newest_despite_file_order(tmp_path):
    path = tmp_path / "throttle.log"
    # Simulates two overlapping processes interleaving their appends -
    # the file itself is not guaranteed to be in order.
    path.write_text("3000.0\n1000.0\n2000.0\n")

    state = load_throttle_state(path)

    assert list(state.request_timestamps) == [
        pytest.approx(1000.0), pytest.approx(2000.0), pytest.approx(3000.0),
    ]


def test_append_failure_is_not_lost_from_the_next_read(tmp_path):
    # A directory at the state-file path makes every append fail, exactly
    # like the "unreadable" test above makes every read fail. note_request
    # must not raise, and the failed timestamp must not simply vanish.
    path = tmp_path / "throttle.log"
    path.mkdir()
    state = ThrottleState()

    note_request(state, path=path)  # must not raise

    assert len(state.unpersisted_timestamps) == 1


# =========================================================================
# T-107 - compact_state_file (interface)
# =========================================================================

def test_compact_drops_only_entries_older_than_the_longest_window(tmp_path):
    path = tmp_path / "throttle.log"
    now = time.time()
    very_old = now - 30 * 24 * 3600  # far outside any of the three windows
    very_recent = now - 1

    lines = [f"{very_old}" for _ in range(3)] + [f"{very_recent}" for _ in range(3)]
    path.write_text("\n".join(lines) + "\n")

    compact_state_file(path)

    remaining = [float(x) for x in path.read_text().splitlines() if x.strip()]
    assert len(remaining) == 3
    assert all(t == pytest.approx(very_recent) for t in remaining)


def test_compact_is_a_pure_no_op_including_mtime_when_nothing_expired(tmp_path):
    path = tmp_path / "throttle.log"
    now = time.time()
    path.write_text(f"{now - 1}\n{now - 2}\n{now - 3}\n")
    original_bytes = path.read_bytes()
    original_mtime = path.stat().st_mtime

    compact_state_file(path)

    assert path.read_bytes() == original_bytes
    assert path.stat().st_mtime == pytest.approx(original_mtime, abs=0.01)


def test_compact_keeps_a_within_window_malformed_trailing_line_verbatim(tmp_path):
    path = tmp_path / "throttle.log"
    now = time.time()
    original_text = f"{now - 1}\nTRAILING-GARBAGE"
    path.write_text(original_text)

    compact_state_file(path)

    # The malformed line ages via the file's own (recent) mtime, so it's
    # within every window and must be kept - and kept byte-for-byte, not
    # rewritten as a derived numeric timestamp.
    assert "TRAILING-GARBAGE" in path.read_text()


def test_compact_drops_a_malformed_line_whose_derived_age_is_out_of_window(tmp_path):
    path = tmp_path / "throttle.log"
    now = time.time()
    # The valid entry is textually "recent" so it must survive; the trailing
    # garbage line's age is derived from the file's mtime, which we then
    # force far into the past so the garbage line ages out.
    path.write_text(f"{now}\nTRAILING-GARBAGE")
    old_mtime = now - 30 * 24 * 3600
    os.utime(path, (old_mtime, old_mtime))

    compact_state_file(path)

    remaining = path.read_text()
    assert "TRAILING-GARBAGE" not in remaining
    assert str(now) in remaining or f"{now}" in remaining


def test_compact_abandons_the_rewrite_when_the_file_changed_since_it_was_read(
    tmp_path, monkeypatch,
):
    path = tmp_path / "throttle.log"
    now = time.time()
    very_old = now - 30 * 24 * 3600
    path.write_text(f"{very_old}\n{now}\n")
    original_bytes = path.read_bytes()

    real_stat = os.stat
    calls = {"n": 0}

    def flaky_stat(target, *args, **kwargs):
        result = real_stat(target, *args, **kwargs)
        if str(target) == str(path):
            # compact_state_file may legitimately stat this path more than
            # twice (e.g. once just to check existence, before its actual
            # read-time and pre-write-recheck stats) — asserting a call
            # count would pin an internal detail. Instead make EVERY call
            # against this path report a distinct size/mtime, so any two
            # stats compact_state_file compares will disagree regardless
            # of which call indices it happens to use.
            calls["n"] += 1
            n = calls["n"]

            class _Touched:
                st_size = result.st_size + n
                st_mtime = result.st_mtime + n
                st_mtime_ns = result.st_mtime_ns + n * 1_000_000_000

            return _Touched()
        return result

    monkeypatch.setattr(os, "stat", flaky_stat)

    compact_state_file(path)

    # Abandoning is safe by design: the file is simply left un-shortened,
    # not damaged - content must be byte-identical to before the call.
    assert path.read_bytes() == original_bytes


# =========================================================================
# T-101 - enforce_throttle (interface): cross-process persistence
# =========================================================================

@pytest.mark.asyncio
async def test_hourly_window_counts_the_previous_processs_requests(tmp_path):
    path = tmp_path / "throttle.log"
    max_per_hour = 5
    now = time.time()
    path.write_text("\n".join(str(now) for _ in range(max_per_hour)) + "\n")

    # A brand-new, in-memory-empty state - nothing here says "budget
    # exhausted" except what the previous process wrote to disk.
    state = ThrottleState()

    result = await _enforce(
        state,
        now_fn=lambda: now + 1,
        path=path,
        max_requests_per_hour=max_per_hour,
        min_call_interval_seconds=0.01,
        activity_streak_limit_minutes=1000,
        rest_duration_minutes=1,
    )

    assert result is not None
    assert result.kind == "throttled"


@pytest.mark.asyncio
async def test_first_call_of_a_new_run_still_pays_the_interval_floor(monkeypatch, tmp_path):
    sleep_calls: list[float] = []

    async def fake_sleep(seconds):
        sleep_calls.append(seconds)

    monkeypatch.setattr("mcp104.browser.throttle._sleep", fake_sleep)

    path = tmp_path / "throttle.log"
    min_interval = 5.0
    now = time.time()
    # The previous process's last call finished a moment ago - well inside
    # the floor for a brand-new process's very first call.
    path.write_text(f"{now - 1}\n")

    state = ThrottleState()  # nothing carried over in memory
    result = await _enforce(
        state,
        now_fn=lambda: now,
        path=path,
        min_call_interval_seconds=min_interval,
        max_requests_per_hour=10_000,
        activity_streak_limit_minutes=1000,
        rest_duration_minutes=1,
    )

    assert result is None  # the call proceeds, it just waits first
    assert len(sleep_calls) == 1
    waited = sleep_calls[0]
    # Two back-to-back runs must not see a zero gap at the boundary, and
    # the wait must not exceed the floor itself.
    assert waited > 0
    assert waited <= min_interval


@pytest.mark.asyncio
async def test_previous_processs_activity_streak_can_trigger_rest_in_a_new_run(tmp_path):
    path = tmp_path / "throttle.log"
    now = time.time()
    # A short, tight burst of "calls" one second apart - well under any
    # plausible activity-gap-reset threshold, so they read as one
    # unbroken streak reaching back several seconds.
    timestamps = [now - i for i in range(6, -1, -1)]
    path.write_text("\n".join(str(t) for t in timestamps) + "\n")

    state = ThrottleState()  # a fresh process, nothing in memory
    result = await _enforce(
        state,
        now_fn=lambda: now,
        path=path,
        min_call_interval_seconds=0.01,
        max_requests_per_hour=10_000,
        activity_streak_limit_minutes=6 / 60,  # 6 seconds - the streak above just reaches it
        rest_duration_minutes=3,
    )

    assert result is not None
    assert result.kind == "throttled"
    assert result.payload["retry_after_seconds"] == pytest.approx(180, rel=0.05)


@pytest.mark.asyncio
async def test_unreadable_state_file_aborts_as_internal_config_not_throttled(tmp_path):
    path = tmp_path / "throttle.log"
    path.mkdir()  # exists, but can never be parsed as the log - read fails

    state = ThrottleState()
    result = await _enforce(state, now_fn=lambda: time.time(), path=path)

    # (1) does not raise - already true by virtue of reaching this line.
    # (2) kind is exactly internal_config, not a made-up value.
    assert result is not None
    assert result.kind == "internal_config"
    # (3) the request is not sent - the guard treats any non-None result
    #     from enforce_throttle as an abort, which this is.
    # (4) payload is None, detail is non-empty.
    assert result.payload is None
    assert result.detail != ""


@pytest.mark.asyncio
async def test_resting_until_does_not_carry_over_a_partial_remainder_across_runs(tmp_path):
    # A previous process's continuous-activity streak, persisted as plain
    # request timestamps (resting_until itself is never written to disk).
    path = tmp_path / "throttle.log"
    now = time.time()
    timestamps = [now - i for i in range(4, -1, -1)]
    path.write_text("\n".join(str(t) for t in timestamps) + "\n")

    rest_minutes = 3
    state = ThrottleState()  # a brand-new process - no resting_until in memory
    result = await _enforce(
        state,
        now_fn=lambda: now,
        path=path,
        min_call_interval_seconds=0.01,
        max_requests_per_hour=10_000,
        activity_streak_limit_minutes=4 / 60,
        rest_duration_minutes=rest_minutes,
    )

    assert result is not None
    assert result.kind == "throttled"
    # A full rest is served, not some fraction "remaining" from a rest the
    # previous process may have already been serving - there is nothing to
    # resume from, because resting_until never crossed the process boundary.
    assert result.payload["retry_after_seconds"] == pytest.approx(rest_minutes * 60, rel=0.05)
