"""Tests for tools/discovery.py -- browse_filter_values(key, code=None) and
describe_result_fields(), the two zero-request/zero-quota discovery tools.

Both tools are pure, offline, module-level functions wrapped by a thin async closure
(`register_discovery_tools`) -- these tests exercise the pure functions directly
(`_browse_filter_values`, `_describe_result_fields`), the same pattern tests/test_search.py
uses for tools/search.py's `_build_*_response` functions, and the same "no MCP Context
needed" property steering/structure.md requires of decision logic.
"""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

import pytest

import mcp104.tools.categories as categories_mod
import mcp104.tools.filters as filters_mod
import mcp104.tools.discovery as discovery_mod
import mcp104.tools.search as search_mod
from mcp104.tools.discovery import (
    RESUME_ROW_FIELD_GLOSS,
    _browse_filter_values,
    _completeness_note,
    _describe_result_fields,
    _row_facing_key,
    register_discovery_tools,
)
from mcp104.tools.filters import CONDITIONS, VALID_FILTER_KEYS, Condition

from tests.test_contract_docs import _all_tool_descriptions, _normalize_ws

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── browse_filter_values: a legal shape for EVERY key, swept ────────────────────────

def test_browse_filter_values_legal_shape_for_every_valid_filter_key():
    """Every key in VALID_FILTER_KEYS must produce a success shape carrying at least
    key/value_source/note -- swept, never a hard-coded key list or count (the plan's
    own instruction, and this project's general rule against population-count
    assertions -- steering/structure.md)."""
    assert VALID_FILTER_KEYS, "sanity: no filter keys defined at all"
    for key in VALID_FILTER_KEYS:
        result = _browse_filter_values(key, None)
        assert "error" not in result, f"'{key}': expected success, got {result}"
        assert result["key"] == key
        assert result["value_source"] == CONDITIONS[key].value_source
        assert isinstance(result["note"], str) and result["note"], f"'{key}': empty note"

        value_source = CONDITIONS[key].value_source
        if value_source == "dataset":
            assert result["dataset"] == CONDITIONS[key].dataset
            assert isinstance(result["path"], list) and result["path"] == []
            assert isinstance(result["values"], list) and result["values"]
        elif value_source == "enum":
            assert isinstance(result["values"], dict) and result["values"]
        elif value_source == "composite":
            # NOT identity-with-the-source (Round I1 finding: `result["structure"] ==
            # CONDITIONS[key].value_domain` is true for ANY content whatsoever, since
            # it just proves the two sides are the same object/value regardless of
            # what that value actually is) -- falsifiable content checks per composite
            # key instead, each pinned to what `_validate_*` in tools/filters.py
            # actually requires, exercised in detail below.
            assert result["structure"], (
                f"'{key}': structure must not be empty/None -- a composite shipped "
                f"without its own caller_shape would satisfy `None == None` on the "
                f"line below and stay green (Smell H, Round I2); this catches a "
                f"future composite added without one"
            )
            assert result["structure"] == CONDITIONS[key].caller_shape
            assert result["structure"] != CONDITIONS[key].value_domain, (
                f"'{key}': structure must be the caller-facing SHAPE (caller_shape), "
                f"never the caveat prose (value_domain) -- Bug A, Round I1"
            )
        else:  # codes-only / numeric / free-text -- no values/structure key at all
            assert "values" not in result
            assert "structure" not in result


# ── composite `structure`: a real, buildable shape -- not the caveat-prose dict ─────

def test_expect_pay_structure_matches_what_validate_filters_actually_requires():
    structure = _browse_filter_values("expect_pay", None)["structure"]
    assert set(structure) == {"types", "month"}, structure
    assert isinstance(structure["month"], dict), (
        "expect_pay's month must be a NESTED dict (what validate_filters requires), "
        "not a dotted-key string like 'month.mode'"
    )
    assert set(structure["month"]) == {"mode", "min", "max"}, structure["month"]


def test_work_exp_time_structure_carries_min_and_max():
    structure = _browse_filter_values("work_exp_time", None)["structure"]
    assert {"mode", "min", "max"} <= set(structure), (
        f"work_exp_time's structure is missing min/max entirely (Round I1 Bug A "
        f"finding) -- got {structure}"
    )


def test_language_skills_structure_has_languages_list_and_slot_cap_mentioned():
    structure = _browse_filter_values("language_skills", None)["structure"]
    assert "languages" in structure, structure
    assert "3" in structure["languages"], (
        f"language_skills' structure must mention the 3-slot cap "
        f"(_MAX_LANGUAGE_SLOTS) -- got {structure['languages']!r}"
    )
    assert "fulfills_all" in structure


# ── dataset value_source: root layer, one drill-down, a leaf code ───────────────────

def test_browse_dataset_key_root_layer():
    result = _browse_filter_values("jobcat", None)
    assert result["path"] == []
    assert all({"code", "name", "terminal", "has_children"} <= set(v) for v in result["values"])


def test_browse_dataset_key_one_drill_down_reports_path_and_children():
    root = _browse_filter_values("jobcat", None)["values"]
    branch = next(v for v in root if v["has_children"])
    child_result = _browse_filter_values("jobcat", branch["code"])
    assert [p["code"] for p in child_result["path"]] == [branch["code"]]
    assert child_result["values"], "sanity: chosen branch had no children after all"


def test_browse_dataset_key_leaf_code_is_success_with_empty_values():
    # 2007001004 (軟體工程師) is a documented leaf -- tests/test_categories.py's own
    # _DOCUMENTED_NODES table -- independent of this file's own root/children walk.
    result = _browse_filter_values("jobcat", "2007001004")
    assert "error" not in result
    assert result["values"] == []
    assert "葉節點" in result["note"] or "leaf" in result["note"].lower()


# ── Unknown key / unknown code ───────────────────────────────────────────────────────

def test_browse_filter_values_unknown_key_is_an_error_naming_legal_keys():
    result = _browse_filter_values("not-a-real-key", None)
    assert "error" in result
    assert "values" not in result
    for key in VALID_FILTER_KEYS:
        assert key in result["error"]


def test_browse_filter_values_unknown_code_is_an_error_not_an_empty_list():
    result = _browse_filter_values("jobcat", "NO-SUCH-CODE-99999999")
    assert "error" in result
    assert "values" not in result


def test_browse_filter_values_unknown_key_consumes_valid_filter_keys_not_a_rederivation(monkeypatch):
    """The unknown-key gate must be `key not in VALID_FILTER_KEYS`, not a re-derived
    `provenance == "filter-key" and shippable` predicate -- shrinking VALID_FILTER_KEYS
    (as if a row were retired) must make this tool agree immediately, with no
    parallel logic of its own to fall out of step."""
    key = VALID_FILTER_KEYS[0]
    monkeypatch.setattr(discovery_mod.filters_mod, "VALID_FILTER_KEYS", tuple(
        k for k in VALID_FILTER_KEYS if k != key
    ))
    result = _browse_filter_values(key, None)
    assert "error" in result, (
        "removing a key from VALID_FILTER_KEYS must make browse_filter_values reject "
        "it -- if this still succeeds, the tool re-derived its own notion of "
        "'legal key' instead of consuming VALID_FILTER_KEYS live"
    )


# ── enum rows: non-empty value_domain, non-empty values, domain_completeness notes ──

def test_every_enum_row_carries_a_non_empty_value_domain_and_non_empty_values():
    enum_keys = [k for k in VALID_FILTER_KEYS if CONDITIONS[k].value_source == "enum"]
    assert enum_keys, "sanity: no enum-backed filter key found at all"
    for key in enum_keys:
        assert isinstance(CONDITIONS[key].value_domain, dict) and CONDITIONS[key].value_domain, (
            f"'{key}': enum row must carry a non-empty dict value_domain"
        )
        result = _browse_filter_values(key, None)
        assert result["values"], (
            f"'{key}': enum response must carry a non-empty values -- an empty dict "
            f"here would state a falsehood (a shape-only check passing values={{}} is "
            f"exactly the defect this case guards against)"
        )


def test_every_enum_row_carries_a_domain_completeness_value():
    enum_keys = [k for k in VALID_FILTER_KEYS if CONDITIONS[k].value_source == "enum"]
    for key in enum_keys:
        assert CONDITIONS[key].domain_completeness in ("certified", "measured-subset", "unmeasured")


def test_completeness_note_text_differs_across_all_three_values():
    """The three possible `domain_completeness` values must map to three DIFFERENT
    note strings -- tested directly against the template function rather than
    requiring today's CONDITIONS table to already contain a real example of all
    three (steering/structure.md's rule against population-membership assertions:
    'the oracle ran', not 'the population happens to contain one')."""
    notes = {
        value: _completeness_note(value)
        for value in ("certified", "measured-subset", "unmeasured")
    }
    assert len(set(notes.values())) == 3, (
        f"expected three distinct note strings, got: {notes}"
    )


def test_plast_action_date_type_and_update_date_type_are_now_domain_certified():
    """2026-08-15 remeasurement (research/results/discover_enum_domains_results.json):
    104's own validation errors state BOTH ends of the domain for these two keys
    (0 -> 'too small (minimum is 1)', 9999 -> 'too big (maximum is 8)') -- the code
    SET is therefore fully known, which is exactly what `domain_completeness`
    answers, so both flip from the earlier round's `measured-subset` to `certified`.
    This superseded an earlier test that pinned `measured-subset` for these two keys
    -- exactly the "measurement got better, don't let the test punish it" case
    steering/structure.md itself warns about, not a hard-coded population count."""
    for key in ("plastActionDateType", "updateDateType"):
        assert CONDITIONS[key].domain_completeness == "certified", (
            f"'{key}': domain (the CODE SET) is now fully bounded by 104's own "
            f"validation errors on both ends -- domain_completeness answers "
            f"exactly that question, independent of whether every code's WINDOW "
            f"or LABEL is also measured"
        )
        result = _browse_filter_values(key, None)
        assert result["note"] == _completeness_note("certified")


def test_domain_completeness_and_label_completeness_stay_separate_axes():
    """The two axes this project has conflated before: whether the CODE SET is fully
    known (`domain_completeness`) and whether every code's MEANING is measured (the
    label text).

    Until 2026-08-15 this was pinned against a live example -- `updateDateType` was
    domain-certified while its code 2 carried an unmeasured window. That example is
    gone: code 2 was corroborated against a wide population the same day (1,564 rows,
    every sampled updateDayDesc the current date). The old assertion required an
    incomplete row to exist, so **improving the measurement broke the test** -- the
    failure mode steering/structure.md warns about, and its own docstring predicted.

    So the separation is now asserted structurally, on synthetic rows rather than on
    whichever real row happens to be worst-measured. Both combinations must remain
    representable, because the day they are not, the two axes have silently merged."""
    base = CONDITIONS["updateDateType"]
    unmeasured_markers = ("[INF]", "未測", "未量測", "無法判定")

    certified_but_unlabelled = dataclasses.replace(
        base, domain_completeness="certified",
        value_domain={"1": "已知標籤", "2": "未量測"})
    assert certified_but_unlabelled.domain_completeness == "certified"
    assert any(m in lab for lab in certified_but_unlabelled.value_domain.values()
               for m in unmeasured_markers), (
        "a row must be able to claim a complete code SET while still admitting an "
        "unmeasured per-code meaning -- if this cannot be expressed the axes merged"
    )

    subset_but_fully_labelled = dataclasses.replace(
        base, domain_completeness="measured-subset",
        value_domain={"1": "已知標籤", "2": "也已知"})
    assert subset_but_fully_labelled.domain_completeness == "measured-subset"
    assert not any(m in lab for lab in subset_but_fully_labelled.value_domain.values()
                   for m in unmeasured_markers), (
        "and the converse: every known code's meaning measured, while the code set "
        "itself is admittedly incomplete"
    )

    # The note the caller actually reads must key on the domain axis alone.
    assert _completeness_note("certified") != _completeness_note("measured-subset")


# ── the published-tool acceptance property ──────────────────────────────────────────
#
# Every key an Agent may put in `filters` must be answerable by
# `browse_filter_values(key)` well enough to CHOOSE a value on purpose — not merely to
# pass validation. A code with no meaning is a value the Agent can send and cannot
# justify sending, which is the tool failing at the job it exists for.
#
# The four entries below are the debt, each owned by a backlog item. The assertion is
# set EQUALITY, deliberately: closing a gap fails this test until the entry is removed,
# and a newly added key or code with no meaning fails it immediately. A subset check
# would let the second case pass silently, which is the whole failure mode being
# guarded against.
_UNMEASURED_MARKERS = ("未量測", "未測", "[INF]", "無法判定")

# EMPTY as of 2026-08-15: every filter key an Agent may pass now answers "what does
# this value mean" from the tool itself. Keeping the set (rather than asserting no gaps
# inline) is deliberate — a future gap gets recorded here with its backlog id, and the
# set-equality assertion below still forces both directions.
_KNOWN_MEANING_GAPS: set[str] = set()


def _has_unmeasured(text) -> bool:
    return any(marker in str(text) for marker in _UNMEASURED_MARKERS)


def _meaning_is_discoverable(key: str) -> bool:
    """Per `value_source`, because "enough to choose" differs by kind."""
    condition = CONDITIONS[key]
    payload = _browse_filter_values(key, None)
    source = condition.value_source
    if source == "enum":
        return not any(_has_unmeasured(label)
                       for label in (condition.value_domain or {}).values())
    if source == "dataset":
        values = payload.get("values") or []
        return bool(values) and all(node.get("name") for node in values)
    if source == "composite":
        subs = payload.get("sub_domains") or {}
        return bool(subs) and not any(_has_unmeasured(v) for v in subs.values())
    # numeric / free-text / codes-only: a note stating the accepted form is the whole
    # of what "discoverable" can mean — there is no value list to enumerate.
    return bool(str(payload.get("note", "")).strip())


def test_every_filter_key_has_a_discoverable_meaning_except_the_recorded_gaps():
    gaps = {key for key in filters_mod.VALID_FILTER_KEYS
            if not _meaning_is_discoverable(key)}
    assert gaps == _KNOWN_MEANING_GAPS, (
        f"meaning-discoverability changed.\n"
        f"  newly undiscoverable (FIX or record with a backlog id): "
        f"{sorted(gaps - _KNOWN_MEANING_GAPS)}\n"
        f"  now discoverable (remove from _KNOWN_MEANING_GAPS): "
        f"{sorted(_KNOWN_MEANING_GAPS - gaps)}"
    )


def test_every_filter_key_states_how_it_combines():
    """Legal values alone do not let a caller build a query: it also has to know
    whether several values may be given at once, and what setting two keys means.
    Swept over every key rather than sampled — a key added without this is a key an
    Agent can pass and cannot reason about."""
    for key in filters_mod.VALID_FILTER_KEYS:
        payload = _browse_filter_values(key, None)
        assert isinstance(payload.get("accepts_multiple"), bool), key
        assert payload.get("combination"), key
        # The advice must agree with what the validator actually enforces, not repeat
        # a second opinion about it.
        assert payload["accepts_multiple"] == (key in filters_mod.MULTI_VALUE_KEYS), key


def test_a_list_on_a_single_value_key_is_refused_before_the_wire():
    """Regression: every non-multi key used to render `str(["0","1"])` into its wire
    value and send it. 104 does not reject everything it cannot parse, so the realistic
    outcome was a filter silently vanishing while the caller believed it applied."""
    single_value = [k for k in filters_mod.VALID_FILTER_KEYS
                    if k not in filters_mod.MULTI_VALUE_KEYS
                    and CONDITIONS[k].encoding != "composite"]
    assert single_value, "sanity: some keys must be single-value for this to mean anything"
    for key in single_value:
        with pytest.raises(filters_mod.FilterError):
            filters_mod.encode_filters({key: ["1", "2"]})


def test_driver_and_transport_are_eleven_flag_domains_not_single_checkboxes():
    """2026-08-15 sweep (research/results/discover_enum_domains_results.json).

    Both rows previously held ONE code and claimed `certified`. The evidence for that
    was `driver[]=1 -> 359,721` -- which proves *code 1 works*, not *only code 1
    works*. `certified` asserts the second proposition, so the row asserted something
    nobody had measured, and `browse_filter_values` published it.

    The sweep settles it: every power of two from 1 to 1024 is accepted, 2048 is
    rejected, and every non-power tried (3,5,6,7,9,10,11,12) is rejected -- a single-
    flag field with eleven options. Pinned here because NOTHING ELSE IN THE SUITE
    WOULD CATCH A REGRESSION: the other domain tests assert only that
    `domain_completeness` is one of three strings and that `value_domain` is a
    non-empty dict, both of which stayed true the whole time the one-code rows were
    wrong.
    """
    expected = {str(1 << n) for n in range(11)}
    for key in ("driver", "transport"):
        domain = CONDITIONS[key].value_domain
        assert set(domain) == expected, (
            f"'{key}': measured code set is the powers of two 1..1024 "
            f"(2048 measured and rejected); got {sorted(domain, key=int)}"
        )
        # The code SET is closed by measurement; every code's MEANING is not. Both
        # halves have to hold, or this row starts asserting labels again -- the form
        # control sits in a collapsed section and was never captured, so not even
        # code 1's label is known.
        assert CONDITIONS[key].domain_completeness == "certified", key
        # 2026-08-15: the meanings ARE now measured, so the opposite property holds —
        # no label may still claim ignorance, and each must name a licence class.
        assert not any("未量測" in label for label in domain.values()), (
            f"'{key}': meanings were measured on 2026-08-15; no label should still "
            f"say 未量測 -- got {domain}"
        )
        assert all(label.strip() for label in domain.values()), key


def test_driver_and_transport_share_a_ladder_but_not_a_matching_rule():
    """The finding that makes the labels usable rather than merely present.

    Both keys use the same eleven ordered vehicle classes, but 104 matches them
    differently: `transport` (自備車輛) literally — transport=1's three samples all
    owned only 輕型機車 — while `driver` (持有駕照) hierarchically, a superior licence
    satisfying a lower code. driver=2's samples included a holder of 大型重型機車駕照
    and no 普通重型, and driver=16's included 大客車 holders with no 大貨車.

    That difference changes what a query MEANS, so it must reach the caller. Pinned
    because a label alone reads as a literal filter and would mislead."""
    driver = CONDITIONS["driver"]
    transport = CONDITIONS["transport"]
    assert list(driver.value_domain) == list(transport.value_domain), (
        "both keys are measured to use the same eleven class codes"
    )
    assert driver.matching_note and "階層" in driver.matching_note
    assert transport.matching_note and "字面" in transport.matching_note
    # and it has to be visible, not merely stored
    assert "階層" in _browse_filter_values("driver", None)["matching"]
    assert "matching" not in _browse_filter_values("sex", None), (
        "a row with nothing measured to say about matching must stay silent -- "
        "an empty or filler note would read as a claim"
    )


def test_plast_action_date_type_has_no_remaining_label_gaps():
    """Unlike updateDateType, EVERY code's window was actually read (all 8 pages
    exhaustively) -- so plastActionDateType should carry zero of the unmeasured
    markers, confirmed by the same sweep as the case above rather than asserting a
    specific code list."""
    value_domain = CONDITIONS["plastActionDateType"].value_domain
    unmeasured_markers = ("[INF]", "未測", "未量測", "無法判定")
    gap_codes = {
        code for code, label in value_domain.items()
        if any(marker in label for marker in unmeasured_markers)
    }
    assert not gap_codes, (
        f"plastActionDateType has individually-unmeasured code labels, but every "
        f"code's window was read exhaustively (8/8 pages) this round: {gap_codes}"
    )


# ── terminal from browse_filter_values agrees with resolve(), including both
# JobCat.json conditions (which are measured to disagree on branch acceptance) ──────

def test_terminal_agrees_with_resolve_for_both_jobcat_json_conditions():
    for key in ("jobcat", "workExpJob"):
        root = _browse_filter_values(key, None)["values"]
        branch = next(v for v in root if v["has_children"])
        dataset = categories_mod.load_dataset(CONDITIONS[key].dataset, key)
        resolution = categories_mod.resolve(dataset, branch["code"])
        resolved = resolution.status == "resolved"
        assert branch["terminal"] == resolved, (
            f"'{key}': browse_filter_values reports terminal={branch['terminal']} for "
            f"branch {branch['code']}, but resolve() {'resolved' if resolved else 'refused'} it"
        )


# ── codes-only: no CONDITIONS row uses this value_source today (the plan's own full
# sweep: enum 16, dataset 11, numeric 3, composite 3, free-text 2 = 35 rows), so the
# sweep above never exercises this branch -- exercised directly here with a synthetic
# Condition instead of waiting for a real row (steering/structure.md's rule is against
# asserting a SAMPLE exists in real data, not against direct unit tests of a branch on
# synthetic input). ───────────────────────────────────────────────────────────────────

def test_browse_codes_only_key_has_no_values_and_names_no_dataset():
    synthetic = Condition(
        key="synthetic_codes_only", provenance="filter-key", wire=("synthetic",),
        encoding="scalar", value_source="codes-only",
    )
    result = discovery_mod._browse_codes_only_key(synthetic)
    assert result["key"] == "synthetic_codes_only"
    assert result["value_source"] == "codes-only"
    assert "values" not in result
    assert "structure" not in result
    assert isinstance(result["note"], str) and result["note"]


# ── describe_result_fields ───────────────────────────────────────────────────────────
#
# Bug B (Round I1): the payload used to be keyed on 104's raw camelCase names, but a
# DELIVERED row has been through `_convert_resume_row`'s snake_case conversion plus the
# idNo -> candidate_id rename -- the intersection with a real row's own keys was 4 of
# 35. Every case below checks against what a row ACTUALLY carries (either the live
# `_row_facing_key` transform, or a real fixture run through the real conversion
# function), never against `RESUME_ROW_FIELD_GLOSS`'s own (intentionally still
# camelCase, filtering-only) keys.

def test_describe_result_fields_returns_exactly_the_row_facing_keys():
    result = _describe_result_fields()
    expected = {_row_facing_key(k) for k in RESUME_ROW_FIELD_GLOSS}
    assert set(result["fields"]) == expected, (
        "describe_result_fields() must return exactly the ROW-FACING keys (swept via "
        "_row_facing_key over RESUME_ROW_FIELD_GLOSS), never the raw camelCase keys"
    )
    assert "id_no" not in result["fields"], (
        "idNo must be published as candidate_id, matching what a delivered row "
        "actually carries -- id_no never appears on a row at all"
    )
    assert "candidate_id" in result["fields"]


# ── describe_result_fields(row_type=...) — the JSON-API messaging migration's
# parameter, added without changing the no-argument (default "resume") behaviour ────

def test_describe_result_fields_no_argument_matches_explicit_resume():
    assert _describe_result_fields() == _describe_result_fields("resume"), (
        "the row_type parameter must preserve today's no-argument behaviour byte "
        "for byte"
    )


def test_describe_result_fields_inbox_row_type():
    result = _describe_result_fields("inbox")
    assert "error" not in result
    assert "candidate_id" in result["fields"]
    assert "job_id" in result["fields"]
    assert "event_progress" in result["fields"]
    assert "event_polarity" in result["fields"]
    # Excluded identifiers are documented under their OWN raw name, not a
    # row-facing one they never actually carry.
    assert "idNo" in result["fields"]
    assert "hid" in result["fields"]
    assert "id_no" not in result["fields"]


def test_describe_result_fields_message_row_type():
    result = _describe_result_fields("message")
    assert "error" not in result
    assert "direction" in result["fields"]
    assert "read" in result["fields"]
    assert "created_at" in result["fields"]
    assert "idNo" in result["fields"]
    assert "snapshotId" in result["fields"]
    assert "userName" in result["fields"]
    assert "source" in result["fields"]
    # None of the excluded raw fields leak a row-facing spelling too.
    assert "id_no" not in result["fields"]
    assert "snapshot_id" not in result["fields"]
    assert "user_name" not in result["fields"]


def test_describe_result_fields_template_row_type():
    """T-73: row_type="template" is a real, fourth describer -- and its fields dict
    is keyed on the PUBLISHED names (id/title/description/type_id/type_desc), never
    the raw wire names (typeId/typeDesc) -- same rule inbox/message already follow
    for their own row-facing keys."""
    result = _describe_result_fields("template")
    assert "error" not in result
    for key in ("id", "title", "description", "type_id", "type_desc"):
        assert key in result["fields"], (
            f"row_type=\"template\" is missing published key {key!r}: "
            f"{sorted(result['fields'])}"
        )
    assert "typeId" not in result["fields"], (
        "template row_type must key on the published type_id, not the raw typeId"
    )
    assert "typeDesc" not in result["fields"], (
        "template row_type must key on the published type_desc, not the raw typeDesc"
    )


def test_describe_result_fields_unknown_row_type_names_the_four_legal_values():
    """T-74: adding row_type="template" as a fourth describer means the unknown-value
    error must now name FOUR legal values, not three -- this test (name and content
    both) is the one place design.md warns will not "follow along" on its own."""
    result = _describe_result_fields("not-a-real-row-type")
    assert "error" in result
    assert "fields" not in result
    for legal in ("resume", "inbox", "message", "template"):
        assert legal in result["error"], (
            f"unknown row_type error must name {legal!r} as a legal value: "
            f"{result['error']!r}"
        )


def test_describe_result_fields_event_progress_gloss_is_generated_from_event_labels():
    # "generated from EVENT_LABELS rather than retyped" — the published gloss text
    # must actually contain the table's own labelled pairs, not a hand-typed summary
    # that could silently drift from what _event_labels() consults.
    result = _describe_result_fields("inbox")
    gloss = result["fields"]["event_progress"]
    for (event_type, event_status), (label, _polarity) in discovery_mod.EVENT_LABELS.items():
        assert f"({event_type},{event_status})" in gloss
        assert label in gloss


def test_describe_result_fields_keys_match_a_real_converted_fixture_row():
    """The case the plan explicitly asked for: gloss keys must equal the keys
    `_convert_resume_row` produces for a REAL fixture row -- not the allow-list
    compared against itself (both camelCase, mutually consistent, and both wrong
    about the actual output, which is exactly how this defect survived the first
    round)."""
    fixture = json.loads((FIXTURES_DIR / "rows_search.json").read_text(encoding="utf-8"))
    raw_row = fixture["result"]["data"][0]
    converted_row = search_mod._convert_resume_row(raw_row)

    published_keys = set(_describe_result_fields()["fields"])
    row_keys = set(converted_row)

    missing_from_doc = row_keys - published_keys
    assert not missing_from_doc, (
        f"these keys are on a REAL converted row but describe_result_fields() does "
        f"not document them: {sorted(missing_from_doc)}"
    )
    # search_resumes' row carries 3 extra fields recommend/match do not (masterUrl,
    # nationality, plastActionDateDesc per docs/104-site-facts.md §6b.3b) -- the
    # allow-list is a superset of any single route's row, so the reverse direction
    # (doc keys not on this particular row) is not asserted here.


def test_every_gloss_entry_is_a_non_empty_string():
    assert RESUME_ROW_FIELD_GLOSS, "sanity: the allow-list is empty"
    for field, gloss in RESUME_ROW_FIELD_GLOSS.items():
        assert isinstance(gloss, str) and gloss, f"'{field}': empty or non-string gloss"


def test_unmeasured_fields_carry_the_unmeasured_mark_and_their_own_raw_field_name():
    """switchStatusView / isTalent50 / hunterStatus / versionNo / isHalfShow (and any
    other field whose meaning is unmeasured) must say so plainly and carry 104's own
    RAW field name verbatim -- swept from the gloss text itself (looking for the
    marker), not a hard-coded list of which fields are unmeasured, so this survives a
    future field being newly measured without editing this test. Checked against the
    raw camelCase key (what the gloss text itself promises to carry "verbatim"), not
    the row-facing key -- the published gloss text names 104's OWN field name, and
    that name is the camelCase one."""
    unmeasured = {f: g for f, g in RESUME_ROW_FIELD_GLOSS.items() if "未量測" in g}
    assert unmeasured, "sanity: no unmeasured field found at all"
    for field, gloss in unmeasured.items():
        assert field in gloss, (
            f"'{field}': marked unmeasured but does not carry its own (104) field "
            f"name verbatim in the gloss text: {gloss!r}"
        )


def test_idno_and_contact_privacy_glosses_do_not_reference_nonexistent_entries():
    """Round I1 findings: the old idNo gloss pointed at a nonexistent 'id_no' row, and
    the old contactPrivacy gloss named camelCase phoneDescAll/callTimeDesc even though
    those fields never appear on a ROW at all (they are get_resume_detail-only)."""
    assert "id_no" not in RESUME_ROW_FIELD_GLOSS
    id_gloss = RESUME_ROW_FIELD_GLOSS["idNo"]
    assert "id_no 一列" not in id_gloss, (
        f"idNo's gloss must not point at a nonexistent 'id_no' entry: {id_gloss!r}"
    )
    contact_gloss = RESUME_ROW_FIELD_GLOSS["contactPrivacy"]
    assert "phoneDescAll" not in contact_gloss and "callTimeDesc" not in contact_gloss, (
        f"contactPrivacy's gloss must not use camelCase field names that never "
        f"appear on a row: {contact_gloss!r}"
    )


# ── p_id note reuse (not paraphrased) ────────────────────────────────────────────────

def test_second_identifier_note_is_reused_verbatim_by_search_module():
    """tools/search.py must import SECOND_IDENTIFIER_NOTE from here rather than carry
    its own copy -- provenance check via monkeypatch + live re-import of the already-
    bound name would not prove anything (it's bound at import time), so this instead
    asserts identity: the same string object (or at least equal text) is what
    tools/search.py's `_SECOND_IDENTIFIER_NOTE` holds."""
    import mcp104.tools.search as search_mod
    assert search_mod._SECOND_IDENTIFIER_NOTE == discovery_mod.SECOND_IDENTIFIER_NOTE


def test_second_identifier_note_states_the_measured_relationship_not_the_stale_disclaimer():
    """T-75: SECOND_IDENTIFIER_NOTE is rewritten in place to the MEASURED state (p_id
    IS the messaging system's pId; the candidate_id on a résumé row is idNo, usable
    only by get_resume_detail) -- it must no longer carry the old "both directions
    unmeasured" / "must not call ... with p_id" disclaimer, which is now factually
    backwards."""
    note = discovery_mod.SECOND_IDENTIFIER_NOTE
    for stale_phrase in ("雙向皆未量測", "不能用 p_id 呼叫", "不得用 p_id 呼叫"):
        assert stale_phrase not in note, (
            f"SECOND_IDENTIFIER_NOTE still carries the stale disclaimer "
            f"{stale_phrase!r}: {note!r}"
        )
    assert "pId" in note, "must name pId as what p_id measurably is"
    assert "訊息工具" in note, (
        "must point the caller at the messaging tools (訊息工具) as where the "
        "p_id -- not this row's candidate_id -- is actually usable"
    )


def test_messaging_candidate_id_note_is_a_distinct_constant_naming_the_digit_guard():
    """T-75: MESSAGING_CANDIDATE_ID_NOTE is a NEW constant (not a rename/alias of
    SECOND_IDENTIFIER_NOTE, not identical text) -- it answers a different question
    ("what does this PARAMETER take") than SECOND_IDENTIFIER_NOTE ("what is on this
    ROW"), and per the digit guard it names the >=12-digit rejection that protects
    the messaging tools' candidate_id from an accidentally-passed résumé idNo."""
    note = discovery_mod.MESSAGING_CANDIDATE_ID_NOTE
    assert isinstance(note, str) and note, "MESSAGING_CANDIDATE_ID_NOTE must be a non-empty string"
    assert note != discovery_mod.SECOND_IDENTIFIER_NOTE, (
        "MESSAGING_CANDIDATE_ID_NOTE must not be identical to SECOND_IDENTIFIER_NOTE "
        "-- the two constants answer different questions and must not be merged"
    )
    assert "12" in note, (
        f"MESSAGING_CANDIDATE_ID_NOTE must name the >=12-digit rejection threshold: {note!r}"
    )


def test_messaging_candidate_id_note_reaches_the_three_messaging_tool_descriptions_verbatim():
    """T-75: MESSAGING_CANDIDATE_ID_NOTE must appear, whitespace-stripped and
    verbatim (CJK text wraps without spaces, so a mid-phrase newline is not a
    content difference), in each of send_message's, send_inquiry's, and
    get_conversation's REGISTERED description -- a live read via the real tool
    registry, not a hand-typed copy of the note planted separately in each
    docstring that could silently drift from the constant."""
    note = _normalize_ws(discovery_mod.MESSAGING_CANDIDATE_ID_NOTE)
    descriptions = _all_tool_descriptions()
    for tool_name in ("send_message", "send_inquiry", "get_conversation"):
        assert tool_name in descriptions, f"sanity: {tool_name} not registered"
        desc = _normalize_ws(descriptions[tool_name])
        assert note in desc, (
            f"{tool_name}'s registered description does not carry "
            f"MESSAGING_CANDIDATE_ID_NOTE verbatim -- a hand-typed copy would "
            f"drift from the constant and this would not catch it: {desc!r}"
        )


def test_messaging_candidate_id_note_reaches_descriptions_by_live_reference_not_copy(monkeypatch):
    """Provenance companion to the test above: patch MESSAGING_CANDIDATE_ID_NOTE
    to a sentinel BEFORE registering the tools, so each of the three messaging
    tool descriptions must carry the sentinel -- a description that hand-typed
    the note's current text instead of referencing the constant would still
    show the pre-patch wording here and fail."""
    sentinel = "MSG-CANDIDATE-ID-NOTE-PROVENANCE-SENTINEL-7b2e91"
    monkeypatch.setattr(discovery_mod, "MESSAGING_CANDIDATE_ID_NOTE", sentinel)

    descriptions = _all_tool_descriptions()
    for tool_name in ("send_message", "send_inquiry", "get_conversation"):
        assert tool_name in descriptions, f"sanity: {tool_name} not registered"
        assert sentinel in descriptions[tool_name], (
            f"{tool_name}'s registered description does not reflect the "
            f"monkeypatched MESSAGING_CANDIDATE_ID_NOTE -- it is not reading "
            f"the constant live at registration time"
        )


# ── T-67-style provenance, re-homed here ─────────────────────────────────────────────
#
# The original T-67 pair (tests/test_search.py) asserted RESOLVE_ACCEPTS_ZH reaches
# published text by a LIVE read, never a hand-typed copy that can drift. Part A moved
# the acceptance-rule text OUT of search_resumes' own description entirely and INTO
# browse_filter_values' -- but nothing then asserted the new home still reads it live,
# so a hand-typed copy planted in `_browse_filter_values_description()` would pass the
# whole suite silently. This is that missing case, at its new home.

def test_browse_filter_values_description_reads_resolve_accepts_zh_live(monkeypatch):
    sentinel = "T67-PROVENANCE-SENTINEL-discovery-3f9a1c8e"
    monkeypatch.setattr(categories_mod, "RESOLVE_ACCEPTS_ZH", sentinel)

    # Call the GENERATOR, not the cached `_BROWSE_FILTER_VALUES_DESCRIPTION` -- that
    # constant binds at import time and would still hold the pre-patch text, proving
    # nothing about live provenance (the exact mistake the original T-67 comment
    # already warns about for tools/search.py's own cached constant).
    generated = discovery_mod._browse_filter_values_description()

    assert sentinel in generated, (
        "_browse_filter_values_description() no longer reads "
        "categories.RESOLVE_ACCEPTS_ZH live -- either the generator stopped reading "
        "the export, or a hand-typed copy was planted in its place"
    )


def test_browse_filter_values_description_reads_candidate_terminal_zh_live(monkeypatch):
    """Same provenance property, for the other reused constant this docstring embeds
    (`categories.CANDIDATE_TERMINAL_ZH`)."""
    sentinel = "T67-PROVENANCE-SENTINEL-terminal-9c1a3f8e"
    monkeypatch.setattr(categories_mod, "CANDIDATE_TERMINAL_ZH", sentinel)

    generated = discovery_mod._browse_filter_values_description()

    assert sentinel in generated, (
        "_browse_filter_values_description() no longer reads "
        "categories.CANDIDATE_TERMINAL_ZH live"
    )


# ── register_discovery_tools: no login required, no network/session/throttle touched ─

class _FakeMCP:
    def __init__(self):
        self.tools: dict[str, object] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self.tools[name] = fn
            self.descriptions[name] = kwargs.get("description") or fn.__doc__ or ""
            return fn
        return decorator


@pytest.mark.asyncio
async def test_browse_filter_values_tool_works_with_no_context_and_no_login():
    """register_discovery_tools must NOT wrap either tool with require_login -- both
    functions must be callable with only their own declared arguments, no `ctx:
    Context` needed at all (unlike every tools/search.py tool)."""
    mcp = _FakeMCP()
    register_discovery_tools(mcp)
    assert set(mcp.tools) == {"browse_filter_values", "describe_result_fields"}

    result = await mcp.tools["browse_filter_values"]("jobcat")
    assert "error" not in result

    fields_result = await mcp.tools["describe_result_fields"]()
    assert set(fields_result["fields"]) == {_row_facing_key(k) for k in RESUME_ROW_FIELD_GLOSS}


def test_no_tool_module_import_touches_the_network_or_session_layer():
    """tools/discovery.py must depend only on tools/categories.py and tools/filters.py
    -- never browser/api_client.py, browser/session.py, browser/throttle.py, or
    tools/helpers.py's guarded_page/guarded_api. Checked against `sys.modules`' own
    record of what tools.discovery actually imported (its `__dict__`'s bound module
    objects), not a text search over the source (which would false-positive on prose
    mentioning these words, and could false-negative on an aliased import)."""
    import types
    forbidden_module_substrings = ("browser.api_client", "browser.session", "browser.throttle")
    forbidden_names = {"guarded_page", "guarded_api"}

    imported_modules = {
        name: getattr(value, "__name__", "")
        for name, value in vars(discovery_mod).items()
        if isinstance(value, types.ModuleType)
    }
    for name, module_name in imported_modules.items():
        for forbidden in forbidden_module_substrings:
            assert forbidden not in module_name, (
                f"tools/discovery.py imports {module_name!r} (as {name!r}) -- this "
                f"module must stay off the network/session/throttle path entirely"
            )
    for forbidden in forbidden_names:
        assert forbidden not in vars(discovery_mod), (
            f"tools/discovery.py imports '{forbidden}' directly -- this module must "
            f"never call guarded_page/guarded_api (steering/structure.md: public "
            f"category data must not be routed through them or the throttle)"
        )
