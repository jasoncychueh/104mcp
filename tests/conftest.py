"""Repo-wide test fixtures and shared test doubles.

A conftest full of autouse fixtures makes "why does this test behave
differently in another file" a real investigation, so this file keeps to the
two autouse fixtures every test needs (identity/data-dir environment, and
no real browser tab) on purpose. `_SeqFetchSpy` is a plain (non-fixture, non-autouse)
test double shared by tests/test_helpers.py and tests/test_messaging.py,
which both drove guarded_api/guarded_sequence's scripted sub-request
sequences through byte-for-byte identical copies of this class before it
was consolidated here.

Several fake app-context classes (tests/test_api_client.py,
tests/test_helpers.py, tests/test_messaging.py, tests/test_search.py) call
get_config() in their own __init__. The autouse fixture points
MCP104_DATA_DIR at tmp_path, so a test that constructs a real Config (rather
than mocking one) does not touch this machine's actual per-user data
directory, and it removes the retired bare setting names: get_config()
refuses to start while any of them is set, and a developer's shell must not
turn every such construction into a failure.

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

from mcp104.config import RETIRED_ENV_NAMES

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
    for retired in RETIRED_ENV_NAMES:
        monkeypatch.delenv(retired, raising=False)
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
