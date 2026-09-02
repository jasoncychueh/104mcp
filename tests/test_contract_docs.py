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

import inspect
import re
import tomllib
from pathlib import Path

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


def _env_var_names_from_config_source() -> list[str]:
    """Every environment variable `config.get_config()` reads, in the order its
    body reads them — extracted from the function's own source rather than
    hand-listed, so a variable added to or removed from `config.py` changes this
    list without anyone updating this test file.
    """
    source = inspect.getsource(config_module.get_config)
    names = re.findall(r'os\.getenv\(\s*"([A-Z_]+)"\s*,', source)
    assert names, (
        "found no os.getenv(...) calls in config.get_config's source — either "
        "the function was rewritten in a way this regex can no longer parse, or "
        "it stopped reading any environment variables; either way this check has "
        "nothing to compare and must not pass silently"
    )
    return names


def test_env_var_table_matches_config_defaults(monkeypatch):
    """CLAUDE.md's environment-variable table (the `| 環境變數 | 預設值 | 用途 |`
    table) must name exactly the variables `config.get_config()` reads, and each
    documented default must equal the value `get_config()` actually returns when
    that variable is unset — read from the live `Config` object, not transcribed,
    so a changed default (as happened to the pacing values this feature) is
    caught here rather than only in review."""
    env_var_names = _env_var_names_from_config_source()

    # Force every one of them unset so get_config() returns its own defaults,
    # regardless of what the ambient test environment happens to have set.
    for name in env_var_names:
        monkeypatch.delenv(name, raising=False)
    defaults = config_module.get_config()

    rows = _table_rows(_claude_md_text(), "| 環境變數 | 預設值 | 用途 |")
    documented = {_cell_value(row[0]): _cell_value(row[1]) for row in rows}

    code_vars = set(env_var_names)
    doc_vars = set(documented)
    missing_from_doc = code_vars - doc_vars
    extra_in_doc = doc_vars - code_vars

    assert not missing_from_doc, (
        "config.get_config() reads environment variables CLAUDE.md's table does "
        f"not document: {sorted(missing_from_doc)}"
    )
    assert not extra_in_doc, (
        "CLAUDE.md's environment-variable table documents variables "
        f"config.get_config() does not read: {sorted(extra_in_doc)}"
    )

    mismatches = {}
    for name in env_var_names:
        field_name = name.lower()
        actual_default = str(getattr(defaults, field_name))
        documented_default = documented[name]
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
