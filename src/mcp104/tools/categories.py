"""Resolve a caller-supplied category name to 104's own code, offline.

Answers: given a dataset (one of the eight files bundled under
`src/mcp104/assets/104-categories/`) and a name a caller typed, which of 104's category
codes did they mean — and how confident should we be in that answer?

Deliberately does not: touch the network, read `config.py`, or know anything about
`guarded_api` or the request throttle. `tools/filters.py`'s own condition
table owns the mapping from a filter *condition* to which dataset backs it; this module
only knows how to load a named dataset file and resolve a query against it, with no
knowledge of which filter keys exist. The datasets are public,
unauthenticated 104 taxonomy data — see `src/mcp104/assets/104-categories/README.md` — so
loading them is local file I/O only, never a request, and is not routed through the guard
or the throttle (steering/structure.md and steering/tech.md both record this explicitly,
because someone will otherwise wire them in "for consistency").

104's category names are decorated (suffixes like "相關"/"業", parenthetical glosses,
embedded abbreviations) `[M docs/104-site-facts.md §6b.3g]`. Exact matching alone
therefore refuses most natural input. Resolution proceeds through three tiers on the
*normalised* name — exact, prefix, substring — and the first tier producing exactly one
match wins. No tier ever picks among several matches: a tier yielding more than one hit
returns those hits as candidates and resolves nothing, because a wrong code returns a
plausible-looking result set with no signal that it is wrong (product.md principle 2).

`[M §6b.3g]` results referenced below (2026-08-14, offline, zero requests, over every
node of every bundled dataset): a precision sweep found 22 misresolutions in 6,302
trials, all 22 at the exact tier via "proper-prefix name families" (a shorter node name
that is itself a valid, distinct node, and is also a strict prefix of a longer node's
name — e.g. `CCI` / `CCIE`) — zero misresolutions at the prefix or substring tiers. That
is the opposite of the intuition that exact matching is inherently safe and fuzzier
tiers are where wrong-but-unique hits happen; it is why an exact hit that is also a
proper prefix of other names still surfaces those names as warnings instead of resolving
silently.
"""
from __future__ import annotations

import json
import unicodedata
from functools import lru_cache
from importlib.resources import files
from typing import NamedTuple

# Bundled datasets live here, not under data/ (the runtime persistence directory whose
# ignore rules cover data files) — see src/mcp104/assets/104-categories/README.md.
# `files("mcp104")` addresses the INSTALLED package tree (chained `/`, `.is_file()`
# verified to work on the container interpreter, Python 3.10.12), not the source tree
# by repo-root arithmetic: production code must read what actually shipped, which for a
# non-editable install is under site-packages, not this file's own directory.
ASSETS_DIR = files("mcp104") / "assets" / "104-categories"

# Branch-code acceptance is measured per (dataset FILE, CONDITION) pair, not per file
# alone, and not inferred from either half on its own [M §6b.3g, §6b.3i-2].
# `_BRANCH_ACCEPTANCE` distinguishes three states a single accepting-set cannot:
# "measured to accept" (True), "measured to refuse" (False), and "never measured"
# (absent — `.get()` in `load_dataset` below defaults it to False, but that default is
# a want-of-a-measurement placeholder, not a finding, and must not be written or read
# as one). Collapsing the first two into "not in the accepting set" is exactly the
# defect that once made this resolver refuse `city=新竹縣市` — the most ordinary
# work-location search there is — despite the accept being sitting in the facts
# document the whole time: a single accepting-set has no way to say "I checked and it
# refuses" versus "I never checked", so every unmeasured (file, condition) pair
# silently inherited a rejection some of them never earned.
#
# **Why the key is a PAIR, not the file alone.** `JobCat.json` now backs two
# conditions — `jobcat` and, since it was bound here, `workExpJob` — and its accept was
# measured by submitting a branch code to `jobcat` specifically. A file-keyed lookup
# would hand that acceptance to `workExpJob` too, on evidence collected for a different
# wire parameter. The direction of that mistake is what makes it dangerous rather than
# merely imprecise: inheriting a *refusal* costs a caller one refusal and a list of
# leaves (recoverable, and the conservative direction — see below); inheriting an
# *acceptance* submits a branch code that other datasets are measured to answer with
# ZERO rows, which reaches a caller as "no candidates found" — indistinguishable from a
# real empty result, and exactly the outcome the certification criterion elsewhere in
# this project refuses to read in either direction because it cannot tell "applied,
# nobody matches" from "the code was never valid." So this table never lets a
# newly-bound condition inherit an acceptance measured for a different condition on the
# same file — each `(file, condition)` pair earns its own `True` independently, and
# only a `False` (or the same want-of-measurement default) is safe to leave implicit.
#
# Measured to ACCEPT a branch code (applied, not silently ignored):
#   - (JobCat.json, jobcat): jobcat=2010001000（操作／技術類人員，a branch）→ verbatim
#     echo ["2010001000"], total 532,064 → 60,176 (2026-08-13). Measured for `jobcat`
#     ONLY, and stayed that way even after `workExpJob` was bound to this same file —
#     see the next entry, earned independently rather than inherited from this one.
#   - (JobCat.json, workExpJob): the same branch, 2010001000, submitted through
#     `workExpJob` instead — echo ['2010001000'], baseline 531,943 → 73,304
#     (2026-08-14, live session; Phase 6 task 3). A fall of 458,639 against a
#     same-session drift band under five, to a non-zero total, certifies under this
#     project's certification criterion (a fall clearing the drift band by orders of
#     magnitude, landing on a non-zero total). Measured, not inherited from `jobcat`'s
#     entry above — the pairing exists precisely so one condition's acceptance is
#     never assumed for another sharing the same file, and this entry is the result of
#     actually running that check for this pair, not an exception to the rule.
#     **The `jobcat` run through the identical branch code is the control**, kept for
#     exactly this reason: 60,176 vs. 73,304 — two different totals from one branch
#     code through the two parameters — is what "shared code space, different
#     questions" (`jobcat`'s own row in tools/filters.py has the fuller account) should
#     look like; identical totals would have meant one parameter was being silently
#     ignored rather than answering its own question.
#     **What this measurement shows and does not show.** It shows the parameter
#     accepts the branch code, echoes it verbatim, and the total moves — parsed and
#     applied, by the same evidence standard `jobcat`'s own branch acceptance was
#     held to. It does not separately verify that the branch expands to the
#     semantically correct subtree of work-history categories; that was never checked
#     for `jobcat` either, so this is consistent with the existing standard, not a new
#     gap introduced alongside it.
#   - (AreaWork.json, city): city=6001006000（新竹縣市，a branch）→ echo
#     ["6001006000"], total → 148,326; two branches comma-joined
#     (6001006000,6001005000) → 211,579 (2026-08-13/14). AreaWork.json's top level *is*
#     the cities/counties a caller means by "city" — its children are industrial parks,
#     a narrower and categorically different thing, not a more-precise version of the
#     city. Refusing the branch here does not push a caller toward precision, it makes
#     searching by city impossible.
#   - (Area.json, home): home=6001006000（新竹縣市，a branch）→ total 50,942
#     (2026-08-13). Same tree shape as AreaWork.json (city/county branch,
#     industrial-park children).
#
# Measured to REFUSE a branch code (silently returns zero rows, no error — a measurement,
# not an absence of one):
#   - (Abroad.json, studyAbroad): branch code → total 0; leaf (台灣) → 503,756
#     (2026-08-14 復驗). `nationality` shares this file but NOT this entry — its own
#     branch behaviour has never been submitted and is left to the unmeasured default
#     (which happens to produce the identical refuse behaviour here, for a different,
#     honest reason: absence of evidence, not a measurement of its own).
#   - (Tool.json, goodTools): branch code → total 0; leaf (AIX) → 539 (2026-08-14 復驗).
#
# Never measured with a branch code, and therefore absent from this table entirely (the
# default applies): (Indust.json, workExpInd), (Indust.json, expectInd), (Major.json,
# major), (Skill.json, certificates), (Abroad.json, nationality). Refusing is the
# correct *default* for these five specifically because no measurement exists, not
# because one showed a refusal — a measurement showing acceptance for any one of them
# changes only that one entry, the same way this structure already changed for
# `AreaWork.json`/`Area.json` and, now, `(JobCat.json, workExpJob)`, without touching
# the others. A future condition newly bound to any of these files starts here too —
# at refuse-until-measured, never at whatever its file-mate happens to be.
_BRANCH_ACCEPTANCE: dict[tuple[str, str], bool] = {
    ("JobCat.json", "jobcat"): True,
    ("JobCat.json", "workExpJob"): True,
    ("AreaWork.json", "city"): True,
    ("Area.json", "home"): True,
    ("Abroad.json", "studyAbroad"): False,
    ("Tool.json", "goodTools"): False,
}


class Candidate(NamedTuple):
    """One node offered back to the caller instead of a silent guess.

    `terminal` marks whether passing `code` straight back into `resolve()` would itself
    return `status="resolved"` — i.e. whether this candidate is actually choosable, not
    just distinguishable. `(code, name)` alone is not always enough: every ambiguous
    name-set is, by construction, two or more nodes under the identical normalised name
    (that identity is what makes a tier match more than once) — so name identity alone
    tells a caller nothing about *which* sets are hard to choose from. What distinguishes
    the hard ones is a **mix**: some ambiguous sets pair a node the dataset will accept
    with one it refuses, under the exact same raw name (the recurring shape is
    `Indust.json`'s "a branch and its one child both carry the branch's name"; the same
    thing happens across two unrelated subtrees in `Skill.json`'s 專案管理). A caller
    reading two byte-identical names in one of those sets has nothing left to choose on
    but the codes — the exact state returning `(code, name)` pairs instead of bare codes
    exists to prevent, reproduced inside the mechanism built to prevent it. No bundled
    dataset has been found where every candidate in a set is non-terminal — resolving one
    further (querying its own `code`, since a non-terminal candidate is by construction a
    branch, and `resolve()` will refuse it and hand back ITS leaves) always terminates.

    Exact counts (how many ambiguous sets exist, how many mix terminal/non-terminal, and
    that zero are entirely non-terminal) are not repeated here: they are a property of
    the bundled dataset files, re-derivable offline at zero cost, recorded in
    `docs/104-site-facts.md` §6b.3g-1 rather than hand-typed a second place a refresh
    (`src/mcp104/assets/104-categories/README.md`) could silently leave behind. What this docstring
    commits to is the *shape* of the finding — some sets mix, none is a dead end — which
    is what the code above actually depends on; the count is not.
    """

    code: str
    name: str
    terminal: bool


class BrowseNode(NamedTuple):
    """One node offered back by `children()`/`path()` for BROWSING a dataset tree —
    distinct from `Candidate` (offered back by `resolve()` on a REFUSAL) even though the
    two carry overlapping information, because they answer different questions and are
    read by different callers: `Candidate` is "here is what you could have meant instead
    of what you typed", scoped to `tools/filters.py`'s CATEGORY-RESOLUTION error path;
    `BrowseNode` is "here is what sits at this position in the tree", scoped to
    `tools/discovery.py`'s zero-request `browse_filter_values` tool, which has no query
    to refuse in the first place. Widening `Candidate` with a fourth field instead of
    adding this type would couple two call sites that have no reason to agree on shape
    whenever one of them needs to change.

    `terminal`: same meaning as `Candidate.terminal` — whether passing `code` back into
    `resolve()` for the SAME condition would itself return `status="resolved"`. Computed
    via `_is_terminal`, never re-derived by a caller: `discovery.py` reads this field, it
    never calls `_is_terminal` itself, because branch-code acceptance is measured per
    (file, condition) pair (`_BRANCH_ACCEPTANCE`) and only a `Dataset` already loaded for
    the correct condition carries that pairing.

    `has_children`: whether this node has at least one child in the tree — independent of
    `terminal` (a branch this dataset accepts, e.g. `jobcat`'s `JobCat.json`, is both
    `terminal=True` and `has_children=True` at once; a node with no children is always
    `terminal=True` and always `has_children=False`). Answers "can I still go one layer
    deeper from here" per node, which "every child at this layer is a leaf" cannot: a
    layer can freely mix nodes that still have children with ones that do not.
    """

    code: str
    name: str
    terminal: bool
    has_children: bool


class Resolution(NamedTuple):
    """The outcome of resolving one query against one dataset.

    `tier` is one of "code", "exact", "prefix", "substring" (only on `status ==
    "resolved"`) — "code" means `query` was already one of the dataset's own codes,
    matched before any name tier ran; see `resolve()`.

    `status` is one of:
    - "resolved"  — `code` and `tier` are set. `candidates` may still carry the longer
      same-prefix siblings of an exact **name** hit (never populated for a code hit, or
      for a prefix/substring hit, since prefix/substring ties are exactly what
      "ambiguous" below already covers).
    - "ambiguous" — a tier produced more than one hit. `code` and `tier` are None;
      `candidates` carries every tied hit.
    - "branch"    — a tier produced exactly one hit (by code or by name), but it is a
      branch node on a dataset not recorded as accepting branch codes. `code` and `tier`
      are None; `candidates` carries the branch's leaf descendants.
    - "unknown"   — no tier produced any hit. `code`, `tier` are None; `candidates` is
      empty.
    """

    code: str | None
    tier: str | None
    candidates: tuple[Candidate, ...]
    status: str


class _Node(NamedTuple):
    code: str
    name: str
    normalised: str
    children: tuple["_Node", ...]

    @property
    def is_branch(self) -> bool:
        return len(self.children) > 0


class Dataset(NamedTuple):
    """A loaded, flattened category tree — every node at every depth, in file order —
    scoped to the ONE CONDITION `load_dataset` built it for. `nodes`/`nodes_by_code` are
    the same data regardless of which condition asked (and are shared, not recopied,
    across conditions on the same file — see `load_dataset`), but
    `accepts_branch_codes` is not: it is measured per `(file, condition)` pair, so two
    `Dataset` instances for the same `filename` can legitimately disagree on it.
    """

    filename: str
    nodes: tuple[_Node, ...]
    accepts_branch_codes: bool
    nodes_by_code: "dict[str, _Node]"  # built once in load_dataset; see resolve()'s
    # code-first check for why this exists — a caller passing 104's own code straight
    # back (the natural round trip after an ambiguous resolution hands out {code, name}
    # candidates) needs O(1) lookup by code, not a linear scan of `nodes` on every call.


def _normalise(text: str) -> str:
    """NFKC-fold, collapse whitespace (including newlines), fold the full-width
    solidus, lowercase.

    This is the ONLY normalisation `resolve()` performs. It deliberately does not strip
    104's decorations (suffixes, parenthetical glosses) — the prefix and substring tiers
    absorb those without any decoration-specific code, because once both sides are
    folded the same way, a decorated name always contains the plain name as a prefix or
    a substring (`機械工程相關` starts with `機械工程`; `TOEIC (多益測驗)` contains
    `TOEIC`). Encoding decoration rules into the resolver would be re-deriving something
    the tier structure already provides for free, and would need updating every time
    104 adds a new decoration pattern.
    """
    folded = unicodedata.normalize("NFKC", text or "")
    folded = " ".join(folded.split())
    folded = folded.replace("／", "/")
    return folded.lower()


@lru_cache(maxsize=None)
def _load_tree(filename: str) -> tuple[tuple[_Node, ...], "dict[str, _Node]"]:
    """Parse and flatten one bundled dataset FILE — the expensive part, and the part
    that is genuinely shared across every condition backed by the same file (the tree
    data itself does not depend on which condition is asking). No network access;
    raises `FileNotFoundError`/`json.JSONDecodeError` unmodified — there is no recovery
    path for a dataset that fails to load, since every condition backed by it is
    unusable without it.

    **Memoised** (`@lru_cache`, unbounded — there are exactly eight bundled files, so
    the cache cannot grow past that). `encode_filters` calls `load_dataset` once per
    dataset-backed filter value and `tools/search.py`'s prefix-family warning pass
    calls it again, separately, for the same keys — without caching this parse, that is
    two-plus full parses of files up to `Skill.json`'s 678 KB (~15 ms) **per search
    call**, and the second parse runs inside the held session lock, blocking every other
    connection's request for its duration. A failed load is not cached (`lru_cache`
    does not cache raised exceptions), so a transient failure self-heals on the next
    call rather than being pinned. Split out from `load_dataset` (below) specifically so
    that two conditions sharing one file — `jobcat`/`workExpJob` both on `JobCat.json`,
    `workExpInd`/`expectInd` both on `Indust.json` — reuse this cached parse rather than
    re-reading the file once per condition; only the (cheap) branch-acceptance lookup
    below actually varies per condition.
    """
    path = ASSETS_DIR / filename
    raw = json.loads(path.read_text(encoding="utf-8"))

    flat: list[_Node] = []

    def build(items: list[dict]) -> tuple[_Node, ...]:
        built = []
        for item in items:
            children = build(item.get("n") or [])
            node = _Node(
                code=str(item["no"]),
                name=item["des"],
                normalised=_normalise(item["des"]),
                children=children,
            )
            flat.append(node)
            built.append(node)
        return tuple(built)

    build(raw)
    # Codes are 104's own leaf/branch identifiers and are assumed unique within one
    # dataset file (the category-tree shape these files all share gives every node its
    # own `no`); a later duplicate silently wins here, which has not been observed but
    # is worth naming as an unverified assumption rather than a checked one.
    return tuple(flat), {node.code: node for node in flat}


def load_dataset(filename: str, condition_key: str) -> Dataset:
    """Load `filename`, paired with the branch-code acceptance measured for THIS
    SPECIFIC `(filename, condition_key)` pair — never inherited from a different
    condition that happens to share the file (see `_BRANCH_ACCEPTANCE`'s own comment
    for why that inheritance is unsafe in exactly one direction). `condition_key` is
    required, not defaulted: a caller that does not know which condition it is
    resolving for cannot correctly answer whether a branch code is acceptable, and a
    silently-wrong default here is the one thing this whole restructuring exists to
    rule out.

    The actual file parse is cached by filename alone, via `_load_tree` — two
    conditions sharing a file (`jobcat`/`workExpJob`, `workExpInd`/`expectInd`) reuse
    the identical cached `nodes`/`nodes_by_code`, so binding a second condition to an
    already-bundled file costs a dict/tuple lookup here, not a second file read.
    """
    nodes, nodes_by_code = _load_tree(filename)
    return Dataset(
        filename=filename,
        nodes=nodes,
        # Absent from `_BRANCH_ACCEPTANCE` reads as False here — "never measured for
        # this pair" and "measured to refuse for this pair" both refuse a branch match
        # today, which is the correct behaviour for both, but only one of them is
        # backed by a measurement. See the comment on `_BRANCH_ACCEPTANCE` for which is
        # which.
        accepts_branch_codes=_BRANCH_ACCEPTANCE.get((filename, condition_key), False),
        nodes_by_code=nodes_by_code,
    )


def _leaf_descendants(node: _Node) -> tuple[Candidate, ...]:
    """Every leaf under `node`, depth-first. `node` itself if it is already a leaf
    (defensive only — callers only reach here for a branch node).

    Every result is `terminal=True` unconditionally, with no need to consult
    `Dataset.accepts_branch_codes`: a leaf has no children, so it is never a branch on
    any dataset, and `_is_terminal`'s predicate — "not a branch, or a branch this
    dataset accepts" — is trivially satisfied by its first half alone.
    """
    if not node.children:
        return (Candidate(node.code, node.name, terminal=True),)
    leaves: list[Candidate] = []
    for child in node.children:
        leaves.extend(_leaf_descendants(child))
    return tuple(leaves)


def _is_terminal(dataset: Dataset, node: _Node) -> bool:
    """Whether passing `node.code` back into `resolve()` would itself return
    `status="resolved"` — a leaf always qualifies; a branch qualifies only on a dataset
    recorded as accepting branch codes. Shares `_finalise`'s own branch-refusal
    predicate exactly (`node.is_branch and not dataset.accepts_branch_codes` refuses),
    so a candidate's `terminal` flag cannot silently disagree with what actually happens
    if that candidate is chosen.
    """
    return not node.is_branch or dataset.accepts_branch_codes


@lru_cache(maxsize=None)
def _structure(filename: str) -> tuple["frozenset[str]", "dict[str, str]"]:
    """(root code set, child-code -> parent-code map) for one bundled FILE — structural
    facts about the tree shape that do NOT depend on which condition is browsing it
    (unlike `accepts_branch_codes`, which does), so this is keyed on `filename` alone and
    cached the same way `_load_tree` is: eight bundled files, unbounded cache cannot grow
    past that.

    Root membership cannot be read off node POSITION in `_load_tree`'s flat `nodes`
    tuple — that tuple is built in the tree's DEPTH-FIRST **POSTORDER** (a node is
    appended to `flat` only after all of its own children have been), so a root node's
    index is not "first" or "last" in any way a caller could rely on. It CAN be read off
    membership: a node is a root iff its code never appears as some other node's child,
    which this function computes once, here, rather than leaving every caller to
    re-derive the same test.
    """
    nodes, _ = _load_tree(filename)
    child_codes: set[str] = set()
    parent_of: dict[str, str] = {}
    for node in nodes:
        for child in node.children:
            child_codes.add(child.code)
            parent_of[child.code] = node.code
    root_codes = frozenset(node.code for node in nodes if node.code not in child_codes)
    return root_codes, parent_of


def _to_browse_node(dataset: Dataset, node: _Node) -> BrowseNode:
    return BrowseNode(
        code=node.code, name=node.name,
        terminal=_is_terminal(dataset, node), has_children=bool(node.children),
    )


def children(dataset: Dataset, code: str | None) -> tuple[BrowseNode, ...]:
    """The next layer down from `code` (or the root layer, when `code is None`) —
    `tools/discovery.py`'s `browse_filter_values` is the only intended caller. Takes an
    already-loaded `Dataset`, never a bare filename: `terminal` on each returned node
    depends on `dataset.accepts_branch_codes`, which is measured per (file, condition)
    pair, not per file — a filename-typed signature would force this function to load
    the dataset itself with no condition to load it FOR, silently reverting `terminal` to
    file-keying, the one thing branch-acceptance measurement in this module refuses to
    do. Raises `KeyError(code)` for a `code` absent from this dataset — `discovery.py`
    turns that into the tool's `{"error": ...}` payload; this function never returns an
    error shape of its own, since it has no request/response envelope to shape.

    Order matches the bundled JSON's own top-level (for the root layer) or `n` (for a
    node's own children) array order — `_load_tree`'s postorder `flat` list happens to
    preserve top-level order among just the root-coded subset (each top-level item's
    entire subtree is fully appended, itself last, before the next top-level item's
    subtree begins), and a node's own `children` tuple is built directly from `n` in
    array order, untouched by the postorder walk. Neither property is re-derived here;
    both fall out of `_load_tree`'s existing construction.
    """
    if code is None:
        root_codes, _ = _structure(dataset.filename)
        return tuple(
            _to_browse_node(dataset, node)
            for node in dataset.nodes if node.code in root_codes
        )
    node = dataset.nodes_by_code.get(str(code))
    if node is None:
        raise KeyError(code)
    return tuple(_to_browse_node(dataset, child) for child in node.children)


def path(dataset: Dataset, code: str) -> tuple[BrowseNode, ...]:
    """Root -> `code`, inclusive. Raises `KeyError(code)` for a `code` absent from this
    dataset, same convention as `children()`. Three levels down into a 2,661-node tree
    (`Skill.json`), an Agent otherwise has no way to say where it is — `browse_filter_
    values`'s `path` field exists for exactly that, and is built from this function, not
    re-derived at the tool layer.
    """
    node = dataset.nodes_by_code.get(str(code))
    if node is None:
        raise KeyError(code)
    _, parent_of = _structure(dataset.filename)
    chain = [node]
    current_code = node.code
    while current_code in parent_of:
        current_code = parent_of[current_code]
        chain.append(dataset.nodes_by_code[current_code])
    chain.reverse()
    return tuple(_to_browse_node(dataset, n) for n in chain)


# Model-facing Traditional Chinese, describing what `Candidate.terminal` means — kept
# beside `_is_terminal`, the predicate that computes it, for the same reason
# `RESOLVE_ACCEPTS_ZH` sits beside `resolve()`: a hand-written copy of this sentence in
# a consumer module is a claim about this module's behaviour maintained somewhere this
# module cannot enforce, and that is exactly how a stale, contradicted claim survives —
# ten hand-written copies of one clause going stale together is the earlier instance of
# this same failure, not a hypothetical one.
CANDIDATE_TERMINAL_ZH = (
    "terminal 為 true 時，把該候選項目的 code 傳回同一個篩選鍵可直接解析成功；為 "
    "false 時，該 code 本身是一個分支節點，傳回去仍會被拒絕，並改回傳它自己的葉節點"
    "供選擇——不是死路，只是還要再選一層"
)

# A short inline marker for the ONE consumer that annotates candidates conditionally
# rather than structurally (the prefix-family warning — prose, not a payload key, so an
# unmarked entry there carries no signal at all and marking every selectable one would
# be noise around the rare unselectable one). Deliberately a separate export from
# `CANDIDATE_TERMINAL_ZH` above rather than a substring of it: an unconditional key in a
# structured payload and a conditional mark in free text are different shapes for a
# reason — an absent key is information in the first and noise in the second — and the
# two consumers must not be unified; slicing one string for both would quietly re-couple
# them the next time either wording changes.
NON_TERMINAL_FAMILY_MARK_ZH = "（此代碼本身會被拒絕，需再選其葉節點）"


def _finalise(dataset: Dataset, winner: _Node, tier_name: str) -> Resolution:
    """Turn one winning node into a `Resolution`, applying the branch-refusal check
    every hit path shares (a direct code match or any of the three name tiers) — written
    once so the code-first check in `resolve()` cannot drift out of step with the
    name-tier checks it mirrors, the way two independently-written copies of the same
    rule tend to.
    """
    if winner.is_branch and not dataset.accepts_branch_codes:
        # Refused, never expanded into a multi-code query — that would submit a query
        # the caller did not write.
        return Resolution(
            code=None, tier=None, candidates=_leaf_descendants(winner), status="branch"
        )

    # A family only exists at the exact **name** tier: a code hit is already 104's own
    # unambiguous identifier with nothing to warn about, and a prefix- or substring-tier
    # win is, by that tier's own construction, already the caller's query being a strict
    # prefix/substring of the winner's name, and "more than one such node" is exactly
    # what the ambiguous branch in `resolve()` already refuses. Only an exact **name**
    # hit can additionally have *other*, longer nodes sharing its name as a prefix.
    family: tuple[Candidate, ...] = ()
    if tier_name == "exact":
        family = tuple(
            Candidate(node.code, node.name, _is_terminal(dataset, node))
            for node in dataset.nodes
            if node is not winner
            and len(node.normalised) > len(winner.normalised)
            and node.normalised.startswith(winner.normalised)
        )

    return Resolution(code=winner.code, tier=tier_name, candidates=family, status="resolved")


# Model-facing Traditional Chinese, describing what `resolve()` accepts and what it
# refuses — kept beside the function that decides both, so a future change to either
# is made within sight of the sentence describing it. This is the text `tools/search.py`
# reads into its generated per-key tool description; it must not be re-typed there.
# Three things kept deliberately true of the wording:
#   - It states the RULE ("a name or 104's own code"), never today's tiers. "Normalised
#     exact, prefix, substring, or a code" would go stale the moment a tier changes —
#     the exact failure this export exists to prevent, reproduced inside the fix for it.
#   - It folds branch refusal in here rather than leaving it stated separately: a
#     resolved-but-refused branch node is one more way `resolve()` returns candidates
#     instead of a code, alongside an ambiguous name, and a caller deciding what a
#     `candidates` response means needs both reasons in one place, not two.
#   - A total miss is named SEPARATELY from the other two, not folded into the same
#     "一律" (uniformly) clause: only an ambiguous name or a refused branch returns a
#     `candidates` list; a total miss (`status="unknown"`) returns none, and the tool's
#     error payload omits the `candidates` key entirely rather than sending an empty
#     list — an empty container on a failure shape would read as a successful empty
#     result. Centralising this text raised the cost of getting that distinction wrong:
#     one imprecise "all three behave alike" clause here would previously have been
#     wrong in one hand-written copy; it would now be wrong in every generated line at
#     once.
RESOLVE_ACCEPTS_ZH = (
    "名稱字串或 104 自己的分類代碼皆可（代碼優先於名稱比對）；名稱有多筆同名符合，或"
    "解析到一個此資料集未開放的分支節點時，回傳候選清單並拒絕查詢，不會代為挑選或展開"
    "成多筆查詢；完全找不到相符項目時同樣拒絕查詢，但不附候選清單（避免讓失敗看起來像"
    "是查無資料的成功結果）"
)


def resolve(dataset: Dataset, query: str) -> Resolution:
    """Resolve `query` against `dataset`. Pure — no I/O; `dataset` must already be
    loaded.

    `RESOLVE_ACCEPTS_ZH`, directly above, is the model-facing description of what this
    accepts and refuses — read that constant into generated text rather than re-typing
    a summary of this docstring beside it.

    Checks, in order:

    1. **A direct code match.** If `query` is already one of `dataset`'s own codes,
       return it immediately — this is the round trip after an ambiguous resolution
       hands the caller `{code, name}` candidates and they pass the chosen code straight
       back, and it must not fail as an unresolvable *name*. Checked before any name
       tier: codes and 104's category names cannot collide by construction (codes are
       all-digit, names are not), so this introduces no ambiguity about which one a
       given string is meant to be.
    2. **The normalised exact / prefix / substring name tiers**, in that order — see the
       module docstring.

    Both paths route through `_finalise`, so a code match is refused exactly like a
    name-tier match when it resolves to a branch node on a dataset not recorded as
    accepting one; a raw branch code is not a back door around that refusal.
    """
    code_hit = dataset.nodes_by_code.get(str(query))
    if code_hit is not None:
        return _finalise(dataset, code_hit, "code")

    normalised_query = _normalise(query)
    if not normalised_query:
        return Resolution(code=None, tier=None, candidates=(), status="unknown")

    tiers: tuple[tuple[str, "callable[[_Node], bool]"], ...] = (
        ("exact", lambda node: node.normalised == normalised_query),
        ("prefix", lambda node: node.normalised.startswith(normalised_query)),
        ("substring", lambda node: normalised_query in node.normalised),
    )

    for tier_name, matches in tiers:
        hits = [node for node in dataset.nodes if matches(node)]
        if not hits:
            continue

        if len(hits) > 1:
            # Refusal is absolute on ambiguity — no tier ever picks among several
            # matches, because a wrong code returns a plausible result set with
            # nothing to show it is wrong. `terminal` matters most exactly here: some
            # of these candidates can share an identical name with another candidate in
            # the same tuple (a branch and its one child, or two unrelated subtrees
            # landing on the same label — see `Candidate`'s own docstring), so `(code,
            # name)` alone does not always let the caller tell them apart.
            candidates = tuple(Candidate(h.code, h.name, _is_terminal(dataset, h)) for h in hits)
            return Resolution(code=None, tier=None, candidates=candidates, status="ambiguous")

        return _finalise(dataset, hits[0], tier_name)

    return Resolution(code=None, tier=None, candidates=(), status="unknown")
