"""Repo-wide test fixtures and shared test doubles.

The autouse fixture below exists for exactly one reason (design.md
§Testing Strategy "既有測試套件的處置" item 四) — a conftest full of
autouse fixtures makes "why does this test behave differently in another
file" a real investigation, so this file is kept to a single autouse
fixture on purpose. `_SeqFetchSpy` is a plain (non-fixture, non-autouse)
test double shared by tests/test_helpers.py and tests/test_messaging.py,
which both drove guarded_api/guarded_sequence's scripted sub-request
sequences through byte-for-byte identical copies of this class before it
was consolidated here.

`get_config()` now raises ConfigError when MCP104_ACCOUNT is unset
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
MCP104_ACCOUNT's own required-value validation (T-104). An
unconditional supply here is what would make that validation permanently
untestable, so this fixture must never become the thing that guarantees the
value always has a value from a caller's point of view — it only supplies a
default a test can still override or remove.

MCP104_DATA_DIR is set alongside it, pointed at tmp_path, so a test that
happens to construct a real Config (rather than mocking one) does not touch
this machine's actual per-user data directory.

`load_fixture` is how every test reads tests/fixtures/*.json. Those files are
built from real 104 responses and carry candidates' personal data, so they are
not in version control: each machine builds its own (tests/fixtures/README.md
says what every file must contain). A missing file must therefore turn the
tests that need it red -- not skip them, and not fall back to something else --
and the failure has to tell the reader where to look. It is a plain function,
called inside a test body or a fixture, never at module level: a module-level
read would turn one missing file into a collection error for the whole test
module, and only one missing file would show at a time.
"""

import json
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


def load_fixture(name: str) -> Any:
    """Read tests/fixtures/<name> as JSON. Raises when the file is absent."""
    path = FIXTURES_DIR / name
    if not path.is_file():
        raise FileNotFoundError(
            f"tests/fixtures/{name} is missing: test data is not in version control; "
            f"every machine builds its own -- see tests/fixtures/README.md"
        )
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture(autouse=True)
def _default_identity_env(monkeypatch, tmp_path):
    monkeypatch.delenv("MCP104_ACCOUNT", raising=False)  # dropped 2026-09-04; must not be needed
    monkeypatch.setenv("MCP104_DATA_DIR", str(tmp_path))


class _SeqFetchSpy:
    """Drives guarded_api/guarded_sequence's sub-requests with pre-scripted
    outcomes, consumed strictly in call order. A scripted item that is a
    BaseException instance is raised instead of returned -- the same shape a
    real transport timeout takes when it escapes fetch() (guarded_api's
    existing except-Exception around fetch() is what turns this into a
    ToolAbort(kind="transport"), so raising here exercises that same path,
    not a hand-built exception at the guard boundary).
    """

    def __init__(self, scripted):
        self._scripted = list(scripted)
        self.calls: list[tuple[object, object, object]] = []

    async def __call__(self, endpoint, *, cookie_header, params=None, body=None):
        self.calls.append((endpoint, params, body))
        item = self._scripted.pop(0)
        if isinstance(item, BaseException):
            raise item
        return item


@pytest.fixture(autouse=True)
def _never_open_a_real_browser_tab(monkeypatch):
    """login() now asks the OS to open the login page in the user's default
    browser (tools/auth._open_in_local_browser) in the same-machine
    configuration. No test may ever pop a real browser window: stub it to
    "could not open" for every test, and let the tests that care about the
    call re-patch it with a recorder."""
    try:
        import mcp104.tools.auth as auth_mod
    except ImportError:
        return
    monkeypatch.setattr(auth_mod, "_open_in_local_browser", lambda url: False, raising=False)
