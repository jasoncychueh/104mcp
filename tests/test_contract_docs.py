"""Contract test for the registered MCP tool surface (T-61, R12.1).

The registered tools' descriptions and schemas are the only surface an Agent
reads when deciding how to call a tool. This file checks that surface against
the code that produces it (`config.py`'s own environment-variable reading
shape, `pyproject.toml`'s dependency list, the live tool registry) — nothing
here transcribes a table by hand, so the suite does not need updating when the
underlying values change; it only goes red when the registered surface itself
drifts from its own internal contracts.
"""
from __future__ import annotations

import ast
import inspect
import re
import tomllib
from pathlib import Path

import pytest
from mcp.server.fastmcp import FastMCP

import mcp104.config as config_module
from mcp104.tools.auth import register_auth_tools
from mcp104.tools.discovery import register_discovery_tools
from mcp104.tools.messaging import register_messaging_tools
from mcp104.tools.search import register_search_tools
from mcp104.tools.status import register_status_tools

REPO_ROOT = Path(__file__).resolve().parent.parent

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


# ── Regression coverage for the `ctx` auto-injection bug (Phase 5 acceptance) ─────────
#
# FastMCP's Tool.from_function decides whether a parameter is the auto-injected
# server Context (never a caller-facing field) via `issubclass(param.annotation,
# Context)` -- a real class check. `from __future__ import annotations` (PEP 563)
# turns every annotation in the module into a string at runtime, which makes that
# issubclass() call fail silently (returns False, no error) and treats `ctx` as an
# ordinary tool argument instead. Shipped symptom: Claude Code rejected every call
# to login()/search_resumes()/etc. with "1 validation error for loginArguments --
# ctx: Field required", because every tool's `inputSchema.required` gained a `ctx`
# entry nothing ever supplies. Two tests below: (a) black-box, registers the real
# tool registry and checks the published schema directly; (b) white-box, AST-scans
# tools/*.py so a future module cannot reintroduce the same combination even before
# any tool test happens to catch it.


def _register_all_tools(mcp) -> None:
    register_auth_tools(mcp)
    register_search_tools(mcp)
    register_messaging_tools(mcp)
    register_status_tools(mcp)
    register_discovery_tools(mcp)


@pytest.mark.asyncio
async def test_no_registered_tool_exposes_ctx_as_a_caller_field():
    """`ctx: Context` is FastMCP's auto-injected server context, never a caller-
    facing argument -- it must never appear in a tool's published `inputSchema`
    (neither in `properties` nor in `required`). See the module-level comment
    above for the bug this guards against."""
    mcp = FastMCP("contract-test")
    _register_all_tools(mcp)
    tools = await mcp.list_tools()
    assert tools, "sanity: no tools registered at all"

    offenders = {}
    for tool in tools:
        schema = tool.inputSchema or {}
        properties = schema.get("properties", {})
        required = schema.get("required", [])
        if "ctx" in properties or "ctx" in required:
            offenders[tool.name] = {
                "in_properties": "ctx" in properties,
                "in_required": "ctx" in required,
            }
    assert not offenders, (
        f"these tools publish `ctx` as a caller-facing field in their inputSchema "
        f"-- FastMCP failed to auto-inject it (see the module-level comment above "
        f"this test): {offenders}"
    )


def _module_has_future_annotations(tree: ast.AST) -> bool:
    return any(
        isinstance(node, ast.ImportFrom)
        and node.module == "__future__"
        and any(alias.name == "annotations" for alias in node.names)
        for node in ast.walk(tree)
    )


def _module_has_ctx_param_function(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        args = node.args
        all_params = (
            list(args.posonlyargs)
            + list(args.args)
            + list(args.kwonlyargs)
            + ([args.vararg] if args.vararg else [])
            + ([args.kwarg] if args.kwarg else [])
        )
        if any(param.arg == "ctx" for param in all_params):
            return True
    return False


def _module_registers_tools(tree: ast.AST) -> bool:
    """True iff the module's source calls `<something>.tool(...)` or
    `<something>.add_tool(...)` anywhere -- i.e. it registers at least one MCP tool
    on a FastMCP instance. A module can define a helper that merely takes a `ctx`
    parameter (e.g. tools/helpers.py's `get_session_id`/`resolve_session`/
    `guarded_api`) without itself ever being handed to FastMCP for schema
    generation -- `from __future__ import annotations` is harmless there, because
    nothing ever calls `issubclass()` on that helper's own annotations. Only a
    module that registers tools is where PEP 563 can break FastMCP's `ctx`
    auto-injection (see the module-level comment above the other test in this
    section)."""
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in ("tool", "add_tool")
        ):
            return True
    return False


def test_no_ctx_param_module_uses_future_annotations():
    """White-box counterpart to test_no_registered_tool_exposes_ctx_as_a_caller_field:
    AST-scans every src/mcp104/tools/*.py module and fails if a module both (a)
    registers at least one MCP tool (calls `.tool(...)`/`.add_tool(...)`) and (b)
    defines any function taking a `ctx` parameter, while also (c) carrying `from
    __future__ import annotations` -- that combination is exactly what silently
    breaks FastMCP's issubclass()-based auto-injection detection (see the
    module-level comment above the other test in this section): mcp relies on the
    parameter's RUNTIME annotation object to recognise Context, and PEP 563 turns
    every annotation in the module into a string, so `ctx` leaks into the
    published inputSchema as an ordinary caller-facing field. Scoped to
    tool-registering modules only: a module that merely defines a `ctx`-taking
    helper never used for schema generation (e.g. tools/helpers.py) is not at
    risk, and flagging it would be a false positive. This catches the defect at
    the source-file level, ahead of and independent from the schema-inspection
    test, so a future module cannot reintroduce it even before any
    tool-registration test happens to run."""
    tools_dir = REPO_ROOT / "src" / "mcp104" / "tools"
    offenders = []
    registering_modules = []
    for path in sorted(tools_dir.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        if not _module_registers_tools(tree):
            continue
        registering_modules.append(path.name)
        if _module_has_ctx_param_function(tree) and _module_has_future_annotations(tree):
            offenders.append(path.name)

    # Not a vacuous rule: if fewer than the four known tool-registering modules
    # (auth/search/messaging/discovery) are ever detected, `_module_registers_tools`
    # itself is broken and the loop above would pass by finding nothing to check.
    assert len(registering_modules) >= 4, (
        f"expected at least 4 tool-registering modules (auth/search/messaging/"
        f"discovery), found {registering_modules} -- _module_registers_tools() "
        f"itself may be broken, which would make this whole test vacuous"
    )

    assert not offenders, (
        "these tool-registering tools/*.py modules define a `ctx`-taking function "
        "AND carry `from __future__ import annotations`, which stringifies the "
        "`ctx: Context` annotation and breaks FastMCP's issubclass()-based "
        f"auto-injection detection: {offenders}"
    )


# ── outbound-contact Phase 5 (T-76..T-80): send_inquiry / list_templates land this
# cycle, plus a CLAUDE.md/.mcp.json rewrite ──────────────────────────────────────────
#
# T-76 ("every registered tool's description fits budget with >=100 headroom",
# measured via _all_tool_descriptions) and T-80 ("no tool leaks ctx into
# inputSchema" + "tools/messaging.py carries no `from __future__ import
# annotations`") are ALREADY covered, generically, by
# test_every_tool_description_fits_claude_code_budget_with_headroom (above) and by
# test_no_registered_tool_exposes_ctx_as_a_caller_field /
# test_no_ctx_param_module_uses_future_annotations (above) -- all three sweep the
# live registry / tools/*.py directory rather than a hard-coded tool list, so
# send_inquiry and list_templates (registered inside messaging.py's
# register_messaging_tools) are covered automatically the moment they exist, with
# no new test needed for either case.

_WS_RUN_RE = re.compile(r"\s+")


def _normalize_ws(text: str) -> str:
    """Strip every run of whitespace -- including a newline a wrapped
    docstring inserts mid-sentence -- entirely (not collapse to a single
    space). CJK text wraps without spaces between characters, so replacing a
    mid-phrase newline with a space would leave a spurious space the
    original phrase never had -- observed on send_inquiry's positive
    warning, which wraps mid-phrase in the registered description. Apply to
    BOTH sides of any substring check that matches a Chinese phrase against
    text that may itself be wrapped -- the target phrase constants below
    happen to contain no whitespace of their own today, so normalising them
    is a no-op, but a caller must not assume that keeps holding.
    """
    return _WS_RUN_RE.sub("", text)


_POSITIVE_WARNING_ZH = "會立刻送到一位真實求職者手上"


def test_search_resumes_description_keeps_at_least_100_chars_headroom():
    """T-77 (R8.1): search_resumes is the budget table's one perpetually-tight
    description (121 chars headroom as of the outbound-contact design) -- it
    must clear the >=100-char headroom bar on its own, not merely survive as
    part of the whole-registry sweep the budget test above already runs."""
    descriptions = _all_tool_descriptions()
    assert "search_resumes" in descriptions, "sanity: search_resumes not registered"
    length = len(descriptions["search_resumes"])
    headroom = _TOOL_DESCRIPTION_CHAR_BUDGET - length
    assert headroom >= _TOOL_DESCRIPTION_HEADROOM, (
        f"search_resumes must keep at least {_TOOL_DESCRIPTION_HEADROOM} chars of "
        f"headroom under the {_TOOL_DESCRIPTION_CHAR_BUDGET}-char budget -- got "
        f"{length} chars, {headroom} headroom (this is the one description with "
        f"essentially no slack, so any future addition to a constant it "
        f"references -- e.g. SECOND_IDENTIFIER_NOTE -- risks pushing it over)"
    )


def test_send_message_and_send_inquiry_each_carry_the_positive_warning_and_name_the_other():
    """T-78 (R8.2): both send tools must state plainly that the call reaches a
    real jobseeker immediately, and each must name the OTHER tool -- send_message
    covering the plain-text case, send_inquiry covering the event case -- so a
    caller reading either description is pointed at the one it doesn't cover."""
    descriptions = _all_tool_descriptions()
    for name in ("send_message", "send_inquiry"):
        assert name in descriptions, f"sanity: {name} not registered"

    other_tool = {"send_message": "send_inquiry", "send_inquiry": "send_message"}
    for name, other_name in other_tool.items():
        desc = _normalize_ws(descriptions[name])
        assert _normalize_ws(_POSITIVE_WARNING_ZH) in desc, (
            f"{name}'s description must carry the positive warning "
            f"{_POSITIVE_WARNING_ZH!r} (R8.2) -- got: {desc!r}"
        )
        assert other_name in desc, (
            f"{name}'s description must name {other_name!r} -- each of the two "
            f"send tools must point the caller at the other tool for the case it "
            f"does not cover (R8.2): {desc!r}"
        )


# code -> Traditional-Chinese label, the six measured template categories
# (wire-confirmed via PUT, §8.15/§8.17).
_TEMPLATE_CATEGORIES_ZH = {
    "1": "詢問意願",
    "2": "邀約面試",
    "3": "感謝函",
    "4": "到職日期提醒",
    "5": "邀性格測驗",
    "0": "不分類",
}


def _code_precedes_label(text: str, code: str, label: str, window: int = 6) -> bool:
    """True iff `label` appears in `text` with `code` somewhere in the `window`
    characters immediately before it -- tolerant of whatever separator the
    description actually uses (`1=詢問意願`, "`1` 詢問意願", "(1)詢問意願", ...)
    without asserting a specific one, since no separator is specified by the
    design basis, only that code and name are both present together.
    """
    start = 0
    while True:
        pos = text.find(label, start)
        if pos == -1:
            return False
        if code in text[max(0, pos - window):pos]:
            return True
        start = pos + 1


def test_list_templates_description_lists_all_six_measured_categories_as_account_archival():
    """T-79 (R3.7): list_templates' description must name all six measured
    template categories (code + Traditional-Chinese label each) and must say
    these categories are the account's own archival categorisation -- not a
    vocabulary this project defines."""
    descriptions = _all_tool_descriptions()
    assert "list_templates" in descriptions, "sanity: list_templates not registered"
    desc = _normalize_ws(descriptions["list_templates"])

    missing = [
        f"{code} {label}" for code, label in _TEMPLATE_CATEGORIES_ZH.items()
        if not _code_precedes_label(desc, code, label)
    ]
    assert not missing, (
        f"list_templates' description must name all six measured template "
        f"categories, code immediately preceding its label -- missing: {missing}. "
        f"Description: {desc!r}"
    )
    # R3.7's own wording ("這個帳號自己的歸檔分類") is descriptive prose, not a
    # verbatim-transcription contract like CANDIDATE_TERMINAL_ZH -- so this checks
    # for the two content words ("帳號自己" ownership, "歸檔" archival/filing) it
    # must convey, not the exact phrase or grammar around them.
    assert "帳號自己" in desc and "歸檔" in desc, (
        "list_templates' description must say these categories are the "
        f"account's own archival/filing categorisation (R3.7: '這個帳號自己的歸檔"
        f"分類'), not a project-defined vocabulary: {desc!r}"
    )


# Marks the "what to do after a timeout" guidance: check the back office or
# read_messages, and do NOT resend -- R8.7 requires this be readable from the
# tool description alone, since a timed-out Agent may have nothing else in
# front of it at the moment it needs the instruction.
_TIMEOUT_GUIDANCE_MARKERS = ("read_messages", "重送")


def _has_timeout_guidance(text: str) -> bool:
    return all(marker in text for marker in _TIMEOUT_GUIDANCE_MARKERS)


@pytest.mark.parametrize("tool_name", ["send_message", "send_inquiry"])
def test_timeout_after_guidance_appears_in_send_message_and_send_inquiry_description(tool_name):
    """T-79 (R8.7): the post-timeout instruction (check the back office or
    read_messages to confirm; do not resend) must be readable from the tool
    description, and the guidance is documented for both send_message and
    send_inquiry, not just send_inquiry."""
    descriptions = _all_tool_descriptions()
    assert tool_name in descriptions, f"sanity: {tool_name} not registered"
    desc = _normalize_ws(descriptions[tool_name])
    assert _has_timeout_guidance(desc), (
        f"{tool_name}'s description must carry the post-timeout guidance "
        f"(mentions read_messages and tells the caller not to resend, R8.7) -- "
        f"got: {desc!r}"
    )
