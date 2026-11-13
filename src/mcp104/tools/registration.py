"""Tool registration: the one place a tool function is handed to FastMCP together with its
description.

Kept out of tools/helpers.py on purpose: that module carries `from __future__ import
annotations` and defines `ctx`-taking functions, and a module that registers tools must not
carry that import (it stringifies `ctx: Context`, so mcp's `issubclass()` check silently fails
and `ctx` leaks into the published inputSchema). This module has neither.
"""

from mcp.server.fastmcp import FastMCP

from mcp104.tools.helpers import require_login


def register_tool(mcp: FastMCP, fn, doc: str) -> None:
    """Register one tool with `doc` as its ACTUAL __doc__ — never as a parallel
    `description=` argument the function's own docstring doesn't carry.

    The docstring is the ONLY text the Agent reads when choosing a tool. A `description=` override is
    technically read by FastMCP too (`Tool.from_function`: `description or fn.__doc__`),
    but a check written against `fn.__doc__` — the natural, framework-independent way to
    assert "does this tool's docstring say X" — would silently miss content that only
    ever lived in a `description=` argument, and the two channels agreeing today gives no
    guarantee they keep agreeing after an edit to only one of them. Binding the
    requirement to `fn.__doc__` itself, not to whichever channel happens to also carry
    the text, is what makes the rule enforceable rather than merely usually-true.

    Some of these tools' full text (search_resumes') is generated from
    tools/filters.py's CONDITIONS table at import time, so it cannot be written as a
    literal triple-quoted docstring — Python only recognises a compile-time string
    constant in that position, never an f-string or any other runtime expression. `doc`
    is therefore assigned here, as a plain attribute, before `require_login`'s
    `@functools.wraps` copies `__doc__` onto the wrapper this actually hands to
    `mcp.tool()` — which is the object whose `__doc__` ends up as `Tool.description`
    once no `description=` argument is given to override it.
    """
    fn.__doc__ = doc
    mcp.tool()(require_login(fn))
