"""Own the résumé-search filter surface: the condition table, wire encoding, pre-request
validation and drop detection.

Answers: given a caller's `filters` dict (keyed by our own condition names, documented in
the tool docstring in `tools/search.py`), what does 104's API expect on the wire, is the
request even worth sending, and did the server actually apply what we asked for?

Deliberately does not: issue any HTTP request, touch the session/guard/throttle, decide
what to do about a dropped or unresolved condition, or read the certification corpus —
those are `tools/search.py`'s and `tools/drop_detection.py`'s jobs respectively; this
module only reports the facts (a wire pair list, a validation error) for those layers to
act on. The bundled category datasets are public and unauthenticated
(`src/mcp104/assets/104-categories/README.md`), so `tools/categories.py` reads them
directly here — that file access is local and not routed through
`guarded_api` or the throttle (steering/structure.md, steering/tech.md).

**Drop detection (`detect_dropped`, the certification corpus, echo-evidence derivation)
lives in `tools/drop_detection.py`, not here.** This module has no corpus read and no
raise on a missing corpus — importing it alone leaves every `CONDITIONS` row at its
dataclass default (`echo_evidence="unmeasured"`), which is deliberate: `research/probes/
certify_conditions.py` (the only tool that can regenerate the corpus) imports this module
and must be able to start when the corpus is absent. See `drop_detection.py`'s own module
docstring for the full account of why the split exists.

**The condition table (`CONDITIONS`) is the single definition of the filter surface.**
`tools/search.py`'s docstring, this module's own unknown-key error, and drop detection all
read it; none restates it. Its 35 rows are authored against
`docs/104-site-facts.md` §6b.3g, whose figures are cited (`[M §6b.3g]`) rather than
copied — this module states derived facts (encoding, echo behaviour) and cites the total
that certified each one, not the raw measurement log itself.

**`nationality` ships, via `Abroad.json` — the apparent contradiction in the source
measurements was a stale, superseded row, not two live facts.** `docs/104-site-facts.md`
§6b.3g originally carried a "復驗" (re-verification) table claiming category codes
returned 0 for `nationality`, alongside an earlier human-form-capture section showing
two 10-digit `Abroad.json` leaf codes each producing a clean order-of-magnitude drop
(`7001010000` → 393, `7001001000` → 112 against a 2,886,647 baseline). The facts document
has since been corrected: the "復驗" row is struck through with an explicit note that its
category-code conclusion is void (it was written before the leaf-code capture and never
retired once that capture landed); the part of it that does still hold — small integers
and the bracketed form both return 0, and the field is sparse in the population — is kept
separate from the retracted part. `nationality`'s certifying total is therefore the two
leaf-code drops, same dataset and same code shape as `studyAbroad`/`city`/`jobcat`.

**Branch-code acceptance for `nationality` is untested, not merely unrecorded.** Both of
its certifying submissions used leaf codes; `studyAbroad`, the *other* condition backed by
this same `Abroad.json`, is separately measured to reject branch codes and require leaves.
`categories.py`'s branch-refusal rule (`Abroad.json` is recorded `False` — measured to
refuse — in `categories._BRANCH_ACCEPTANCE`) therefore applies to `nationality` exactly as
it does to `studyAbroad` — a resolved branch node is refused with its leaf candidates
rather than risking the same silent zero-row failure `studyAbroad` was measured to have.

**Composite rows.** Three conditions have wire parameters that are interdependent — a mode
meaningless without its bound, or a slot index — and are written first in this table
because their value structure cannot be inferred from the key name:

- `language_skills` — slot-structured (`language[]` once per slot, `languageAbility{N}[]`
  four times per slot for listening/speaking/reading/writing, `langAbilityFulfills`
  optional). `N` is the slot index, capped at 3 (the server's own parameter vocabulary has
  no `languageAbility4` `[M §6b.3g]`).
- `expect_pay` — on the **wire**, the monthly-pay bound is two repeated bracketed
  parameters whose position carries meaning (ten-thousands digit, then thousands digit),
  not an amount. **Caller-facing, it is a plain amount** (`48000`, not a pre-split pair)
  — `_split_pay_bound` performs the ten-thousands/thousands split, and
  `_require_pay_bound` rejects an amount the split cannot represent (not a multiple of
  1,000) before any request. Hiding the split here, rather than asking the caller to do
  it, is what removes the way to get it wrong: a caller who split it themselves could as
  easily write `[15, 0]` for 150,000 as `[1, 5]`, and either is a syntactically valid
  request meaning a different salary that 104's own echo would confirm as sent — this is
  also why `work_exp_time.min`/`.max` (below) and `expect_pay.month.min`/`.max` are both
  plain ints despite encoding to different wire shapes.
- `work_exp_time` — the bound is **two single unbracketed values**, both on the wire and
  caller-facing (no split needed — 104 accepts this one as a plain value already).

**The two range composites (`expect_pay`, `work_exp_time`) do not share a wire encoding,
even though their caller-facing shapes are deliberately uniform** (both a `mode` plus
plain-int bound(s)). `_encode_expect_pay` and
`_encode_work_exp_time` are written and tested independently; nothing is generalised from
one to the other.
"""
from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import mcp104.tools.categories as categories

# Language slots are capped at 3 — the server's own returned parameter vocabulary carries
# `languageAbility1..3` and no `languageAbility4` [M docs/104-site-facts.md §6b.3g,
# 2026-08-14]. The archived form markup cannot settle this cap (the relevant parameters are
# generated at submit time and are absent from it); the server's own vocabulary is the
# measured source, not the form's visual layout.
_MAX_LANGUAGE_SLOTS = 3

# Modes needing at least a lower bound, on both range composites (names happen to match;
# the wire encoding of the bound does not — see module docstring).
_RANGE_MODES = frozenset({"down", "up", "to"})


class FilterError(Exception):
    """Base for every error this module raises. All are raised before any request is
    issued — that is the point of `validate_filters` running ahead of `encode_filters`.
    """


class UnknownFilterKeyError(FilterError):
    """A `filters` key is not in `VALID_FILTER_KEYS`."""

    def __init__(self, unknown_keys: list[str], valid_keys: tuple[str, ...]):
        self.unknown_keys = tuple(unknown_keys)
        self.valid_keys = valid_keys
        super().__init__(
            f"未知的篩選鍵：{', '.join(self.unknown_keys)}。合法的篩選鍵："
            f"{', '.join(valid_keys)}"
        )


class MultiValueNotAcceptedError(FilterError):
    """A list/tuple/set was given for a key whose wire encoding carries exactly one
    value.

    Before this existed, such a value reached `encode_filters` and was rendered with
    `str()`, putting a Python repr on the wire — `sex=['0', '1']`. 104 does not reject
    everything it cannot parse (see `autobiography` in CLAUDE.md: silently discarded,
    `total` unchanged), so the realistic outcome is the filter vanishing while the
    caller believes it applied. That is strictly worse than an error: the Agent is told
    it searched women-or-men and actually searched everyone.

    It is also the natural mistake. `edu=['8','16']` is legal and means 大學 or 碩士, so
    a caller generalising to `sex=['0','1']` is reasoning correctly about a rule this
    project never stated. The message therefore names the keys that DO accept several
    values rather than only refusing.
    """

    def __init__(self, key: str, encoding: str, multi_value_keys: tuple[str, ...]):
        self.key = key
        self.encoding = encoding
        super().__init__(
            f"篩選鍵 '{key}' 只接受單一值，不接受列表（wire 編碼為 {encoding}）。"
            f"若要表達「其中之一即可」，只有這些鍵支援多值："
            f"{', '.join(multi_value_keys)}。其餘鍵請改為只傳一個值，或分次查詢。"
        )


class CompositeValidationError(FilterError):
    """A composite condition's structure is invalid — a missing companion, a slot-count
    violation, or an unsupported mode/field combination.
    """

    def __init__(self, key: str, reason: str):
        self.key = key
        self.reason = reason
        super().__init__(f"篩選條件 '{key}' 驗證失敗：{reason}")


class CategoryResolutionError(FilterError):
    """A dataset-sourced condition's name did not resolve to exactly one code.

    Carries the `categories.Resolution` so the caller (tools/search.py) can report the
    candidate list back to the caller instead of searching, without re-deriving it —
    `resolution.status` is "ambiguous", "branch" or "unknown"; it is never "resolved",
    since this exception is only raised when resolution did not succeed.
    """

    def __init__(self, key: str, query: str, resolution: "categories.Resolution"):
        self.key = key
        self.query = query
        self.resolution = resolution
        super().__init__(
            f"篩選條件 '{key}' 的名稱 '{query}' 無法唯一解析（{resolution.status}）"
        )


@dataclass(frozen=True)
class Condition:
    """One row of the condition table.

    `wire` lists the condition's wire/echo parameter name(s) **without** the `[]` array
    suffix — the suffix is applied at encode time (per `encoding`) and dropped again for
    the echo comparison, never stored as part of a condition's identity. For a composite,
    `wire` lists every sub-field's canonical name, in the order drop detection should
    consider them; encoding/decoding a composite is handled by its own dedicated
    functions, not by the generic per-family logic below.
    """

    key: str
    provenance: str  # "filter-key" | "top-level"
    wire: tuple[str, ...]
    encoding: str  # "comma" | "scalar" | "repeated" | "composite"
    value_source: str  # "enum" | "dataset" | "codes-only" | "free-text" | "numeric" | "composite"
    # "echoed" | "not-echoed" | "unmeasured" — what was OBSERVED, not a verdict a
    # runtime null test could stand in for. "echoed": the server returned the
    # submitted value back. "not-echoed": the server returned nothing for it WHILE
    # the condition was independently confirmed applied — a finding, not a gap.
    # "unmeasured" (the default below): no echo observation exists for this row at
    # all, which is a different claim from either measured outcome and is not the
    # same as choosing one of them by guess. Filter-key rows do not set this
    # explicitly in their own `_row(...)` call — see `tools/drop_detection.py`'s
    # `_derive_echo_evidence`, which patches every row present in the certification
    # corpus (`drop_detection.CORPUS_PATH`) after the corpus is loaded, binding this
    # very `CONDITIONS` dict in place rather than a copy — this module alone never
    # patches it, so a newly added row cannot inherit a measured-looking value by
    # copy-paste; it reads "unmeasured" until `drop_detection` is imported and an
    # actual corpus entry says otherwise. Only the two top-level rows (`kws`, `page`)
    # set it explicitly here, because neither is a filter-key row the certification
    # probe (research/probes/certify_conditions.py) tests — their own evidence comes
    # from a separate, already-cited measurement instead.
    #
    # An applied-but-unechoed condition and a misnamed, silently-discarded parameter
    # are indistinguishable at runtime without a second baseline query — see
    # `drop_detection.detect_dropped`'s own docstring for the measured case that
    # makes this observable rather than hypothetical (`autobiography=1`, an
    # unrecognised parameter name accepted, echoed null, and never changing the
    # total).
    echo_evidence: str = "unmeasured"
    # How 104 MATCHES this key — a third axis, independent of which values are legal
    # (`value_domain`) and of how many may be given at once (`encoding`). Empty means
    # literal matching: a résumé matches a code when it carries exactly that value.
    #
    # `driver` is the measured counterexample (2026-08-15): its codes are ordered
    # licence classes and 104 matches HIERARCHICALLY — a holder of a superior class
    # satisfies a lower code without ever carrying that code's own class. This is not a
    # curiosity. It changes what a query MEANS: `driver=1` returns every motorcycle
    # licence holder, so「只要輕型機車」is a request this filter cannot express at all,
    # and an Agent that does not know it will read the result set as narrower than it is.
    #
    # It also defeats the correlation method that derives labels: the true label is
    # absent from the résumés of superior-class holders, so intersection can never find
    # it (see research/probes/measure_code_meanings.py).
    matching_note: str = ""
    dataset: str | None = None  # filename under src/mcp104/assets/104-categories/, when value_source == "dataset"
    value_domain: Any = None  # dict[code, label] for "enum"; a short description otherwise
    # "certified" | "measured-subset" | "unmeasured" — whether `value_domain` (for an
    # "enum" row) is the FULL domain or only the codes measured so far. Read only by
    # `tools/discovery.py`'s `browse_filter_values` to derive its per-key `note` — never
    # hand-written there, because a `values` list returned by a tool NAMED "browse" is
    # the strongest possible invitation to read it as complete, and this project has
    # already lost this exact distinction once by gluing it inside a label string
    # (`plastActionDateType`'s "其餘代碼值域未測" and `updateDateType`'s "確切文字標籤
    # 未測", both still present in `value_domain` below — this field states the same
    # fact structurally, it does not replace the prose). Default "certified" is correct
    # for every enum row whose `value_domain` carries no such caveat in its own labels;
    # only rows that DO carry one flip it explicitly, at the row, beside the caveat
    # itself, rather than the default silently drifting out of step with a label added
    # later.
    domain_completeness: str = "certified"
    # For `value_source == "composite"` ONLY: the caller-facing dict SHAPE (field names,
    # required-ness, type) — deliberately a SEPARATE field from `value_domain`, which for
    # a composite row carries prose CAVEATS about the shape (e.g. `language`'s value
    # domain being unmeasured beyond 1/2), not the shape itself. Conflating the two once
    # made `tools/discovery.py`'s `browse_filter_values` return
    # `value_domain` under the key `structure`, which is dotted-key annotation prose
    # (`"month.mode"`) rather than the nested dict `validate_filters` actually requires —
    # a caller building a request FROM that payload produced an invalid dict every time.
    # This field is derived directly from each composite's own `_validate_*` function
    # below (the single source of truth for what is required/optional/capped), so
    # `discovery.py` has one real definition to read rather than a third hand-typed copy
    # beside CLAUDE.md's existing prose account of the same shape.
    caller_shape: Any = None
    certifying_total: str = ""  # citation for the order-of-magnitude change that certified this row
    shippable: bool = True  # A condition's own certifying evidence can fail — it has,
    # in both directions on the same day for one condition — and this is what lets a row
    # be retired (or reinstated) by flipping one value on its own line, still present
    # with its measurement and citation intact, rather than by deleting the row or
    # maintaining a second exclusion list that can drift from it. No row sets this to
    # False today.


def _row(**kwargs) -> Condition:
    return Condition(**kwargs)


# `driver` and `transport`'s measured code set: every power of two from 1 to 1024.
# Written as a generated tuple rather than eleven literals because the property being
# recorded IS "the powers of two up to 1024" — 2048 was measured and rejected, and a
# hand-typed list invites a twelfth entry to be added without one.
_FLAG_CODES_TO_1024: tuple[str, ...] = tuple(str(1 << n) for n in range(11))


# ---------------------------------------------------------------------------------------
# The condition table. 35 rows: 2 top-level (kws, page) + 33 filter-key rows. 104's form
# carries 33 conditions once keyword is set aside; `agemin`/`agemax` are the one condition
# split across two rows (age has no shared companion structure the way the composites do,
# so each bound is its own row), which is why 32 conditions still produce 33 filter-key
# rows. Simple-key rows group by encoding family: comma-joined (9), scalar (14, including
# both age rows and `nationality`), repeated (7).
# ---------------------------------------------------------------------------------------
CONDITIONS: dict[str, Condition] = {
    # --- top-level (provenance="top-level"): not `filters` dict keys ------------------
    "kws": _row(
        key="kws", provenance="top-level", wire=("kws",), encoding="scalar",
        echo_evidence="echoed",  # not corpus-derived — kws is top-level, outside what
        # research/probes/certify_conditions.py tests; this is its own separate,
        # already-cited measurement, kept explicit here rather than left to default.
        value_source="free-text",
        certifying_total="baseline, not itself certified — every other total in this "
                          "table is measured against a kws=工程師 baseline [M §6b.3g]. "
                          "echo_evidence=\"echoed\" is itself measured, not assumed: "
                          "kws=工程師 → echo '工程師'; kws=Python␣␣工程師 (two "
                          "consecutive spaces, deliberately) → echo unchanged, confirming "
                          "the server neither drops nor collapses the keyword before "
                          "echoing it back [M §6b.3g, four-request run].",
    ),
    "page": _row(
        key="page", provenance="top-level", wire=("page",), encoding="scalar",
        # Not corpus-derived, and not really a finding of "not-echoed" in the same
        # sense as a genuine measured-null case either — page's own reason for
        # exclusion is unique (below) and doesn't fit the three-state vocabulary
        # cleanly; "not-echoed" is used only because it produces the correct
        # exclusion from drop comparison, and the full reason is in certifying_total,
        # not compressed into this one word.
        echo_evidence="not-echoed",
        value_source="numeric",
        certifying_total="N/A — the server populates `page` in the echo whether or not "
                          "we send one, so its echo proves nothing; the page actually "
                          "served is read from the pagination block instead, not from "
                          "this echo",
    ),

    # --- comma-joined codes (9) --------------------------------------------------------
    "jobcat": _row(
        key="jobcat", provenance="filter-key", wire=("jobcat",), encoding="comma", value_source="dataset", dataset="JobCat.json",
        certifying_total="jobcat=2010001023 → 4,640 (baseline 532,064); comma multi-value "
                          "→ 8,673 [M §6b.3g]. One of three datasets confirmed to accept "
                          "a branch code (verbatim echo, not expanded to leaves) — see "
                          "`categories._BRANCH_ACCEPTANCE`.",
    ),
    "city": _row(
        key="city", provenance="filter-key", wire=("city",), encoding="comma", value_source="dataset", dataset="AreaWork.json",
        certifying_total="single (a branch, 新竹縣市) → 148,326; two branches "
                          "comma-joined → 211,579 [M §6b.3g]. AreaWork.json's top level "
                          "IS the cities/counties this key means by \"city\" — its "
                          "children are industrial parks, not finer-grained cities — so "
                          "this dataset accepts a branch code (`categories."
                          "_BRANCH_ACCEPTANCE[\"AreaWork.json\"] = True`); refusing it "
                          "would make searching by city impossible, not more precise.",
    ),
    "home": _row(
        key="home", provenance="filter-key", wire=("home",), encoding="comma", value_source="dataset", dataset="Area.json",
        certifying_total="single (a branch, 新竹縣市) → 50,942 [M §6b.3g]. Same tree "
                          "shape as AreaWork.json (city/county branch, industrial-park "
                          "children); also accepts a branch code (`categories."
                          "_BRANCH_ACCEPTANCE[\"Area.json\"] = True`). Comma-joined "
                          "multi-value itself is [INF] — assumed to work like the "
                          "measured jobcat/city, never itself measured.",
    ),
    "workExpInd": _row(
        key="workExpInd", provenance="filter-key", wire=("workExpInd",), encoding="comma",
        value_source="dataset", dataset="Indust.json",
        certifying_total="→ 83,233 [M §6b.3g]. Only a single value has been measured; "
                          "comma-joined multi-value is [INF] — assumed to work like the "
                          "measured jobcat/city, never itself measured.",
    ),
    "expectInd": _row(
        key="expectInd", provenance="filter-key", wire=("expectInd",), encoding="comma",
        value_source="dataset", dataset="Indust.json",
        certifying_total="→ 280,170 [M §6b.3g]. Only a single value has been measured; "
                          "comma-joined multi-value is [INF] — assumed to work like the "
                          "measured jobcat/city, never itself measured.",
    ),
    "workExpJob": _row(
        key="workExpJob", provenance="filter-key", wire=("workExpJob",), encoding="comma",
        value_source="dataset", dataset="JobCat.json",
        certifying_total="→ 26,641 [M §6b.3g] (original certification, code not "
                          "preserved). Bound to `JobCat.json` on separate, later "
                          "evidence: workExpJob=2001001002（儲備幹部, a JobCat.json "
                          "leaf）→ 7,817 against a same-session baseline of 531,979, "
                          "echoed ['2001001002'] [M §6b.3i-2]. A moved total alone "
                          "cannot distinguish this from a coincidence — two unrelated "
                          "ten-digit code spaces can share a number — so the binding "
                          "rests on the check that actually settles it: of the 50 rows "
                          "returned, 50 carry that same category inside their own work "
                          "history, in `expJobArr[].expJobDesc` and "
                          "`expJobArr[].expTitle` [M §6b.3i-2]. Content in the returned "
                          "rows is not subject to the total-alone ambiguity a moved "
                          "count can never resolve on its own. `jobcat`（希望職類）and "
                          "`workExpJob`（經歷職務）share this one code space and ask "
                          "different questions — jobcat is what a candidate wants next, "
                          "workExpJob is what they have already done — and the tool "
                          "docstring must say so, since a reader who notices the shared "
                          "dataset will otherwise assume a shared meaning; the two "
                          "select disjoint populations from the same vocabulary. "
                          "Branch-code acceptance did NOT come bundled with this "
                          "binding — it was measured separately, later, per "
                          "(file, condition) pair (`categories._BRANCH_ACCEPTANCE`): "
                          "the same branch code, 2010001000, submitted through "
                          "`workExpJob` → echo ['2010001000'], baseline 531,943 → "
                          "73,304 (2026-08-14, live session, Phase 6 task 3) — "
                          "certified, and independently earned rather than inherited "
                          "from `jobcat`'s own acceptance of that branch. `jobcat` run "
                          "through the identical code the same session is the control: "
                          "60,176 vs. 73,304 is two different totals for one branch "
                          "code through the two parameters, which is what \"shared "
                          "code space, different questions\" should look like — "
                          "identical totals would have meant one of them was being "
                          "silently ignored. This shows the branch is accepted, "
                          "echoed, and moves the total; it does not separately verify "
                          "the branch expands to the semantically correct subtree, "
                          "which was never checked for `jobcat`'s own acceptance "
                          "either — the same evidence standard, not a new gap. Until "
                          "this measurement, workExpJob's branch behaviour defaulted "
                          "to refuse (leaves returned) like any other never-measured "
                          "(file, condition) pair; a future condition newly bound to "
                          "`JobCat.json` would start at that same default, not at "
                          "whichever of these two entries happens to exist already. "
                          "Only a single value has been "
                          "measured; comma-joined multi-value is [INF] — assumed to "
                          "work like the measured jobcat/city, never itself measured.",
    ),
    "major": _row(
        key="major", provenance="filter-key", wire=("major",), encoding="comma", value_source="dataset", dataset="Major.json",
        certifying_total="→ 6,725 [M §6b.3g]. Only a single value has been measured; "
                          "comma-joined multi-value is [INF] — assumed to work like the "
                          "measured jobcat/city, never itself measured.",
    ),
    "goodTools": _row(
        key="goodTools", provenance="filter-key", wire=("goodTools",), encoding="comma",
        value_source="dataset", dataset="Tool.json",
        certifying_total="branch code → 0; leaf code (AIX) → 539 [M §6b.3g, 2026-08-14 "
                          "復驗]. Branch codes are refused client-side (`categories."
                          "_BRANCH_ACCEPTANCE[\"Tool.json\"] = False` — measured to "
                          "refuse, not merely unmeasured). Only a single value "
                          "has been measured; comma-joined multi-value is [INF] — assumed "
                          "to work like the measured jobcat/city, never itself measured.",
    ),
    "certificates": _row(
        key="certificates", provenance="filter-key", wire=("certificates",), encoding="comma",
        value_source="dataset", dataset="Skill.json",
        certifying_total="→ 103,682 [M §6b.3g]. Only a single value has been measured; "
                          "comma-joined multi-value is [INF] — assumed to work like the "
                          "measured jobcat/city, never itself measured.",
    ),

    # --- scalar (14) --------------------------------------------------------------------
    "agemin": _row(
        key="agemin", provenance="filter-key", wire=("agemin",), encoding="scalar", value_source="numeric", value_domain="正整數，歲",
        certifying_total="agemin=20&agemax=30 → 169,866 [M §6b.3g]",
    ),
    "agemax": _row(
        key="agemax", provenance="filter-key", wire=("agemax",), encoding="scalar", value_source="numeric", value_domain="正整數，歲",
        certifying_total="agemin=20&agemax=30 → 169,866 [M §6b.3g]",
    ),
    "sex": _row(
        key="sex", provenance="filter-key", wire=("sex",), encoding="scalar", value_source="enum", value_domain={"0": "女", "1": "男"},
        certifying_total="窮盡加總驗證：0=421,944 + 1=109,758 ≈ 基準 532,078 "
                          "[M §6b.3g, 2026-08-14]. sex=2（不拘）等於基準，是表單預設值，"
                          "故不在值域內 —— 不篩選就不要送出這個鍵。",
    ),
    "empStatus": _row(
        key="empStatus", provenance="filter-key", wire=("empStatus",), encoding="scalar",
        value_source="enum", value_domain={"1": "在職", "2": "待業"},
        certifying_total="窮盡加總驗證：1=291,956 + 2=235,719 ≈ 基準 [M §6b.3g, "
                          "2026-08-14]。empStatus=0（不拘）等於基準，是表單預設值——"
                          "2026-08-15 真人表單側錄直接證實：畫面上該欄位停在「不拘」，"
                          "同一次提交的 URL 就送出 empStatus=0 [M docs/104-site-facts.md "
                          "§6b.3g-4, research/results/user_click_enum_domains.json]。",
    ),
    "updateDateType": _row(
        key="updateDateType", provenance="filter-key", wire=("updateDateType",),
        encoding="scalar", value_source="enum",
        # Domain is 1-8, NOT 2/3/4 (the earlier row's mistake — this project simply did
        # not know 7/8 existed) [M research/results/discover_enum_domains_results.json,
        # 2026-08-15]: 0 rejected ("Update Date Type is too small (minimum is 1)."),
        # 9999 rejected ("...too big (maximum is 8)."), 7 and 8 both accepted, return
        # non-baseline totals, and (per the same-baseline sweep below) their own
        # measured windows. **The direction was also recorded backwards**: total
        # rises monotonically with the code (3 < 4 < 5 < 6 < 7 < 8) — the window WIDENS
        # as the code increases, it does not narrow. Every "window" label below is the
        # OLDEST ROW OBSERVED after reading every page of that code's result set under
        # a narrowed, same-session baseline (949) — a LOWER BOUND on the window, not a
        # boundary 104 itself stated the way the domain's own min/max are (those two
        # came from validation error text, a different and stronger kind of evidence;
        # see docs/104-site-facts.md §6b.3g-2 for why the two must not be conflated).
        value_domain={
            "1": "不拘（預設，等同基準，不篩選）",
            "2": "本日最新（2026-08-15 寬母體實測：kws=工程師 基準 531,838，代碼 2 "
                 "→ 1,564 筆，前 5 筆 updateDayDesc 全部是當日 2026/08/15）",
            "3": "三日內（觀察窗下界 ≥3 天，今天 2026-08-15；最舊一筆 2026/08/12）",
            "4": "一週內（觀察窗下界 ≥6 天，約 1 週；最舊一筆 2026/08/09）——下界是量到"
                 "的 6 天，不是無條件進位成的 7 天",
            "5": "兩週內（觀察窗下界 ≥12 天，約 2 週；最舊一筆 2026/08/03）",
            "6": "一個月內（觀察窗下界 ≥31 天，約 1 個月；最舊一筆 2026/07/15）",
            "7": "無表單標籤（觀察窗下界 ≥61 天，約 2 個月；最舊一筆 2026/06/15）—— "
                 "表單完全不提供這個選項，只有 API 接受；表單只有 6 個選項（1–6），"
                 "這是 API-only 的代碼，量到的視窗就是它目前唯一可陳述的意義",
            "8": "無表單標籤（觀察窗下界 ≥92 天，約 3 個月；最舊一筆 2026/05/15）—— "
                 "同 7，API-only，表單不提供",
        },
        # code 1-6 的中文標籤（不拘/本日最新/三日內/一週內/兩週內/一個月內）取自表單
        # 六個選項本身由上而下的順序（2026-08-15 真人表單側錄），與掃描量出的下界
        # （3≥3天、4≥約1週、5≥約2週、6≥約1個月）一致；但 7/8 兩個代碼表單完全沒有
        # 提供對應選項，是 API 端點才接受、表單枚舉不出來的兩個代碼——這正是「表單
        # 掃描」與「API 探域（送 9999 讀驗證錯誤上界）」兩種方法都各自單獨用也不會
        # 發現的一格：表單只給出 6 個選項看不到 7/8，API 探域只給出「1–8」這個上界
        # 數字、給不出 7/8 各自的文字標籤 [M docs/104-site-facts.md §6b.3g-4,
        # research/results/user_click_enum_domains.json].
        domain_completeness="certified",  # The CODE SET is now fully known (1-8, both
        # ends stated by 104's own validation errors) — this is the axis
        # domain_completeness answers. It is NOT the same claim as "every code's window
        # is measured": code 2 above (total=0) is still explicitly marked unmeasured on
        # THAT axis (docs/104-site-facts.md §6b.3g-2's own warning against conflating
        # the two) — 7 and 8 no longer are, now that their windows are measured too.
        certifying_total="窮盡讀完每個代碼的全部頁面，同一 session 基準 949（kws=電控"
                          "工程師 + city=新竹縣市 + work_exp_time 2–3 年）：=1 → 949"
                          "（=基準）；=2 → 0（不可判定）；=3 → 30（讀完 1 頁，最舊"
                          "2026/08/12）；=4 → 48（讀完 1 頁，最舊 2026/08/09）；=5 → 73"
                          "（讀完 2 頁，最舊 2026/08/03）；=6 → 128（讀完 3 頁，最舊"
                          "2026/07/15）；=7 → 192（讀完 4 頁，最舊 2026/06/15，"
                          "距 2026-08-15 為 61 天）；=8 → 246（讀完 5 頁，最舊 "
                          "2026/05/15，距 2026-08-15 為 92 天）—— 與 =1..6 同一 sweep、"
                          "同一基準 949 的延伸讀取，非另一個 session [M research/results/"
                          "measure_date_filters_results.json, 2026-08-15]。此前 =7 → "
                          "1,528、=8 → 1,942 的數字來自另一個 session（基準 7,392），"
                          "只證明非基準值、不足以推導視窗，現已由本次同基準 sweep 取代"
                          "（詳見 research/results/discover_enum_domains_results.json，"
                          "留作 API 探域「1–8」上界仍成立的佐證，但視窗數字改採本行）。"
                          "域邊界：0 → 'Update Date Type is too small (minimum is 1).'；"
                          "9999 → '...too big (maximum is 8).' [M 同上]。取代先前 "
                          "2026-08-14 復驗記載的「2/3/4，2 最窄、4 最寬」——域與方向"
                          "都記錯，見 docs/104-site-facts.md §6b.3g-2。1–6 的中文標籤"
                          "另由 2026-08-15 真人表單側錄取自表單選項順序；7/8 兩碼確認"
                          "表單完全不提供，是 API-only 代碼，見 §6b.3g-4。",
    ),
    "plastActionDateType": _row(
        key="plastActionDateType", provenance="filter-key", wire=("plastActionDateType",),
        encoding="scalar", value_source="enum",
        # Domain is 1-8, both ends stated by 104's own validation errors [M research/
        # results/discover_enum_domains_results.json, 2026-08-15]: 0 rejected ("...too
        # small (minimum is 1)."), 9999 rejected ("...too big (maximum is 8)."). Every
        # window below is the OLDEST ROW OBSERVED after reading every page of that
        # code's result set under a narrowed, same-session baseline (949) — a LOWER
        # BOUND, not a 104-stated boundary; see updateDateType's row comment (identical
        # method, identical caveat) and docs/104-site-facts.md §6b.3g-2.
        value_domain={
            "1": "不拘（預設，等同基準，不篩選）",
            "2": "1天內（觀察窗下界 ≥1 天，最舊一筆 1天內）",
            "3": "3天內（觀察窗下界 ≥3 天，最舊一筆 3天內）",
            "4": "5天內（觀察窗下界 ≥5 天，最舊一筆 5天內）",
            "5": "7天內（觀察窗下界 ≥7 天，最舊一筆 7天內）",
            "6": "14天內（觀察窗下界 ≥14 天，最舊一筆 14天內）",
            "7": "21天內（觀察窗下界 ≥21 天，最舊一筆 21天內）",
            "8": "30天內（觀察窗下界 ≥30 天，最舊一筆 30天內）",
        },
        # 2026-08-15 real user submission upgrades these 8 labels from order-inference
        # to direct evidence: the account holder had this radio on "30天內" and the
        # submitted URL carried plastActionDateType=8 — matching the =8 → oldest-row
        # "30天內" finding below exactly, from an independent direction (form state,
        # not swept totals). The remaining seven labels are read off the form's own
        # eight radio options in visual order, which now also matches every swept
        # window's oldest-row string one-for-one — not just a plausible label guess
        # [M docs/104-site-facts.md §6b.3g-4, research/results/
        # user_click_enum_domains.json].
        domain_completeness="certified",  # Code set fully known (1-8) — see
        # updateDateType's identical comment for the axis this claims and does not
        # claim; unlike updateDateType, every code's window here IS measured (no [INF]
        # markers above), so there is no per-code gap to flag on the label axis either.
        certifying_total="窮盡讀完每個代碼的全部頁面，同一 session 基準 949（kws=電控"
                          "工程師 + city=新竹縣市 + work_exp_time 2–3 年）：=1 → 949"
                          "（=基準）；=2 → 2（讀完 1 頁，最舊 1天內）；=3 → 192（讀完 4 "
                          "頁，最舊 3天內）；=4 → 246（讀完 5 頁，最舊 5天內）；=5 → 253"
                          "（讀完 6 頁，最舊 7天內）；=6 → 302（讀完 7 頁，最舊 14天內）；"
                          "=7 → 329（讀完 7 頁，最舊 21天內）；=8 → 360（讀完 8 頁，最舊 "
                          "30天內）[M research/results/measure_date_filters_results.json, "
                          "2026-08-15]。域邊界：0 → 'Plast Action Date Type is too small "
                          "(minimum is 1).'；9999 → '...too big (maximum is 8).' [M "
                          "research/results/discover_enum_domains_results.json, "
                          "2026-08-15]。取代先前僅 =8（30 天內）的單一補測，見 "
                          "docs/104-site-facts.md §6b.3g-2。code↔label 於 2026-08-15 "
                          "另由真人表單側錄直接確認（畫面選在「30天內」，同次提交送出 "
                          "=8），見 §6b.3g-4。",
    ),
    "studyAbroad": _row(
        key="studyAbroad", provenance="filter-key", wire=("studyAbroad",), encoding="scalar",
        value_source="dataset", dataset="Abroad.json",
        certifying_total="分支代碼 → 0；葉節點（台灣）→ 503,756 [M §6b.3g, 2026-08-14 "
                          "復驗]。只接受葉節點，與 jobcat 相反。",
    ),
    "nationality": _row(
        key="nationality", provenance="filter-key", wire=("nationality",), encoding="scalar",
        value_source="dataset", dataset="Abroad.json",
        certifying_total="nationality=7001010000（泰國）→ 393；=7001001000（中國）→ 112 "
                          "[M §6b.3g, 2026-08-14 真人表單側錄]。小整數（1/2/3）與帶 [] 的"
                          "形式仍是 0，此欄位在母體中亦稀疏——這兩點未被推翻，只是與"
                          "本欄位無關。**分支代碼未測**：兩次認證用的都是葉節點，"
                          "studyAbroad（同一份 Abroad.json）已測得分支代碼回 0、須用葉節點"
                          "——resolve() 的分支拒絕規則對本欄位同樣適用（`categories."
                          "_BRANCH_ACCEPTANCE[\"Abroad.json\"] = False`，是量測到的拒絕，"
                          "不是未量測），保守地把 nationality 的分支行為當作與 studyAbroad "
                          "共用同一份量測。",
    ),
    "schoolKeyword": _row(
        key="schoolKeyword", provenance="filter-key", wire=("schoolKeyword",), encoding="scalar",
        value_source="free-text",
        certifying_total="→ 3,664 [M §6b.3g]",
    ),
    "workShift": _row(
        key="workShift", provenance="filter-key", wire=("workShift",), encoding="scalar",
        value_source="enum", value_domain={"1": "可配合輪班"},
        certifying_total="→ 206,193 [M §6b.3g]",
    ),
    "auto": _row(
        key="auto", provenance="filter-key", wire=("auto",), encoding="scalar", value_source="enum", value_domain={"1": "有自傳"},
        certifying_total="→ 380,397 [M §6b.3g]。正確參數名稱是 auto，不是 autobiography "
                          "—— 後者送出後 total 完全不變、回吐也是 null，是被靜默丟棄的錯誤"
                          "參數名稱範例。",
    ),
    "photo": _row(
        key="photo", provenance="filter-key", wire=("photo",), encoding="scalar", value_source="enum", value_domain={"1": "有附照片"},
        certifying_total="→ 439,837 [M §6b.3g]",
    ),
    "disability": _row(
        key="disability", provenance="filter-key", wire=("disability",), encoding="scalar",
        value_source="enum", value_domain={"1": "身心障礙"},
        certifying_total="→ 3,007 [M §6b.3g]。與 photo/auto/workShift 同型的單值旗標，"
                          "value_source 同樣是 enum，不是 numeric——「數字」在這裡是代碼的"
                          "書寫形式，不是量值。",
    ),
    "contactPrivacy": _row(
        key="contactPrivacy", provenance="filter-key", wire=("contactPrivacy",),
        encoding="scalar", value_source="enum",
        value_domain={"1": "公開 E-mail 及聯絡電話"},
        certifying_total="→ 526,331（基準 532,078）[M §6b.3g, 2026-08-14]。預設 0 等於不"
                          "篩選——2026-08-15 真人表單側錄直接證實：畫面上該欄位停在"
                          "「不拘」，同一次提交的 URL 就送出 contactPrivacy=0 [M "
                          "docs/104-site-facts.md §6b.3g-4, research/results/"
                          "user_click_enum_domains.json]。",
    ),

    # --- repeated-parameter (7) ---------------------------------------------------------
    "edu": _row(
        key="edu", provenance="filter-key", wire=("edu",), encoding="repeated", value_source="enum",
        value_domain={"1": "高中以下", "2": "高中", "4": "專科", "8": "大學", "16": "碩士",
                       "32": "博士"},
        certifying_total="edu[]=4&edu[]=8 → 317,940 [M §6b.3g]",
    ),
    "military": _row(
        key="military", provenance="filter-key", wire=("military",), encoding="repeated",
        value_source="enum",
        value_domain={"5": "免役", "3": "未役", "4": "待役", "2": "屆退", "1": "役畢"},
        certifying_total="military[]=5 → 175,199（magnitude change confirmed, applied）"
                          "[M §6b.3g]. echo_evidence=\"echoed\" is corpus-derived "
                          "(research/probes/certify_conditions.py's run, tracked at "
                          "src/mcp104/assets/certification/"
                          "certify_conditions_results.json), not "
                          "hand-typed: that run's echo for this submission is "
                          "`searchForm['military'] = ['5']`, populated. This CORRECTS "
                          "what this row said before, rather than merely updating it — an "
                          "earlier probe recorded that same canonical key as always null, "
                          "with a mechanism offered for it ('sent bracketed, canonical name "
                          "unbracketed, lookup misses'). Both are superseded, and which of "
                          "the two readings was ever accurate is not established (a "
                          "behaviour change on 104's side and an error in the original "
                          "probe remain equally plausible) — but it no longer needs "
                          "deciding: the corpus is the current, reproducible measurement, "
                          "and there is exactly one production submission form to measure "
                          "at all. 104 rejects the unbracketed `military=5` outright "
                          "(USER_ERROR), so no alternate form exists to contrast against. "
                          "code↔label is no longer order-inference alone: a 2026-08-15 "
                          "real user submission carried military[]=5,3,4,2,1, matching "
                          "this row's order top-to-bottom against the form's own visible "
                          "option order [M docs/104-site-facts.md §6b.3g-4, "
                          "research/results/user_click_enum_domains.json].",
    ),
    "workInterval": _row(
        key="workInterval", provenance="filter-key", wire=("workInterval",), encoding="repeated",
        value_source="enum",
        value_domain={"1": "日班", "2": "晚班", "4": "大夜班", "8": "假日班"},
        certifying_total="workInterval[]=1 → 516,356（magnitude change confirmed, "
                          "applied）[M §6b.3g]. Same history as `military`'s row, same "
                          "resolution — see it for the full account: echo_evidence="
                          "\"echoed\" is corpus-derived, this run's echo for the "
                          "submission is `searchForm['workInterval'] = ['1']`, and the "
                          "earlier always-null claim plus the mechanism offered for it "
                          "are both superseded.",
    ),
    "role": _row(
        key="role", provenance="filter-key", wire=("role",), encoding="repeated", value_source="enum",
        value_domain={"1": "全職", "2": "兼職", "7": "實習", "8": "寒暑假工讀"},
        certifying_total="role[]=1 → 502,034；role[]=2 → 55,970 [M §6b.3g]. "
                          "2026-08-15 real user submission confirms this domain is "
                          "sparse AND complete: the form itself shows exactly these 4 "
                          "options, codes 3-6 do not exist (not merely unmeasured) [M "
                          "docs/104-site-facts.md §6b.3g-4, research/results/"
                          "user_click_enum_domains.json].",
    ),
    # driver / transport — 2026-08-15: both are ELEVEN-flag fields, not single
    # checkboxes. Each accepts every power of two from 1 to 1024 and rejects 2048;
    # every non-power tested (3,5,6,7,9,10,11,12) is rejected, so a value carries
    # exactly one flag and combinations are not accepted. Until this sweep both rows
    # held ONE code and claimed `certified` — "code 1 works" had been read as "only
    # code 1 works", which is a different proposition and was never measured.
    # The CODE SET is now closed by measurement; every code's MEANING is not. The
    # form labels the two fields 「持有駕照」/「自備車輛」, but the form control sits in
    # a collapsed section and which code(s) it submits was not captured — so even
    # code 1's label is unverified and is no longer asserted.
    "driver": _row(
        key="driver", provenance="filter-key", wire=("driver",), encoding="repeated",
        value_source="enum",
        value_domain={
            "1": "輕型機車駕照",
            "2": "普通重型機車駕照",
            "4": "大型重型機車駕照",
            "8": "普通小型車駕照",
            "16": "普通大貨車駕照",
            "32": "普通大客車駕照",
            "64": "普通聯結車駕照",
            "128": "職業小型車駕照",
            "256": "職業大貨車駕照",
            "512": "職業大客車駕照",
            "1024": "職業聯結車駕照",
        },
        domain_completeness="certified",
        matching_note="階層比對（2026-08-15 實測）：持有更高等級駕照即滿足較低代碼——"
                      "`driver=1` 會回傳所有機車駕照持有人（含只有大型重型的人），"
                      "所以「只要輕型機車」這個需求本篩選器表達不出來。"
                      "代碼由低到高依序為 1→1024，`transport` 用同一套類別但為字面比對。",
        certifying_total="driver[]=1 → 359,721 [M §6b.3g]。值域見 §6b.3g-5（1–1024 的"
                         "2 次方，2048 被拒）。代碼語意 2026-08-15 以履歷 "
                         "driverLicenseDesc 相關性推導 [M research/results/"
                         "code_meaning_results.json + falsification_ladder.json]，逐碼"
                         "證據強度：8=普通小型車駕照 為直接量測（n=5，其中 2 人完全無"
                         "機車駕照，排除所有替代解）；32 n=5 全數命中；2、16 n=5 反證"
                         "確認為階層比對；4 n=4、64 n=3、512 n=3、128/256/1024 n=2 由"
                         "交集＋約束消解得出；1 無法由交集推導（階層比對，真標籤不會"
                         "出現在較高等級持有人的履歷上），改由與代碼 2 的邊界差異"
                         "（1 收輕型、2 不收）加上 transport 同碼獨立解出而定。"
                         "⚠ 這是相關性推導，不是 104 自述的對照表。",
    ),
    "transport": _row(
        key="transport", provenance="filter-key", wire=("transport",), encoding="repeated",
        value_source="enum",
        value_domain={
            "1": "輕型機車",
            "2": "普通重型機車",
            "4": "大型重型機車",
            "8": "普通小型車",
            "16": "普通大貨車",
            "32": "普通大客車",
            "64": "普通聯結車",
            "128": "職業小型車",
            "256": "職業大貨車",
            "512": "職業大客車",
            "1024": "職業聯結車",
        },
        domain_completeness="certified",
        matching_note="字面比對（2026-08-15 實測）：與 `driver` 共用同一套類別代碼，"
                      "但問的是「自備哪些車輛」而非「持有哪些駕照」，因此不是階層的"
                      "——擁有大型重型機車不代表也擁有一台輕型機車。transport=1 的"
                      "3 份樣本全部只持有輕型機車，正是這個差異的直接證據。",
        certifying_total="transport[]=1 → 61,975 [M §6b.3g]。值域見 §6b.3g-5。"
                         "代碼語意 2026-08-15 以履歷 transportsIdDesc 相關性推導"
                         " [M research/results/code_meaning_results.json + "
                         "falsification_ladder.json]，11 碼由交集＋約束消解一次全解，"
                         "無爭用、無空集合；其中 1、2、16 另以 n=3 反證確認"
                         "（全數命中假設類別，且 1 的三份樣本皆只持有輕型機車）。"
                         "其餘各碼樣本數 n=1–2，屬較弱的推導。"
                         "⚠ 這是相關性推導，不是 104 自述的對照表。",
    ),
    "specialStatus": _row(
        key="specialStatus", provenance="filter-key", wire=("specialStatus",), encoding="repeated",
        value_source="enum",
        value_domain={"1": "學生", "2": "應屆畢業生", "3": "原住民", "4": "外籍人士",
                       "9": "僑生", "10": "新住民", "5": "二度就業", "8": "研發替代役"},
        certifying_total="→ 47,302 [M §6b.3g]. code↔label is no longer order-inference "
                          "alone: a 2026-08-15 real user submission carried "
                          "specialStatus[]=1,2,3,4,9,10,5,8, matching this row's order "
                          "against the form's own visible option order; the form itself "
                          "shows exactly these 8 options, codes 6/7 do not exist [M "
                          "docs/104-site-facts.md §6b.3g-4, research/results/"
                          "user_click_enum_domains.json].",
    ),

    # --- composite (3) — see module docstring for why these are written first ----------
    "language_skills": _row(
        key="language_skills", provenance="filter-key",
        wire=("language", "languageAbility1", "languageAbility2", "languageAbility3",
              "langAbilityFulfills"),
        encoding="composite", value_source="composite",
        # 2026-08-15: swept and now genuinely certified. The paragraph below is kept
        # because WHY it was wrong matters more than that it was.
        #
        # domain_completeness was "unmeasured", NOT the dataclass default "certified".
        # The default is right for a row whose codes were each certified; here the row's
        # own text says 完整值域未取得 for two of three sub-fields, so "certified" was a
        # claim the row contradicted on the next line. It survived because a composite
        # is not `value_source == "enum"` and every domain sweep this project has run
        # enumerates enum rows — the classification deciding what gets measured was
        # never itself among the things checked. See bl-c4e719.
        domain_completeness="certified",
        matching_note="abilities 是**階層比對**（2026-08-15 反證確認）：送某一級會連同"
                      "更高等級一起收進來。直接證據——`ability=4`（略懂，最低級）的 20 "
                      "份樣本裡，有 5 人四項技能全是「中等」、完全沒有「略懂」。"
                      "對呼叫端的意義：送 4 不等於「英文只有略懂的人」，而是「略懂以上"
                      "皆可」；要鎖定單一等級，本篩選器做不到。"
                      "⚠ 前一次 n=5 的檢定得到 0 反例，看起來像乾淨的否證，其實在階層"
                      "為真時有約 47% 機率出現——樣本數不足的結果與真正的否證長得一樣。",
        value_domain={
            "language": "16 個代碼（2026-08-15 掃描，值域稀疏——6、7、8 被拒，"
                        "31/32/40/64/99/128 亦被拒）："
                        "1=英文、2=日文、3=法文、4=德文、5=西班牙文、9=越文、10=泰文、"
                        "11=馬來文、12=印尼文、13=韓文、14=俄文、15=義大利文、"
                        "16=葡萄牙文、17=阿拉伯文、18=中文、19=菲律賓文",
            "abilities": "位元旗標，**不是序數**——代碼順序與熟練度順序不一致："
                         "2=精通、4=略懂、8=中等（1 是「不拘」哨兵，total 等於基準、"
                         "不篩選，不要送）。16 以上全被拒，值域就這 3 個等級",
            "fulfills_all": "true 對應「所有語言同時符合」勾選框",
        },
        # Derived from _validate_language_skills below: `languages` is a required,
        # non-empty list capped at _MAX_LANGUAGE_SLOTS; each slot requires both
        # `language` and `abilities`; `abilities` must be exactly 4 elements.
        caller_shape={
            "languages": "list，必填、不可為空，最多 3 個元素；每個元素是一個 dict："
                          "{'language': <代碼>, 'abilities': [4 個數字，依序聽/說/讀/寫]}",
            "fulfills_all": "bool，選填，對應「所有語言同時符合」勾選框",
        },
        certifying_total="英文/四項皆精通 → 50,454（基準 2,886,647）；+日文/四項皆中等 → "
                          "112；+同時符合 → 8 [M §6b.3g, 2026-08-14]。"
                          "echo_evidence=\"echoed\" is measured (and, for the sent "
                          "sub-parameters, corpus-derived) rather than assumed: the "
                          "original capture found searchForm echoing `language`/"
                          "`languageAbility1`/`languageAbility2`/`langAbilityFulfills` "
                          "verbatim [M §6b.3g, 2026-08-14]; a later three-slot run "
                          "confirmed it again — language[]=1,2,3 with all three ability "
                          "slots filled echoed back ['1','2','3'] and three "
                          "['8','8','8','8'] arrays [M §6b.3g, four-request run]; and "
                          "research/probes/certify_conditions.py's own corpus run "
                          "(src/mcp104/assets/certification/"
                          "certify_conditions_results.json) confirms "
                          "`language`/`languageAbility1` populated for the single "
                          "sub-parameters it actually sent.",
    ),
    "expect_pay": _row(
        key="expect_pay", provenance="filter-key",
        wire=("expectPayType", "expectPayMonthType", "expectPayMonthMin", "expectPayMonthMax"),
        encoding="composite", value_source="composite",
        value_domain={
            "types": {"1": "月薪", "2": "時薪", "4": "面議"},
            "month.mode": "down（以下）/ up（以上）/ to（至）",
            "month.min/month.max": "純整數新台幣月薪（如 48000），須為 1,000 的倍數；"
                                    "萬位/千位的 wire 拆分由 _split_pay_bound 內部完成，"
                                    "呼叫端不需也不應自行拆分",
        },
        # Derived from _validate_expect_pay below: `types` is a required non-empty list;
        # `month` is an optional NESTED dict (not dotted keys) requiring `mode`; `min` is
        # required for down/up/to; `max` is required for `to` only and rejected otherwise.
        caller_shape={
            "types": "list，必填、不可為空，元素為 '1'（月薪）/'2'（時薪）/'4'（面議）",
            "month": {
                "mode": "str，選填欄位 month 存在時必填，'down'/'up'/'to'",
                "min": "int，1000 的倍數，mode 為 down/up/to 時必填",
                "max": "int，1000 的倍數，僅 mode='to' 時必填，其餘模式不可帶",
            },
        },
        certifying_total="down 3萬以下 → 10,189；to 5~8萬 → 61,994（基準 532,074）"
                          "[M §6b.3g]。up 模式在真實資料上幾乎沒有篩選能力但不報錯——它比"
                          "對的是求職者期望薪資的下限，而幾乎每個有填月薪期望的人下限都"
                          "≥1萬，語意上近乎無效但技術上正確送達；這一點屬於 tools/search.py"
                          "docstring 該說明的呼叫端語意，不是本模組編碼/驗證的職責範圍。"
                          " echo_evidence=\"echoed\" is measured, not assumed, and "
                          "specifically contradicts a plausible over-generalisation "
                          "from a bracketed wire parameter's *shape* to a null echo — "
                          "the measured case for that shape actually producing a null "
                          "echo is `autobiography=1` (an unrecognised parameter *name*, "
                          "not a bracket effect; see `detect_dropped`'s own docstring): "
                          "expectPayType[]=1, expectPayMonthType=up, "
                          "expectPayMonthMin[]=4,8 echoed back ['1'], 'up', ['4','8'] in "
                          "that order [M §6b.3g, four-request run] — this bracketed "
                          "parameter echoes correctly. `expectPayMonthMax` was null in "
                          "that `up`-mode run only because `up` never sends it, not "
                          "because it fails to echo — confirmed separately by a "
                          "`to`-mode run, which sends both bounds: "
                          "expectPayMonthMin[]=5,0 / expectPayMonthMax[]=8,0 echoed back "
                          "['5','0'] / ['8','0'], order preserved, total 61,966 "
                          "[M §6b.3g, to-mode run]. `expectPayMonthMax`'s echo is "
                          "therefore confirmed, not merely un-contradicted — and "
                          "research/probes/certify_conditions.py's own corpus run "
                          "independently reconfirms every sub-parameter it actually "
                          "sent (`expectPayType`/`expectPayMonthType`/"
                          "`expectPayMonthMin`) populated.",
    ),
    "work_exp_time": _row(
        key="work_exp_time", provenance="filter-key",
        wire=("workExpTimeType", "workExpTimeMin", "workExpTimeMax"),
        encoding="composite", value_source="composite",
        value_domain={"mode": "all（不拘，預設，等同不送出此鍵）/ none（無經驗）/ "
                               "down（以下）/ up（以上）/ to（至）"},
        # Derived from _validate_work_exp_time below: `mode` is required; `min` is
        # required for down/up/to and rejected for all/none; `max` is required only for
        # `to` and rejected for every other mode.
        caller_shape={
            "mode": "str，必填，'all'/'none'/'down'/'up'/'to'",
            "min": "int，mode 為 down/up/to 時必填，all/none 不可帶",
            "max": "int，僅 mode='to' 時必填，其餘模式不可帶",
        },
        certifying_total="none → 44,747；down 1年 → 131,573；up 1年 → 435,561；to 1~5年 → "
                          "175,788（基準 532,074）[M §6b.3g]。"
                          "echo_evidence=\"echoed\" is measured: workExpTimeType=to, "
                          "Min=1, Max=5 echoed back 'to', '1', '5' [M §6b.3g, "
                          "four-request run], reconfirmed by "
                          "research/probes/certify_conditions.py's own corpus run.",
    ),
}

# Derived, never hand-written — a hand-maintained copy drifts from the table within one
# revision, and this key list is consumed in several places (validate_filters here, plus
# whatever docstring/error text tools/search.py builds from it). Excludes both top-level
# rows (kws/page are not `filters` dict keys). `Condition.shippable` exists for a
# condition whose own certifying evidence does not hold up — every row is shippable today,
# but the field stays as the place to park that judgement if it recurs, rather than
# re-inventing an exclusion mechanism under time pressure.
VALID_FILTER_KEYS: tuple[str, ...] = tuple(
    sorted(
        condition.key
        for condition in CONDITIONS.values()
        if condition.provenance == "filter-key" and condition.shippable
    )
)

# The two wire encodings that can carry more than one value for a single key, and so
# express "any of these":
#   repeated -> `edu[]=8&edu[]=16`
#   comma    -> `city=6001006000,6001001000`
# `scalar` carries exactly one; `composite` takes a dict whose own validator decides.
# Derived, never hand-listed: the encoding field is what actually determines this, and
# a second copy would drift the first time a row's encoding changes.
_MULTI_VALUE_ENCODINGS: frozenset[str] = frozenset({"repeated", "comma"})

MULTI_VALUE_KEYS: tuple[str, ...] = tuple(
    sorted(
        condition.key
        for condition in CONDITIONS.values()
        if condition.provenance == "filter-key" and condition.shippable
        and condition.encoding in _MULTI_VALUE_ENCODINGS
    )
)


def _as_list(value: Any) -> list:
    """Normalise a caller-supplied filter value to a list. A caller may pass a single
    value or a list for any comma-joined/repeated condition; a scalar condition never
    reaches this helper.
    """
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def _resolve_value(condition: Condition, raw: Any) -> str:
    """Turn one caller-supplied value into its wire-ready string.

    For `value_source == "dataset"` the caller passes **either** a name or one of the
    dataset's own codes — `tools.categories.resolve()` accepts both, checking a direct
    code match before any name tier. Both are accepted, not just names, specifically to
    keep a round trip open: an ambiguous or branch-refused resolution hands the caller
    `(code, name, terminal)` candidates (`CategoryResolutionError.resolution.candidates`,
    `tools.categories.Candidate`), and the natural next call passes the chosen `code`
    straight back — that must not fail as an unresolvable name. Accepting both introduces
    no ambiguity about which one a given string is: 104's codes and category names never
    collide by construction (codes are all-digit, names are not).

    For every other value_source (`enum`, `codes-only`, `free-text`, `numeric`), the value
    is passed through as-is — validation of enum membership happens at 104, not here
    (attempting a client-side enum allow-list would duplicate the value_domain data and
    drift from it; the server already answers an invalid enum value loudly with its own
    `status: "USER_ERROR"`, which is a job for the HTTP-calling layer to surface, not this
    module's).
    """
    if condition.value_source != "dataset":
        return str(raw)
    # `condition.key`, not just `condition.dataset` — branch-code acceptance is
    # measured per (file, condition) pair, never inherited by a different condition
    # sharing the same file (see categories._BRANCH_ACCEPTANCE's own comment).
    dataset = categories.load_dataset(condition.dataset, condition.key)
    resolution = categories.resolve(dataset, str(raw))
    if resolution.status != "resolved":
        raise CategoryResolutionError(condition.key, str(raw), resolution)
    return resolution.code


def _validate_language_skills(value: Any) -> None:
    if not isinstance(value, dict) or "languages" not in value:
        raise CompositeValidationError("language_skills", "缺少必要欄位 'languages'")
    languages = value["languages"]
    # `list`/`tuple` checked explicitly, before any len()/membership test below — the
    # caller filling this dict is a language model with no per-argument typing, so a
    # string where a list belongs is the expected mistake, not an exotic one. A string
    # is both truthy and has a `len()`, so skipping this check lets e.g. a 4-character
    # string slip past the slot/ability-count checks below as if it were a real list.
    if not isinstance(languages, (list, tuple)):
        raise CompositeValidationError(
            "language_skills",
            f"'languages' 必須是 list（每個元素是一個語言槽位的 dict），收到 "
            f"{type(languages).__name__}",
        )
    if not languages:
        raise CompositeValidationError("language_skills", "'languages' 不可為空")
    if len(languages) > _MAX_LANGUAGE_SLOTS:
        raise CompositeValidationError(
            "language_skills",
            f"最多支援 {_MAX_LANGUAGE_SLOTS} 個語言槽位（伺服器參數詞彙沒有第 4 槽 "
            f"[M §6b.3g]），收到 {len(languages)} 個",
        )
    for i, slot in enumerate(languages, start=1):
        if not isinstance(slot, dict) or "language" not in slot or "abilities" not in slot:
            raise CompositeValidationError(
                "language_skills", f"第 {i} 個語言槽位缺少 'language' 或 'abilities'"
            )
        abilities = slot["abilities"]
        if not isinstance(abilities, (list, tuple)):
            raise CompositeValidationError(
                "language_skills",
                f"第 {i} 個語言槽位的 'abilities' 必須是 list（4 個值，聽/說/讀/寫各"
                f"一），收到 {type(abilities).__name__}",
            )
        if len(abilities) != 4:
            raise CompositeValidationError(
                "language_skills",
                f"第 {i} 個語言槽位的 'abilities' 必須恰好 4 個值（聽/說/讀/寫各一），"
                f"收到 {len(abilities)} 個",
            )


def _encode_language_skills(value: dict) -> list[tuple[str, str]]:
    pairs: list[tuple[str, str]] = []
    for i, slot in enumerate(value["languages"], start=1):
        pairs.append(("language[]", str(slot["language"])))
        for ability in slot["abilities"]:
            pairs.append((f"languageAbility{i}[]", str(ability)))
    if value.get("fulfills_all"):
        pairs.append(("langAbilityFulfills", "1"))
    return pairs


# The wire form carries the ten-thousands digit and the thousands digit as two separate
# repeated parameters (two dropdown selects on the real form), not an amount — see
# `_split_pay_bound`. `_PAY_BOUND_GRANULARITY` is that wire form's resolution: it has no
# representation for anything finer than a multiple of 1,000, so a caller-supplied amount
# that isn't one is rejected here rather than silently rounded. The top of the
# ten-thousands digit's own domain has never been measured, so no ceiling is checked —
# only the granularity this encoding is mechanically unable to represent.
_PAY_BOUND_GRANULARITY = 1000


def _require_pay_bound(amount: Any, field_name: str) -> None:
    """A caller-facing monthly-pay bound is a plain amount (e.g. `48000`), never a
    pre-split [ten-thousands, thousands] pair — splitting it is `_split_pay_bound`'s job,
    not the caller's. Hiding the split here is what removes the way to get it wrong: a
    caller who split it themselves could send `[4, 8]` for 48,000 but just as easily
    `[15, 0]` for 150,000 or `[0, 5]` for 5,000, and a mis-split is a syntactically valid
    request meaning a different salary — 104's echo would confirm it as sent, giving no
    signal that anything was wrong.
    """
    if not isinstance(amount, int) or isinstance(amount, bool):
        raise CompositeValidationError(
            "expect_pay", f"'{field_name}' 必須是純整數金額（新台幣月薪，如 48000）"
        )
    if amount % _PAY_BOUND_GRANULARITY != 0:
        raise CompositeValidationError(
            "expect_pay",
            f"'{field_name}' 必須是 {_PAY_BOUND_GRANULARITY} 的倍數 —— wire 形式只到千位"
            f"（萬位、千位各一個下拉選單），{amount} 無法被這個編碼表示；寧可拒絕，也不要"
            f"悄悄四捨五入成呼叫端沒有要求的金額",
        )


def _split_pay_bound(amount: int) -> tuple[int, int]:
    """Mechanical and total: the ten-thousands digit, then the thousands digit. Called
    only after `_require_pay_bound` has confirmed `amount` is a non-negative multiple of
    `_PAY_BOUND_GRANULARITY`, so nothing is lost in the split.
    """
    return amount // 10000, amount % 10000 // 1000


def _validate_expect_pay(value: Any) -> None:
    if not isinstance(value, dict):
        raise CompositeValidationError("expect_pay", "必須是一個 dict")
    types = value.get("types")
    # `list`/`tuple` checked explicitly: a string is truthy and iterable, so
    # `{"types": "12"}` would otherwise pass a bare truthiness check and then iterate
    # character-by-character in the encoder, silently emitting expectPayType[]=1 AND
    # =2 (月薪 + 時薪 together) for an input that looks like one code, not two.
    if not isinstance(types, (list, tuple)) or not types:
        raise CompositeValidationError(
            "expect_pay",
            f"'types' 必須是非空的 list（每個元素是一個待遇類型代碼），收到 "
            f"{type(types).__name__}",
        )
    month = value.get("month")
    if month is None:
        return
    if not isinstance(month, dict) or "mode" not in month:
        raise CompositeValidationError("expect_pay", "'month' 需要 'mode'")
    mode = month["mode"]
    if mode not in _RANGE_MODES:
        raise CompositeValidationError("expect_pay", "'month.mode' 必須是 down / up / to 之一")
    if "min" not in month:
        raise CompositeValidationError("expect_pay", f"模式 '{mode}' 需要 'month.min'")
    _require_pay_bound(month["min"], "month.min")
    if mode == "to":
        if "max" not in month:
            raise CompositeValidationError("expect_pay", "模式 'to' 需要 'month.max'")
        _require_pay_bound(month["max"], "month.max")
    elif "max" in month:
        raise CompositeValidationError(
            "expect_pay", f"模式 '{mode}' 不接受 'month.max'（僅 'to' 模式支援上限）"
        )


def _encode_expect_pay(value: dict) -> list[tuple[str, str]]:
    pairs = [("expectPayType[]", str(t)) for t in value["types"]]
    month = value.get("month")
    if month is None:
        return pairs
    pairs.append(("expectPayMonthType", str(month["mode"])))
    wan, qian = _split_pay_bound(month["min"])
    pairs.append(("expectPayMonthMin[]", str(wan)))
    pairs.append(("expectPayMonthMin[]", str(qian)))
    if month["mode"] == "to":
        wan_max, qian_max = _split_pay_bound(month["max"])
        pairs.append(("expectPayMonthMax[]", str(wan_max)))
        pairs.append(("expectPayMonthMax[]", str(qian_max)))
    return pairs


def _validate_work_exp_time(value: Any) -> None:
    if not isinstance(value, dict) or "mode" not in value:
        raise CompositeValidationError("work_exp_time", "缺少必要欄位 'mode'")
    mode = value["mode"]
    if mode not in {"all", "none", *_RANGE_MODES}:
        raise CompositeValidationError(
            "work_exp_time", "'mode' 必須是 all / none / down / up / to 之一"
        )
    if mode in _RANGE_MODES:
        if "min" not in value:
            raise CompositeValidationError("work_exp_time", f"模式 '{mode}' 需要 'min'")
        if mode == "to" and "max" not in value:
            raise CompositeValidationError("work_exp_time", "模式 'to' 需要 'max'")
    else:
        if "min" in value:
            raise CompositeValidationError("work_exp_time", f"模式 '{mode}' 不接受 'min'")
    if mode != "to" and "max" in value:
        raise CompositeValidationError(
            "work_exp_time", f"模式 '{mode}' 不接受 'max'（僅 'to' 模式支援上限）"
        )


def _encode_work_exp_time(value: dict) -> list[tuple[str, str]]:
    pairs = [("workExpTimeType", str(value["mode"]))]
    if "min" in value:
        pairs.append(("workExpTimeMin", str(value["min"])))
    if "max" in value:
        pairs.append(("workExpTimeMax", str(value["max"])))
    return pairs


_COMPOSITE_VALIDATORS = {
    "language_skills": _validate_language_skills,
    "expect_pay": _validate_expect_pay,
    "work_exp_time": _validate_work_exp_time,
}

_COMPOSITE_ENCODERS = {
    "language_skills": _encode_language_skills,
    "expect_pay": _encode_expect_pay,
    "work_exp_time": _encode_work_exp_time,
}


def validate_filters(filters: dict) -> None:
    """Raise before any request is issued: unknown keys, composite companion violations,
    slot-count violations. Pure — does not encode, does not resolve dataset names (that
    happens in `encode_filters`, which calls this first).
    """
    unknown = sorted(set(filters) - set(VALID_FILTER_KEYS))
    if unknown:
        raise UnknownFilterKeyError(unknown, VALID_FILTER_KEYS)

    # A list on a single-value key: reject here rather than let `encode_filters` put
    # `str(["0", "1"])` on the wire. Keyed on `encoding` — the field that actually
    # decides whether several values can be expressed — never on a hand-kept key list.
    for key, value in filters.items():
        condition = CONDITIONS[key]
        if condition.encoding in _MULTI_VALUE_ENCODINGS or condition.encoding == "composite":
            continue
        if isinstance(value, (list, tuple, set)):
            raise MultiValueNotAcceptedError(key, condition.encoding, MULTI_VALUE_KEYS)

    for key, validator in _COMPOSITE_VALIDATORS.items():
        if key in filters:
            validator(filters[key])


def encode_filters(filters: dict) -> list[tuple[str, str]]:
    """Build the wire parameter list, in the caller's own key order (a composite's own
    sub-parameters are always emitted in the fixed order its structure requires, since
    that order is part of the wire contract, not the caller's choice).

    Runs `validate_filters` first. Dataset-sourced conditions are resolved to codes here,
    via `tools.categories` — a name that does not resolve to exactly one code raises
    `CategoryResolutionError` before any wire pair is produced, so a malformed or
    ambiguous name never reaches a request.

    **This function reports only failure, by design — not a gap to fill in later.** A
    successful resolution's "longer same-prefix candidates" is a warning about what the
    caller *might have meant*, which belongs in the tool response's warnings channel
    alongside `applied_filters` (what *was* actually searched); it is not a reason to
    refuse or alter the request `encode_filters` builds, and a wire-pair list has no slot
    to carry advisory text without overloading its declared shape. If you are wiring
    `tools/search.py` and looking for where that warning text comes from: it does not
    come from here. Call `tools.categories.resolve()` again yourself, once per
    dataset-backed key present in the caller's `filters`, and read `.candidates` off a
    "resolved" `Resolution` for the warning text — the repeated resolution is cheap and
    local (no network), and keeping it out of this function is what lets `encode_filters`
    stay a plain "build the wire pairs or raise" call.
    """
    validate_filters(filters)
    pairs: list[tuple[str, str]] = []
    for key, value in filters.items():
        condition = CONDITIONS[key]
        if condition.encoding == "composite":
            pairs.extend(_COMPOSITE_ENCODERS[key](value))
        elif condition.encoding == "comma":
            resolved = [_resolve_value(condition, v) for v in _as_list(value)]
            pairs.append((condition.wire[0], ",".join(resolved)))
        elif condition.encoding == "repeated":
            for v in _as_list(value):
                pairs.append((f"{condition.wire[0]}[]", _resolve_value(condition, v)))
        else:  # scalar
            pairs.append((condition.wire[0], _resolve_value(condition, value)))
    return pairs


def group_by_wire_param(pairs: Sequence[tuple[str, str]]) -> dict[str, list[str]]:
    """Group wire pairs by their canonical (bracket-stripped) parameter name, preserving
    submission order within each group.

    This is the single definition of "what a wire parameter's identity is, once the `[]`
    array-encoding suffix is set aside" — every consumer that needs to fold a
    repeated-parameter's several pairs back into one entry per canonical name must call
    this rather than keep its own copy of the two-line stripping rule. Two independent
    implementations of one rule is exactly the shape of defect that lets the rule drift
    out from under one of its callers unnoticed — `tools/drop_detection.py`'s
    `detect_dropped` is one caller (imported from here, not re-derived, since this
    module has no corpus dependency of its own); `tools/search.py`'s `applied_filters`
    assembly is the other, and also imports this function rather than re-deriving the
    grouping itself.
    """
    grouped: dict[str, list[str]] = {}
    for wire_key, value in pairs:
        canonical = wire_key[:-2] if wire_key.endswith("[]") else wire_key
        grouped.setdefault(canonical, []).append(value)
    return grouped

