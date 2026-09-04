"""Request-level pacing and rate limiting for a single logged-in session.

Built from ONE passive recording of a real human session (4 minutes, 626
requests, 12 navigations, 2 tabs) — see docs/104-site-facts.md. Every
default in this module is a CONSERVATIVE GUESS anchored on that one short,
single-session sample, not a derived safe threshold. In particular the
sample's idle-gap distribution (median ~14s, tail to 88s) likely
UNDERSTATES real human pauses — a longer recording would probably show
more/longer idle gaps, not fewer. Treat every constant here as a starting
point to be recalibrated once a longer recording exists, not as a
measured ceiling.

What the recording actually showed: burst shape is not where automation
differs from a human — a page load fires ~44-106 requests with near-zero
gaps between them either way. The difference is entirely in (a) the idle
time BETWEEN actions (human: 10-88s, median ~14s; the old uniform-3-8s
delay was well short of that) and (b) session DURATION (the human
stopped after 4 minutes; scripted runs went on for hours). This module
addresses both: `evaluate()` paces inter-action gaps and enforces a
mandatory rest after sustained continuous activity, to model "the human
stops and leaves" — the behaviour the original uniform-delay approach most
conspicuously lacked.

★ Inter-action pacing is now an INTERVAL FLOOR (MIN_CALL_INTERVAL_SECONDS),
not a distribution drawn to approximate the recording's idle-gap shape.
The two are opposite in effect on traffic shape: a drawn delay ADDS an
interval sampled from a distribution of our own devising, so timing
converges on a shape we manufactured — and uniform pacing is itself an
anomaly signature, the exact thing this module exists to avoid. A floor
leaves the caller's own rhythm intact (inference time varies with output
length; the Agent reads results before deciding what to call next) and
clips only the fastest tail.

★ MAX_REQUESTS_PER_HOUR's default was lowered (1800 -> 300) around the
same time this ThrottleState first served only five tools going through
guarded_api and issuing exactly one HTTP request per call. The JSON-API
messaging migration (read_messages / get_conversation / send_message)
moved the remaining three tools onto the same per-request unit too — the
budget is counted per REQUEST, not per tool call, through this one
ThrottleState, with no second traffic shape sharing it. 300/hour is
therefore a call-volume budget for the whole tool surface, sized by how
many requests actually go out, not a number that binds some tools and is
slack for others.

★ The outbound-contact feature adds ONE exception to "one request per
tool call": send_inquiry issues three (a reverse-bridge GET, an
event/last-info GET, then the willingness-event POST itself). The
per-request unit both this module's callers ultimately share is
`tools/helpers.py`'s `_issue_one` — `guarded_api` (one request per tool
call) and its sibling `guarded_sequence` (N requests, one lock, one
throttle-gate check for the whole sequence) both call it once per
sub-request, and `_issue_one` calls `note_request` once per sub-request
in turn, so all three of send_inquiry's requests still land in this same
ThrottleState — the 300/hour budget counts each of the three
individually, not once per tool call. What changes in THIS module is the
gate, not the ledger: evaluate() and enforce_throttle() both take a
`slots_needed` parameter (default 1, the single-request case every other
tool still uses) so the rolling-window check can ask "does the window
have room for this many MORE requests", not just "is it already full" —
a caller that reserves 3 slots is refused
up front if only 1 is free, rather than being admitted and then blowing
the cap by 2 partway through its own burst. See evaluate()'s and
enforce_throttle()'s own docstrings for the mechanism; this module still
computes the reservation, never the English words describing why it was
refused (that stays the guard's job, per the caveat below).

This single-path state does NOT retire the caveats below it used to
carry for the five-tool case — if anything the migration makes them
apply more widely, not less: every constant in this module is still a
conservative guess anchored on ONE 4-minute human recording whose
idle-gap distribution likely UNDERSTATES real pauses (see the module's
opening paragraphs); and that recording's own traffic SHAPE — a page
load firing ~44-106 requests with near-zero gaps between them — is now
something production never produces at all, on any tool this project
registers, which puts the derivation basis further from what actually
ships than it was before this migration, not closer to it. A client that fetches only
JSON and never the surrounding page assets is itself a distinguishing
signature (steering/tech.md §6b.6: "量少不等於可疑度低" — low request
volume is not the same as low suspicion), which is a reason to keep
treating these numbers as a starting point, not a reason the migration
made moot. Stating the single path plainly must not read as license to
stop being careful about where these numbers came from.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from pathlib import Path
log = logging.getLogger("104-mcp.throttle")

# Rolling window for the hourly request budget. Fixed, not env-configurable
# — only the request COUNT allowed within it (MAX_REQUESTS_PER_HOUR in
# config.py) is. "Rolling", not a fixed clock hour: the window is always
# "the last 3600 seconds as of now", so it never resets in a burst at the
# top of an hour.
REQUEST_WINDOW_SECONDS = 3600.0

# A gap this long or longer between successive PROCEEDING calls resets the
# "continuous activity" streak used by the mandatory-rest rule. Not exposed
# as an env var — the dispatch that introduced this module called out only
# the 20-minute streak limit and the 3-minute rest duration as configurable.
ACTIVITY_GAP_RESET_SECONDS = 120.0


@dataclass
class ThrottleState:
    """Per-session throttling bookkeeping. One instance lives on each
    SessionInfo (browser/session.py), created lazily via its default
    factory so a session that never calls a tool costs nothing extra.

    request_timestamps: epoch-second floats, counted toward the rolling
        hourly budget. Populated by note_request only — every tool now
        goes through `tools/helpers.py`'s `_issue_one`, the per-request
        unit shared by `guarded_api` (one request per tool call) and
        `guarded_sequence` (N requests, one per sub-request), calling
        note_request once per sub-request; the aiohttp-based API client
        never issues a counted request outside that path, so this is the
        sole source of counted requests. Tests populate this directly with
        fake-clock values instead of real time.time() values — never mix
        real and fake clocks within one instance, or the rolling-window
        pruning compares them against each other.
    last_request_logged_at: set by note_request only, used only to log the
        observed inter-call interval — kept separate from
        last_call_finished_at (below), which drives the pacing floor and is
        committed at a different point in the call (before the request is
        issued, not after note_request runs).
    unpersisted_timestamps: timestamps that note_request counted in-memory
        but failed to append to the on-disk state file (see note_request's
        own docstring for why that failure is swallowed rather than raised
        or silently dropped). load_throttle_state's result is unioned with
        this list rather than replacing it, so a request that never made it
        to disk is still counted by every subsequent decision made by this
        same process. evaluate() never reads it directly — only the merge
        step in enforce_throttle folds it into request_timestamps.
    """
    request_timestamps: deque[float] = field(default_factory=deque)
    last_call_finished_at: float | None = None
    activity_streak_start: float | None = None
    last_action_at: float | None = None
    resting_until: float | None = None
    last_request_logged_at: float | None = None
    unpersisted_timestamps: list[float] = field(default_factory=list)


@dataclass(frozen=True)
class ThrottleDecision:
    proceed: bool
    wait_seconds: float = 0.0
    reason: str = ""  # "" | "rest" | "budget" | "pacing" — meaningful only when proceed is False


@dataclass(frozen=True)
class ThrottleAbort:
    """What enforce_throttle returns instead of raising when a call must not
    proceed. `kind` selects one of the caller-facing error shapes the guard
    (tools/helpers.py) already knows how to raise as a ToolAbort — this
    module never raises the abort itself, only builds the value describing
    one.

    This is a two-mode type, and the legality of a given (kind, payload,
    detail) combination is a relationship BETWEEN the fields, decided by
    kind in both directions, not a property of any one field alone:

        kind == "throttled"  => payload is not None and detail == ""
        every other kind     => payload is None     and detail != ""

    Writing the rule as "exactly one of payload/detail is set" would look
    equivalent but isn't: it also accepts kind="throttled" with no payload,
    which reaches the caller as a retry-after refusal with no
    retry_after_seconds — indistinguishable from an internal error. The
    property above is checked in both directions at construction time
    (__post_init__) precisely to rule that combination out, not just its
    mirror image.

    payload, when present, is `_retry_after_payload`'s product — that
    wording belongs to this module, since it's this module's own notion of
    "try again in N seconds". detail, when present, is a plain description
    of which file and which kind of read failure; the caller-facing wording
    around it belongs to the guard, not here — this module never imports
    from tools/ (steering/structure.md) and never writes Agent-facing
    prose.
    """
    kind: str
    payload: dict | None
    detail: str

    def __post_init__(self) -> None:
        if self.kind == "throttled":
            if self.payload is None or self.detail != "":
                raise ValueError(
                    "ThrottleAbort(kind='throttled') requires a non-None "
                    "payload and an empty detail"
                )
        else:
            if self.payload is not None or self.detail == "":
                raise ValueError(
                    f"ThrottleAbort(kind={self.kind!r}) requires payload=None "
                    "and a non-empty detail"
                )


def evaluate(
    state: ThrottleState,
    *,
    now: float,
    max_requests_per_hour: int,
    max_inline_wait_seconds: float,
    activity_streak_limit_seconds: float,
    rest_duration_seconds: float,
    min_call_interval_seconds: float,
    slots_needed: int = 1,
) -> ThrottleDecision:
    """Pure decision function — no sleeping, no I/O. Mutates `state` only
    when it commits to a verdict (a rejected call leaves state exactly as
    it found it, except for entering a fresh rest period, which IS the
    verdict). Deterministic given `now`, which is the whole point: tests
    inject it and never need a real clock or real sleep.

    `slots_needed` (default 1, bit-identical to every prior single-request
    call site) asks a different question than the old check did: not "is
    the window already full" but "does the window have room for this many
    MORE requests". A multi-request caller (guarded_sequence's callers:
    send_inquiry with three requests in one tool call, and the two asset
    tools with two each) that started when the
    window had only 1 free slot would otherwise be admitted here and then
    blow the hourly cap on its own — the window is charged per
    request (note_request, once per sub-request) but this gate would have
    looked at it only once, for the whole burst. See the budget branch
    below for how the refusal's retry_after_seconds is computed to match:
    the caller needs ALL `slots_needed` slots free, not just one, so the
    wait is timed to when the Nth-oldest entry (not simply the oldest)
    expires.
    """
    # 1. Already resting from an earlier mandatory-rest trigger?
    if state.resting_until is not None:
        if now < state.resting_until:
            return ThrottleDecision(False, state.resting_until - now, "rest")
        # Rest served. Streak starts clean; last_call_finished_at is left
        # alone deliberately — a 3-minute rest trivially satisfies any
        # pacing gap, so step 4 below computes wait=0 for the first
        # post-rest call either way.
        state.resting_until = None
        state.activity_streak_start = None
        state.last_action_at = None

    # 2. Would this call push continuous activity past the streak limit?
    # Checked before the budget check: if both would fire, the Agent
    # should hear the (shorter, more specific) rest wait once, not the
    # budget wait now and the rest wait on its very next retry.
    gap = now - state.last_action_at if state.last_action_at is not None else None
    streak_start = now if (gap is None or gap >= ACTIVITY_GAP_RESET_SECONDS) else (state.activity_streak_start or now)
    if (now - streak_start) >= activity_streak_limit_seconds:
        state.resting_until = now + rest_duration_seconds
        state.activity_streak_start = None
        # last_action_at intentionally NOT advanced — this call was
        # rejected, not performed.
        return ThrottleDecision(False, rest_duration_seconds, "rest")

    # 3. Hourly request budget, rolling window. The question is "does the
    # window have room for `slots_needed` MORE requests", not "is it
    # already full" — for slots_needed=1 these are the same question
    # (len + 1 > max  <=>  len >= max), which is what keeps every existing
    # single-request call site's verdict bit-identical.
    cutoff = now - REQUEST_WINDOW_SECONDS
    while state.request_timestamps and state.request_timestamps[0] <= cutoff:
        state.request_timestamps.popleft()
    if len(state.request_timestamps) + slots_needed > max_requests_per_hour:
        # Exactly this many entries must expire before `slots_needed` slots
        # are free. For slots_needed=1 this is always 1, so the index below
        # is always 0 — the same "oldest" this branch always waited on.
        entries_that_must_expire = len(state.request_timestamps) + slots_needed - max_requests_per_hour
        nth_oldest = state.request_timestamps[entries_that_must_expire - 1]
        wait = max(0.0, nth_oldest + REQUEST_WINDOW_SECONDS - now)
        return ThrottleDecision(False, wait, "budget")

    # 4. Inter-action pacing: a FLOOR, not a drawn distribution — see the
    # module docstring for why a floor (leaving the caller's own timing
    # variance intact) is the opposite of, and preferred over, a
    # manufactured delay shape.
    if state.last_call_finished_at is None:
        pacing_wait = 0.0  # first call of the session — nothing to pace against
    else:
        elapsed = now - state.last_call_finished_at
        pacing_wait = max(0.0, min_call_interval_seconds - elapsed)

    if pacing_wait > max_inline_wait_seconds:
        # Too long to sleep inside a tool call without risking the MCP
        # client's own timeout (a tool call already takes 15-25s; adding
        # an 87s sleep on top of that would make the client report the
        # call as failed while the work continued server-side — this
        # already happened once with read_messages). Defer instead of
        # blocking: state is untouched, so re-evaluating at retry time
        # recomputes from scratch.
        return ThrottleDecision(False, pacing_wait, "pacing")

    # Proceeding. Commit bookkeeping at the point the action will actually
    # happen (now + whatever inline wait the caller is about to sleep).
    effective_now = now + pacing_wait
    state.activity_streak_start = streak_start
    state.last_action_at = effective_now
    state.last_call_finished_at = effective_now
    return ThrottleDecision(True, pacing_wait, "")


async def _sleep(seconds: float) -> None:
    """Indirection around asyncio.sleep so tests can neutralize JUST this
    module's inline waits via monkeypatch("mcp104.browser.throttle._sleep", ...).

    Patching "mcp104.browser.throttle.asyncio.sleep" instead would look
    module-scoped but isn't: `throttle.py` does `import asyncio` (the
    module object, not a copy), so mcp104.browser.throttle.asyncio IS
    sys.modules["asyncio"] — patching its .sleep attribute mutates the
    ONE global asyncio module for the whole process, silently breaking
    asyncio.sleep(0.01)-as-a-yield-point everywhere else (including test
    code that relies on it to let a concurrently-scheduled task run at
    all). This function exists specifically so patching stays scoped to
    calls made through it.
    """
    await asyncio.sleep(seconds)


def _retry_after_payload(wait_seconds: float) -> dict:
    n = max(1, round(wait_seconds))
    return {
        "error": f"為避免觸發 104 的機器人偵測，請於 {n} 秒後再試",
        "retry_after_seconds": n,
    }


# --- Cross-run persistence ---------------------------------------------
#
# The state file is one timestamp per line, plain text, append-only. One
# line per entry keeps an append a single short write; plain text keeps a
# damaged file readable by a human instead of a total loss.


def _parse_timestamp_line(line: str) -> float | None:
    try:
        return float(line.strip())
    except ValueError:
        return None


def _dated_timestamps(lines: list[str], mtime: float) -> list[float]:
    """One timestamp per line, in file order. A parseable line contributes
    its own value. An unparseable line is dated at the nearest parseable
    timestamp that comes AFTER it in the file (an append is ordered, so the
    line was written before whatever follows it — dating it off that later
    entry never underestimates its age) or, when nothing parseable follows
    it (it sits at the very end of the file), the file's mtime.

    Never "now": a line re-dated to the current time on every read would
    never age out of the rolling window, and the volume budget would jam
    permanently once enough bad lines accumulated.
    """
    ages: list[float] = [0.0] * len(lines)
    next_parseable: float | None = None
    for i in range(len(lines) - 1, -1, -1):
        value = _parse_timestamp_line(lines[i])
        if value is not None:
            ages[i] = value
            next_parseable = value
        else:
            ages[i] = next_parseable if next_parseable is not None else mtime
    return ages


def _derive_streak_start(ordered_timestamps: list[float]) -> float:
    """Walk from the newest timestamp backward, extending the streak while
    the gap to the next-older entry stays under ACTIVITY_GAP_RESET_SECONDS.
    `ordered_timestamps` must already be sorted ascending and non-empty.
    """
    streak_start = ordered_timestamps[-1]
    for i in range(len(ordered_timestamps) - 2, -1, -1):
        gap = ordered_timestamps[i + 1] - ordered_timestamps[i]
        if gap >= ACTIVITY_GAP_RESET_SECONDS:
            break
        streak_start = ordered_timestamps[i]
    return streak_start


def load_throttle_state(path: Path) -> ThrottleState:
    """Read `path` into a ThrottleState determined ONLY by the file's
    content: fields the file can't speak to are left at their dataclass
    defaults. The only caller is enforce_throttle, once per throttling
    decision — this is not a one-time startup hydration step (see the
    module's cross-run-persistence notes for why there is deliberately no
    such step).

    A missing file returns an empty state — that's the ordinary first-run
    case, not an error. An EXISTING file this can't read (permission
    changed, disk fault, corrupt encoding) raises instead of returning an
    empty state: silently falling back to "no requests on record" is
    exactly the undercount this module exists to refuse. The caller
    (enforce_throttle) is the one that turns that raise into a reportable
    value — this function's own contract is unchanged by that: it still
    never swallows a read failure into an empty result.
    """
    if not path.exists():
        return ThrottleState()

    stat_result = path.stat()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        return ThrottleState()

    ages = _dated_timestamps(lines, stat_result.st_mtime)
    ordered = sorted(ages)

    loaded = ThrottleState()
    loaded.request_timestamps = deque(ordered)
    latest = ordered[-1]
    loaded.last_call_finished_at = latest
    loaded.last_action_at = latest
    loaded.activity_streak_start = _derive_streak_start(ordered)
    return loaded


def _merge_loaded_state(state: ThrottleState, loaded: ThrottleState, now: float) -> None:
    """Fold a freshly loaded ThrottleState into the in-memory `state` ahead
    of a throttling decision, per the field-by-field merge rules: some
    fields are replaced outright, some take the earlier/later of the two
    values, and some are deliberately left untouched by a read. See
    load_throttle_state and note_request for what each field means and why
    it crosses (or doesn't cross) a process boundary this way.
    """
    # request_timestamps: the file is the sole truth for this field — this
    # process's own successfully-written requests are already in it.
    # unpersisted_timestamps (requests counted in-memory that failed to
    # reach disk) is unioned back in rather than being replaced away.
    state.request_timestamps = deque(
        sorted(list(loaded.request_timestamps) + state.unpersisted_timestamps)
    )

    # last_call_finished_at / last_action_at: derived from the file, never
    # earlier than the in-memory value, and clamped so a clock skew or a
    # file from another machine can't push the pacing wait past the
    # interval floor itself.
    if loaded.last_call_finished_at is not None:
        merged = loaded.last_call_finished_at
        if state.last_call_finished_at is not None:
            merged = max(merged, state.last_call_finished_at)
        state.last_call_finished_at = min(merged, now)
    if loaded.last_action_at is not None:
        merged = loaded.last_action_at
        if state.last_action_at is not None:
            merged = max(merged, state.last_action_at)
        state.last_action_at = min(merged, now)

    # activity_streak_start: earlier of the two when both are known — the
    # process that has been running longer is the one whose streak start
    # should win.
    if loaded.activity_streak_start is not None:
        if state.activity_streak_start is not None:
            state.activity_streak_start = min(
                state.activity_streak_start, loaded.activity_streak_start
            )
        else:
            state.activity_streak_start = loaded.activity_streak_start

    # resting_until, last_request_logged_at: a read never
    # touches these — see ThrottleState's docstring / this module's
    # cross-run-persistence notes for why each one is exempt.


def compact_state_file(path: Path, *, now_fn=time.time) -> None:
    """Run once, at startup, by the process's startup sequence (not by this
    module). Drops only entries older than the longest rolling window this
    module enforces (REQUEST_WINDOW_SECONDS) — anything inside any window
    stays. When nothing is expired, the file is not touched AT ALL, not
    even its mtime: a rewrite would re-date a trailing unparseable line's
    age (which falls back to the file's mtime) to "just now" on every
    single process start, pinning the two derived fields to that moment
    forever and making every process's first call pay an interval-floor
    wait it wouldn't otherwise owe.

    A read failure propagates — this step doubles as the startup
    precondition check for load_throttle_state, and an unreadable-but-
    present file is a startup failure, not something to route around.

    A WRITE failure is different and does not propagate: it only means the
    file wasn't shortened this time, which harms none of the three
    mechanisms, so it's logged and swallowed. The same non-propagating
    treatment applies when the file is found to have changed (size or
    mtime) since it was read — rewriting over a concurrent append would
    lose the timestamp that append just wrote, which is an undercount, so
    the compaction is abandoned instead. No lock is taken to prevent that
    race; abandoning cleanly when it's detected is the chosen alternative.
    """
    if not path.exists():
        return

    stat_before = path.stat()
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()
    if not lines:
        return

    ages = _dated_timestamps(lines, stat_before.st_mtime)
    cutoff = now_fn() - REQUEST_WINDOW_SECONDS
    keep = [age >= cutoff for age in ages]
    if all(keep):
        return

    kept_lines = [line for line, keep_it in zip(lines, keep) if keep_it]
    new_content = "".join(f"{line}\n" for line in kept_lines)

    try:
        stat_after = path.stat()
        if (
            stat_after.st_size != stat_before.st_size
            or stat_after.st_mtime != stat_before.st_mtime
        ):
            log.warning(
                "throttle: %s changed since it was read; abandoning compaction",
                path,
            )
            return
        path.write_text(new_content, encoding="utf-8")
    except OSError as exc:
        log.warning("throttle: could not rewrite %s: %s", path, exc)


async def enforce_throttle(
    state: ThrottleState,
    *,
    path: Path,
    max_requests_per_hour: int,
    max_inline_wait_seconds: float,
    activity_streak_limit_minutes: float,
    rest_duration_minutes: float,
    min_call_interval_seconds: float,
    now_fn=time.time,
    slots_needed: int = 1,
) -> ThrottleAbort | None:
    """Async boundary around evaluate(): rereads `path` and folds it into
    `state` (see load_throttle_state / _merge_loaded_state), performs the
    actual inline sleep (only ever a real sleep in production — now_fn is
    the injection point tests use instead), and turns a deferred verdict
    into a ThrottleAbort the guard (tools/helpers.py) raises as a
    ToolAbort.

    Rereading on every call — rather than hydrating `state` once and
    reusing it — is deliberate: it's the only way this process sees a
    request a concurrently-running process just wrote, and overlap between
    runs is exactly what cross-run persistence exists to handle. The cost
    is one small local file read ahead of every 104 request, negligible
    next to the network call itself.

    This function still never raises — that contract predates this
    revision and is explicitly preserved here: the one new dependency that
    CAN raise (load_throttle_state, on an existing-but-unreadable state
    file) is caught here and turned into a returned ThrottleAbort instead
    of being let through. Returns None to mean "proceed, already paced".

    `slots_needed` (default 1) is a caller-declared reservation for a
    multi-request tool call (guarded_sequence) — see evaluate()'s
    docstring for what it changes about the budget check. A `slots_needed`
    that could never fit in the window regardless of its current state
    (greater than `max_requests_per_hour`) is a caller configuration bug,
    not a throttling outcome — evaluate()'s Nth-oldest index would run
    negative trying to answer it, so it is refused HERE, before
    evaluate() is ever called, as a returned (never raised)
    ThrottleAbort(kind="internal_config") — this module writes no
    Agent-facing wording (see the module docstring); the guard turns
    "internal_config" into its own existing "this is a program bug,
    please report it" phrasing the same way it already does for every
    other internal_config abort.
    """
    if slots_needed > max_requests_per_hour:
        detail = (
            f"slots_needed={slots_needed} exceeds max_requests_per_hour="
            f"{max_requests_per_hour}"
        )
        return ThrottleAbort(kind="internal_config", payload=None, detail=detail)

    now = now_fn()
    try:
        loaded = load_throttle_state(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        detail = f"讀取節流狀態檔失敗：{path}（{exc}）"
        return ThrottleAbort(kind="internal_config", payload=None, detail=detail)

    _merge_loaded_state(state, loaded, now)

    decision = evaluate(
        state,
        now=now,
        max_requests_per_hour=max_requests_per_hour,
        max_inline_wait_seconds=max_inline_wait_seconds,
        activity_streak_limit_seconds=activity_streak_limit_minutes * 60,
        rest_duration_seconds=rest_duration_minutes * 60,
        min_call_interval_seconds=min_call_interval_seconds,
        slots_needed=slots_needed,
    )
    if not decision.proceed:
        log.warning(
            "throttle: deferring call, reason=%s wait_seconds=%.1f",
            decision.reason, decision.wait_seconds,
        )
        return ThrottleAbort(
            kind="throttled",
            payload=_retry_after_payload(decision.wait_seconds),
            detail="",
        )

    if decision.wait_seconds > 0:
        await _sleep(decision.wait_seconds)
    return None


def note_request(state: ThrottleState, *, path: Path, now_fn=time.time) -> None:
    """Count one API request toward the rolling-window volume cap, log the
    interval observed since the previous call on this session, and append
    the request's timestamp to the on-disk state file so it survives past
    this process.

    Called once per sub-request from `_issue_one` (tools/helpers.py) —
    the per-request unit shared by `guarded_api` (one request per tool
    call) and `guarded_sequence` (N requests per tool call) — regardless
    of whether that sub-request ultimately succeeds — this is the only
    place any request is ever counted, so without it the hourly window
    would count zero requests while 104 received every one the session
    actually made.

    The logged interval is this module's own self-check, at zero
    production cost: the interval floor's justification rests on the
    assumption that caller-driven call timing varies naturally (inference
    time varies with output length, and the Agent reads results before
    deciding what to call next). The case that would falsify it is
    parallel tool calls landing in one turn — the session lock serialises
    them, so they would arrive back-to-back and the logged interval would
    reflect the lock's queue rather than the caller's own rhythm. Nothing
    reacts to the logged value automatically; it exists to be read later.

    Called from `_issue_one`'s `finally`, so this never raises — an
    exception here would clobber the outcome of the request it's just
    finishing recording. That means an append failure can't be handled by
    aborting the call it belongs to (the request has already gone out by
    this point). It's also not handled by a bare warning-and-drop: reads
    are REPLACE semantics (load_throttle_state), so a timestamp that never
    reached the file would be erased on the very next read — an undercount,
    which this module refuses everywhere else. So a failed append instead
    goes into state.unpersisted_timestamps, which the next read unions back
    in (see _merge_loaded_state), and a warning is logged — not silence,
    but also not a lost count. A persistent failure (disk full, permission
    revoked) isn't swallowed forever by this path: it also breaks reads,
    and reads DO report — the next enforce_throttle call surfaces it as
    ThrottleAbort(kind="internal_config"). Only genuinely one-off append
    failures are absorbed here.
    """
    now = now_fn()
    if state.last_request_logged_at is not None:
        log.info(
            "throttle: observed inter-call interval=%.2fs",
            now - state.last_request_logged_at,
        )
    state.last_request_logged_at = now
    state.request_timestamps.append(now)
    try:
        with path.open("a", encoding="utf-8") as f:
            f.write(f"{now:.6f}\n")
    except OSError as exc:
        log.warning("throttle: could not append to state file %s: %s", path, exc)
        state.unpersisted_timestamps.append(now)
