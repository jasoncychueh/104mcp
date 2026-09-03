"""Phase 4 tool-surface tests — design.md Testing Strategy, Components §9, Data Models
(Response shapes / Résumé detail field handling), Error Handling.

Written BLIND to tools/search.py's *implementation* (spec-tester Mode 1 rule): every
assertion below is derived from design.md's declared tool interfaces (Components §9),
Data Models, requirements.md R1-R5/R11/R12, and docs/104-site-facts.md's measured facts —
never from reading tools/search.py itself, which was being rewritten in parallel by
another agent for this same cycle. What WAS read (permitted — completed, prior-phase
modules, not this cycle's blind file):

- `browser/api_client.py` (Endpoint/RawResponse/Verdict, `classify()`, `ENDPOINTS`) —
  needed to build canned HTTP responses and to know, e.g., that HTTP 400 on a family-A
  endpoint maps to `kind="missing_param"` (T-8).
- `tools/helpers.py` (`guarded_api`, the `GuardAbort`/`ERROR_*` payloads) — needed to
  know the guard's `except GuardAbort as e: return e.payload` convention, which is why a
  failure result is asserted to carry ONLY `{"error": ...}`.
- `tools/filters.py`, `tools/categories.py` (`CONDITIONS`, `VALID_FILTER_KEYS`,
  `encode_filters`, `resolve`, `load_dataset`) — already-complete Phase 3 deliverables,
  used directly (not mocked) to build realistic filtered-search scenarios (T-29, T-58)
  and to check an unknown-key/tombstone error names a real valid replacement (T-3, T-4).
- `db/database.py`, `browser/session.py`, and the sibling `tests/test_api_client.py` /
  `tests/test_helpers.py` fixture-harness idiom (`FakeCtx`/`FakeApp`/
  `_install_fake_fetch`) — reused/adapted here so this file's seam-substitution
  matches the rest of the suite: the HTTP transport (`browser.api_client.fetch`) is
  faked (there is no BrowserContext left to fake post-login, §C7 — a session under
  test is built by handing `SessionInfo.cookies` a list directly); `guarded_api`,
  `classify`, `encode_filters`, `resolve` and the actual `tools.search` tool
  functions are exercised for real, never mocked.

Cases: T-1, T-3, T-4, T-5, T-6, T-7, T-8, T-9, T-10, T-11, T-12, T-13, T-14, T-15, T-16,
T-17, T-18, T-28, T-29, T-41, T-42, T-43, T-58, T-59.

**PINNED (was a genuine ambiguity in Mode 1, resolved by the coordinator in Mode 2):**
détail success nests the résumé fields under a `resume` key, alongside `browse_limit` and
`warnings` — `{"resume": {…113 mechanically converted fields…}, "browse_limit": …,
"warnings": [...]}` — per design.md Data Models — Response shapes. Two other readings were
tried and rejected first, and the reasoning matters for how T-13/T-14/T-16 are written:
the whole family-B `data` envelope (this test author's first bet, grounded in
`classify()`'s `payload=data`) was rejected because `data.userName`/`data.companyName` are
the signed-in **operator's** identity, not the candidate's, and would leak into every
détail response; `data["resume"]` flattened to the response's top level (what the
implementation independently produced before this ruling) was rejected because it puts
104's field namespace and ours in one dict with no structural guarantee against a future
collision. T-13/T-14/T-16 therefore compare against `fixture["data"]["resume"]` nested one
level under `result["resume"]`, and T-13 additionally asserts the envelope's other
siblings (operator identity, job-context, telemetry) do NOT leak into the top level.

**A second, narrower assumption:** the exhaustive "every field / no collision" checks
(T-13, T-14, T-16) rely only on order- and count-preservation at every nesting level
(what any mechanical `{f(k): g(v) for k, v in d.items()}` conversion satisfies) — never on
knowing the exact camelCase→snake_case algorithm. Row-level tests (T-1, T-7, T-9, T-10,
T-11, T-17, T-18) deliberately do NOT run this exhaustive check: design.md calls row
fields "an allow-list" (Data Models — Response shapes), which — unlike détail's explicit
"every field... no field omitted" — leaves open whether the row shape is a filtered
subset of the raw fields. Row tests therefore spot-check only fields whose spelling is
invariant under any reasonable snake_case conversion (`candidate_id` per the one
documented `idNo` exception; `age`, `sex` — already lowercase, single-word) rather than
asserting a specific multi-word mapping (e.g. `userName` → `user_name`) as fact.

**Fixture gap disclosed, matching the precedent already set in tests/fixtures/README.md
and tests/test_api_client.py for other unmeasured shapes:** no committed fixture exists
for `list_jobs`' success body (unlike the other four tools). T-17/T-18 use a body
synthesised from the field names docs/104-site-facts.md §6b.3b records for
`/job-api/jobs/recommend` (`jobno`, `job`, `switch`, `role`, `department`) — the
BEHAVIOUR (all jobs returned, the flag passed through unfiltered) is covered, but the
exact body shape is not measured to the byte.

**T-8's HTTP 400 body is likewise synthesised**, not captured: docs/104-site-facts.md
line 711 only records "未帶 `jobno` 時推薦與配對回 HTTP 400 + JSON（不是 404，也不是空
結果）" with no example body. `classify()`'s family-A branch dispatches on `raw.status ==
400` alone (any dict body, regardless of its `status`/`message` values) to
`kind="missing_param"`, so the exact body content does not affect which code path fires
— only the HTTP status does, and that IS measured.

**T-42's "warning threshold" is now pinned at `on_that_day_count >= 270`** (ninety per
cent of the measured 300 daily maximum) per design.md Data Models — Response shapes.
Tested directly at 270 (warning fires) and at 269 (it does not) in addition to the
separate at-maximum non-refusal check — the design basis originally gave no number here,
so the first draft of this test only exercised the unambiguous at-maximum case.

**T-59's docstring-content checks are deliberately loose** (substring/keyword presence,
not exact wording) since design.md states WHAT each docstring must say, never the exact
Traditional-Chinese prose. A loose check can false-negative on unlucky phrasing but
cannot false-positive on a docstring that never raises the concept at all.
"""
from __future__ import annotations

import copy
import dataclasses
import json
import re
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio

from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools.helpers import get_session_id
import mcp104.tools.categories as categories
import mcp104.tools.filters as filters
import mcp104.tools.helpers as helpers_mod
import mcp104.tools.search as search_mod
from mcp104.tools.search import register_search_tools

FIXTURES_DIR = Path(__file__).parent / "fixtures"


# ── Fixture / fake-transport plumbing (mirrors tests/test_api_client.py's idiom) ────

def _load(name: str):
    return json.loads((FIXTURES_DIR / name).read_text(encoding="utf-8"))


def _raw(status: int, content_type: str, body: str, parsed_json, location: str | None = None) -> RawResponse:
    return RawResponse(status=status, location=location, content_type=content_type,
                        body=body, parsed_json=parsed_json)


def _raw_from_wrapper(fixture_name: str) -> RawResponse:
    d = _load(fixture_name)
    return _raw(d["http_status"], d["content_type"], d["body_text"], d["body_json"])


def _raw_from_bare_json(fixture_name: str, http_status: int = 200) -> RawResponse:
    body_json = _load(fixture_name)
    return _raw(http_status, "application/json; charset=utf-8",
                json.dumps(body_json, ensure_ascii=False), body_json)


def _raw_from_body(body: dict, status: int = 200) -> RawResponse:
    return _raw(status, "application/json; charset=utf-8", json.dumps(body, ensure_ascii=False), body)


class _FetchSpy:
    """Records every call and always answers with the same canned RawResponse —
    sufficient for every case here, none of which needs per-call variation."""

    def __init__(self, response: RawResponse):
        self._response = response
        self.calls: list[tuple[object, object, object]] = []

    async def __call__(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append((endpoint, params, body))
        return self._response


class _NeverCalledFetch:
    """For the tombstone/unknown-key cases: the request must never be issued."""

    async def __call__(self, *args, **kwargs):
        raise AssertionError("fetch must not be called — this case must be rejected before any request")


def _install_fake_fetch(monkeypatch, raw: RawResponse) -> _FetchSpy:
    spy = _FetchSpy(raw)
    # tools/helpers.py does `from mcp104.browser.api_client import ..., fetch, ...` —
    # a name-bound import, so guarded_api's call resolves via
    # mcp104.tools.helpers.fetch, not mcp104.browser.api_client.fetch; both must be
    # patched (confirmed by reading tools/helpers.py's import line — an
    # already-complete, non-blind module).
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


def _install_never_called_fetch(monkeypatch) -> None:
    spy = _NeverCalledFetch()
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)


class FakeSessionObj:
    pass


class FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class FakeApp:
    def __init__(self, session_pool: SessionPool, database: Database):
        self.session_pool = session_pool
        self.config = get_config()
        self.db = database


class FakeCtx:
    def __init__(self, session_pool: SessionPool, database: Database):
        self.session = FakeSessionObj()
        self.request_context = FakeRequestContext(FakeApp(session_pool, database))


class FakeMCP:
    """Stand-in for FastMCP: `.tool()` just records the function under its own name
    (or an explicit `name=` kwarg) and returns it unchanged — real FastMCP's decorator
    does the same (register-and-return-unmodified), which is what lets the
    `@require_login`-wrapped function underneath be called directly in tests.

    Also records each tool's EFFECTIVE user-facing description: real FastMCP's
    `.tool()` signature (confirmed via `inspect.signature(FastMCP.tool)` — the
    installed third-party library, not this project's code) accepts a `description=`
    kwarg that, when given, is what an Agent actually sees instead of `fn.__doc__`.
    T-59 checks the description an Agent would read, so it must follow the same
    precedence FastMCP itself uses, not blindly inspect `fn.__doc__`."""

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


def _register_tools() -> tuple[dict[str, object], dict[str, str]]:
    mcp = FakeMCP()
    register_search_tools(mcp)
    return mcp.tools, mcp.descriptions


TOOLS, TOOL_DESCRIPTIONS = _register_tools()
search_resumes = TOOLS["search_resumes"]
list_recommended_resumes = TOOLS["list_recommended_resumes"]
list_matched_resumes = TOOLS["list_matched_resumes"]
get_resume_detail = TOOLS["get_resume_detail"]
list_jobs = TOOLS["list_jobs"]


# Every Database _new_session() opens is tracked here and closed by
# _close_opened_databases (autouse, below) at the end of the test that opened it.
# aiosqlite.connect() runs a NON-DAEMON worker thread per open connection — an
# unclosed one keeps the interpreter alive after pytest's own summary has already
# printed (confirmed by direct measurement: one leaked connection reproduces `pytest
# ... ; echo $?` hanging past a green summary until something kills it; closing it
# makes the same run exit 0). tests/test_database.py's own `db` fixture already closes
# what it opens the same way (`yield database; await database.close()`) — this file
# just needed the equivalent, since _new_session() is a plain helper called from
# inside test bodies (sometimes more than once per test — see T-43), not a fixture
# itself, so tracking + draining in one autouse teardown is the least invasive way to
# guarantee every one of them gets closed without threading a fixture through every
# call site.
_OPENED_DATABASES: list[Database] = []


async def _new_session(tmp_path) -> tuple[FakeCtx, SessionInfo, Database]:
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init("test@104.com")
    _OPENED_DATABASES.append(database)
    ctx = FakeCtx(pool, database)
    sid = get_session_id(ctx)
    # guarded_api reads credentials straight off SessionInfo.cookies now —
    # there is no BrowserContext left to fake post-login (§C7).
    info = SessionInfo(cookies=[], account_label="test@104.com")
    pool.activate_direct(sid, info)
    return ctx, info, database


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED_DATABASES:
        await _OPENED_DATABASES.pop().close()


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """Neutralizes the pacing floor's inline sleep — this file exercises tool-surface
    behaviour, not pacing (tests/test_throttle.py's job). Identical fixture to
    tests/test_api_client.py / tests/test_helpers.py."""
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


# ── Structural helpers for the résumé-detail "every field, mechanically converted,
# no collision" claims (T-13, T-14, T-16) — see module docstring for the payload-shape
# assumption these rely on. Neither helper needs to know the exact camelCase→snake_case
# algorithm: both rely only on key-count and key-order preservation, which any
# mechanical `{f(k): g(v) for k, v in d.items()}` conversion satisfies. ─────────────

_SNAKE_KEY_RE = re.compile(r"^[a-z0-9_]*$")


def _assert_mechanical_conversion(source, response, path="$"):
    """Recursively assert `response` is a snake-cased, value-preserving mirror of
    `source`. A field-count drop at any level means two sibling keys collided after
    conversion (T-14's "no collision" claim); a value change anywhere means something
    was renamed/derived/altered rather than mechanically converted (T-13/T-16)."""
    if isinstance(source, dict):
        assert isinstance(response, dict), f"{path}: expected an object, got {type(response).__name__}"
        assert len(response) == len(source), (
            f"{path}: field count changed converting keys ({len(source)} source keys -> "
            f"{len(response)} response keys) — a non-lossy mechanical conversion must "
            f"preserve every field; a drop here means two sibling keys collided"
        )
        for (src_key, src_val), (resp_key, resp_val) in zip(source.items(), response.items()):
            assert _SNAKE_KEY_RE.match(resp_key), f"{path}.{resp_key}: not snake_case (source key was {src_key!r})"
            _assert_mechanical_conversion(src_val, resp_val, f"{path}.{src_key}")
    elif isinstance(source, list):
        assert isinstance(response, list), f"{path}: expected a list, got {type(response).__name__}"
        assert len(response) == len(source), f"{path}: list length changed ({len(source)} -> {len(response)})"
        for i, (s, r) in enumerate(zip(source, response)):
            _assert_mechanical_conversion(s, r, f"{path}[{i}]")
    else:
        assert response == source, f"{path}: value changed from {source!r} to {response!r}"


def _corresponding(source_root, response_root, *path):
    """Walk `path` (SOURCE key names) through both trees in lockstep BY POSITION, not
    by name — lets a caller fetch "whatever the response calls
    data['resume']['email']" without knowing the conversion algorithm. Asserts the
    same count-preservation invariant locally at each level walked."""
    src, resp = source_root, response_root
    for key in path:
        assert isinstance(src, dict) and isinstance(resp, dict), f"expected objects while walking to {path!r}"
        src_keys = list(src.keys())
        resp_keys = list(resp.keys())
        assert len(src_keys) == len(resp_keys), (
            f"key count mismatch while walking to {path!r} at {key!r}: "
            f"{len(src_keys)} source keys vs {len(resp_keys)} response keys"
        )
        idx = src_keys.index(key)
        src = src[key]
        resp = resp[resp_keys[idx]]
    return resp


def _contains_value(node, value):
    """Recursively search for `value` anywhere in `node` — used where the exact
    converted key name is not known but a specific passed-through VALUE must survive
    somewhere (T-18). Bool/int are compared by identity to avoid `True == 1` false
    positives against unrelated numeric fields."""
    if isinstance(node, bool) or isinstance(value, bool):
        if node is value:
            return True
    elif node == value:
        return True
    if isinstance(node, dict):
        return any(_contains_value(v, value) for v in node.values())
    if isinstance(node, list):
        return any(_contains_value(v, value) for v in node)
    return False


# ── T-1 (R1.1): keyword search returns rows, pagination, quota, warnings ────────────

@pytest.mark.asyncio
async def test_keyword_search_returns_documented_success_shape(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("rows_search.json")
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_search.json"))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" not in result
    assert result["pagination"] == {"page": 1, "total_pages": 30, "total": 300063}
    assert result["browse_limit"] == {"resume_max": 300, "on_that_day_count": "11"}
    assert isinstance(result["warnings"], list)
    assert "applied_filters" in result

    fixture_rows = fixture["result"]["data"]
    assert len(result["results"]) == len(fixture_rows) == 50
    first = result["results"][0]
    # candidate_id <- idNo is the one documented row-field renaming exception
    # (design.md Data Models — Response shapes); age/sex are invariant under any
    # camelCase->snake_case conversion (single lowercase words already).
    assert first["candidate_id"] == fixture_rows[0]["idNo"] == "1683384082755"
    assert first["age"] == fixture_rows[0]["age"] == 54
    assert first["sex"] == fixture_rows[0]["sex"] == 1


# ── T-3 (R1.3): unrecognised filters key -> error naming it + valid keys, no request ─

@pytest.mark.asyncio
async def test_unknown_filters_key_errors_naming_it_and_valid_keys_no_request(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    result = await search_resumes(keyword="x", filters={"totally_bogus_key_zzz": "1"}, ctx=ctx)

    assert "error" in result
    assert "results" not in result
    assert "totally_bogus_key_zzz" in result["error"]
    assert any(valid_key in result["error"] for valid_key in filters.VALID_FILTER_KEYS), (
        "R1.3: the error must name the valid keys"
    )


# ── T-4 (R1.4): retired argument -> error naming a replacement, no request ──────────

@pytest.mark.asyncio
@pytest.mark.parametrize("tombstone_kwarg", ["area", "experience", "education"])
async def test_retired_argument_errors_naming_a_replacement_no_request(tombstone_kwarg, tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    result = await search_resumes(keyword="x", ctx=ctx, **{tombstone_kwarg: "台北市"})

    assert "error" in result
    assert "results" not in result
    assert any(valid_key in result["error"] for valid_key in filters.VALID_FILTER_KEYS), (
        f"R1.4: retired argument {tombstone_kwarg!r} must name a replacement that "
        f"exists in the condition table"
    )


# ── T-5 (R1.5): successful search carries applied_filters/browse_limit/warnings ─────

@pytest.mark.asyncio
async def test_successful_search_carries_applied_filters_browse_limit_warnings_present(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_search.json"))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" not in result
    assert "applied_filters" in result
    assert "browse_limit" in result
    assert "warnings" in result


# ── T-6 (R1.6): requesting a page fetches exactly one page ──────────────────────────

@pytest.mark.asyncio
async def test_requesting_a_page_fetches_exactly_one_page(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_search.json"))

    result = await search_resumes(keyword="應用工程師", page=3, ctx=ctx)

    assert "error" not in result
    assert len(spy.calls) == 1, "a page request must fetch exactly one page, never more on the caller's behalf"


# ── T-7 (R2.1): recommend with a job id returns rows in the documented shape ────────

@pytest.mark.asyncio
async def test_recommend_with_job_id_returns_documented_shape(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("rows_recommend.json")
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_recommend.json"))

    result = await list_recommended_resumes(jobno="12355016", ctx=ctx)

    assert "error" not in result
    assert result["pagination"] == {"page": 1, "total_pages": 10, "total": 499}
    assert result["browse_limit"] == {"resume_max": 300, "on_that_day_count": "14"}
    fixture_rows = fixture["result"]["data"]
    assert len(result["results"]) == len(fixture_rows) == 50
    assert result["results"][0]["candidate_id"] == fixture_rows[0]["idNo"]


# ── T-8 (R2.2, R3.4): no job id -> missing-parameter error, not an empty result ─────

_MISSING_PARAM_BODY = {
    "status": "USER_ERROR",
    "message": "缺少必要參數 jobno",
    "result": None,
    "url": "",
    "data": None,
}


@pytest.mark.asyncio
@pytest.mark.parametrize("tool_name", ["list_recommended_resumes", "list_matched_resumes"])
async def test_recommend_or_match_with_no_job_id_surfaces_missing_param_error(tool_name, tmp_path, monkeypatch):
    tool = TOOLS[tool_name]
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_fake_fetch(monkeypatch, _raw_from_body(copy.deepcopy(_MISSING_PARAM_BODY), status=400))

    result = await tool(jobno="", ctx=ctx)

    assert len(spy.calls) == 1, (
        "R2.2/R3.4: the system surfaces the SERVER's rejection, which implies a real "
        "request is made and its HTTP 400 response drives the error — not a client-side "
        "short-circuit before any request"
    )
    assert "error" in result
    assert "results" not in result, "must not be presented as an empty result (R2.2/R3.4)"


# ── T-9 (R2.3): job with no recommendations -> empty result set, not an error ───────

@pytest.mark.asyncio
async def test_recommend_job_with_no_recommendations_is_empty_success_not_error(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("zero_row_recommend.json"))

    result = await list_recommended_resumes(jobno="99999999", ctx=ctx)

    assert "error" not in result
    assert result["results"] == []
    assert result["pagination"] == {"page": 1, "total_pages": 0, "total": 0}


# ── T-10 (R3.1): match with a job id returns rows in the documented shape ───────────

@pytest.mark.asyncio
async def test_match_with_job_id_returns_documented_shape(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("rows_match.json")
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_match.json"))

    result = await list_matched_resumes(jobno="12355016", ctx=ctx)

    assert "error" not in result
    fixture_rows = fixture["result"]["resumes"]
    assert len(result["results"]) == len(fixture_rows) == 3
    assert result["browse_limit"] == {"resume_max": 300, "on_that_day_count": "14"}
    assert result["results"][0]["candidate_id"] == fixture_rows[0]["idNo"] == "20000001239202"


# ── T-11 (R3.2): match route reads container/page from its OWN key names ───────────

@pytest.mark.asyncio
async def test_match_route_reads_container_and_page_from_its_own_keys(tmp_path, monkeypatch):
    # rows_match.json's `result` carries NO `data` key and its `pageInfo` carries NO
    # `this_page` key at all (only `resumes` / `thisPage` — docs/104-site-facts.md
    # §6b.3b, tests/fixtures/README.md) — a reader sharing search/recommend's key names
    # would find nothing here, and per design.md must not silently read that as empty.
    fixture = _load("rows_match.json")
    assert "data" not in fixture["result"]
    assert "this_page" not in fixture["result"]["pageInfo"]

    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_match.json"))

    result = await list_matched_resumes(jobno="12355016", ctx=ctx)

    assert "error" not in result
    assert len(result["results"]) == 3
    assert result["pagination"]["page"] == 1
    assert result["pagination"]["total"] == 3


# ── T-12 (R3.3): closed job on match route -> server's own explanation ──────────────

@pytest.mark.asyncio
async def test_match_closed_job_reports_servers_own_explanation(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    # Real capture: HTTP 404 + JSON, DATA_NOT_FOUND / "此職務已關閉", for
    # /api/match/matchResult?jobno=14832961 on a closed job.
    _install_fake_fetch(monkeypatch, _raw_from_wrapper("failure_data_not_found.json"))

    result = await list_matched_resumes(jobno="14832961", ctx=ctx)

    assert "results" not in result
    assert "error" in result
    assert "此職務已關閉" in result["error"] or "DATA_NOT_FOUND" in result["error"]


# ── update_date_type: omitted / passed through / rejected when blank ────────────────

@pytest.mark.asyncio
async def test_match_update_date_type_omitted_sends_no_wire_param(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_match.json"))

    result = await list_matched_resumes(jobno="12355016", ctx=ctx)

    assert "error" not in result
    _endpoint, params, _body = spy.calls[0]
    assert all(key != "updateDateType" for key, _value in params)


@pytest.mark.asyncio
async def test_match_update_date_type_passed_through_verbatim(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    spy = _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_match.json"))

    result = await list_matched_resumes(jobno="12355016", ctx=ctx, update_date_type="2")

    assert "error" not in result
    _endpoint, params, _body = spy.calls[0]
    assert params.count(("updateDateType", "2")) == 1


@pytest.mark.asyncio
async def test_match_update_date_type_blank_rejected_before_request(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    result = await list_matched_resumes(jobno="12355016", ctx=ctx, update_date_type="  ")

    assert "results" not in result
    assert "error" in result
    assert "內部設定錯誤" not in result["error"], (
        "must not be reported as a client configuration fault (Error Handling "
        "scenarios 1/8) — this is scenario 2, a server-reported not-found"
    )


# ── T-13 (R4.1): résumé detail returns every field the server provided ──────────────

@pytest.mark.asyncio
async def test_get_resume_detail_returns_every_field_the_server_provided(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("resume_unrestricted.json")
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("resume_unrestricted.json"))

    result = await get_resume_detail(candidate_id="1725089703433", ctx=ctx)

    assert "error" not in result
    assert "warnings" in result and isinstance(result["warnings"], list)
    # PINNED (design.md Data Models — Response shapes, coordinator ruling): the résumé
    # fields nest under a `resume` key, alongside `browse_limit` and `warnings` — this
    # matches the other four tools' named-container convention, rather than either of
    # the two readings this test bet on and rejected earlier (the whole family-B `data`
    # envelope, or `data.resume` flattened to the response's own top level).
    assert "resume" in result
    source_resume = fixture["data"]["resume"]
    response_resume = result["resume"]
    assert len(response_resume) == len(source_resume), (
        "field count of the nested `resume` object changed from the server's own "
        "data.resume (R4.1: 'return every field the server provides')"
    )
    assert _corresponding(source_resume, response_resume, "idNo") == "1725089703433"
    assert _corresponding(source_resume, response_resume, "userName") == "邱文家"

    # design.md: "the envelope's other siblings are deliberately dropped" — userName/
    # companyName at the ENVELOPE level are the signed-in OPERATOR's identity, not the
    # candidate's (data.resume already carries the candidate's own userName, asserted
    # above), and job-context/telemetry siblings (hasOnJob, jobNo, resumeTemplate, ...)
    # are dropped for the same reason. Only `resume` and `browse_limit` are surfaced.
    assert set(result.keys()) <= {"resume", "browse_limit", "warnings"}, (
        "only resume, browse_limit and warnings may appear — the operator-identity and "
        "job-context siblings of the raw envelope must not leak into the response"
    )


# ── T-14 (R4.2): mechanical conversion at every depth, no collision ─────────────────

@pytest.mark.asyncio
async def test_get_resume_detail_key_conversion_is_mechanical_with_no_collision(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("resume_unrestricted.json")
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("resume_unrestricted.json"))

    result = await get_resume_detail(candidate_id="1725089703433", ctx=ctx)

    assert "error" not in result
    assert "resume" in result
    source_resume = fixture["data"]["resume"]
    # Recurses through every nested object and array (achievement.list, customResume,
    # expJobArr, etc.) — a collision anywhere, at any depth, shows up as a field-count
    # mismatch at that level. PINNED shape: compared against the nested `resume` object,
    # not the whole envelope (see test_..._returns_every_field... above).
    _assert_mechanical_conversion(source_resume, result["resume"], path="$data.resume")


# ── T-15 (R4.3): restricted-contact résumé appends a warning naming affected fields ─

@pytest.mark.asyncio
async def test_get_resume_detail_restricted_contact_appends_warning(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("resume_restricted.json"))

    result = await get_resume_detail(candidate_id="1842629170771", ctx=ctx)

    assert "error" not in result
    assert "warnings" in result
    assert len(result["warnings"]) >= 1, "restricted résumé must append a masking warning (R4.3)"
    warning_text = " ".join(result["warnings"])
    assert "email" in warning_text, (
        "the warning must name the affected fields; 'email' is one of the fields "
        "docs/104-site-facts.md §6b.3d and tests/fixtures/README.md confirm are "
        "placeholder-filled on a restricted résumé, and is unchanged by any "
        "camelCase->snake_case conversion (already a single lowercase word), so this "
        "check does not depend on the conversion algorithm"
    )


# ── T-16 (R4.4): restricted contact fields byte-identical, no derived flag ──────────

@pytest.mark.asyncio
async def test_get_resume_detail_restricted_contact_fields_byte_identical_no_derived_flag(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("resume_restricted.json")
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("resume_restricted.json"))

    result = await get_resume_detail(candidate_id="1842629170771", ctx=ctx)

    assert "error" not in result
    assert "resume" in result
    source_resume = fixture["data"]["resume"]
    response_resume = result["resume"]

    # tests/fixtures/README.md / docs/104-site-facts.md §6b.3d: these are 104's OWN
    # masking placeholders, not emptied values — callTimeDesc is even LONGER than its
    # unrestricted counterpart, defeating any "shorter means masked" heuristic — so the
    # only correct treatment is byte-identical passthrough. PINNED shape: compared
    # against the nested `resume` object, one level shallower than this test's first
    # draft (see test_..._returns_every_field... above).
    for field in ("email", "phoneDescAll", "callTimeDesc", "addressDesc"):
        expected = source_resume[field]
        actual = _corresponding(source_resume, response_resume, field)
        assert actual == expected, f"resume.{field} must be byte-identical to the fixture, was altered"
    expected_phone = source_resume["phone"]
    actual_phone = _corresponding(source_resume, response_resume, "phone")
    assert actual_phone == expected_phone

    # No derived boolean/summary added anywhere: this recursive count-preservation
    # check would fail if any extra key (e.g. a synthesised "is_restricted" flag) had
    # been injected as a sibling of any existing field, at any depth.
    _assert_mechanical_conversion(source_resume, response_resume, path="$data.resume")


# ── T-17 (R5.1) / T-18 (R5.2): job list — every job including closed, flag passthrough

# No committed fixture exists for list_jobs' success body (unlike the other four
# tools) — see module docstring. Field names per docs/104-site-facts.md §6b.3b:
# jobno, job, switch, role, department{id, code, name}.
_SYNTHETIC_JOB_LIST_BODY = {
    "data": [
        {"jobno": "12355016", "job": "應用工程師", "switch": True, "role": "1",
         "department": {"id": "1001", "code": "D001", "name": "應用工程部"}},
        {"jobno": "99999999", "job": "已關閉的測試職缺", "switch": False, "role": "1",
         "department": {"id": "1002", "code": "D002", "name": "測試部"}},
    ],
    "metadata": {},
}


@pytest.mark.asyncio
async def test_list_jobs_returns_every_job_including_closed(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(copy.deepcopy(_SYNTHETIC_JOB_LIST_BODY)))

    result = await list_jobs(ctx=ctx)

    assert "error" not in result
    assert len(result["results"]) == 2, "list_jobs must return every job, including closed ones (R5.1)"


@pytest.mark.asyncio
async def test_list_jobs_open_closed_flag_passed_through_unfiltered(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_body(copy.deepcopy(_SYNTHETIC_JOB_LIST_BODY)))

    result = await list_jobs(ctx=ctx)

    assert "error" not in result
    assert len(result["results"]) == 2
    # Passed through verbatim, not normalised/filtered away — a value-search avoids
    # needing to know the flag's converted key name.
    assert _contains_value(result["results"][0], True), "open job's flag must survive"
    assert _contains_value(result["results"][1], False), "closed job's flag must survive, not be filtered out"


# ── MalformedResponseError moved to tools/helpers.py (messaging migration, ARB-2) ───
#
# search.py's behaviour is pinned as UNCHANGED across the move: the class the five
# read tools catch must still be the class their own extractors raise, and the
# malformed-response cases below (not previously exercised by this file) must pass
# unmodified. If either needs editing to pass, the move changed behaviour and must be
# reconsidered rather than accommodated (the plan's own condition on ARB-2).

def test_malformed_response_error_class_identity_across_modules():
    assert search_mod.MalformedResponseError is helpers_mod.MalformedResponseError


@pytest.mark.asyncio
async def test_search_resumes_missing_page_info_is_malformed_not_empty_result(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("rows_search.json"))
    del body["result"]["pageInfo"]
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" in result
    assert "results" not in result
    assert "104 回應結構異常" in result["error"]


@pytest.mark.asyncio
async def test_list_jobs_missing_data_is_malformed_not_empty_result(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = {"metadata": {"page": 1, "total": 0}}  # no "data" key at all
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await list_jobs(ctx=ctx)

    assert "error" in result
    assert "results" not in result
    assert "104 回應結構異常" in result["error"]


# ── T-28 (R7.3): a dropped condition -> error naming it, no results key ─────────────

@pytest.mark.asyncio
async def test_dropped_filter_condition_errors_naming_it_no_results_key(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("zero_row_search.json"))
    # zero_row_search.json's searchForm already carries sex=2 (104's own "unfiltered"
    # default) and kws matching what we send below — sending filters={"sex": "1"}
    # against this UNCHANGED echo is exactly a dropped condition: sent "1", echoed "2".
    assert body["result"]["searchForm"]["sex"] == 2
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(
        keyword="zzqxvkwj9871nonexistentkeyword", filters={"sex": "1"}, ctx=ctx
    )

    assert "error" in result
    assert "results" not in result
    assert "sex" in result["error"], "R7.3: the error must name the dropped condition"


# ── T-29 (R7.4): applied_filters carries resolved wire values, not caller's names ───

@pytest.mark.asyncio
async def test_applied_filters_carries_resolved_wire_value_not_callers_name(tmp_path, monkeypatch):
    dataset = categories.load_dataset("JobCat.json", "jobcat")
    leaf = next(n for n in dataset.nodes if not n.children)
    resolution = categories.resolve(dataset, leaf.name)
    assert resolution.status == "resolved"
    assert resolution.code != leaf.name, "need a case where the resolved code differs from the caller's name"

    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("zero_row_search.json"))
    body["result"]["searchForm"]["jobcat"] = resolution.code  # confirm, don't drop
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(
        keyword="zzqxvkwj9871nonexistentkeyword", filters={"jobcat": leaf.name}, ctx=ctx
    )

    assert "error" not in result, f"unexpected failure: {result}"
    assert result["applied_filters"].get("jobcat") == resolution.code
    assert leaf.name not in result["applied_filters"].values(), (
        "applied_filters must carry the server-confirmed wire value, never the "
        "caller's own input name"
    )


# ── T-41 (R11.1, R11.2): quota key present on every success, null when absent ───────

@pytest.mark.asyncio
async def test_quota_key_present_and_populated_on_success(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_search.json"))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" not in result
    assert result["browse_limit"] == {"resume_max": 300, "on_that_day_count": "11"}


@pytest.mark.asyncio
async def test_quota_key_present_and_null_when_response_carries_none(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("zero_row_search.json"))
    del body["result"]["browseLimit"]
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword="zzqxvkwj9871nonexistentkeyword", ctx=ctx)

    assert "error" not in result
    assert "browse_limit" in result, "the key must be present even with nothing to report (R11.2)"
    assert result["browse_limit"] is None


# ── T-42 (R11.3, R11.4): reaching threshold warns; reaching maximum doesn't refuse ──

@pytest.mark.asyncio
async def test_quota_reaching_threshold_appends_warning(tmp_path, monkeypatch):
    # design.md Data Models — Response shapes (pinned): "the warning threshold is
    # on_that_day_count >= 270" — ninety per cent of the measured 300 daily maximum.
    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("rows_search.json"))
    body["result"]["browseLimit"] = {"resumeMax": 300, "onThatDayCount": "270"}
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" not in result
    assert len(result["warnings"]) >= 1, "reaching the pinned warning threshold (270) must append a warning (R11.3)"


@pytest.mark.asyncio
async def test_quota_below_threshold_appends_no_warning(tmp_path, monkeypatch):
    # Negative counterpart, only meaningful now that a real number is pinned: one below
    # 270 must NOT trigger the quota warning (a test that only ever checks the positive
    # side would still pass if the threshold were e.g. always-on).
    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("rows_search.json"))
    body["result"]["browseLimit"] = {"resumeMax": 300, "onThatDayCount": "269"}
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" not in result
    assert result["warnings"] == [], "below the pinned threshold (270) no quota warning should fire"


@pytest.mark.asyncio
async def test_quota_at_maximum_does_not_refuse_call(tmp_path, monkeypatch):
    # design.md (pinned): "the tool does not refuse at the maximum — the allowance is
    # 104's to enforce, we have never observed it enforcing one" (R11.4).
    ctx, _info, _db = await _new_session(tmp_path)
    body = copy.deepcopy(_load("rows_search.json"))
    body["result"]["browseLimit"] = {"resumeMax": 300, "onThatDayCount": "300"}
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword="應用工程師", ctx=ctx)

    assert "error" not in result, "reaching the quota maximum must not refuse the call on its own (R11.4)"
    assert "results" in result


# ── T-43 (R12.3): success/empty share one shape; failure carries no results key ─────

@pytest.mark.asyncio
async def test_success_and_empty_result_share_one_shape_per_tool(tmp_path, monkeypatch):
    ctx1, _info1, _db1 = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("rows_search.json"))
    success = await search_resumes(keyword="應用工程師", ctx=ctx1)

    ctx2, _info2, _db2 = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_bare_json("zero_row_search.json"))
    empty = await search_resumes(keyword="zzqxvkwj9871nonexistentkeyword", ctx=ctx2)

    assert "error" not in success and "error" not in empty
    assert set(success.keys()) == set(empty.keys()), (
        "a tool's success shape must not vary between a populated and an empty result "
        "(design.md Data Models: 'the stable-shape promise is per tool')"
    )
    assert empty["results"] == []


@pytest.mark.asyncio
async def test_failure_carries_no_results_key_and_no_empty_containers(tmp_path, monkeypatch):
    ctx, _info, _db = await _new_session(tmp_path)
    _install_fake_fetch(monkeypatch, _raw_from_wrapper("failure_family_a_logged_out.json"))

    result = await search_resumes(keyword="工程師", ctx=ctx)

    assert "error" in result
    assert set(result.keys()) == {"error"}, (
        "a failure must carry no results key and no empty containers (design.md Data "
        "Models: 'deliberately no empty containers either, since padding an error with "
        "them would make it structurally resemble a successful empty result')"
    )


# ── T-58 (e2e, R1.1+R7.1): filtered search resolves a name, submits, confirms, rows ─

@pytest.mark.asyncio
async def test_filtered_search_resolves_name_submits_and_confirms_in_echo(tmp_path, monkeypatch):
    dataset = categories.load_dataset("JobCat.json", "jobcat")
    leaf = next(n for n in dataset.nodes if not n.children)
    resolution = categories.resolve(dataset, leaf.name)
    assert resolution.status == "resolved", "picked dataset node must resolve at the exact tier"

    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("rows_search.json")
    body = copy.deepcopy(fixture)
    body["result"]["searchForm"]["jobcat"] = resolution.code
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword="應用工程師", filters={"jobcat": leaf.name}, ctx=ctx)

    assert "error" not in result, f"filtered search unexpectedly failed: {result}"
    assert len(result["results"]) == len(fixture["result"]["data"]) == 50
    assert result["applied_filters"].get("jobcat") == resolution.code


# ── Shared sweep helpers for T-68 / T-70 (Round I5, added after 7afe06d) ────────────
#
# Round I5's own brief arrived carrying two node names -- "人力仲介代辦" /
# "人力仲介代辦服務業" -- that do not exist in any bundled dataset; the coordinator had
# copied them from another agent's report without being able to read the report's own
# (mojibake'd) output. The correction: "do not hard-code either name from any brief,
# mine included... let the data supply the oracle." Both discovery helpers below take
# that literally -- neither hard-codes a dataset node name anywhere. They locate their
# scenario by calling `categories.resolve()` itself (never re-implementing its tiering
# or refusal rule) and confirm choosability the same way T-69 does: by round-tripping
# a candidate's own code back through `resolve()`, never by inspecting node shape.

_ALL_DATASET_FILES = [
    "JobCat.json", "AreaWork.json", "Area.json", "Indust.json",
    "Major.json", "Tool.json", "Skill.json", "Abroad.json",
]

# The dataset -> filter-key mapping is design.md's own "Bundled datasets" table
# (Data Models), stable prior to and untouched by this round's diff -- not read from
# tools/filters.py, which is one of the files being changed in parallel this round.
# Where a dataset backs two keys, either is a valid choice; one is picked arbitrarily.
_DATASET_TO_FILTER_KEY = {
    "JobCat.json": "jobcat",
    "AreaWork.json": "city",
    "Area.json": "home",
    "Indust.json": "workExpInd",
    "Major.json": "major",
    "Tool.json": "goodTools",
    "Skill.json": "certificates",
    "Abroad.json": "studyAbroad",
}


def _round_trip_resolves(dataset, code: str) -> bool:
    return categories.resolve(dataset, code).status == "resolved"


def _find_ambiguous_mixed_instance():
    """Sweep every bundled dataset for an AMBIGUOUS tie (>1 node sharing one exact,
    byte-identical name) whose candidates round-trip to a MIX of outcomes -- at least
    one that resolves when passed back, at least one that does not. Returns
    (dataset_filename, query_name, {code: (name, round_trip_resolves)}) for the first
    one found, or None.
    """
    for dataset_name in _ALL_DATASET_FILES:
        # Round I9: load_dataset() now requires a condition_key (branch-code
        # acceptance is measured per (dataset, condition) pair). Using the SAME
        # mapping here as the caller uses to build its filter (`_DATASET_TO_FILTER_
        # KEY`) is what keeps the round trip below well-defined -- resolving a
        # candidate's code back under a DIFFERENT condition than it was offered
        # under could answer a different question.
        dataset = categories.load_dataset(dataset_name, _DATASET_TO_FILTER_KEY[dataset_name])
        seen_names = set()
        for node in dataset.nodes:
            if node.name in seen_names:
                continue
            seen_names.add(node.name)
            resolution = categories.resolve(dataset, node.name)
            if resolution.status != "ambiguous":
                continue
            outcomes = {
                candidate.code: (candidate.name, _round_trip_resolves(dataset, candidate.code))
                for candidate in resolution.candidates
            }
            resolves = [ok for _name, ok in outcomes.values() if ok]
            refused = [ok for _name, ok in outcomes.values() if not ok]
            if resolves and refused:
                return dataset_name, node.name, outcomes
    return None


def _find_family_instances():
    """Sweep every bundled dataset for two RESOLVED-with-family scenarios (an exact
    hit whose name is a proper prefix of a longer sibling's name):

    - mixed: a family containing at least one sibling that does NOT resolve when its
      own code is passed back (the resolver would refuse it again).
    - pure:  a family that is non-empty and every sibling DOES resolve when passed
      back.

    Returns (mixed, pure), each either None or (dataset_filename, query_name,
    {code: (name, round_trip_resolves)}).
    """
    mixed = pure = None
    for dataset_name in _ALL_DATASET_FILES:
        dataset = categories.load_dataset(dataset_name, _DATASET_TO_FILTER_KEY[dataset_name])
        seen_names = set()
        for node in dataset.nodes:
            if node.name in seen_names:
                continue
            seen_names.add(node.name)
            resolution = categories.resolve(dataset, node.name)
            if resolution.status != "resolved" or not resolution.candidates:
                continue
            outcomes = {
                candidate.code: (candidate.name, _round_trip_resolves(dataset, candidate.code))
                for candidate in resolution.candidates
            }
            all_resolve = all(ok for _name, ok in outcomes.values())
            any_refused = any(not ok for _name, ok in outcomes.values())
            if any_refused and mixed is None:
                mixed = (dataset_name, node.name, outcomes)
            if all_resolve and pure is None:
                pure = (dataset_name, node.name, outcomes)
            if mixed and pure:
                return mixed, pure
    return mixed, pure


# ── T-68 (R9.7, R9.3): an ambiguous name whose tied nodes share an identical raw name
# is refused with a candidate list that tells them apart, and no request is issued ──
#
# Asserted at the TOOL boundary -- what search_resumes actually returns to a caller --
# never against resolve()'s own return value. Round I4 added `terminal` to `Candidate`,
# computed correctly at every construction site inside tools/categories.py, and
# discarded before it reached this exact boundary; every case up to T-67 asserted
# resolve()'s output, which is upstream of that loss, so the suite went green with the
# field shipping inert. This is the case that would have caught it.

@pytest.mark.asyncio
async def test_ambiguous_identically_named_candidates_are_told_apart_no_request(tmp_path, monkeypatch):
    found = _find_ambiguous_mixed_instance()
    assert found is not None, (
        "sanity: the sweep must find at least one ambiguous tie with mixed "
        "choosability somewhere across the bundled datasets (design.md Components §6: "
        "7 of 27 duplicate-name groups are 'mixed')"
    )
    dataset_name, query_name, outcomes = found
    filter_key = _DATASET_TO_FILTER_KEY[dataset_name]

    names = {name for name, _resolves in outcomes.values()}
    assert names == {query_name}, (
        f"the discovered tie must share the identical raw name it was queried with -- "
        f"got {names!r} for query {query_name!r}"
    )

    ctx, _info, _db = await _new_session(tmp_path)
    _install_never_called_fetch(monkeypatch)

    result = await search_resumes(keyword="x", filters={filter_key: query_name}, ctx=ctx)

    assert "error" in result
    assert "results" not in result
    assert "candidates" in result, "an ambiguous refusal must hand back a candidate list"

    returned = {c["code"]: c for c in result["candidates"] if c.get("code") in outcomes}
    assert set(returned) == set(outcomes), (
        f"expected the tied candidates {set(outcomes)} in the returned candidate "
        f"list, got {set(returned)}: {result['candidates']!r}"
    )
    for code, (name, expected_resolves) in outcomes.items():
        assert returned[code]["name"] == name
        assert returned[code]["terminal"] is expected_resolves, (
            f"{code} ({name!r}): stated terminal={returned[code]['terminal']!r} does "
            f"not match what passing this code back actually does "
            f"(round-trip resolves={expected_resolves})"
        )

    # The whole point: identically-named entries must be tellable apart by something
    # other than the code being present in the list.
    terminal_values = {returned[code]["terminal"] for code in outcomes}
    assert len(terminal_values) > 1, (
        f"the tied, identically-named entries all report the same choosability "
        f"({terminal_values!r}) -- they remain indistinguishable to a caller, which "
        f"is exactly the state R9.7 exists to prevent"
    )


# ── T-59 (R5.3, R7.5, R9.5): docstrings state the required contract details ─────────

def test_tool_docstrings_state_required_contract_details():
    # Reads TOOL_DESCRIPTIONS (the effective text an Agent sees — `description=` kwarg
    # if the tool was registered with one, else fn.__doc__; see FakeMCP above), not
    # raw `.__doc__` — a first run of this test against `.__doc__` directly found
    # list_jobs' own docstring is a short pointer ("See _LIST_JOBS_DESCRIPTION.") to a
    # module-level constant passed as `@mcp.tool(description=...)`, confirming real
    # FastMCP's `description=` kwarg (verified via `inspect.signature` on the
    # installed third-party library) is genuinely in use here, not merely possible.
    jobs_doc = TOOL_DESCRIPTIONS.get("list_jobs", "")
    # R5.3: list_jobs' docstring states what a closed job means for the match route —
    # list_jobs is the authoritative job_id source for list_matched_resumes, so a
    # caller reading only THIS tool's docstring needs to know that consequence.
    assert "配對" in jobs_doc or "match" in jobs_doc.lower(), (
        "list_jobs' docstring must state the closed-job consequence for the match route (R5.3)"
    )
    assert "關閉" in jobs_doc or "closed" in jobs_doc.lower()

    search_doc = TOOL_DESCRIPTIONS.get("search_resumes", "")
    # R7.5: expect_pay's semantically-inert "up" mode's actual meaning must be stated
    # (tools/filters.py's own docstring explicitly defers this to tools/search.py).
    assert "expect_pay" in search_doc, "search_resumes' docstring must document the expect_pay condition"
    assert "up" in search_doc, "search_resumes' docstring must mention the semantically inert 'up' mode"

    # R9.5: codes-only conditions must be named — workExpJob's dataset was never
    # identified among the captured category files (tools/filters.py CONDITIONS table).
    assert "workExpJob" in search_doc, "search_resumes' docstring must name workExpJob as codes-only"

    detail_doc = TOOL_DESCRIPTIONS.get("get_resume_detail", "")
    # Data Models — "Résumé detail field handling": restricted-contact placeholder
    # behaviour must be documented, not just implemented.
    assert any(term in detail_doc for term in ("佔位", "placeholder", "遮罩", "隱藏")), (
        "get_resume_detail's docstring must state that restricted-contact fields hold "
        "placeholder text (R4.3 / Data Models)"
    )

    # The unbridgeable "second identifier" (pId) — Data Models: "returned and
    # documented rather than omitted... a documented one carries the warning that its
    # key space is unknown and must not be bridged to the messaging id space."
    all_list_docs = " ".join([
        search_doc, jobs_doc,
        TOOL_DESCRIPTIONS.get("list_recommended_resumes", ""),
        TOOL_DESCRIPTIONS.get("list_matched_resumes", ""),
    ])
    assert "pId" in all_list_docs or "p_id" in all_list_docs or "pid" in all_list_docs.lower(), (
        "some list tool's docstring must document the second identifier (pId) and "
        "that it is not confirmed to share the messaging id space"
    )


# ── T-67 (R9.5), REVISED for part A of the filter-value-discovery feature: the
# per-key acceptance text (`categories.RESOLVE_ACCEPTS_ZH`, 133 chars) used to be
# emitted VERBATIM once per dataset-backed key inside `_filter_key_reference()` --
# 1,463 chars of pure repetition, a third of why search_resumes' published
# description was 5,031 chars against Claude Code's measured 2,048-char slice budget
# (see CLAUDE.md) and silently lost 59% of itself. The fix removes the repetition
# entirely rather than stating it once inline: `_filter_key_reference()` now names
# only the BACKING DATASET per key (`tools/discovery.py`'s `browse_filter_values`
# is where the full acceptance rule is read, in full, exactly once). These two
# cases now check that removal actually happened -- not merely got smaller -- and
# that the published description still gives a caller a live path to the full rule.

def test_filter_key_reference_no_longer_embeds_the_resolver_acceptance_text(monkeypatch):
    """`_filter_key_reference()` must not emit `categories.RESOLVE_ACCEPTS_ZH`
    (in full or in part) at all -- not once, and not once per dataset-backed key.
    Patching the export to a sentinel and finding ZERO occurrences in a freshly
    generated reference is what actually distinguishes "removed" from "just
    shortened"; a substring check against the live (unpatched) export could still
    pass by coincidence against unrelated short text."""
    dataset_backed_keys = [
        key for key, cond in filters.CONDITIONS.items() if cond.value_source == "dataset"
    ]
    assert dataset_backed_keys, (
        "sanity check on the oracle itself: no dataset-backed condition was found "
        "at all, which would make the assertion below vacuous"
    )

    sentinel = "T67-PROVENANCE-SENTINEL-3f9a1c8e"
    monkeypatch.setattr(categories, "RESOLVE_ACCEPTS_ZH", sentinel)
    generated = search_mod._filter_key_reference()

    assert sentinel not in generated, (
        "_filter_key_reference() still embeds categories.RESOLVE_ACCEPTS_ZH -- part "
        "A of the filter-value-discovery feature requires this repeated once per "
        "dataset-backed key be removed entirely, not merely shortened"
    )
    # Every dataset-backed key must still name ITS OWN dataset file -- removing the
    # repeated acceptance-rule text must not silently drop which file backs which
    # key, since that is the one piece of per-key information this reference still
    # has room to carry.
    for key in dataset_backed_keys:
        dataset = filters.CONDITIONS[key].dataset
        assert dataset in generated and key in generated, (
            f"'{key}' (backed by {dataset}) is missing from the bare filter-key "
            f"reference"
        )


def test_search_resumes_description_points_to_browse_filter_values():
    """The full acceptance rule and every value domain moved OUT of search_resumes'
    published description (part A) into `browse_filter_values` -- this only checks
    that search_resumes' text actually tells a caller where to go look, which is
    what makes "moved", not merely "deleted"."""
    search_doc = TOOL_DESCRIPTIONS.get("search_resumes", "")
    assert search_doc, "search_resumes must be registered with a non-empty published description"
    assert "browse_filter_values" in search_doc, (
        "search_resumes' description no longer names browse_filter_values -- a "
        "caller reading only this description would have no way to discover that "
        "the value domains moved there rather than simply vanishing"
    )


# ── T-70 (R9.8, R9.4): a prefix-family warning naming a category the resolver would
# refuse identifies it as such; a warning naming only selectable categories carries no
# such mark. Both directions. ────────────────────────────────────────────────────────
#
# The instance is found by `_find_family_instances()` above (shared with T-68's sibling
# helper), never hard-coded: this round's own brief demonstrated that a node name typed
# by hand -- even confidently, even by the coordinator -- can name a node that does not
# exist. Design.md does not pin the exact Traditional-Chinese wording of the mark (only
# its EFFECT, R9.8), so the presence check below is loose/keyword-based, matching this
# suite's own established idiom for un-pinned model-facing prose (see T-59's docstring).
# Drawn from vocabulary tools/categories.py's own RESOLVE_ACCEPTS_ZH export already uses
# for this same underlying concept (refusal / branch nodes), since design.md ties the
# `terminal` flag's published meaning to being generated "beside the predicate that
# computes it" in that same module.

_REFUSAL_MARK_KEYWORDS = (
    "拒絕", "不可選", "不可直接", "無法直接", "分支", "非葉節點", "不可用", "不會生效",
    "refuse", "refused", "branch", "not selectable", "non-terminal", "cannot",
)


def _windows_around(text: str, needle: str, radius: int = 120) -> list[str]:
    windows = []
    start = 0
    while True:
        idx = text.find(needle, start)
        if idx == -1:
            break
        windows.append(text[max(0, idx - radius): idx + len(needle) + radius])
        start = idx + len(needle)
    return windows


@pytest.mark.asyncio
async def test_prefix_family_warning_marks_only_the_refused_sibling(tmp_path, monkeypatch):
    mixed, pure = _find_family_instances()
    assert mixed is not None, (
        "sanity: the sweep must find at least one prefix family naming an entry the "
        "resolver would refuse (design.md Components §6: exactly one, across the "
        "bundled datasets)"
    )
    assert pure is not None, (
        "sanity: the sweep must find at least one prefix family whose every member "
        "is selectable"
    )

    mixed_dataset, mixed_query, mixed_outcomes = mixed
    pure_dataset, pure_query, pure_outcomes = pure
    mixed_key = _DATASET_TO_FILTER_KEY[mixed_dataset]
    pure_key = _DATASET_TO_FILTER_KEY[pure_dataset]
    mixed_resolution = categories.resolve(categories.load_dataset(mixed_dataset, mixed_key), mixed_query)
    pure_resolution = categories.resolve(categories.load_dataset(pure_dataset, pure_key), pure_query)

    # Direction 1: a family naming a refused sibling identifies it as such.
    ctx, _info, _db = await _new_session(tmp_path)
    fixture = _load("rows_search.json")
    keyword = fixture["result"]["searchForm"]["kws"]  # matches the fixture's own echo
    body = copy.deepcopy(fixture)
    body["result"]["searchForm"][mixed_key] = mixed_resolution.code
    _install_fake_fetch(monkeypatch, _raw_from_body(body))

    result = await search_resumes(keyword=keyword, filters={mixed_key: mixed_query}, ctx=ctx)
    assert "error" not in result, f"the exact-tier hit must resolve and search: {result}"

    refused_names = [name for name, resolves in mixed_outcomes.values() if not resolves]
    assert refused_names, "sanity: the mixed instance must actually name a refused sibling"
    warnings_list = result.get("warnings", [])
    joined = " ".join(warnings_list)
    for refused_name in refused_names:
        assert refused_name in joined, (
            f"the refused sibling {refused_name!r} must be named in the warnings at all: {warnings_list!r}"
        )
        mentions = _windows_around(joined, refused_name)
        assert any(any(kw in w for kw in _REFUSAL_MARK_KEYWORDS) for w in mentions), (
            f"a family entry the resolver would refuse ({refused_name!r}) must be "
            f"identified as such near its own mention (R9.8); warnings: {warnings_list!r}"
        )

    # Direction 2: a family naming only selectable siblings carries no such mark.
    ctx2, _info2, _db2 = await _new_session(tmp_path)
    body2 = copy.deepcopy(fixture)
    body2["result"]["searchForm"][pure_key] = pure_resolution.code
    _install_fake_fetch(monkeypatch, _raw_from_body(body2))

    result2 = await search_resumes(keyword=keyword, filters={pure_key: pure_query}, ctx=ctx2)
    assert "error" not in result2, f"the exact-tier hit must resolve and search: {result2}"

    selectable_names = [name for name, resolves in pure_outcomes.values() if resolves]
    assert selectable_names, "sanity: the pure instance must actually name a selectable sibling"
    warnings_list2 = result2.get("warnings", [])
    joined2 = " ".join(warnings_list2)
    for selectable_name in selectable_names:
        assert selectable_name in joined2, (
            f"selectable sibling {selectable_name!r} must still be named in the "
            f"warnings: {warnings_list2!r}"
        )
    assert not any(kw in joined2 for kw in _REFUSAL_MARK_KEYWORDS), (
        f"a family naming only selectable categories must carry no refusal mark; "
        f"got warnings: {warnings_list2!r}"
    )


# ── T-73 half 2 (R7.2, behavior): `unmeasured` and measured `not-echoed` reach the
# caller as the SAME single not-echo-confirmed flag ─────────────────────────────────
#
# design.md: "That caller-facing flag is deliberately one flag, not three... a
# caller cannot [act on the distinction] — in both cases the value was submitted and
# the server did not confirm it, and there is no different move to make." The exact
# shape of that flag is not pinned by design.md, so this does not assert a literal
# for it. It asserts EQUALITY instead: swap ONLY the echo-evidence state on the same
# condition (not-echoed vs unmeasured), keep the rest of the request identical, and
# require the caller-facing output to come back byte-identical. An implementation
# that surfaces a third, distinguishing signal for one state over the other -- the
# exact defect this case exists to catch -- would make the two captured outputs
# differ. (See tests/test_filters.py for the sibling half of T-73: exclusion from
# drop comparison, which is tools/filters.py's responsibility, not tools/search.py's.)

_ECHO_EVIDENCE_VALUES = ("echoed", "not-echoed", "unmeasured")


def _echo_evidence_field_name(condition) -> str:
    if dataclasses.is_dataclass(condition):
        pairs = [(f.name, getattr(condition, f.name)) for f in dataclasses.fields(condition)]
    elif hasattr(condition, "_asdict"):
        pairs = list(condition._asdict().items())
    else:
        pairs = list(vars(condition).items())
    matches = [name for name, value in pairs if value in _ECHO_EVIDENCE_VALUES]
    assert len(matches) == 1, (
        f"expected exactly one field holding an echo-evidence literal on {condition!r}, "
        f"found {matches!r}"
    )
    return matches[0]


def _replace_echo_evidence(condition, new_value: str):
    field_name = _echo_evidence_field_name(condition)
    if dataclasses.is_dataclass(condition):
        return dataclasses.replace(condition, **{field_name: new_value})
    if hasattr(condition, "_replace"):
        return condition._replace(**{field_name: new_value})
    raise TypeError(f"don't know how to clone-and-replace a {type(condition)!r}")


@pytest.mark.asyncio
async def test_unmeasured_and_not_echoed_reach_the_caller_as_one_flag(tmp_path, monkeypatch):
    real_jobcat = filters.CONDITIONS["jobcat"]
    field_name = _echo_evidence_field_name(real_jobcat)
    assert getattr(real_jobcat, field_name) == "echoed", "sanity: jobcat must start out echoed"

    fixture = _load("rows_search.json")
    keyword = fixture["result"]["searchForm"]["kws"]
    dataset = categories.load_dataset("JobCat.json", "jobcat")
    leaf = next(n for n in dataset.nodes if not n.children)
    resolution = categories.resolve(dataset, leaf.name)
    assert resolution.status == "resolved"

    async def _run_with_echo_evidence(state: str):
        ctx, _info, _db = await _new_session(tmp_path)
        synthetic_condition = _replace_echo_evidence(real_jobcat, state)
        monkeypatch.setitem(filters.CONDITIONS, "jobcat", synthetic_condition)
        body = copy.deepcopy(fixture)
        # jobcat is now excluded from drop comparison either way, by table lookup --
        # its searchForm echo is deliberately left at whatever the base fixture
        # already carries (irrelevant to this condition once excluded).
        _install_fake_fetch(monkeypatch, _raw_from_body(body))
        result = await search_resumes(keyword=keyword, filters={"jobcat": leaf.name}, ctx=ctx)
        assert "error" not in result, f"filtered search unexpectedly failed under {state!r}: {result}"
        return result

    not_echoed_result = await _run_with_echo_evidence("not-echoed")
    unmeasured_result = await _run_with_echo_evidence("unmeasured")

    assert not_echoed_result == unmeasured_result, (
        f"a condition recorded 'not-echoed' and one recorded 'unmeasured' must reach "
        f"the caller identically -- got two different caller-facing outputs:\n"
        f"not-echoed: {not_echoed_result!r}\n"
        f"unmeasured: {unmeasured_result!r}"
    )
    # Sanity: the shared result must actually carry SOME marking for jobcat in
    # applied_filters -- otherwise the equality above would hold trivially by both
    # sides omitting the key entirely, which would prove nothing.
    assert "jobcat" in result_repr_keys(not_echoed_result), (
        f"expected jobcat to appear somewhere in the not-echo-confirmed response "
        f"shape; got {not_echoed_result!r}"
    )


def result_repr_keys(result: dict) -> str:
    """Flatten a result dict to one string for a simple containment sanity check --
    deliberately not structural, since the exact shape of the not-echo-confirmed
    marking is not pinned by design.md (see module comment above)."""
    return json.dumps(result, ensure_ascii=False, default=str)
