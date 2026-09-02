"""Repo-wide test fixtures.

This file exists for exactly one reason (design.md §Testing Strategy
"既有測試套件的處置" item 四) — do not add anything else to it. A conftest
full of autouse fixtures makes "why does this test behave differently in
another file" a real investigation; this one is kept to a single fixture on
purpose.

`get_config()` now raises ConfigError when MCP104_ACCOUNT_LABEL is unset
(config.py) — the identity value has no machine-derivable default (see that
module's docstring for why). Several existing fake app-context classes
(tests/test_api_client.py, tests/test_helpers.py, tests/test_messaging.py,
tests/test_search.py — and any future one, e.g. this cycle's AppContext
field-list change) call get_config() in their own __init__, so leaving the
env var unset would turn every one of those constructions into a startup
failure at test collection/setup time, not an assertion failure.

autouse + monkeypatch.setenv (function-scoped, restored after each test) is
deliberate: it supplies a value that is obviously a test value, while still
letting a test that needs to see "the variable is unset" delenv it itself
inside its own body — see tests/test_config.py and
tests/test_contract_docs.py, both of which do exactly that to exercise
MCP104_ACCOUNT_LABEL's own required-value validation (T-104). An
unconditional supply here is what would make that validation permanently
untestable, so this fixture must never become the thing that guarantees the
value always has a value from a caller's point of view — it only supplies a
default a test can still override or remove.

MCP104_DATA_DIR is set alongside it, pointed at tmp_path, so a test that
happens to construct a real Config (rather than mocking one) does not touch
this machine's actual per-user data directory.
"""

import pytest


@pytest.fixture(autouse=True)
def _default_identity_env(monkeypatch, tmp_path):
    monkeypatch.setenv("MCP104_ACCOUNT_LABEL", "test-account@104.example")
    monkeypatch.setenv("MCP104_DATA_DIR", str(tmp_path))
