"""Contract-document consistency test (T-61, R12.1; Round I6 Bug AK).

CLAUDE.md is not commentary on the code — it and the tool docstrings are the only
surface an Agent reads when deciding how to call a tool (steering: Usability NFR,
"the requirement is on the text the framework actually publishes"). An enforcement
mechanism no test reads is a comment. This file's job is to fail the moment
CLAUDE.md drifts from the three pieces of ground truth it transcribes:

  1. the `filters` key vocabulary table, which must name exactly the keys
     `tools.filters.VALID_FILTER_KEYS` defines (no more, no fewer) — the condition
     table in `tools/filters.py` is the single definition of the filter surface
     per that module's own docstring, so CLAUDE.md's table is *derived from* it,
     never hand-compared against it;
  2. the environment-variable table, which must name exactly the `os.getenv(...)`
     calls in `config.get_config()` and agree with the values that function
     actually returns by default;
  3. the `terminal` candidate-field prose, which CLAUDE.md itself labels a verbatim
     transcription of `tools.categories.CANDIDATE_TERMINAL_ZH` ("與
     tools/categories.py 的 CANDIDATE_TERMINAL_ZH 常數同步，逐字抄錄，不是重新描述")
     — the one consumer of that constant that cannot read it live (the other three
     are Python and import it), so it is the only one a drift in the constant
     cannot reach on its own, and the only one that needs a check here rather than
     none at all.

All three checks parse the live `CLAUDE.md` and consult the live `tools.filters` /
`config` / `tools.categories` modules directly — nothing here transcribes a key
name, a variable name, a default value or the candidate-field prose by hand, so the
suite does not need updating when the vocabulary, the defaults or the prose change;
it only goes red when CLAUDE.md fails to follow.
"""
from __future__ import annotations

import ast
import inspect
import re
import tomllib
from pathlib import Path

import pytest

import mcp104.config as config_module
from mcp104.tools.auth import register_auth_tools
from mcp104.tools.categories import CANDIDATE_TERMINAL_ZH
from mcp104.tools.discovery import register_discovery_tools
from mcp104.tools.filters import VALID_FILTER_KEYS
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from mcp104.tools.status import register_status_tools

REPO_ROOT = Path(__file__).resolve().parent.parent
CLAUDE_MD_PATH = REPO_ROOT / "CLAUDE.md"

_CELL_BACKTICK = re.compile(r"`([^`]*)`")
_SEPARATOR_CELL = re.compile(r":?-+:?")


def _claude_md_text() -> str:
    return CLAUDE_MD_PATH.read_text(encoding="utf-8")


def _comment_paragraphs(markdown: str) -> list[str]:
    """Join consecutive `# `-prefixed lines (CLAUDE.md's tool contract is written as
    Python-comment-wrapped prose inside a ```python fence) into single strings, with
    the `# ` marker and the line breaks that exist only for wrapping removed —
    nothing else is altered, so any substring that survived word-wrap unchanged in
    the source still matches here character for character.

    A paragraph boundary is any line that is not `# `-prefixed (a blank line, a
    line of actual code, or a bare `#`, which becomes an empty line within its
    paragraph rather than a break — several tool docstrings in CLAUDE.md use a bare
    `#` as a blank line inside one comment block). This normalises the DOCUMENT
    side only; `CANDIDATE_TERMINAL_ZH` itself is read unmodified from
    `tools.categories` — the constant is the authority, so rewriting it to match
    the document would invert which side is the source.
    """
    paragraphs: list[str] = []
    current: list[str] = []
    for line in markdown.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            current.append(stripped[2:])
        elif stripped == "#":
            current.append("")
        else:
            if current:
                paragraphs.append("".join(current))
                current = []
    if current:
        paragraphs.append("".join(current))
    return paragraphs


def _table_rows(markdown: str, header_line: str) -> list[list[str]]:
    """Return the data rows of the markdown pipe-table whose header row is exactly
    `header_line`, each row a list of raw (still-backticked) cell strings.

    Raises `AssertionError` — rather than returning `[]` — when the header is not
    found or the table has no data rows, so a renamed heading or an emptied table
    fails the test loudly instead of letting a downstream "all documented X are
    valid" check pass vacuously over zero rows.
    """
    lines = markdown.splitlines()
    header_index = next(
        (i for i, line in enumerate(lines) if line.strip() == header_line), None
    )
    assert header_index is not None, (
        f"expected table header not found in CLAUDE.md: {header_line!r} — has the "
        f"table been renamed or removed?"
    )
    rows: list[list[str]] = []
    for line in lines[header_index + 1:]:
        stripped = line.strip()
        if not stripped.startswith("|"):
            break
        cells = [c.strip() for c in stripped.strip("|").split("|")]
        if all(_SEPARATOR_CELL.fullmatch(c) for c in cells):
            continue  # the |---|---|---| separator row
        rows.append(cells)
    assert rows, f"table under {header_line!r} has a header but no data rows"
    return rows


def _cell_value(cell: str) -> str:
    """Strip one layer of Markdown backticks from a table cell, e.g. "`city`" ->
    "city". Every key/name/value cell this test reads is backtick-quoted in
    CLAUDE.md; a cell that is not is returned unchanged so a formatting slip shows
    up as a mismatch rather than being silently unwrapped.
    """
    match = _CELL_BACKTICK.fullmatch(cell)
    return match.group(1) if match else cell


def test_filter_key_table_matches_condition_table():
    """CLAUDE.md's `filters` vocabulary table (the `| 鍵 | 型別 | 值域 / 說明 |`
    table) must document exactly the keys `tools.filters.VALID_FILTER_KEYS`
    defines — nothing missing (a caller reading only CLAUDE.md would not know the
    key exists) and nothing extra (a caller reading only CLAUDE.md would try a key
    the code rejects as unknown)."""
    rows = _table_rows(_claude_md_text(), "| 鍵 | 型別 | 值域 / 說明 |")
    documented_keys = {_cell_value(row[0]) for row in rows}
    code_keys = set(VALID_FILTER_KEYS)

    missing_from_doc = code_keys - documented_keys
    extra_in_doc = documented_keys - code_keys

    assert not missing_from_doc, (
        "tools.filters.VALID_FILTER_KEYS defines keys CLAUDE.md's filter table "
        f"does not document: {sorted(missing_from_doc)}"
    )
    assert not extra_in_doc, (
        "CLAUDE.md's filter table documents keys tools.filters.VALID_FILTER_KEYS "
        f"does not define: {sorted(extra_in_doc)}"
    )


# The exact literal CLAUDE.md's "必填／預設值" column cell must carry for a
# required variable (MCP104_ACCOUNT_LABEL today) — defined ONCE here, not
# retyped in both the assertion and CLAUDE.md by memory, so the two can only
# ever agree by construction, never by two independent transcriptions
# happening to match.
_REQUIRED_MARKER = "**必填，沒有預設值**"

_ENV_VAR_TABLE_HEADER = "| 環境變數 | 必填／預設值 | 用途 |"

# Matches any of the three call forms config.py reads an environment variable
# through: a bare `os.getenv("NAME")` (optionally with a literal string
# default), the `_parse_int_env("NAME", default)` helper (always has an int
# literal default), or the `_parse_optional_int_env("NAME")` helper (never has
# a default — its whole contract is "unset means None", not "unset means a
# fallback value"). Exactly one of the three named-group triples matches per
# hit; which one tells `_scan_env_var_calls` how to read the name/default off
# it, since the three forms carry that information differently.
_ENV_VAR_CALL_RE = re.compile(
    r'os\.getenv\(\s*"(?P<getenv_name>[A-Z0-9_]+)"\s*(?:,\s*"(?P<getenv_default>[^"]*)")?\s*\)'
    r'|_parse_int_env\(\s*"(?P<int_name>[A-Z0-9_]+)"\s*,\s*(?P<int_default>-?\d+)\s*\)'
    r'|_parse_optional_int_env\(\s*"(?P<opt_name>[A-Z0-9_]+)"\s*\)'
)


def _scan_env_var_calls(source: str, info: dict[str, dict], *, only_mcp104_prefixed: bool) -> None:
    matches = list(_ENV_VAR_CALL_RE.finditer(source))
    for i, m in enumerate(matches):
        if m.group("getenv_name") is not None:
            # Only this form can ever be required: `_parse_int_env` and
            # `_parse_optional_int_env` both resolve unset input on their own
            # (a default, or None) and never raise, so only a bare
            # `os.getenv(...)` can be the one paired with a `raise
            # ConfigError` in the source immediately after it.
            name = m.group("getenv_name")
            has_default = m.group("getenv_default") is not None
            default = m.group("getenv_default")
            window_end = matches[i + 1].start() if i + 1 < len(matches) else len(source)
            window = source[m.end():window_end]
            required = (not has_default) and ("raise ConfigError" in window)
        elif m.group("int_name") is not None:
            name = m.group("int_name")
            has_default = True
            default = m.group("int_default")
            required = False
        else:
            name = m.group("opt_name")
            has_default = False
            default = None
            required = False

        if only_mcp104_prefixed and not name.startswith("MCP104_"):
            continue
        info[name] = {"has_default": has_default, "default": default, "required": required}


def _env_vars_from_config_source() -> dict[str, dict]:
    """Every environment variable `config.py` reads as an APPLICATION
    configuration knob — not merely every `os.getenv(...)` call textually
    present in the module. Two functions are scanned, with different scope:

    - `get_config()` itself, unrestricted — every variable it reads directly
      is a documented knob (this is where the seven no-prefix legacy names
      and the three other `MCP104_`-prefixed ones live).
    - `resolve_data_dir()`, restricted to `MCP104_`-prefixed names only. That
      function ALSO reads `APPDATA`/`LOCALAPPDATA` (Windows) and
      `XDG_DATA_HOME` (Linux) as platform-convention fallbacks when
      `MCP104_DATA_DIR` is unset — those are the operating system's own
      environment, not a knob this project defines or documents (CLAUDE.md's
      table has never listed them, and they are not `app.config.*`-shaped:
      `resolve_data_dir()` reads them directly, unlike every documented knob,
      which is read once by `get_config()` and thereafter accessed only
      through `Config`). Scanning `resolve_data_dir()` at all — rather than
      skipping it — is still required, because it is the one place
      `MCP104_DATA_DIR` itself is read; `get_config()` calls it but does not
      inline it, so a scan of `get_config()`'s own source alone would
      silently miss that one real knob.

    Extracted from source rather than hand-listed, so a variable added to or
    removed from `config.py` changes this without anyone updating this test.
    Returns `{name: {"has_default": bool, "default": str | None, "required":
    bool}}`. `has_default` comes straight from the call form (a literal
    string second argument to `os.getenv`, or its absence). `required` is a
    SEPARATE signal, also read from source, not inferred from `has_default`
    alone: three variables (`MCP104_DATA_DIR`, `MCP104_AUTH_BASE_URL`,
    `MCP104_AUTH_BIND_PORT`) also have no literal default yet are genuinely
    optional (they resolve to `None` or a derived value when unset) — only
    `MCP104_ACCOUNT_LABEL` is required. What actually marks a variable
    required in `get_config()`'s own body is that its `os.getenv(...)` call
    is followed, before the NEXT `os.getenv(...)` call, by `raise
    ConfigError` — exactly the shape of `account_label`'s own validation
    block. That is read here from the source text, not asserted about it.
    """
    info: dict[str, dict] = {}
    _scan_env_var_calls(inspect.getsource(config_module.get_config), info, only_mcp104_prefixed=False)
    _scan_env_var_calls(inspect.getsource(config_module.resolve_data_dir), info, only_mcp104_prefixed=True)
    assert info, (
        "found no os.getenv(...) calls in config.get_config/resolve_data_dir's "
        "source — either those functions were rewritten in a way this regex "
        "can no longer parse, or they stopped reading any environment "
        "variables; either way this check has nothing to compare and must "
        "not pass silently"
    )
    return info


def test_claude_md_env_var_table_names_and_required_marks_match_config():
    """CLAUDE.md's environment-variable table must name EXACTLY the variables
    `config.py` reads (both directions — a variable config.py reads but the
    table omits, and a variable the table documents that config.py no longer
    reads, are both a drift this must catch; `DB_PATH`/`AUTH_BASE_URL` must
    appear on NEITHER side, since both were deleted with no alias this cycle,
    §C2), and the table's required-marker cells must name exactly the
    variables `config.py`'s own call form marks required — not a hand-picked
    subset. Deliberately calls `get_config()` nowhere: an unset
    `MCP104_ACCOUNT_LABEL` is a startup failure by design (T-104), and a test
    of table MEMBERSHIP must not depend on that value being set at all.
    """
    info = _env_vars_from_config_source()
    code_names = set(info)

    rows = _table_rows(_claude_md_text(), _ENV_VAR_TABLE_HEADER)
    documented = {_cell_value(row[0]): _cell_value(row[1]) for row in rows}
    doc_names = set(documented)

    missing_from_doc = code_names - doc_names
    extra_in_doc = doc_names - code_names
    assert not missing_from_doc, (
        "config.py reads environment variables CLAUDE.md's table does not "
        f"document: {sorted(missing_from_doc)}"
    )
    assert not extra_in_doc, (
        "CLAUDE.md's environment-variable table documents variables config.py "
        f"does not read: {sorted(extra_in_doc)}"
    )
    assert "DB_PATH" not in code_names and "DB_PATH" not in doc_names, (
        "DB_PATH was deleted with no alias this cycle (§C2) — it must appear "
        "in neither config.py nor CLAUDE.md's table"
    )
    assert "AUTH_BASE_URL" not in code_names and "AUTH_BASE_URL" not in doc_names, (
        "AUTH_BASE_URL was deleted with no alias this cycle (§C2) — it must "
        "appear in neither config.py nor CLAUDE.md's table"
    )

    code_required = {name for name, v in info.items() if v["required"]}
    # (b) from design.md's T-114: zero required variables extracted is itself
    # a failure — the one case a downstream set-equality check would not
    # otherwise call out by name (an extraction quietly blind to the
    # required-marking call shape looks, from the assertion below alone,
    # identical to "this project genuinely has no required variables").
    assert code_required, (
        "extraction found no environment variable marked required in "
        "config.py's own source — either MCP104_ACCOUNT_LABEL's validation "
        "block no longer matches the 'os.getenv(...) then raise ConfigError "
        "before the next os.getenv' shape this regex looks for, or the "
        "required-variable validation itself was removed; either way this "
        "check has nothing to compare and must not pass silently"
    )

    doc_required = {name for name in doc_names if documented[name] == _REQUIRED_MARKER}
    assert code_required == doc_required, (
        "CLAUDE.md's required-marker cells disagree with config.py's own "
        f"call-form-derived required set: code says {sorted(code_required)}, "
        f"CLAUDE.md's '{_REQUIRED_MARKER}' cells say {sorted(doc_required)}"
    )


def test_env_var_regex_extraction_does_not_miss_a_fourth_call_form():
    """I2-N: `_ENV_VAR_CALL_RE` only recognizes three call shapes
    (`os.getenv(...)`, `_parse_int_env(...)`, `_parse_optional_int_env(...)`).
    If `get_config()`/`resolve_data_dir()` ever read an environment variable
    through a fourth shape the regex does not match (e.g. `os.environ["X"]`,
    an f-string built name, or a new helper), the regex-based extraction
    above would silently miss it rather than fail loudly. This check only
    covers the literal string constants that appear directly in these two
    functions' own bodies (via `inspect.getsource` + `ast.walk`) — it walks
    for every such string constant and checks each is one the regex already
    accounted for. Two shapes it cannot see, and does not claim to: a
    variable name held in a module-level constant read through a level of
    indirection (`_VAR = "MCP104_X"` elsewhere in the module, then
    `os.environ[_VAR]` inside the function — the literal string never
    appears in the function's own source) and an f-string-built name
    (`os.getenv(f"MCP104_{suffix}")` — no literal name exists to find at
    all). Either of those would need a different check (or a rewrite of
    `_ENV_VAR_CALL_RE` to name the indirection).
    """
    env_name_re = re.compile(r"^[A-Z][A-Z0-9_]+$")
    for func in (config_module.get_config, config_module.resolve_data_dir):
        source = inspect.getsource(func)

        # What the regex itself actually matched in THIS function's source --
        # not `_env_vars_from_config_source()`'s filtered `code_names`, which
        # deliberately drops resolve_data_dir()'s non-MCP104_-prefixed
        # matches (APPDATA/LOCALAPPDATA/XDG_DATA_HOME are platform-convention
        # fallbacks, not knobs this project owns, per that function's own
        # docstring). Comparing against the filtered set would flag those as
        # "missed by the regex" when the regex caught them fine and a later
        # filter intentionally discarded them -- this must test the regex's
        # own miss, not the filter's own choice.
        regex_matched_names = set()
        for m in _ENV_VAR_CALL_RE.finditer(source):
            name = m.group("getenv_name") or m.group("int_name") or m.group("opt_name")
            if name is not None:
                regex_matched_names.add(name)

        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.Constant) and isinstance(node.value, str):
                candidate = node.value
                if env_name_re.match(candidate) and candidate not in regex_matched_names:
                    pytest.fail(
                        f"{func.__name__} contains the ENV-VAR-shaped string "
                        f"constant {candidate!r}, which _ENV_VAR_CALL_RE's "
                        "three recognized call forms did not pick up -- a "
                        "fourth call form reading an environment variable "
                        "was likely added without updating the regex"
                    )


def test_env_var_defaults_in_claude_md_match_config_defaults(monkeypatch):
    """CLAUDE.md's documented default for every variable that HAS a literal
    default (the seven no-prefix variables, per §C2) must equal the value
    `get_config()` actually returns when that variable is unset — read from
    the live `Config` object, not transcribed, so a changed default (as
    happened to the pacing values this feature) is caught here rather than
    only in review.

    Deliberately narrower than the membership/required check above: a
    variable with NO literal default (three of the four `MCP104_`-prefixed
    ones) has nothing here to compare a documented default against, and
    `MCP104_ACCOUNT_LABEL` unset makes `get_config()` itself fail — the
    required variable is excluded from this half by its OWN call-form label
    (`required`), not by a hand-written skip-list that could silently miss
    the next required variable this project adds (design.md §Testing
    Strategy item 五).
    """
    info = _env_vars_from_config_source()
    has_default_names = [name for name, v in info.items() if v["has_default"]]
    assert has_default_names, (
        "extraction found no environment variable with a literal default in "
        "config.py — either every variable became required/optional-with-no-"
        "default, or the extraction regex no longer recognises the "
        "has-a-default call form; either way this check has nothing to "
        "compare and must not pass silently"
    )

    # Force every one of them unset so get_config() returns its own defaults,
    # regardless of what the ambient test environment happens to have set.
    # MCP104_ACCOUNT_LABEL (required) and MCP104_DATA_DIR (optional, no
    # literal default) are left exactly as tests/conftest.py's autouse
    # fixture set them — this half never touches either.
    for name in has_default_names:
        monkeypatch.delenv(name, raising=False)
    defaults = config_module.get_config()

    rows = _table_rows(_claude_md_text(), _ENV_VAR_TABLE_HEADER)
    documented = {_cell_value(row[0]): _cell_value(row[1]) for row in rows}

    mismatches = {}
    for name in has_default_names:
        field_name = name.lower()
        actual_default = str(getattr(defaults, field_name))
        documented_default = documented.get(name)
        if actual_default != documented_default:
            mismatches[name] = {
                "config_default": actual_default,
                "claude_md_says": documented_default,
            }
    assert not mismatches, (
        f"CLAUDE.md's documented defaults disagree with config.get_config(): "
        f"{mismatches}"
    )


# ── Bug AK (Round I6): CLAUDE.md's `terminal` prose must AGREE WITH the live
# `CANDIDATE_TERMINAL_ZH` export, not merely resemble it ────────────────────────────
#
# Of `CANDIDATE_TERMINAL_ZH`'s four consumer sites, three (the `search_resumes`
# published description, and two other in-process readers) import the constant and
# read it live — a change to the constant reaches them automatically, with nothing
# to test. CLAUDE.md is the fourth: a Markdown file, which no live import can reach.
# It is therefore the one consumer an edit to the constant can silently leave behind,
# and CLAUDE.md itself says as much, in the very passage this checks ("與
# tools/categories.py 的 CANDIDATE_TERMINAL_ZH 常數同步，逐字抄錄，不是重新描述").
#
# T-71 (tests/test_search.py) already checks that the published description NAMES
# `terminal` and explains it nearby — a presence/keyword check, adequate for prose
# that isn't claimed to be a transcription. This passage claims more: verbatim
# agreement. Presence of the word "terminal" plus a nearby keyword survives the
# constant's wording changing entirely, as long as some refusal-flavoured word stays
# somewhere nearby — exactly the way an earlier form of T-67 survived a hand-typed
# copy standing in for a live read. Since CLAUDE.md cannot execute code, provenance
# (T-67's fix) is not available to it; agreement against the live constant is the
# strongest check that is.

def test_claude_md_terminal_prose_agrees_with_the_exported_constant():
    paragraphs = _comment_paragraphs(_claude_md_text())
    assert any(CANDIDATE_TERMINAL_ZH in paragraph for paragraph in paragraphs), (
        "CLAUDE.md must carry tools.categories.CANDIDATE_TERMINAL_ZH's text "
        "verbatim -- CLAUDE.md's own 'terminal' passage labels itself a verbatim "
        "transcription of this constant, so a hand-edited or stale copy is a "
        "broken promise the document makes about itself, not merely missing "
        "coverage. Current export:\n"
        f"{CANDIDATE_TERMINAL_ZH!r}"
    )


# ── package-layout restructure: requirements.txt must equal pyproject.toml's
# [project] dependencies ─────────────────────────────────────────────────────────────
#
# The Dockerfile installs runtime dependencies from requirements.txt, as a layer cached
# above the (large, slow) Chromium download, then later `pip install --no-deps .` from
# pyproject.toml for the package itself — `--no-deps` is what makes requirements.txt's
# list the ONLY place those dependencies get installed from in the image. A drift
# between the two lists is silent: `pip install --no-deps .` never complains about a
# dependency pyproject.toml declares that requirements.txt does not carry, it simply
# is not installed, and the failure surfaces later as an ImportError at server startup
# inside the container, far from this file.

def _pyproject_dependencies() -> list[str]:
    with (REPO_ROOT / "pyproject.toml").open("rb") as f:
        data = tomllib.load(f)
    return list(data["project"]["dependencies"])


def _requirements_txt_entries() -> list[str]:
    lines = (REPO_ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
    return [
        stripped for stripped in (line.strip() for line in lines)
        if stripped and not stripped.startswith("#")
    ]


def test_requirements_txt_equals_pyproject_dependencies():
    pyproject_deps = _pyproject_dependencies()
    requirements_entries = _requirements_txt_entries()
    assert requirements_entries == pyproject_deps, (
        f"requirements.txt must hold exactly pyproject.toml's [project] dependencies "
        f"(same entries) -- requirements.txt: {requirements_entries!r}; "
        f"pyproject.toml [project] dependencies: {pyproject_deps!r}"
    )


# ── Every registered tool's description must fit Claude Code's 2,048-char slice
# budget, with real headroom ─────────────────────────────────────────────────────────
#
# Claude Code slices every tool description at 2,048 characters before the model ever
# sees it (measured: bue=2048, applied in prompt() -- the text that actually reaches
# the model, not description() -- a character slice, not tokens; see CLAUDE.md).
# search_resumes' description was 5,031 chars until this fix -- 59% of it silently
# invisible to the model, including its entire tail (the area/experience/education
# tombstone warning, the p_id note, the success/failure shape contract). Swept over
# the WHOLE registry via each tools/*.py module's own `register_*_tools(mcp)` function
# -- never a hard-coded tool list -- so a tool added later is covered automatically,
# not only the one that happened to blow the budget this round.

_TOOL_DESCRIPTION_CHAR_BUDGET = 2048
_TOOL_DESCRIPTION_HEADROOM = 100  # "real headroom", not merely squeaking under budget


class _DescriptionCaptureMCP:
    """Stand-in for FastMCP sufficient to capture every registered tool's EFFECTIVE
    published description -- `description=` kwarg if given, else `fn.__doc__` -- same
    precedence real FastMCP's `Tool.from_function` uses (`description or fn.__doc__`).
    Mirrors tests/test_search.py's own FakeMCP; kept as a separate, smaller class here
    since this file only needs the description capture, not the tool-callable capture.
    """

    def __init__(self):
        self.descriptions: dict[str, str] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self.descriptions[name] = kwargs.get("description") or fn.__doc__ or ""
            return fn
        return decorator


def _all_tool_descriptions() -> dict[str, str]:
    mcp = _DescriptionCaptureMCP()
    register_auth_tools(mcp)
    register_search_tools(mcp)
    register_messaging_tools(mcp)
    register_status_tools(mcp)
    register_discovery_tools(mcp)
    return mcp.descriptions


def test_every_tool_description_fits_claude_code_budget_with_headroom():
    descriptions = _all_tool_descriptions()
    assert descriptions, "sanity: no tools registered at all"

    over_budget = {
        name: length for name, desc in descriptions.items()
        if (length := len(desc)) > _TOOL_DESCRIPTION_CHAR_BUDGET
    }
    assert not over_budget, (
        f"these tool descriptions exceed Claude Code's "
        f"{_TOOL_DESCRIPTION_CHAR_BUDGET}-char slice budget (CLAUDE.md) -- the model "
        f"never sees the truncated remainder: {over_budget}"
    )

    no_headroom = {
        name: length for name, desc in descriptions.items()
        if _TOOL_DESCRIPTION_CHAR_BUDGET - (length := len(desc)) < _TOOL_DESCRIPTION_HEADROOM
    }
    assert not no_headroom, (
        f"these tool descriptions are within {_TOOL_DESCRIPTION_HEADROOM} chars of "
        f"the {_TOOL_DESCRIPTION_CHAR_BUDGET}-char budget -- real headroom is "
        f"required, not merely squeaking under it (this is exactly how "
        f"search_resumes went over in the first place): {no_headroom}"
    )
