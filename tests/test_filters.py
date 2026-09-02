"""Tests for tools/filters.py's condition table, wire encoding, pre-request
validation and drop detection.

Written blind to tools/filters.py's implementation (spec-driven-development
Mode 1/2): every expected wire form below comes from
docs/104-site-facts.md §6b.3g's measured request/echo pairs; every composite
wire structure and every composite **caller-facing** dict shape comes from
design.md's Data Models — Condition table (the "Caller-facing value shapes"
table pins `work_exp_time`, `expect_pay` and `language_skills` explicitly,
as of the round-2 design revision — an earlier draft of this file inferred
those shapes and got `expect_pay`'s field names wrong; see this session's
report).

`expect_pay` and `work_exp_time` deliberately share the caller-facing key
`mode` (both are "a range with a mode") while still not sharing a *wire*
encoding — design.md calls this out explicitly because it looks like a
contradiction and is not. `test_encode_filters_range_composites_do_not_share_an_encoding`
below asserts the wire asymmetry; nothing about the caller-facing `mode`
key is shared code to that assertion.

`detect_dropped`'s `sent` parameter is `encode_filters`'s return value
**verbatim** (`list[tuple[str, str]]`, pinned in Components §5) — the tests
below compose the two directly rather than hand-writing wire tuples, both
because that is what a real caller does and because it means this file
never has to independently know the wire spelling of every key used here.
"""
from __future__ import annotations

import ast
import dataclasses
import json
import subprocess
import sys
from pathlib import Path

import pytest

import mcp104.tools.categories as categories
import mcp104.tools.filters as filters_mod
from mcp104.tools.drop_detection import detect_dropped
from mcp104.tools.filters import encode_filters, validate_filters

REPO_ROOT = Path(__file__).resolve().parent.parent
# Source-tree arithmetic, not `importlib.resources.files("mcp104")`: this file RENAMES
# the corpus aside and restores it (see test_missing_corpus_fails_drop_detection_import_
# but_not_filters_import and the corpus-perturbation tests below), which must operate on
# the tracked file under version control -- on an editable install the two happen to be
# the same physical file, but this test does not rely on that coincidence.
CORPUS_PATH = (
    REPO_ROOT / "src" / "mcp104" / "assets" / "certification" / "certify_conditions_results.json"
)

# The three literal values design.md's Data Models — Condition table pins for the
# "echo evidence" column. The Python attribute NAME holding one of these on a
# `Condition` is NOT pinned by design.md (only the column's conceptual name and its
# three values are), so the helpers below locate it by VALUE rather than assuming an
# identifier -- the same shape-hedge discipline tests/test_categories.py already
# applies to `Resolution`'s field names.
_ECHO_EVIDENCE_VALUES = ("echoed", "not-echoed", "unmeasured")


def _echo_evidence_field_name(condition) -> str:
    if dataclasses.is_dataclass(condition):
        pairs = [(f.name, getattr(condition, f.name)) for f in dataclasses.fields(condition)]
    elif hasattr(condition, "_asdict"):
        pairs = list(condition._asdict().items())
    else:
        pairs = list(vars(condition).items())
    # Identify by the two literals UNIQUE to this axis. "unmeasured" is shared with
    # `domain_completeness` (2026-08-15: `language_skills` became the first row to hold
    # it there), so a lookup over all three values matches two fields and the
    # exactly-one assertion fails on a row that is perfectly well-formed. The hedge is
    # preserved -- still no identifier is assumed -- but it now keys on the part of the
    # vocabulary that actually distinguishes the axis.
    discriminating = ("echoed", "not-echoed")
    matches = [name for name, value in pairs if value in discriminating]
    if not matches:
        # This row happens to sit at "unmeasured", which names no field on its own.
        # Fall back to the field that holds an echo literal on some OTHER row.
        for other in filters_mod.CONDITIONS.values():
            other_pairs = [(f.name, getattr(other, f.name))
                           for f in dataclasses.fields(other)]
            found = [n for n, v in other_pairs if v in discriminating]
            if len(set(found)) == 1:
                return found[0]
        raise AssertionError(
            f"no row in filters_mod.CONDITIONS holds {discriminating!r}, so the echo-evidence "
            f"field cannot be located by value at all"
        )
    assert len(set(matches)) == 1, (
        f"expected exactly one field holding an echo-evidence literal "
        f"{discriminating!r} on {condition!r}, found {matches!r}"
    )
    return matches[0]


def _echo_evidence(condition) -> str:
    return getattr(condition, _echo_evidence_field_name(condition))


def _replace_echo_evidence(condition, new_value: str):
    """A clone of `condition` with its echo-evidence field set to `new_value`,
    everything else unchanged. Used to construct synthetic not-echoed/unmeasured
    conditions without needing a real corpus example of either -- see
    test_detect_dropped_excludes_unmeasured_exactly_like_not_echoed below for why
    that matters this round."""
    field_name = _echo_evidence_field_name(condition)
    if dataclasses.is_dataclass(condition):
        return dataclasses.replace(condition, **{field_name: new_value})
    if hasattr(condition, "_replace"):
        return condition._replace(**{field_name: new_value})
    raise TypeError(f"don't know how to clone-and-replace a {type(condition)!r}")


# ── T-74 (R9.1): a name supplied for workExpJob (經歷職務, work experience) resolves
# against the SAME dataset jobcat (希望職類, expectation) resolves against, and
# submits the resolved code under its OWN wire parameter ────────────────────────────
#
# design.md: "workExpJob is backed by JobCat.json... jobcat and workExpJob share a
# code space and ask different questions." Sharing a code space means a name common
# to both must resolve to the IDENTICAL code either way (same dataset file, same
# tree); asking different questions means that code must land on the WIRE PARAMETER
# the caller actually named, never silently on `jobcat`'s. A leaf node is used
# (rather than a branch) so this case is about routing alone, independent of T-75's
# branch-acceptance concern — discovered structurally (`next(... if not children)`,
# the same idiom test_search.py's T-58 already uses for JobCat.json), not hard-coded
# to a specific node name.

def test_workexpjob_resolves_against_jobcats_dataset_under_its_own_wire_key():
    jobcat_dataset = categories.load_dataset("JobCat.json", "jobcat")
    workexpjob_dataset = categories.load_dataset("JobCat.json", "workExpJob")
    leaf = next(n for n in jobcat_dataset.nodes if not n.children)

    jobcat_resolution = categories.resolve(jobcat_dataset, leaf.name)
    workexpjob_resolution = categories.resolve(workexpjob_dataset, leaf.name)

    assert jobcat_resolution.status == "resolved"
    assert workexpjob_resolution.status == "resolved"
    assert workexpjob_resolution.code == jobcat_resolution.code == leaf.code, (
        "the same name must resolve to the identical code under either condition -- "
        "they share one dataset file"
    )

    params = encode_filters({"workExpJob": leaf.name})
    assert params == [("workExpJob", workexpjob_resolution.code)], (
        f"workExpJob's resolved code must be submitted under its OWN wire "
        f"parameter, never silently under jobcat's; got {params}"
    )


# jobcat is a `dataset`-sourced condition — Data Models says plainly "the
# caller passes a **name**, resolved per §6", never a raw code — so every
# jobcat value below is a real JobCat.json leaf name, confirmed unique and
# resolvable to the code shown as a comment (see this session's report:
# passing a raw code here was this test file's own first-draft mistake,
# caught by re-reading the Condition table, not by reading tools/filters.py).

# ── T-2 (R1.2): only what the caller supplied is emitted, no server default ──

def test_encode_filters_emits_only_supplied_conditions():
    params = encode_filters({"sex": "1", "jobcat": ["機械裝配員"]})  # -> 2010001023
    keys = {k for k, _ in params}
    assert keys == {"sex", "jobcat"}
    assert ("sex", "1") in params
    assert ("jobcat", "2010001023") in params


def test_encode_filters_empty_filters_emits_nothing():
    assert encode_filters({}) == []


# ── T-47 (encode_filters, interface): one example per encoding family ──
# §6b.3g's "全部 33 條件的線上編碼實測結果" table records these wire forms.

def test_encode_filters_comma_joined_family():
    # jobcat=2010001023,2010001010 (comma-joined multi-value), §6b.3g.
    params = encode_filters({"jobcat": ["機械裝配員", "機械加工技術人員"]})
    assert params == [("jobcat", "2010001023,2010001010")]


def test_encode_filters_scalar_family():
    # sex=1, single unbracketed value, §6b.3g.
    params = encode_filters({"sex": "1"})
    assert params == [("sex", "1")]


def test_encode_filters_repeated_parameter_family():
    # edu[]=4&edu[]=8 (repeated, array-suffixed on the wire; the array
    # suffix is not part of the key itself), §6b.3g. Order preserved.
    params = encode_filters({"edu": ["4", "8"]})
    assert params == [("edu[]", "4"), ("edu[]", "8")]


# ── T-32 (R8.3) / T-47: the two range composites do not share an encoding ──

def test_encode_filters_range_composites_do_not_share_an_encoding():
    # work_exp_time's bounds are single unbracketed values (workExpTimeMin,
    # no brackets) -- §6b.3g: "注意年資的 Min/Max 不帶 []（單值）".
    work_exp_params = encode_filters({"work_exp_time": {"mode": "down", "min": 5}})
    assert ("workExpTimeType", "down") in work_exp_params
    assert ("workExpTimeMin", "5") in work_exp_params
    assert not any(k == "workExpTimeMin[]" for k, _ in work_exp_params)

    # expect_pay's bound is two REPEATED bracketed parameters whose
    # POSITION carries meaning -- ten-thousands digit, then thousands
    # digit. 48,000 = 4萬8千 is the exact figure §6b.3g measured from a
    # real human-recorded form submission. Caller-facing shape per Data
    # Models — Condition table: {"types": [...], "month": {"mode", "min",
    # "max"}}.
    pay_params = encode_filters(
        {"expect_pay": {"types": ["1"], "month": {"mode": "down", "min": 48000}}}
    )
    assert ("expectPayType[]", "1") in pay_params
    assert ("expectPayMonthType", "down") in pay_params
    bracketed_min = [v for k, v in pay_params if k == "expectPayMonthMin[]"]
    assert bracketed_min == ["4", "8"]  # 萬 digit first, then 千 digit
    assert not any(k == "expectPayMonthMin" for k, _ in pay_params)


# ── T-30 (R8.1): a composite missing a required companion is rejected ──

def test_validate_filters_composite_missing_companion_rejected_before_request():
    # work_exp_time's "down"/"up" modes are meaningless without a bound --
    # `min` is a required companion, not an independent parameter (Data
    # Models — Condition table, "Row rule").
    with pytest.raises(Exception):
        validate_filters({"work_exp_time": {"mode": "down"}})


# ── T-31 (R8.2): a fourth language slot is rejected before any request ──

def test_validate_filters_rejects_a_fourth_language_slot():
    # The server's own returned parameter vocabulary carries
    # languageAbility1..3 and no languageAbility4 (§6b.3g, 2026-08-14
    # human-recorded submission) -- a fourth slot's ability values would
    # silently vanish server-side while language[] still carried four
    # values, producing a search broader than requested with a
    # correct-looking echo. Rejected locally instead. Caller-facing shape
    # per Data Models — Condition table: {"languages": [{"language": code,
    # "abilities": [a, a, a, a]}, ...], "fulfills_all": bool}.
    four_slots = {
        "languages": [
            {"language": "1", "abilities": [2, 2, 2, 2]},
            {"language": "2", "abilities": [8, 8, 8, 8]},
            {"language": "3", "abilities": [2, 2, 2, 2]},
            {"language": "4", "abilities": [2, 2, 2, 2]},
        ],
    }
    with pytest.raises(Exception):
        validate_filters({"language_skills": four_slots})


# ── T-48 (validate_filters, interface): unknown key / composite / slot ──

def test_validate_filters_rejects_unknown_key():
    with pytest.raises(Exception):
        validate_filters({"not_a_real_condition_key": "1"})


def test_validate_filters_rejects_composite_companion_violation():
    # expect_pay's month.mode="down" requires a `min`; omitted here.
    with pytest.raises(Exception):
        validate_filters({"expect_pay": {"types": ["1"], "month": {"mode": "down"}}})


def test_validate_filters_rejects_slot_count_violation():
    four_slots = {
        "languages": [
            {"language": str(i), "abilities": [2, 2, 2, 2]}
            for i in range(1, 5)
        ]
    }
    with pytest.raises(Exception):
        validate_filters({"language_skills": four_slots})


# ── T-26 (R7.1): a compared condition whose echo matches passes ──

def test_detect_dropped_matching_echo_is_not_reported_dropped():
    sent = encode_filters({"jobcat": ["機械裝配員"]})  # -> [("jobcat", "2010001023")]
    search_form = {"jobcat": ["2010001023"]}
    assert detect_dropped(sent, search_form) == []


# ── T-27 (R7.2): table lookup, never a runtime null test ──
#
# Re-grounded (Round I7). The original form hard-coded `military` as its
# measured-not-echoed example. This round's corpus overturned that premise:
# military[]=5 now echoes ["5"], so it derives as `echoed`, and `detect_dropped`
# correctly reports a drop when fed a null echo for it -- the test failed because
# its EXAMPLE was refuted, not because the rule it protects regressed. Drop-checking
# is still decided by TABLE LOOKUP, never by inspecting whether the runtime echo
# happens to be null, and that is still true and still worth protecting.
#
# The measured case that carries the rule now is docs/104-site-facts.md §6b.3g's 自傳
# row: `autobiography=1` (the WRONG parameter name — the correct one is `auto`) is
# silently discarded by 104 — total identical to the baseline, echo null, no error.
# That is indistinguishable at runtime from a condition that applies correctly while
# echoing nothing, which is exactly why table lookup cannot be replaced by a null
# check. `autobiography` cannot drive this test directly, though: it is not a
# CONDITIONS entry (it is the discarded WRONG name — `auto`'s own table row carries
# this exact citation as its rationale, not a key `detect_dropped` can be handed).
#
# The rule must hold even with ZERO real not-echoed filter-key conditions on the
# shipped surface — "a table entry fails loudly when a row is missing; a runtime
# null test adapts silently to new data" (design.md, same section) is the whole
# argument, and it does not get weaker just because today's corpus happens to leave
# `not-echoed` empty. So this DISCOVERS a real not-echoed filter-key condition if one
# currently exists (reusing its own corpus-recorded `filters_sent` value, valid for
# whatever its value domain is), and falls back to a synthetic clone of a real,
# currently-echoed condition if none does — the same technique T-73's exclusion half
# already uses — rather than hard-coding a name a future re-measurement can strand
# again.

def _real_not_echoed_filter_key_condition():
    return next(
        (
            key for key, condition in filters_mod.CONDITIONS.items()
            if condition.provenance == "filter-key" and _echo_evidence(condition) == "not-echoed"
        ),
        None,
    )


def test_detect_dropped_uses_table_lookup_not_runtime_null_test(monkeypatch):
    excluded_key = _real_not_echoed_filter_key_condition()
    if excluded_key is not None:
        corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))
        excluded_value = corpus.get(excluded_key, {}).get("filters_sent", {}).get(excluded_key)
        assert excluded_value is not None, (
            f"found {excluded_key!r} derived not-echoed, but its own corpus entry "
            f"carries no filters_sent value to reuse as a valid submission"
        )
    else:
        # No filter-key condition is currently derived as not-echoed -- true as of
        # this round's corpus (contactPrivacy/workExpJob are `unmeasured`; every
        # other filter-key row measured so far is `echoed`). The rule under test
        # does not depend on a real example existing, so a synthetic clone of a
        # real, currently-echoed condition drives it instead.
        excluded_key = "sex"
        excluded_value = "1"
        base_condition = filters_mod.CONDITIONS[excluded_key]
        assert _echo_evidence(base_condition) == "echoed", "sanity: base condition must start out echoed"
        monkeypatch.setitem(
            filters_mod.CONDITIONS, excluded_key, _replace_echo_evidence(base_condition, "not-echoed")
        )

    # A DIFFERENT, genuinely echoed condition proves the detector is not
    # blanket-suppressing every null echo -- it must still catch a real drop.
    caught_key = "photo" if excluded_key != "photo" else "disability"
    assert _echo_evidence(filters_mod.CONDITIONS[caught_key]) == "echoed", (
        f"sanity: {caught_key!r} must be genuinely echoed, distinct from "
        f"{excluded_key!r}, for this contrast to mean anything"
    )

    sent = encode_filters({excluded_key: excluded_value, caught_key: "1"})
    search_form = {excluded_key: None, caught_key: None}
    assert detect_dropped(sent, search_form) == [caught_key], (
        f"a condition recorded not-echoed ({excluded_key!r}) must be excluded from "
        f"drop comparison by table lookup regardless of its null runtime echo, "
        f"while a genuinely echoed condition ({caught_key!r}) with a null echo "
        f"must still be caught"
    )


# ── T-49 (detect_dropped, interface): set comparison over string lists ──

def test_detect_dropped_reordered_and_deduplicated_echo_compares_equal():
    # edu[]=4&edu[]=8 -> searchForm echoes `edu` (normalised name, array
    # suffix dropped). Element order is measured once and duplicates carry
    # no known meaning, so a server that reorders or de-duplicates its
    # echo must not look like a dropper.
    sent = encode_filters({"edu": ["4", "8"]})
    search_form = {"edu": ["8", "4", "4"]}
    assert detect_dropped(sent, search_form) == []


def test_detect_dropped_scalar_sent_against_array_echo_compares_equal():
    # nationality is `dataset`-sourced (like jobcat, the caller passes a
    # name) and sent as a single scalar (no []), but its searchForm echo
    # comes back as a one-element array -- measured 2026-08-14, §6b.3g's
    # "國籍" section: "送出是單一純量、不帶 []；searchForm 回吐為陣列"
    # (泰國/Thailand -> 7001010000). Comparing scalar-to-scalar would
    # wrongly flag this correctly-applied filter as dropped and refuse the
    # search.
    sent = encode_filters({"nationality": "泰國"})  # -> [("nationality", "7001010000")]
    search_form = {"nationality": ["7001010000"]}
    assert detect_dropped(sent, search_form) == []


def test_detect_dropped_branch_code_echoed_verbatim_compares_equal():
    # jobcat accepts a branch code and echoes it verbatim rather than
    # expanding it into its leaf codes -- measured 2026-08-14, §6b.3g:
    # sending jobcat=2010001000 (操作／技術類人員, a 40-leaf branch) echoes
    # back ["2010001000"], not the 40 leaves. Set-equality must see this as
    # a match; expansion-based comparison would misreport a correctly
    # applied branch filter as dropped.
    sent = encode_filters({"jobcat": ["操作／技術類人員"]})  # -> [("jobcat", "2010001000")]
    search_form = {"jobcat": ["2010001000"]}
    assert detect_dropped(sent, search_form) == []


# ── T-72 (R7.6, interface): echo evidence is DERIVED from the certification corpus,
# and a condition with no echo observation reads `unmeasured` ──────────────────────
#
# `echoed: bool` could not carry contactPrivacy's real state -- its certification
# went through a bespoke row-level path that recorded returned rows and privacy
# flags and never captured an echo at all, so `True` and `False` would each assert a
# measurement nobody took. The fix has two parts, and the derivation half is the one
# that needs care: an assertion that the column's values merely AGREE with the
# corpus is satisfied by a hand-typed table that happens to match today, exactly the
# gap T-67 closed for the generated tool description earlier this round. So the
# check below does not compare CONDITIONS against a copy of the corpus read here --
# it PERTURBS the on-disk corpus file design.md names as authoritative and shows the
# derived value follows, in a subprocess (never a reload-in-place: `mcp104.tools.filters`
# is already imported by other test modules in this same session, and mutating its
# module-level CONDITIONS dict in place would leak the perturbation into every other
# test that reads it afterward).

def _condition_echo_evidence_via_fresh_import(key: str) -> str:
    """Import mcp104.tools.drop_detection FRESH in an isolated subprocess (so any
    import-time derivation from CORPUS_PATH runs against whatever that file currently
    holds on disk) and return the one echo-evidence-shaped value found on
    mcp104.tools.filters.CONDITIONS[key] -- derivation happens on drop_detection
    import, not filters import (filters.py has no corpus read of its own), and
    drop_detection binds filters.CONDITIONS itself rather than a copy, so reading it
    off either module name afterward sees the same patched dict."""
    script = (
        "import json\n"
        "import mcp104.tools.drop_detection\n"
        "import mcp104.tools.filters as filters_mod\n"
        f"condition = filters_mod.CONDITIONS[{key!r}]\n"
        "import dataclasses\n"
        "if dataclasses.is_dataclass(condition):\n"
        "    pairs = [(f.name, getattr(condition, f.name)) for f in dataclasses.fields(condition)]\n"
        "elif hasattr(condition, '_asdict'):\n"
        "    pairs = list(condition._asdict().items())\n"
        "else:\n"
        "    pairs = list(vars(condition).items())\n"
        f"matches = [v for _n, v in pairs if v in {_ECHO_EVIDENCE_VALUES!r}]\n"
        "print(matches[0] if len(matches) == 1 else 'AMBIGUOUS:' + repr(matches))\n"
    )
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True,
        encoding="utf-8", errors="replace", timeout=60,
    )
    assert result.returncode == 0, (
        f"subprocess import of mcp104.tools.drop_detection failed (stderr below) -- "
        f"if this is the ONLY thing failing in this test, it likely means "
        f"echo_evidence has not landed yet:\n{result.stderr}"
    )
    output = result.stdout.strip()
    assert not output.startswith("AMBIGUOUS:"), (
        f"CONDITIONS[{key!r}] has more than one field matching an echo-evidence "
        f"literal: {output}"
    )
    return output


def test_echo_evidence_is_derived_from_the_corpus_not_hand_maintained():
    original_bytes = CORPUS_PATH.read_bytes()
    try:
        corpus = json.loads(original_bytes.decode("utf-8"))
        assert "echoed" in corpus.get("jobcat", {}), (
            "sanity: jobcat must be a measured, echoed baseline in the real corpus "
            "for removing its entry to test anything"
        )

        before = _condition_echo_evidence_via_fresh_import("jobcat")
        assert before == "echoed", f"sanity: jobcat must start out 'echoed', got {before!r}"

        perturbed = dict(corpus)
        del perturbed["jobcat"]  # simulate "no echo observation exists for this row"
        CORPUS_PATH.write_bytes(json.dumps(perturbed, ensure_ascii=False).encode("utf-8"))

        after = _condition_echo_evidence_via_fresh_import("jobcat")
    finally:
        CORPUS_PATH.write_bytes(original_bytes)

    assert CORPUS_PATH.read_bytes() == original_bytes, "the corpus file must be restored byte-for-byte"
    assert before != after, (
        "removing jobcat's corpus entry did not change its derived echo evidence -- "
        "the column does not appear to be actually derived from the corpus at read "
        "time (it may be hand-typed, or cached from a stale import)"
    )
    assert after == "unmeasured", (
        f"a condition with no echo observation in the corpus must derive to "
        f"'unmeasured', not either measured outcome; got {after!r}"
    )


def test_condition_with_no_corpus_echo_observation_reads_unmeasured():
    # The default half. Round I8 finding: the first form of this test asserted
    # `no_observation` was non-empty -- a POPULATION assertion in disguise ("at
    # least one unmeasured condition must exist"), the exact shape last round's own
    # brief warned against. contactPrivacy and workExpJob were the instances when
    # this case was written; both have SINCE been measured (targeted follow-up
    # requests, recorded in the corpus's own `provenance_note` fields) and the
    # sanity check went red -- not because the default stopped working, but because
    # measurement coverage IMPROVED and closed the last real gap. A test for a
    # DEFAULT must not require an unclaimed row to exist to prove the default fires.
    #
    # So: discover a real instance structurally and check it directly when one
    # exists (still the strongest form -- no perturbation needed); when none does,
    # that is a GOOD state, not a coverage gap, and the mechanism is proven
    # SYNTHETICALLY instead of skipped -- reusing the same corpus-perturbation
    # technique test_echo_evidence_is_derived_from_the_corpus_not_hand_maintained
    # above already established (delete a real, currently-measured condition's
    # corpus entry, in an isolated subprocess, and require the freshly-derived
    # value to be 'unmeasured'). Either branch proves the same property; which one
    # runs depends on the corpus's current shape, not on this test's premise.
    corpus = json.loads(CORPUS_PATH.read_text(encoding="utf-8"))

    # agemin/agemax are recorded under one COMBINED "agemin+agemax" corpus entry
    # (both bounds sent and echoed together in the one certifying request), while
    # CONDITIONS carries them as two separate rows -- a key-granularity mismatch
    # this discovery step cannot resolve without re-deriving the very mapping under
    # test, so both are excluded rather than risked in either direction.
    age_keys = {"agemin", "agemax"}

    filter_key_conditions = {
        key: condition for key, condition in filters_mod.CONDITIONS.items()
        if condition.provenance == "filter-key" and key not in age_keys
    }

    # Corpus entries do not all share the same key set (contactPrivacy/workExpJob
    # now also carry `provenance_note`, `row_level_confirmation`, etc.) -- "no echo
    # observation" is specifically the absence of `echoed`/`echoed_value`, checked
    # by key membership, never by assuming a fixed entry shape.
    no_observation = [
        key for key in filter_key_conditions
        if "echoed" not in corpus.get(key, {}) and "echoed_value" not in corpus.get(key, {})
    ]

    if no_observation:
        mismatches = {
            key: _echo_evidence(filter_key_conditions[key])
            for key in no_observation
            if _echo_evidence(filter_key_conditions[key]) != "unmeasured"
        }
        assert mismatches == {}, (
            f"these conditions have no echo observation in the corpus but do not "
            f"read 'unmeasured': {mismatches}"
        )
    else:
        # Every filter-key condition is currently measured -- proven synthetically:
        # jobcat is real and currently `echoed` (asserted by the sibling test
        # above); removing its corpus entry simulates "no echo observation exists
        # for this row" and must derive 'unmeasured', exactly as a genuinely
        # unmeasured row would.
        original_bytes = CORPUS_PATH.read_bytes()
        try:
            perturbed = dict(json.loads(original_bytes.decode("utf-8")))
            del perturbed["jobcat"]
            CORPUS_PATH.write_bytes(json.dumps(perturbed, ensure_ascii=False).encode("utf-8"))
            synthetic_result = _condition_echo_evidence_via_fresh_import("jobcat")
        finally:
            CORPUS_PATH.write_bytes(original_bytes)
        assert CORPUS_PATH.read_bytes() == original_bytes, "the corpus file must be restored byte-for-byte"
        assert synthetic_result == "unmeasured", (
            f"a condition with no echo observation in the corpus must derive to "
            f"'unmeasured'; got {synthetic_result!r} after removing a real "
            f"condition's corpus entry (no real filter-key condition is currently "
            f"unmeasured, so this is the only way left to exercise the default)"
        )


# ── T-73 half 1 (R7.2, behavior): a condition recorded `unmeasured` is excluded
# from drop comparison exactly as a measured `not-echoed` one is ──────────────────
#
# The corpus has no condition CURRENTLY measured as a confirmed not-echoed outcome
# (every measured row is `echoed`; only unmeasured rows are excluded today), so this
# cannot be driven by finding a real not-echoed example -- and does not need to be:
# `detect_dropped` is a pure function whose per-condition behaviour is decided by
# that condition's table entry, not by what 104 actually returns, so a SYNTHETIC
# clone of a real (currently `echoed`) condition with its echo-evidence field
# swapped is a faithful way to drive both non-`echoed` states through the real
# exclusion logic. The tool-level "both reach the caller as ONE flag" half of T-73
# lives in tests/test_search.py, since that surfacing (`applied_filters`) is
# tools/search.py's responsibility, not tools/filters.py's.

def test_detect_dropped_excludes_unmeasured_exactly_like_not_echoed(monkeypatch):
    real_jobcat = filters_mod.CONDITIONS["jobcat"]
    assert _echo_evidence(real_jobcat) == "echoed", "sanity: jobcat must start out echoed"

    sent = encode_filters({"jobcat": ["操作／技術類人員"]})  # -> [("jobcat", "2010001000")]
    null_echo_form = {"jobcat": None}  # what a dropped OR a non-compared condition looks like

    # Contrast case: an actually-echoed condition with a null echo IS reported dropped.
    assert detect_dropped(sent, null_echo_form) == ["jobcat"], (
        "sanity: a measured-echoed condition with a null echo must still be caught, "
        "or the two cases below would not be proving anything was excluded"
    )

    for synthetic_state in ("not-echoed", "unmeasured"):
        synthetic_condition = _replace_echo_evidence(real_jobcat, synthetic_state)
        monkeypatch.setitem(filters_mod.CONDITIONS, "jobcat", synthetic_condition)
        assert detect_dropped(sent, null_echo_form) == [], (
            f"a condition recorded {synthetic_state!r} must be excluded from drop "
            f"comparison by table lookup, exactly like a measured not-echoed one -- "
            f"got it reported dropped instead"
        )


# ── A missing certification corpus must FAIL IMPORTING mcp104.tools.drop_detection
# specifically, and must NOT block mcp104.tools.filters -- not just fail somehow ─────
#
# A missing corpus that silently derived every condition as `unmeasured` would make
# `detect_dropped` exclude everything -- drop detection silently off, the exact
# silent-wrong-answer failure class this whole feature is organised against. That is
# why loading the corpus raises rather than degrading. But the corpus read lives in
# `mcp104.tools.drop_detection`, not `mcp104.tools.filters`: `tools.filters` is what
# `research/probes/certify_conditions.py` (the only tool that can REGENERATE the
# corpus) itself imports, for the condition table alone -- if the corpus read (and its
# raise) lived there too, a missing corpus would make the one tool that could fix it
# unable to start. Moving the corpus read (and `detect_dropped`, and the echo-evidence
# derivation) into the separate `drop_detection` module, which the probe does NOT
# import (see test_probe_source_does_not_import_drop_detection below), resolves that
# self-inflicted deadlock. `mcp104.tools.filters` alone now has no corpus read and no
# raise; every `CONDITIONS` row simply stays at its dataclass default (`"unmeasured"`)
# until `drop_detection` is imported.
#
# Three assertions: `mcp104.tools.filters` must import successfully with the corpus
# absent (drop detection can be entirely missing its data source and the condition
# table itself must still be usable); `mcp104.tools.drop_detection` must fail to
# import (the consequence, not merely "a raise exists somewhere"); and the failure
# message must name the packaged-data cause -- only a stable token ("package-data") is
# asserted, not the whole string, so a rewording of the surrounding prose does not
# turn this into a transcription check.
#
# Subprocess, not an in-process import, for both modules: `mcp104.tools.filters` (and,
# transitively through other test modules in this same session,
# `mcp104.tools.drop_detection`) is already imported elsewhere in this session, and
# mutating its already-derived CONDITIONS in place (or re-deriving into a fresh module
# object that some, but not all, other references would see) would leak into every
# later test that reads it -- the identical reasoning T-72's corpus-provenance check
# already established for this exact module. No `cwd=` on either subprocess call: an
# editable install makes `mcp104` importable regardless of the subprocess's working
# directory, which is what lets the manual path-manipulation this test used to need go away
# entirely. The corpus file is moved aside and restored in a `finally`, and the
# restore is itself asserted, matching T-72's precedent.

def test_missing_corpus_fails_drop_detection_import_but_not_filters_import():
    original_bytes = CORPUS_PATH.read_bytes()
    moved_aside = CORPUS_PATH.with_name(CORPUS_PATH.name + ".moved_for_test")
    assert not moved_aside.exists(), (
        f"refusing to run: a stale {moved_aside.name} already exists from a "
        f"previous interrupted run -- resolve that by hand before trusting this test"
    )
    try:
        CORPUS_PATH.rename(moved_aside)
        filters_result = subprocess.run(
            [sys.executable, "-c", "import mcp104.tools.filters"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
        drop_detection_result = subprocess.run(
            [sys.executable, "-c", "import mcp104.tools.drop_detection"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=60,
        )
    finally:
        if moved_aside.exists():
            moved_aside.rename(CORPUS_PATH)

    assert CORPUS_PATH.read_bytes() == original_bytes, "the corpus file must be restored byte-for-byte"

    assert filters_result.returncode == 0, (
        f"importing mcp104.tools.filters with the corpus absent must still SUCCEED -- "
        f"filters.py has no corpus read at all (that responsibility lives in "
        f"drop_detection.py), and a missing corpus must not block the one module "
        f"research/probes/certify_conditions.py needs to import to regenerate that "
        f"very corpus.\n"
        f"stdout: {filters_result.stdout!r}\nstderr: {filters_result.stderr!r}"
    )

    assert drop_detection_result.returncode != 0, (
        f"importing mcp104.tools.drop_detection with the corpus absent must FAIL, not "
        f"silently derive every condition as 'unmeasured' and start with drop "
        f"detection switched off; subprocess exited "
        f"{drop_detection_result.returncode} cleanly.\n"
        f"stdout: {drop_detection_result.stdout!r}\nstderr: {drop_detection_result.stderr!r}"
    )
    assert "package-data" in drop_detection_result.stderr, (
        f"the failure diagnostic must name the packaged-data cause -- after the "
        f"package-layout restructure, a Dockerfile COPY allowlist or a "
        f"pyproject.toml package-data pattern omitting "
        f"src/mcp104/assets/certification/ is how this file actually goes missing "
        f"from a build; a generic 'file not found' would satisfy an existence-only "
        f"check while losing the actionable content that makes this failure "
        f"fixable. Full stderr:\n{drop_detection_result.stderr}"
    )


# ── The probe that regenerates the corpus must not import the module that reads it,
# or regenerating a missing corpus would deadlock on itself ──────────────────────────

def test_probe_source_does_not_import_drop_detection():
    probe_source = (
        REPO_ROOT / "research" / "probes" / "certify_conditions.py"
    ).read_text(encoding="utf-8")
    assert "drop_detection" not in probe_source, (
        "research/probes/certify_conditions.py must not import (or otherwise "
        "reference) mcp104.tools.drop_detection -- that module raises when the "
        "certification corpus is absent, and this probe is the only tool that can "
        "regenerate it; importing drop_detection here would make the probe unable "
        "to start in exactly the state it exists to fix"
    )


# ── Isolation guard: only filters.py and drop_detection.py may READ
# Condition.echo_evidence ─────────────────────────────────────────────────────────────
#
# echo_evidence is the field the corpus derivation (drop_detection.py) patches and
# detect_dropped (also drop_detection.py) reads by table lookup; filters.py's own
# Condition dataclass carries the field's definition and default, which is a
# definition, not a read. Any OTHER module reading it directly would be reading a
# value that stays "unmeasured" for every row unless mcp104.tools.drop_detection has
# separately been imported somewhere in the process first -- import-order-dependent
# behaviour this guard exists to catch before it creeps in silently.
#
# An AST walk, not a textual grep: tools/search.py's docstring (around the line
# explaining why kws is compared and page is not) carries the literal identifier
# `echo_evidence` inside prose, not a read -- a grep is red on day one and cannot tell
# the two apart. Two AST node forms count as a read:
#   - ast.Attribute whose .attr == "echo_evidence" (e.g. `condition.echo_evidence`)
#   - ast.Call to getattr(..., "echo_evidence", ...) with a CONSTANT second argument --
#     a constant-argument getattr is a live idiom in this codebase (tools/helpers.py's
#     get_session_id uses exactly this shape for a different attribute), so it must
#     count as a read too, or this guard would have a gap a real idiom in this same
#     project already demonstrates is used.
# A computed-name getattr (the attribute name built at runtime, e.g. from a variable)
# is NOT statically detectable by either node form -- that hole is accepted rather
# than closed, since closing it would mean pinning the field name in a way this very
# test file's own `_echo_evidence_field_name` deliberately does not (it locates the
# field by VALUE, never by assuming an identifier -- see that helper's docstring).

_ECHO_EVIDENCE_ALLOWED_FILENAMES = {"filters.py", "drop_detection.py"}


def _echo_evidence_reads(tree: ast.AST) -> list[ast.AST]:
    reads: list[ast.AST] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == "echo_evidence":
            reads.append(node)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and node.args[1].value == "echo_evidence"
        ):
            reads.append(node)
    return reads


def test_echo_evidence_reads_detector_has_a_positive_control():
    """Guards the guard: `_echo_evidence_reads` must find at least one read in
    `drop_detection.py`'s own source -- the one file EXCLUDED from the isolation walk
    below precisely because it is expected to contain real reads (`detect_dropped`'s
    `condition.echo_evidence != "echoed"` and the derivation code's own attribute
    accesses). Without this, a future edit that breaks detection itself (the field
    gets renamed, a third node form is introduced, `node.attr` is typo'd) would leave
    the isolation test passing -- `violations == {}` is satisfied just as well by "no
    file has an echo_evidence read" as by "no file OUTSIDE the allowed two has one",
    and only a positive control distinguishes the two. Same instrument-hides-the-
    answer shape as a filter matching rule codes and silently dropping the one line
    that would have shown the real answer.
    """
    drop_detection_path = REPO_ROOT / "src" / "mcp104" / "tools" / "drop_detection.py"
    tree = ast.parse(drop_detection_path.read_text(encoding="utf-8"), filename=str(drop_detection_path))
    reads = _echo_evidence_reads(tree)
    assert len(reads) >= 1, (
        "expected _echo_evidence_reads to find at least one AST-level read of "
        "echo_evidence in drop_detection.py (it contains detect_dropped's own "
        "condition.echo_evidence comparison) -- found none, which means the "
        "detector itself is broken and test_echo_evidence_read_only_in_filters_"
        "and_drop_detection below cannot be trusted: it would report 'no "
        "violations' just as happily whether isolation holds or the detector "
        "simply stopped seeing anything"
    )


def test_echo_evidence_read_only_in_filters_and_drop_detection():
    src_root = REPO_ROOT / "src" / "mcp104"
    violations: dict[str, int] = {}
    for path in sorted(src_root.rglob("*.py")):
        if path.name in _ECHO_EVIDENCE_ALLOWED_FILENAMES:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        reads = _echo_evidence_reads(tree)
        if reads:
            violations[str(path.relative_to(REPO_ROOT))] = len(reads)
    assert violations == {}, (
        f"echo_evidence must be READ only from tools/filters.py and "
        f"tools/drop_detection.py -- found AST-level read(s) (ast.Attribute or "
        f"constant-argument getattr) elsewhere: {violations}. A module outside "
        f"those two reading this field directly may see 'unmeasured' for every row "
        f"unless mcp104.tools.drop_detection has separately been imported somewhere "
        f"in the process -- import-order-dependent behaviour this guard exists to "
        f"catch"
    )
