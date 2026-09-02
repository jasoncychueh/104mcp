"""Certification-corpus-backed drop detection for `tools/filters.py`'s condition table.

Answers: given what was actually submitted on the wire for one search call and 104's own
echo (`searchForm`), which conditions did the server NOT apply — and, at import time,
derives `Condition.echo_evidence` for every filter-key row from the certification corpus
`research/probes/certify_conditions.py` produces, mutating `tools/filters.py`'s own
`CONDITIONS` dict in place.

**Split out of `tools/filters.py`.** `research/probes/certify_conditions.py` is the only
tool that can regenerate the corpus this module reads, and it imports `tools.filters` for
the condition table (`CONDITIONS[key].wire`, `encode_filters`, `_encode_work_exp_time`). A
corpus read at `tools.filters` import time would therefore make the one tool that could
fix a missing corpus unable to start while the corpus was missing — a self-inflicted
deadlock. `tools/filters.py` now has no corpus read and no raise: importing it alone
leaves every `CONDITIONS` row at its dataclass default (`echo_evidence="unmeasured"`).
Only importing THIS module runs `_derive_echo_evidence()` and raises if the corpus is
absent. `research/probes/certify_conditions.py` does not import this module, and must not
start doing so — `tests/test_filters.py` guards this by grepping the probe's own source
for this module's name.

**Binds `tools.filters.CONDITIONS` itself, never a copy or a snapshot.**
`_derive_echo_evidence()` below mutates it in place via `CONDITIONS[key] = replace(...)`;
`tests/test_filters.py` and `tests/test_search.py` both depend on that identity, patching
individual rows via `monkeypatch.setitem(filters_mod.CONDITIONS, ...)` — a `.copy()` here
would make those patches invisible to `detect_dropped`, since it would then be reading a
different dict than the one under test.

Deliberately does not: issue any HTTP request, touch the session/guard/throttle, or decide
what to do about a dropped condition once found — `tools/search.py` raises
`DroppedFiltersError` and turns it into the caller-facing error payload; this module only
reports the fact.
"""
from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import replace
from importlib.resources import files
from typing import Any

from mcp104.tools.filters import CONDITIONS, Condition, group_by_wire_param, _COMPOSITE_ENCODERS

# The two monthly-pay bound parameters are positional (see `filters._split_pay_bound`):
# the first value sent is the ten-thousands digit, the second the thousands digit. Every
# other multi-valued parameter compares as a set below (the server may reorder or
# de-duplicate without that being evidence of a drop), but set comparison is blind to a
# transposition on exactly these two — sent ["4","8"] (48,000) against an echo of
# ["8","4"] (84,000) would set-compare equal, silently confirming a different salary than
# the one requested. That is precisely the failure `_split_pay_bound` exists to prevent,
# so the comparison protecting it cannot itself be order-blind.
_ORDERED_ECHO_PARAMS = frozenset({"expectPayMonthMin", "expectPayMonthMax"})


def _to_string_set(value: Any) -> set[str]:
    """Normalise one side of a comparison (a sent value, or an echoed one) to a set of
    strings. Splits every string on comma, unconditionally: this recovers per-code
    granularity from both wire representations of a multi-value condition (a
    comma-joined single string, or several repeated-parameter tuples already split into
    a list) without needing to know which encoding family produced the value. Applying
    the same split to both the sent side and the echoed side of a genuine free-text value
    (e.g. `schoolKeyword`) is safe: if the two strings are equal, their splits are equal
    too, and set comparison already discards order and duplicate count — 104's echo has
    been observed to reorder and de-duplicate, and neither is evidence of a drop.

    Not used for `_ORDERED_ECHO_PARAMS` — see `_to_string_sequence`.
    """
    if value is None:
        return set()
    items = value if isinstance(value, (list, tuple, set)) else [value]
    out: set[str] = set()
    for item in items:
        for piece in str(item).split(","):
            piece = piece.strip()
            if piece:
                out.add(piece)
    return out


def _to_string_sequence(value: Any) -> list[str]:
    """Like `_to_string_set`, but order- and count-preserving, for `_ORDERED_ECHO_PARAMS`
    — the one pair of parameters whose position carries meaning, where a transposed or
    de-duplicated echo must NOT compare equal to what was sent. A bare `set` is
    deliberately not accepted as a multi-value input here (unlike `_to_string_set`): a
    set has no defined order, so treating one as an ordered sequence would silently
    assign it a meaningless one.
    """
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else [value]
    out: list[str] = []
    for item in items:
        for piece in str(item).split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def detect_dropped(sent: Sequence[tuple[str, str]], search_form: dict) -> list[str]:
    """Compare what was sent against 104's own echo, condition by condition, and report
    which conditions the server did not apply. Pure.

    **`sent` is the full parameter list actually submitted for one call, not just the
    `filters`-derived subset `encode_filters` returns.** Concretely:
    `[("kws", keyword), *encode_filters(filters), ("page", str(page))]`. The keyword is
    drop-checked like any other parameter: its echo evidence is `"echoed"`, the server
    does echo it, and it is the parameter that determines the entire result set — a
    dropped or misnamed keyword would return the whole unfiltered population in the
    documented success shape, with nothing in the response showing a keyword was ever
    sent, which is the least acceptable parameter to leave uninspected. Passing only
    `encode_filters`'s return value here silently exempts the keyword from every check
    this function performs. The invariant this restores: every parameter the tool
    submits is either compared, or carries a table entry stating why it is not — `page`
    is the only current exclusion (`CONDITIONS["page"].echo_evidence != "echoed"`), and
    it is excluded by that entry rather than by never reaching this function at all.

    Whether a condition is compared at all is `CONDITIONS[key].echo_evidence ==
    "echoed"` — a table lookup, never a runtime null test on the echo. The ambiguity
    that rules this out is measured, not hypothetical: `autobiography=1` was accepted,
    echoed null, and left the total completely unchanged — discarded in silence as an
    unrecognised parameter *name*, with nothing distinguishing that outcome at runtime
    from a condition that applied correctly without being echoed. A null echo therefore
    cannot decide, by itself, whether a condition belongs in this comparison; that
    property is recorded per row instead (`"echoed"` / `"not-echoed"` / `"unmeasured"`
    — see `filters.Condition.echo_evidence`'s own docstring, and `_derive_echo_evidence`
    below for where the value actually comes from). `"not-echoed"` and `"unmeasured"`
    are both excluded here, deliberately not merged into one "skip" state despite
    behaving identically in this function today — one is a finding, the other a gap in
    the evidence, and only the code that reads `Condition.echo_evidence` keeps that
    distinction visible to a later reader; collapsing it here would erase it silently.

    A composite is reported dropped if **any** of its echoed sub-parameters mismatches;
    it is never partially dropped in the return value, since `filters` only ever
    supplies or omits a composite as a whole. Every sub-parameter compares as a set
    (`_to_string_set`) **except** the two named in `_ORDERED_ECHO_PARAMS`, which compare
    as an ordered sequence (`_to_string_sequence`) instead — see that constant's
    comment.
    """
    sent_by_param = group_by_wire_param(sent)

    dropped: set[str] = set()
    for condition in CONDITIONS.values():
        if condition.echo_evidence != "echoed":
            continue
        for param in condition.wire:
            if param not in sent_by_param:
                continue  # this sub-parameter was not sent — nothing to compare
            echoed_value = search_form.get(param)
            if param in _ORDERED_ECHO_PARAMS:
                mismatch = _to_string_sequence(sent_by_param[param]) != _to_string_sequence(
                    echoed_value
                )
            else:
                mismatch = _to_string_set(sent_by_param[param]) != _to_string_set(echoed_value)
            if mismatch:
                dropped.add(condition.key)
                break
    return sorted(dropped)


# ── Echo evidence, derived from the certification corpus ────────────────────────────
#
# research/probes/certify_conditions.py's output — the record of what was ACTUALLY
# submitted and echoed for each shipped condition, one baseline+treatment run per
# filter-key row. Tracked in git (unlike research/captures/, which holds real
# candidates' data): this file carries only totals, ratios and our own filter inputs,
# no personal data. Packaged as `src/mcp104/assets/certification/…` — see
# `[tool.setuptools.package-data]` in pyproject.toml — so `files("mcp104")` addresses it
# through the installed package, exactly like the category datasets `tools/categories.py`
# reads.
CORPUS_PATH = files("mcp104") / "assets" / "certification" / "certify_conditions_results.json"


def _load_corpus() -> dict:
    """The certification corpus. **Raises if the file is absent — deliberately.**

    This previously returned `{}` on absence, reasoning that every row already
    defaults to `unmeasured` so a missing file merely leaves them all there. That
    reasoning collapses two different states into one, which is the same error this
    column was rewritten to fix: `unmeasured` means *this row has no observation*,
    and a missing corpus means *no observations exist at all*. They are not the same
    claim, and only the first is something `echo_evidence` can honestly express.

    The consequence of the old behaviour was not a degraded check but **no check**:
    `detect_dropped` compares only rows whose evidence is `echoed`, so with every row
    at `unmeasured` it returns "nothing dropped" for a response in which the server
    dropped everything. Every filtered search would report success while returning a
    broader result set than was asked for — the precise failure this feature is
    organised against, arrived at through the mechanism built to prevent it.

    The corpus is a committed, version-controlled input, not runtime data, so its
    absence means a broken build rather than an unlucky deployment. `load_dataset`
    already treats its own bundled files exactly this way and for the same reason:
    there is no recovery path, because everything downstream is unusable without it.
    A server that cannot honour its contract should fail to start rather than start
    and serve searches that look correct — the alternative costs one line of a startup
    log and saves a class of silently-broadened results.
    """
    if not CORPUS_PATH.is_file():
        raise FileNotFoundError(
            f"certification corpus missing: {CORPUS_PATH}.\n"
            f"Drop detection cannot run without it, and starting without drop "
            f"detection would serve broader result sets than callers asked for "
            f"while reporting success — so this is a hard failure rather than a "
            f"degraded mode.\n"
            f"Likely cause: a container build whose Dockerfile COPY allowlist, or "
            f"pyproject.toml's [tool.setuptools.package-data] pattern, omitted "
            f"src/mcp104/assets/certification/ — the corpus is committed and must "
            f"reach the installed package.\n"
            f"If you deleted it deliberately to force a clean re-run of "
            f"research/probes/certify_conditions.py, restore it (`git checkout` it) "
            f"and delete individual condition entries instead, which is what the "
            f"probe's resumability is for — that probe does not import this module, "
            f"so it is free to run against the file you just restored."
        )
    return json.loads(CORPUS_PATH.read_text(encoding="utf-8"))


# The two rows whose corpus entry doesn't share the general {wire_param: value} echo
# shape — `research/probes/certify_conditions.py`'s null-echo-specific test recorded a
# single `echoed_value` for `military`/`workInterval` instead, because that test
# existed specifically to check one thing: whether a fixed null echo could be
# reproduced. (It could not: the corpus's `echoed_value` for both is populated, not
# null — see their own rows' `certifying_total` for the full account of what that
# supersedes.)
_BESPOKE_ECHO_FIELD = "echoed_value"


def _echo_evidence_for_simple(condition: Condition, entry: dict) -> str:
    """A simple (non-composite) row has exactly one wire parameter, so its echo
    evidence is a direct read of the corpus entry — no re-encoding needed to know
    what was sent, because a simple condition's presence in `filters_sent` already
    means its one wire parameter was sent.
    """
    if _BESPOKE_ECHO_FIELD in entry:
        value = entry[_BESPOKE_ECHO_FIELD]
    elif "echoed" in entry:
        value = entry["echoed"].get(condition.wire[0])
    else:
        return "unmeasured"
    return "not-echoed" if value is None else "echoed"


def _echo_evidence_for_composite(condition: Condition, entry: dict) -> str:
    """A composite's corpus entry records the echo for every sub-parameter the row
    DECLARES (`Condition.wire`), not only the ones that particular run actually sent —
    e.g. `language_skills` declares three ability slots but a one-slot run only sends
    one. Re-running that composite's own encoder (`filters._COMPOSITE_ENCODERS`,
    imported above) against the corpus's own `filters_sent` recovers exactly which
    sub-parameters were sent THIS run, via `group_by_wire_param` — the same canonical-
    name rule `detect_dropped` itself uses — so only those are checked; a sub-parameter
    the run never sent echoing null is not a finding, it is simply unsent.
    """
    if "echoed" not in entry or "filters_sent" not in entry:
        return "unmeasured"
    encoder = _COMPOSITE_ENCODERS[condition.key]
    sent_pairs = encoder(entry["filters_sent"][condition.key])
    sent_params = group_by_wire_param(sent_pairs)
    echoed = entry["echoed"]
    if any(echoed.get(wire_param) is None for wire_param in sent_params):
        return "not-echoed"
    return "echoed"


# `agemin`/`agemax` are the one pair certified as a single joint run (see the
# condition table's own comment on why they are two rows for one condition) — the
# corpus key for that run is "agemin+agemax", not either row's own key, so each of
# the two rows is pointed at that shared entry explicitly rather than silently
# reading as unmeasured for lack of an exact key match.
_CORPUS_KEY_ALIASES = {"agemin": "agemin+agemax", "agemax": "agemin+agemax"}


def _derive_echo_evidence() -> None:
    """Patch every `filters.CONDITIONS` row's `echo_evidence` from the certification
    corpus, replacing the dataclass default (`"unmeasured"`) wherever the corpus
    records an observation. Runs once, at THIS module's import time — never at
    `tools.filters` import time, which is the entire point of the split (see this
    module's own docstring).

    Mutates `CONDITIONS` — imported from `tools.filters`, not copied — in place via
    `CONDITIONS[key] = replace(condition, ...)`, so every other reference to that same
    dict object (including `tools.filters.CONDITIONS` itself, and `tools/search.py`'s
    `filters_mod.CONDITIONS`) observes the patched values too.

    Only `provenance == "filter-key"` rows are touched — `kws` and `page` are not
    conditions `research/probes/certify_conditions.py` tests at all (they are
    top-level tool arguments, not `filters` dict keys), so they keep the
    `echo_evidence` their own `_row(...)` call already set explicitly, from their own
    separately-cited measurements, rather than being silently reset to "unmeasured"
    for not appearing in a corpus that was never meant to cover them.
    """
    corpus = _load_corpus()
    for key, condition in list(CONDITIONS.items()):
        if condition.provenance != "filter-key":
            continue
        corpus_key = _CORPUS_KEY_ALIASES.get(key, key)
        entry = corpus.get(corpus_key)
        if entry is None:
            continue  # dataclass default ("unmeasured") already covers this
        if condition.encoding == "composite":
            evidence = _echo_evidence_for_composite(condition, entry)
        else:
            evidence = _echo_evidence_for_simple(condition, entry)
        CONDITIONS[key] = replace(condition, echo_evidence=evidence)


_derive_echo_evidence()
