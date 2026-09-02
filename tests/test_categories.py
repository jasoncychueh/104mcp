"""Tests for tools/categories.py's bundled datasets and tiered category-name
resolver.

Written blind to tools/categories.py's implementation (spec-driven-development
Mode 1/2). Every expected outcome below comes from one of two independent
oracles, never from reading the resolver's own code:

- The committed dataset files under `src/mcp104/assets/104-categories/`, parsed here by
  this test's own small JSON walker (`_load_raw_nodes`) -- never through
  `load_dataset`, so a bug in `load_dataset` cannot also corrupt the
  expected values.
- `docs/104-site-facts.md` §6b.3g's measured resolution trials and its own
  name-decoration/prefix-family census.

**`Resolution` is now fully pinned by design.md's Components §6** (round-2
design revision): a named tuple `(code, tier, candidates, status)`, where
`status` (`"resolved"` / `"ambiguous"` / `"branch"` / `"unknown"`) is the
discriminator for which of the other three fields are populated, and
`candidates` is `tuple[Candidate, ...]` with `Candidate = (code, name)` —
never bare codes, because the entire point of returning candidates is that
the *caller* chooses, and a bare code is not choosable. Tests below access
fields via `_get()`, which accepts either attribute or dict-key access; that
narrower hedge (dataclass vs. dict *shape*, not field *names*) is the one
assumption this file still carries knowingly, alongside one more:

- The literal tier identifiers design.md's prose names ("Normalised exact",
  "Prefix", "Substring") are assumed to surface as the lowercase strings
  `TIER_EXACT` / `TIER_PREFIX` / `TIER_SUBSTRING` below, following
  structure.md's snake_case convention — design.md does not pin a literal
  for these the way it now pins `status`'s four values. If the
  implementation reports different literals, that is a join mismatch to
  adjudicate, not necessarily a defect in either side. (In this session's
  Mode 1 run, "exact" and "prefix" both matched the live implementation.)
"""
from __future__ import annotations

import json
import re
import unicodedata
from pathlib import Path

import pytest

from mcp104.tools.categories import children, load_dataset, path, resolve

# Independent oracle: parses the committed JSON file directly, never going through
# tools/categories.py's load_dataset. Addresses the SOURCE tree by repo-root arithmetic
# (this file's own directory, not `importlib.resources.files("mcp104")`) — reading
# through the installed package would route this "independent" oracle through the very
# packaging machinery it exists to stay independent of, and would silently start
# checking a stale copy on an editable install pointed elsewhere.
ASSETS_DIR = Path(__file__).resolve().parent.parent / "src" / "mcp104" / "assets" / "104-categories"

TIER_EXACT = "exact"
TIER_PREFIX = "prefix"
TIER_SUBSTRING = "substring"

_ALL_DATASET_FILES = [
    "JobCat.json", "AreaWork.json", "Area.json", "Indust.json",
    "Major.json", "Tool.json", "Skill.json", "Abroad.json",
]

# Round I9: load_dataset() gained a required `condition_key` parameter -- branch-code
# acceptance is now measured per (dataset, condition) pair, not per dataset file alone
# (workExpJob binds to JobCat.json but was never measured to accept a branch the way
# jobcat was, so a shared per-file default would let it silently inherit an acceptance
# nobody measured for it). This is design.md's own "Bundled datasets" table -- the
# PRIMARY condition each file serves, for tests that only need any one valid condition
# to load a dataset under and are not about branch-acceptance specifics themselves.
# Where a dataset backs two conditions, the one already used in this file's existing
# measured citations (comments below) is kept, not picked arbitrarily.
_DATASET_TO_CONDITION = {
    "JobCat.json": "jobcat",
    "AreaWork.json": "city",
    "Area.json": "home",
    "Indust.json": "workExpInd",
    "Major.json": "major",
    "Tool.json": "goodTools",
    "Skill.json": "certificates",
    "Abroad.json": "studyAbroad",
}

# T-69's sweep is the one case that must cover EVERY (dataset, condition) pair, not
# just one per file: terminality (a candidate's `terminal` flag) is now
# condition-relative (design.md / this round's report: jobcat accepts the same branch
# code workExpJob refuses, on the identical JobCat.json file), so a sweep keyed only by
# dataset file would silently skip the exact divergence this property exists to catch.
_ALL_DATASET_CONDITION_PAIRS = [
    ("JobCat.json", "jobcat"),
    ("JobCat.json", "workExpJob"),
    ("AreaWork.json", "city"),
    ("Area.json", "home"),
    ("Indust.json", "workExpInd"),
    ("Indust.json", "expectInd"),
    ("Major.json", "major"),
    ("Tool.json", "goodTools"),
    ("Skill.json", "certificates"),
    ("Abroad.json", "studyAbroad"),
    ("Abroad.json", "nationality"),
]


def _get(resolution, field):
    """Resolution's exact representation (dataclass vs dict) is not pinned
    by design.md; accept either so assertions target values, not shape."""
    if hasattr(resolution, field):
        return getattr(resolution, field)
    return resolution[field]


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = s.replace("／", "/")
    s = " ".join(s.split())
    return s.casefold()


def _walk(node, out):
    if isinstance(node, dict):
        out.append(node)
        for c in node.get("n") or []:
            _walk(c, out)
    elif isinstance(node, list):
        for c in node:
            _walk(c, out)


def _load_raw_nodes(filename: str) -> list[dict]:
    """Independent oracle: parses the committed JSON file directly, never
    going through tools/categories.py's load_dataset."""
    data = json.loads((ASSETS_DIR / filename).read_text(encoding="utf-8"))
    out: list[dict] = []
    _walk(data, out)
    return out


# ── T-51 (load_dataset, interface): each dataset loads and contains its
# documented codes. Each (dataset, code, name) triple below is independently
# confirmed against the committed JSON files in this session, and six of the
# eight also appear in docs/104-site-facts.md §6b.3g's own verification
# table ("八份分類資料集對應到哪個條件").
#
# load_dataset's own return shape (`Dataset`) is not pinned by design.md
# beyond "loads from src/mcp104/assets/104-categories/, no network access", so this
# exercises it through resolve()'s specified interface rather than assuming
# an internal node/field layout: an exact-name resolution can only succeed
# if load_dataset actually parsed that code in under that name.
#
# Two of the facts document's own verification names turned out, on closer
# inspection while writing this file, to name BRANCH nodes rather than
# leaves (AreaWork.json's "新竹縣市" has 7 children; Indust.json's
# "半導體業" has 3). Using a branch name here would conflate this case with
# T-36's branch-refusal behaviour, so those two rows use a leaf child of the
# same branch instead (still confirmed against the committed file and still
# unique within its dataset), keeping this case about load_dataset content
# only.

_DOCUMENTED_NODES = [
    ("JobCat.json", "2007001004", "軟體工程師"),
    ("AreaWork.json", "8020000000", "新竹科學園區"),  # leaf under the "新竹縣市" branch
    ("Area.json", "6001006001", "新竹市"),
    ("Indust.json", "1001006002", "半導體製造業"),  # leaf under the "半導體業" branch
    ("Major.json", "3011011000", "機械工程相關"),
    ("Tool.json", "12002003001", "AutoCAD"),
    ("Skill.json", "4023001001", "國際專案管理師PMP"),
    ("Abroad.json", "7001003000", "日本"),
]


@pytest.mark.parametrize("dataset_name, code, name", _DOCUMENTED_NODES)
def test_load_dataset_contains_its_documented_codes(dataset_name, code, name):
    dataset = load_dataset(dataset_name, _DATASET_TO_CONDITION[dataset_name])
    resolution = resolve(dataset, name)
    assert _get(resolution, "code") == code


# ── T-33 (R9.1, R9.2): documented resolution trials resolve at their
# measured tier. docs/104-site-facts.md §6b.3g's "十個真實輸入" table
# records tier-1-exact and tier-3-substring hit counts but not tier-2, so
# the deciding tier below is computed directly from the committed dataset
# files using the exact three-tier algorithm design.md specifies
# (normalised exact -> prefix -> substring, first tier yielding exactly one
# match wins) -- every expected value is still derived from the datasets,
# never from tools/categories.py. See this session's report for the
# computation.

_TRIALS_RESOLVE = [
    # (query, dataset, expected code, expected tier)
    ("軟體工程師", "JobCat.json", "2007001004", TIER_EXACT),
    ("AutoCAD", "Tool.json", "12002003001", TIER_EXACT),
    ("日本", "Abroad.json", "7001003000", TIER_EXACT),
    ("機械工程", "Major.json", "3011011000", TIER_PREFIX),
    ("資訊工程", "Major.json", "3008004000", TIER_PREFIX),
]

_TRIALS_AMBIGUOUS = [
    # (query, dataset, expected candidate count)
    ("PMP", "Skill.json", 8),
    ("TOEIC", "Skill.json", 4),
    ("半導體", "Indust.json", 2),
    ("新竹", "Area.json", 15),
    ("專案管理", "Skill.json", 2),
]


@pytest.mark.parametrize(
    "query, dataset_name, expected_code, expected_tier", _TRIALS_RESOLVE
)
def test_documented_trial_resolves_at_its_measured_tier(
    query, dataset_name, expected_code, expected_tier
):
    dataset = load_dataset(dataset_name, _DATASET_TO_CONDITION[dataset_name])
    resolution = resolve(dataset, query)
    assert _get(resolution, "status") == "resolved"
    assert _get(resolution, "code") == expected_code
    assert _get(resolution, "tier") == expected_tier


@pytest.mark.parametrize(
    "query, dataset_name, expected_candidate_count", _TRIALS_AMBIGUOUS
)
def test_documented_trial_yields_candidates_not_a_resolution(
    query, dataset_name, expected_candidate_count
):
    dataset = load_dataset(dataset_name, _DATASET_TO_CONDITION[dataset_name])
    resolution = resolve(dataset, query)
    assert _get(resolution, "status") == "ambiguous"
    assert not _get(resolution, "code")
    assert len(_get(resolution, "candidates")) == expected_candidate_count


# ── T-34 (R9.3): a tier yielding several matches returns candidates, no
# resolved code.

def test_ambiguous_tier_returns_candidates_and_no_code():
    # 專案管理 hits two Skill.json nodes with the identical exact name -- a
    # leaf (4002036027) and a branch (4023001000) sharing a name, confirmed
    # against the committed file and matching §6b.3g's own account of this
    # exact pair. The textbook case a tier yielding several matches must
    # refuse rather than guess between. `status == "ambiguous"` per
    # Components §6's status table; `candidates` holds every tied hit as
    # `Candidate(code, name)` pairs, not bare codes, so a caller can choose
    # between them.
    dataset = load_dataset("Skill.json", "certificates")
    resolution = resolve(dataset, "專案管理")
    assert _get(resolution, "status") == "ambiguous"
    assert not _get(resolution, "code")
    candidate_pairs = {(c.code, c.name) for c in _get(resolution, "candidates")}
    assert {("4002036027", "專案管理"), ("4023001000", "專案管理")} <= candidate_pairs


# ── T-35 (R9.4): an exact hit with longer same-prefix names resolves and
# names them.

def test_exact_hit_with_longer_names_resolves_and_names_the_family():
    # "MCP" is an exact, unique Skill.json node (4002004004) that is also a
    # proper prefix of three longer sibling names -- MCPD (4002004015),
    # MCP+I (4002004005), MCP+SB (4002004013) -- one of the 166 true-prefix
    # families §6b.3g's precision sweep found, chosen here because it is
    # independently confirmable against the committed dataset. Refusing on
    # ambiguity would wrongly reject this common input; resolving silently
    # would hide the siblings from a caller who has no way to know they
    # exist. Per the status table, this is `status == "resolved"` with a
    # *non-empty* `candidates` -- the same field ambiguity/branch use for a
    # different reason (the caller's choice vs. the siblings they could not
    # otherwise know about).
    dataset = load_dataset("Skill.json", "certificates")
    resolution = resolve(dataset, "MCP")
    assert _get(resolution, "status") == "resolved"
    assert _get(resolution, "code") == "4002004004"
    assert _get(resolution, "tier") == TIER_EXACT
    family = {(c.code, c.name) for c in _get(resolution, "candidates")}
    assert family == {
        ("4002004015", "MCPD"),
        ("4002004005", "MCP+I"),
        ("4002004013", "MCP+SB"),
    }
    assert "4002004004" not in {c.code for c in _get(resolution, "candidates")}


# ── T-36 (R9.6): a branch name without recorded branch acceptance is
# refused with candidate leaves, never expanded.

def test_branch_name_without_recorded_branch_acceptance_is_refused_with_leaves():
    # Abroad.json serves studyAbroad and nationality. studyAbroad is
    # measured to reject a branch code outright (total unchanged from
    # baseline; §6b.3g: "留學國家...分支代碼回 0，必須用葉節點"), and
    # branch acceptance for nationality was never separately measured -- so
    # this dataset is not one where branch acceptance is recorded as
    # working. "亞洲" is a real branch node (7001000000) with 49 leaf
    # children (confirmed against the committed file), so an exact-name
    # resolution finds it -- and per design.md's Components §6 must refuse
    # rather than resolve to a branch code that would silently widen the
    # search into a multi-code query the caller did not write. Per the
    # status table this is `status == "branch"`, with `candidates` holding
    # the branch's leaf descendants as `Candidate(code, name)` pairs.
    dataset = load_dataset("Abroad.json", "studyAbroad")
    resolution = resolve(dataset, "亞洲")
    assert _get(resolution, "status") == "branch"
    assert not _get(resolution, "code")
    candidates = _get(resolution, "candidates")
    codes = {c.code for c in candidates}
    assert "7001000000" not in codes  # never the branch itself
    assert "7001003000" in codes  # 日本, one of its 49 leaves
    names_by_code = {c.code: c.name for c in candidates}
    assert names_by_code["7001003000"] == "日本"
    assert len(candidates) == 49


# ── Regression (R9.6): branch-code acceptance is a MEASURED PER-DATASET
# property, and it must be driven by configuration, not asserted from one
# sampled dataset in each direction. A defect shipped past the full suite
# because branch acceptance was hard-coded to a single dataset (only
# JobCat.json accepted), silently refusing two datasets independently
# measured to accept -- meaning `search_resumes(filters={"city":
# "新竹縣市"}))` (searching by work location -- among the most ordinary
# queries this tool serves) could not be expressed at all. The test above
# (T-36) only ever exercised the REFUSE side of this mechanism
# (Abroad.json/"亞洲"); nothing in this file exercised the ACCEPT side, and
# the defect lived exactly in that gap. This drives every dataset in the
# branch-acceptance table, from both directions, so a future change that
# flips any one dataset's verdict cannot pass silently.
#
# Each row's source (docs/104-site-facts.md §6b.3g), and — deliberately —
# which rows are *measured* versus *defaulted*:
#
#   JobCat.json   ACCEPT (measured) — jobcat=2010001000 (操作／技術類人員,
#     40 leaves) echoes verbatim ["2010001000"]; total 532,064 -> 60,176
#     (the branch filter is genuinely applied, not merely accepted as a
#     no-op value).
#   AreaWork.json ACCEPT (measured) — city=6001006000 (新竹縣市) -> 148,326,
#     echo ["6001006000"] verbatim.
#   Area.json     ACCEPT (measured) — home=6001006000 (新竹縣市 — same code
#     and name as AreaWork.json's node; a different dataset that happens to
#     share this taxonomy entry) -> 50,942, echo ["6001006000"] verbatim.
#   Abroad.json   REFUSE (measured) — studyAbroad=<branch> -> total
#     unchanged from baseline (0 rows); leaf (台灣) -> 503,756. "與 jobcat
#     相反 — 留學國家不接受分支代碼."
#   Tool.json     REFUSE (measured) — goodTools=<branch> -> total unchanged
#     (0 rows); leaf (AIX) -> 539. The exact branch code used in that
#     measurement is not recorded in the facts document, so "作業系統類"
#     (AIX's real parent in the committed file, confirmed independently in
#     this session) exercises the dataset-level refuse verdict; it is not
#     claimed to be the literally-tested node.
#   Indust.json, Major.json, Skill.json — NEVER MEASURED for branch
#     acceptance. Per Components §6, "where branch acceptance is not
#     recorded, a branch name is refused" — a DEFAULT, not a finding. Rows
#     are marked as such so that a future measurement adding one of these
#     to the accept list changes this test on purpose, rather than the
#     change reading as an unexplained regression.

_BRANCH_ACCEPTANCE_FIXTURES = [
    # (dataset_file, condition_key, branch_name, branch_code, expected_status, provenance)
    # condition_key is the condition each measurement below was actually taken
    # under (Round I9: branch acceptance is per (dataset, condition) pair, not per
    # dataset file alone -- see _DATASET_TO_CONDITION's docstring above).
    ("JobCat.json", "jobcat", "操作／技術類人員", "2010001000", "resolved", "measured: accept"),
    ("AreaWork.json", "city", "新竹縣市", "6001006000", "resolved", "measured: accept"),
    ("Area.json", "home", "新竹縣市", "6001006000", "resolved", "measured: accept"),
    ("Abroad.json", "studyAbroad", "亞洲", "7001000000", "branch", "measured: refuse"),
    ("Tool.json", "goodTools", "作業系統類", "12001001000", "branch", "measured: refuse"),
    ("Indust.json", "workExpInd", "半導體業", "1001006000", "branch", "default: never measured"),
    ("Major.json", "major", "教育學科類", "3001000000", "branch", "default: never measured"),
    ("Skill.json", "certificates", "語言類", "4001000000", "branch", "default: never measured"),
]


@pytest.mark.parametrize(
    "dataset_name, condition_key, branch_name, branch_code, expected_status, provenance",
    _BRANCH_ACCEPTANCE_FIXTURES,
)
def test_branch_acceptance_is_driven_by_per_dataset_configuration(
    dataset_name, condition_key, branch_name, branch_code, expected_status, provenance
):
    dataset = load_dataset(dataset_name, condition_key)
    resolution = resolve(dataset, branch_name)
    assert _get(resolution, "status") == expected_status, (
        f"{dataset_name}'s branch-code verdict for {branch_name!r} regressed "
        f"({provenance}): expected status={expected_status!r}, got "
        f"{_get(resolution, 'status')!r}"
    )
    if expected_status == "resolved":
        # ACCEPT: the branch resolves to its own code, exactly as any other
        # exact-tier hit would -- not refused, not silently expanded.
        assert _get(resolution, "code") == branch_code
    else:
        # REFUSE (measured or defaulted): no code, and the branch's own
        # code is never handed back disguised as a candidate -- candidate
        # leaves are offered instead of a silent empty refusal.
        assert not _get(resolution, "code")
        codes = {c.code for c in _get(resolution, "candidates")}
        assert branch_code not in codes
        assert codes


# ── T-50 (resolve, interface): reports code, deciding tier and family
# together, in the plain case where there is no family.

def test_resolve_reports_code_tier_and_empty_family_together():
    dataset = load_dataset("JobCat.json", "jobcat")
    resolution = resolve(dataset, "軟體工程師")
    assert _get(resolution, "status") == "resolved"
    assert _get(resolution, "code") == "2007001004"
    assert _get(resolution, "tier") == TIER_EXACT
    assert list(_get(resolution, "candidates")) == []


# ── T-52 (resolve, interface): precision sweep over every node of every
# bundled dataset. Oracle: each node's own name is ground truth for what it
# should resolve back to. This drives resolve() itself, not a
# hand-reproduced algorithm, so a rule change that damages precision fails
# here rather than being silently re-baselined. Per design.md's Testing
# Strategy, the sweep's headline "misresolution rate" needs a
# population-size caveat to state correctly (§6b.3g already carries that
# caveat for its own run); this test instead asserts the property the
# design commits to: prefix and substring tiers misresolve zero times.
#
# The input-generation method mirrors research/probes/sweep_resolution_
# precision.py (read for method only; this test never reads
# tools/categories.py and never depends on research/captures/, which is
# excluded from version control).

_DECOR_SUFFIXES = ("相關", "業", "類", "人員", "相關業")
_PAREN = re.compile(r"[(（][^)）]*[)）]")
_MIN_LEN = 3


def _plausible_inputs(name: str) -> set[str]:
    """What a caller types when they mean this node: the parenthetical
    gloss stripped, a decoration suffix stripped, or the last character
    dropped."""
    base = name.strip()
    got: set[str] = set()
    stripped = _PAREN.sub("", base).strip()
    if stripped and stripped != base:
        got.add(stripped)
    for suf in _DECOR_SUFFIXES:
        for cand in (base, stripped):
            if cand.endswith(suf) and len(cand) > len(suf):
                got.add(cand[: -len(suf)].strip())
    for cand in (base, stripped):
        if len(cand) >= _MIN_LEN + 1:
            got.add(cand[:-1])
    return {g for g in got if len(g) >= _MIN_LEN and g != base}


# ── T-66 (resolve, interface): a direct code match, tried BEFORE any name tier and
# reported as tier="code" — this arrived as a review fix rather than through the case
# table, so nothing pinned it until now. Three behaviours the change exists for:

TIER_CODE = "code"


def test_code_resolves_directly_and_reports_tier_code():
    # Reuses the (dataset, code, name) triple T-51 already independently confirms
    # against the committed JSON file. Passing the CODE straight to resolve() — never
    # the name — must land on the same node and report the fourth tier value.
    dataset = load_dataset("JobCat.json", "jobcat")
    resolution = resolve(dataset, "2007001004")
    assert _get(resolution, "status") == "resolved"
    assert _get(resolution, "code") == "2007001004"
    assert _get(resolution, "tier") == TIER_CODE


def test_branch_code_is_refused_identically_to_the_branch_name():
    # The one thing that matters most: the code route must not become a back door
    # around branch refusal. Abroad.json is measured to refuse a branch code applied
    # through the ordinary filter path (see _BRANCH_ACCEPTANCE_FIXTURES above), and
    # "亞洲" (7001000000) is the same branch node T-36 already drives by name.
    # Resolving by the branch's own CODE must refuse the same way — same status, same
    # candidate leaves, no code handed back — not merely "also refuse" with some
    # different outcome.
    dataset = load_dataset("Abroad.json", "studyAbroad")
    by_name = resolve(dataset, "亞洲")
    by_code = resolve(dataset, "7001000000")

    assert _get(by_name, "status") == "branch"
    assert _get(by_code, "status") == "branch"
    assert not _get(by_code, "code")
    assert not _get(by_code, "tier")

    name_candidates = {(c.code, c.name) for c in _get(by_name, "candidates")}
    code_candidates_set = {(c.code, c.name) for c in _get(by_code, "candidates")}
    assert code_candidates_set == name_candidates
    assert "7001000000" not in {code for code, _name in code_candidates_set}  # never the branch itself


def test_candidate_from_an_ambiguous_refusal_resolves_when_passed_back():
    # The round trip the code tier exists for: an ambiguous refusal hands the caller
    # {code, name} candidates, and the caller's natural next move is to pass the
    # chosen code straight back. Without this tier that follow-up call would fail
    # against a name index, making the candidate list decorative: it would tell the
    # caller what to choose and then reject the choice.
    #
    # "TOEIC" against Skill.json is T-33's own ambiguous fixture (4 tied exact-name
    # hits, independently confirmed against the committed file) — reused rather than
    # re-derived, and deliberately NOT "專案管理" (T-34's fixture): one of that tie's
    # two hits is itself a branch node, and per this same design a branch candidate's
    # code correctly refuses again on round trip (branch refusal is not bypassable via
    # code either) rather than "resolving" — a different, already-covered behaviour
    # (see test_branch_code_is_refused_identically_to_the_branch_name above), not this
    # case's round-trip claim. Every one of "TOEIC"'s four candidates is a leaf, so
    # every one is expected to resolve.
    dataset = load_dataset("Skill.json", "certificates")
    ambiguous = resolve(dataset, "TOEIC")
    assert _get(ambiguous, "status") == "ambiguous"
    candidates = _get(ambiguous, "candidates")
    assert len(candidates) == 4

    for candidate in candidates:
        round_tripped = resolve(dataset, candidate.code)
        assert _get(round_tripped, "status") == "resolved", (
            f"candidate {candidate!r}, returned by an ambiguous refusal, did not "
            f"resolve when passed straight back"
        )
        assert _get(round_tripped, "code") == candidate.code
        assert _get(round_tripped, "tier") == TIER_CODE


# T-66's two premises, worth pinning cheaply since the code tier's safety rests on
# them: across all eight bundled datasets, no node name is all-digits (so a code
# lookup shadows no name) and no code is duplicated within a dataset (so the code
# index loses nothing). Independent oracle: this file's own _load_raw_nodes JSON
# walker, never load_dataset/resolve.

def test_no_dataset_node_name_is_all_digits_and_no_code_is_duplicated_within_a_dataset():
    for filename in _ALL_DATASET_FILES:
        nodes = _load_raw_nodes(filename)
        names = [n["des"] for n in nodes]
        codes = [n["no"] for n in nodes]

        all_digit_names = [name for name in names if name.strip().isdigit()]
        assert all_digit_names == [], (
            f"{filename}: an all-digit node name would be shadowed by the code tier "
            f"(resolve() checks codes before any name tier runs): {all_digit_names}"
        )

        seen: set = set()
        duplicates: set = set()
        for code in codes:
            if code in seen:
                duplicates.add(code)
            seen.add(code)
        assert not duplicates, (
            f"{filename}: a duplicated code would make nodes_by_code lose a node "
            f"(later duplicate silently wins): {duplicates}"
        )


# ── T-69 (R9.7, R9.8): every candidate's `terminal` flag matches what passing its own
# code back into resolve() actually does, swept over every node of every bundled
# dataset (via each unique raw name, which resolve() folds every tied node under).
#
# Round I5's own finding is the brief for this case: T-68 already catches the field
# being discarded at the tool boundary using one concrete instance; this case catches a
# DIFFERENT possible defect -- the flag surviving to the boundary but being WRONG for
# some node the one concrete instance does not exercise. The oracle is the round trip
# itself, exactly as design.md specifies: "take the stated flag, pass the code back,
# compare against what happens — never the rule that produced the flag." Re-deriving
# the branch/tie refusal rule here to predict the expected boolean would make this test
# unable to fail when THAT rule is wrong, which is precisely the gap the brief warns
# against reproducing.

def _round_trip_resolves(dataset, code: str) -> bool:
    return _get(resolve(dataset, code), "status") == "resolved"


def test_every_candidates_terminal_flag_matches_its_own_round_trip():
    # Round I9: branch-code acceptance (and therefore `terminal`) is now measured per
    # (dataset, condition) pair, not per dataset file -- the report that landed this
    # round found jobcat ACCEPTS a branch code on JobCat.json that workExpJob, the
    # same file, REFUSES. A sweep keyed only by dataset file would load exactly one
    # of those two acceptance configurations and silently never exercise the other,
    # missing precisely the divergence this property exists to catch. So this drives
    # every (dataset, condition) pair `_ALL_DATASET_CONDITION_PAIRS` lists, loading
    # (and round-tripping) the dataset under the SAME condition each time -- the
    # round trip is only well-defined when both legs agree on which condition is
    # asking.
    checked_ambiguous = 0
    checked_branch = 0
    checked_family = 0
    mismatches = []

    for filename, condition_key in _ALL_DATASET_CONDITION_PAIRS:
        dataset = load_dataset(filename, condition_key)
        raw_nodes = _load_raw_nodes(filename)
        unique_names = sorted({node["des"] for node in raw_nodes})

        for name in unique_names:
            resolution = resolve(dataset, name)
            status = _get(resolution, "status")
            candidates = _get(resolution, "candidates") if status in ("ambiguous", "branch", "resolved") else ()

            if status == "ambiguous":
                checked_ambiguous += len(candidates)
            elif status == "branch":
                checked_branch += len(candidates)
            elif status == "resolved":
                checked_family += len(candidates)  # the prefix-family list; often empty

            for candidate in candidates:
                expected = _round_trip_resolves(dataset, candidate.code)
                if candidate.terminal != expected:
                    mismatches.append((filename, condition_key, status, name, candidate, expected))

    # Sanity on the sweep itself: all three paths must actually have been exercised,
    # or a zero count below would mean this test silently checked nothing on that path.
    assert checked_ambiguous > 0, "sweep never hit an ambiguous tie -- coverage gap in the sweep itself"
    assert checked_branch > 0, "sweep never hit a branch refusal -- coverage gap in the sweep itself"
    assert checked_family > 0, "sweep never hit a prefix family -- coverage gap in the sweep itself"

    assert mismatches == [], (
        f"{len(mismatches)} terminal-flag mismatches against their own round trip "
        f"(dataset, condition, path, query, candidate, actual_round_trip_resolves): "
        f"{mismatches[:10]}"
    )


def test_prefix_and_substring_tiers_never_misresolve():
    # This property (a prefix/substring hit's code always matches the node that
    # actually owns it) doesn't depend on branch-acceptance specifics -- a
    # misresolution would be wrong regardless of which of a dataset's conditions is
    # asking -- so one representative condition per dataset file is sufficient; see
    # test_every_candidates_terminal_flag_matches_its_own_round_trip above for the
    # sweep that DOES need every (dataset, condition) pair.
    misresolutions = []
    for filename in _ALL_DATASET_FILES:
        dataset = load_dataset(filename, _DATASET_TO_CONDITION[filename])
        nodes = _load_raw_nodes(filename)
        for node in nodes:
            origin_code = node["no"]
            origin_name = node["des"]
            for query in _plausible_inputs(origin_name):
                resolution = resolve(dataset, query)
                tier = _get(resolution, "tier")
                code = _get(resolution, "code")
                if code and tier in (TIER_PREFIX, TIER_SUBSTRING) and code != origin_code:
                    misresolutions.append(
                        (filename, query, origin_name, origin_code, code, tier)
                    )
    assert misresolutions == [], (
        f"{len(misresolutions)} prefix/substring misresolutions "
        f"(dataset, query, meant_name, meant_code, got_code, tier): "
        f"{misresolutions[:10]}"
    )


# ── load_dataset interface pin (Round I9): `condition_key` is required, no default ──
#
# design.md: acceptance is recorded per (dataset, condition) pair specifically so an
# unaware call site cannot silently inherit a measured acceptance. A default value
# would reopen exactly that hole -- any caller who forgot to pass a condition would
# get SOME condition's acceptance table by accident rather than an error. This pins
# that the parameter has no default, not merely that today's call sites happen to
# supply one.

def test_load_dataset_requires_condition_key_with_no_default():
    with pytest.raises(TypeError):
        load_dataset("JobCat.json")  # type: ignore[call-arg]


# ── T-75 (R9.6): a condition newly bound to a dataset does NOT inherit a branch
# acceptance measured for a DIFFERENT condition on that dataset; a measured refusal
# DOES apply consistently to conditions sharing a dataset ───────────────────────────
#
# design.md: "acceptance is recorded per (dataset, condition) pair and is never
# inherited by a condition newly bound to a dataset; a refusal, being the
# conservative answer, transfers freely." The asymmetry is deliberate and is what
# this case protects, not incidentally exercises: inheriting a refusal costs a
# caller one extra turn (recoverable); inheriting an acceptance would submit a
# branch code that datasets measured to refuse it answer with zero rows -- reaching
# a caller as "no candidates found", the silent wrong answer the whole design is
# organised against. A test that only checked "the two conditions differ" would
# pass an implementation that leaked the acceptance the OTHER way (both conditions
# accept) as long as something, anywhere, differed -- the direction has to be
# pinned, not just the fact of divergence.
#
# THE RULE THIS FILE HAS NOW PROVED FOUR TIMES IN ONE SESSION: a test may assert
# that its oracle ran; it may not assert that the world still contains an example.
# The first survives measurement, the second is hostage to it. Earlier instances:
# `assert no_observation` (test_filters.py), the dataset-backed-key count
# (test_search.py), T-27's `military` example (test_filters.py). This one: this
# test originally hard-required a REAL diverging (dataset, condition) pair to
# exist, with JobCat.json's jobcat/workExpJob as the one instance -- and
# workExpJob was then measured to accept branch codes on its own wire parameter,
# closing that instance. A GOOD state (more of the surface measured), not a
# coverage gap -- and the property under test ("a condition newly bound to a
# dataset does not inherit another's acceptance") is about non-inheritance as a
# MECHANISM, not about any particular pair diverging today.
#
# So: discover a real diverging pair structurally first (never hard-coded to
# jobcat/workExpJob -- every dataset file design.md's Bundled datasets table binds
# to more than one condition is checked, using one of that dataset's own branch
# nodes). If one exists, use it -- the strongest form. If none does, prove the
# mechanism SYNTHETICALLY: clone a real, currently-loaded `Dataset` (measured to
# accept) with its `accepts_branch_codes` field flipped, simulating a sibling
# condition on the identical file that has not earned acceptance. `resolve()`
# reads that field on whatever `Dataset` object it is handed, so this drives the
# REAL refusal path inside `resolve()`/`_finalise()` -- only the acceptance FLAG
# is synthetic, never the refusal logic itself.

_MULTI_CONDITION_DATASETS = {
    "JobCat.json": ("jobcat", "workExpJob"),
    "Indust.json": ("workExpInd", "expectInd"),
    "Abroad.json": ("studyAbroad", "nationality"),
}


def _branch_acceptance_by_condition(filename, conditions):
    """For one dataset file bound to multiple conditions, resolve the SAME branch
    node's own code under each condition and report each one's status alongside
    the `Dataset` object it was resolved against. Returns (branch_node,
    {label: (dataset, status)}) -- observing resolve()'s own answer per condition,
    never re-deriving the acceptance rule to predict it."""
    datasets = {c: load_dataset(filename, c) for c in conditions}
    any_dataset = next(iter(datasets.values()))
    branch = next((n for n in any_dataset.nodes if n.children), None)
    assert branch is not None, f"{filename}: expected at least one branch node in its tree"
    outcomes = {
        condition_key: (dataset, _get(resolve(dataset, branch.code), "status"))
        for condition_key, dataset in datasets.items()
    }
    return branch, outcomes


def test_branch_acceptance_is_not_inherited_by_a_newly_bound_condition():
    diverging = None
    consistent = None
    for filename, conditions in _MULTI_CONDITION_DATASETS.items():
        branch, outcomes = _branch_acceptance_by_condition(filename, conditions)
        distinct_statuses = {status for _dataset, status in outcomes.values()}
        if len(distinct_statuses) > 1 and diverging is None:
            diverging = (filename, branch, outcomes)
        if len(distinct_statuses) == 1 and consistent is None:
            consistent = (filename, branch, outcomes)

    assert consistent is not None, (
        "sanity: expected at least one dataset whose bound conditions agree on "
        "branch-code acceptance -- needed to show refusal (or consistent "
        "acceptance) transferring is possible at all, as a contrast to the "
        "diverging case below"
    )

    if diverging is not None:
        source_label, branch, outcomes = diverging
    else:
        # No real (dataset, condition) pair currently diverges -- see the module
        # comment above for why that is a good state, not a gap. Prove the
        # mechanism synthetically instead of requiring a real example to persist.
        accepting_dataset = load_dataset("JobCat.json", "jobcat")
        assert accepting_dataset.accepts_branch_codes is True, (
            "sanity: jobcat must currently be measured to accept branch codes for "
            "this synthetic contrast to mean anything"
        )
        branch = next(n for n in accepting_dataset.nodes if n.children)
        refusing_dataset = accepting_dataset._replace(accepts_branch_codes=False)
        outcomes = {
            "jobcat (real, measured accept)": (
                accepting_dataset, _get(resolve(accepting_dataset, branch.code), "status")
            ),
            "synthetic sibling (accepts_branch_codes flipped to False)": (
                refusing_dataset, _get(resolve(refusing_dataset, branch.code), "status")
            ),
        }
        source_label = "JobCat.json (synthetic: no real pair diverges today)"

    # Claim 1 (the one that matters most): the diverging pair's ACCEPTING condition
    # must stay resolved, and every OTHER, non-accepting condition on that same
    # dataset must REFUSE the identical branch code -- never silently resolve it
    # just because a sibling condition accepts it.
    accepting = [label for label, (_ds, status) in outcomes.items() if status == "resolved"]
    refusing = [label for label, (_ds, status) in outcomes.items() if status == "branch"]
    assert accepting and refusing, (
        f"{source_label}: expected at least one accepting and one refusing "
        f"condition among {sorted(outcomes)} for branch code {branch.code!r}, got "
        f"{ {k: s for k, (_d, s) in outcomes.items()} }"
    )
    for label in refusing:
        dataset, _status = outcomes[label]
        resolution = resolve(dataset, branch.code)
        assert _get(resolution, "status") == "branch", (
            f"{source_label}/{label}: must refuse branch code {branch.code!r} even "
            f"though {accepting} accepts it on the same dataset file"
        )
        assert not _get(resolution, "code"), (
            f"{source_label}/{label}: a branch code must never be handed back as "
            f"resolved just because a DIFFERENT condition on the same dataset "
            f"accepts it"
        )
        leaf_codes = {c.code for c in _get(resolution, "candidates")}
        assert branch.code not in leaf_codes  # never the branch itself, disguised as a leaf
        assert leaf_codes, f"{source_label}/{label}: refusal must still offer leaves"

    # Claim 2: the consistent pair's conditions must agree WITH EACH OTHER -- "a
    # refusal transfers freely" claims agreement, not a specific direction; if a
    # future consistent pair happened to be mutually-ACCEPTING instead, both sides
    # resolving to the identical code is the matching assertion, checked generically
    # below rather than assuming today's refuse/refuse outcome is the only valid one.
    # (Always a REAL discovered pair -- never synthetic; only Claim 1's diverging
    # case needed a synthetic fallback.)
    filename2, branch2, outcomes2 = consistent
    shared_status = next(iter({status for _ds, status in outcomes2.values()}))
    if shared_status == "branch":
        for condition_key, (dataset, _status) in outcomes2.items():
            resolution = resolve(dataset, branch2.code)
            assert _get(resolution, "status") == "branch"
            assert not _get(resolution, "code")
    else:
        codes = set()
        for condition_key, (dataset, _status) in outcomes2.items():
            resolution = resolve(dataset, branch2.code)
            assert _get(resolution, "status") == "resolved"
            codes.add(_get(resolution, "code"))
        assert len(codes) == 1, (
            f"{filename2}: conditions {sorted(outcomes2)} both resolved branch code "
            f"{branch2.code!r} but to DIFFERENT codes ({codes}), which is not "
            f"'agreement' in any useful sense"
        )


# ── children()/path() — added for browse_filter_values (tools/discovery.py). Both take
# an already-loaded Dataset, never a bare filename (a filename-typed signature would
# have no condition to compute `terminal` for and would silently revert it to
# file-keying — the one thing branch-acceptance measurement in this module refuses to
# do, see _BRANCH_ACCEPTANCE's own comment). ─────────────────────────────────────────

def test_children_root_layer_matches_the_committed_json_top_level_order():
    """`children(dataset, code=None)` -- the root layer -- must return exactly the
    dataset's top-level array entries, IN ORDER. Independent oracle: this file's own
    `_load_raw_nodes`/`_walk` never used, so the raw top-level array is read directly
    here instead, never through `_load_tree`'s (postorder) flat list."""
    for filename in _ALL_DATASET_FILES:
        raw_top_level = json.loads((ASSETS_DIR / filename).read_text(encoding="utf-8"))
        expected_codes = [str(item["no"]) for item in raw_top_level]
        assert expected_codes, f"sanity: {filename} has no top-level entries at all"

        dataset = load_dataset(filename, _DATASET_TO_CONDITION[filename])
        roots = children(dataset, None)
        actual_codes = [node.code for node in roots]
        assert actual_codes == expected_codes, (
            f"{filename}: children(dataset, None) does not match the committed "
            f"file's own top-level order"
        )
        for node in roots:
            assert node.has_children == bool(
                next(i for i in raw_top_level if str(i["no"]) == node.code).get("n")
            )


def test_children_of_a_branch_matches_its_own_n_array_in_order():
    """A branch node's own children, drilled down one layer -- independent oracle is
    the same raw JSON's own `n` array for that node."""
    dataset = load_dataset("JobCat.json", "jobcat")
    raw = json.loads((ASSETS_DIR / "JobCat.json").read_text(encoding="utf-8"))

    def find(items, code):
        for item in items:
            if str(item["no"]) == code:
                return item
            found = find(item.get("n") or [], code)
            if found is not None:
                return found
        return None

    branch_code = "2010001000"  # 操作／技術類人員 -- a known branch, per filters.py's own
    # certifying_total citation for `jobcat` (branch-code acceptance, 60,176)
    raw_branch = find(raw, branch_code)
    assert raw_branch is not None, "sanity: the known branch code moved in the fixture"
    expected_child_codes = [str(c["no"]) for c in raw_branch.get("n") or []]
    assert expected_child_codes, "sanity: the known branch has no children in the fixture"

    kids = children(dataset, branch_code)
    assert [n.code for n in kids] == expected_child_codes


def test_children_of_a_leaf_is_empty_and_children_of_unknown_code_raises():
    dataset = load_dataset("JobCat.json", "jobcat")
    leaf_code = "2007001004"  # 軟體工程師, per _DOCUMENTED_NODES above -- a leaf
    kids = children(dataset, leaf_code)
    assert kids == ()

    with pytest.raises(KeyError):
        children(dataset, "NO-SUCH-CODE-99999999")


def test_path_root_to_leaf_is_inclusive_and_ordered_root_first():
    dataset = load_dataset("JobCat.json", "jobcat")
    leaf_code = "2007001004"  # 軟體工程師
    chain = path(dataset, leaf_code)

    assert chain[-1].code == leaf_code, "path() must end at the requested code, inclusive"
    assert len(chain) >= 2, (
        "sanity: 軟體工程師 must sit at least one layer below the root for this to be "
        "a meaningful ordering check"
    )
    # Every node in the chain (after the first) must be a child of the one before it --
    # cross-checked against children(), not re-derived from path()'s own internals.
    for parent_node, child_node in zip(chain, chain[1:]):
        child_codes = {c.code for c in children(dataset, parent_node.code)}
        assert child_node.code in child_codes, (
            f"path() claims {child_node.code} is a child of {parent_node.code}, but "
            f"children() disagrees"
        )
    with pytest.raises(KeyError):
        path(dataset, "NO-SUCH-CODE-99999999")


def test_path_at_the_root_layer_is_a_single_element_chain():
    dataset = load_dataset("JobCat.json", "jobcat")
    roots = children(dataset, None)
    root_code = roots[0].code
    chain = path(dataset, root_code)
    assert [n.code for n in chain] == [root_code]


@pytest.mark.parametrize("filename, condition_key", _ALL_DATASET_CONDITION_PAIRS)
def test_children_terminal_agrees_with_resolve_for_every_dataset_condition_pair(filename, condition_key):
    """T-69-style sweep, extended to children(): a node's reported `terminal` must
    agree with what resolve() actually does when that same code is submitted back --
    covering BOTH JobCat.json conditions (jobcat/workExpJob), which are measured to
    disagree on branch-code acceptance, so a file-keyed (rather than
    (file, condition)-keyed) implementation would pass one and fail the other."""
    dataset = load_dataset(filename, condition_key)
    roots = children(dataset, None)
    assert roots, f"sanity: {filename} has no root layer at all"
    checked_a_branch = False
    for node in roots:
        resolution = resolve(dataset, node.code)
        resolved = _get(resolution, "status") == "resolved"
        assert node.terminal == resolved, (
            f"{filename}/{condition_key}: children() reports terminal={node.terminal} "
            f"for {node.code}, but resolve() {'resolved' if resolved else 'refused'} it"
        )
        if node.has_children:
            checked_a_branch = True
    assert checked_a_branch, (
        f"sanity: {filename}'s root layer has no branch node at all, so this sweep "
        f"never actually exercised the terminal-vs-branch distinction"
    )
