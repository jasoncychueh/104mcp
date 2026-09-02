"""§C1 (src/mcp104/main.py) test cases: T-30, T-32, T-33, T-35, T-36, T-37,
T-49, T-50, T-91, T-111.

Written blind to this cycle's main.py rewrite (spec-tester Mode 1 rule): every
assertion here goes through a subprocess boundary or through introspection of
the public interfaces named in design.md §C1 (`configure_logging`,
`AppContext`, the `mcp104` console script / `python -m mcp104.main` entry
point, and the argv self-test flag `--selftest-browser-stdout` named as
design.md's own example). None of it imports internals of main.py's function
bodies.
"""

import io
import json
import logging
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"


def _base_env(data_dir, label="tester@104.com"):
    env = dict(os.environ)
    env["MCP104_ACCOUNT_LABEL"] = label
    env["MCP104_DATA_DIR"] = str(data_dir)
    # Keep the auth site on an ephemeral local port unless a test overrides it.
    env.pop("MCP104_AUTH_BASE_URL", None)
    env.pop("MCP104_AUTH_BIND_PORT", None)
    return env


def _write_jsonrpc(proc, obj):
    line = json.dumps(obj) + "\n"
    proc.stdin.write(line.encode("utf-8"))
    proc.stdin.flush()


def _read_jsonrpc_line(proc, timeout=30):
    """Read one newline-delimited JSON-RPC message from the subprocess's
    stdout, off the raw pipe (not through the SDK client) so purity checks
    see exactly what the process wrote."""
    import threading

    result = {}

    def _reader():
        result["line"] = proc.stdout.readline()

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    t.join(timeout)
    if t.is_alive():
        raise TimeoutError("no line read from subprocess stdout within timeout")
    return result["line"]


def _handshake(proc):
    """Send the MCP stdio initialize handshake and return the raw bytes lines
    exchanged on stdout (for purity assertions)."""
    _write_jsonrpc(
        proc,
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2024-11-05",
                "capabilities": {},
                "clientInfo": {"name": "spec-tester", "version": "0.0.1"},
            },
        },
    )
    init_line = _read_jsonrpc_line(proc)
    _write_jsonrpc(proc, {"jsonrpc": "2.0", "method": "notifications/initialized"})
    return [init_line]


# ── T-30 (R5.3): data location gets created, init doesn't fail on it ───────

def test_t030_missing_data_dir_is_created_and_startup_succeeds(tmp_path):
    data_dir = tmp_path / "does" / "not" / "exist" / "yet"
    assert not data_dir.exists()

    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp104.main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_base_env(data_dir),
        cwd=str(tmp_path),
    )
    try:
        lines = _handshake(proc)
        resp = json.loads(lines[0])
        assert "result" in resp, f"initialize failed: {resp}"
        # A real handshake completing proves the startup sequence
        # (get_config -> create data dir -> Database.init -> compact_state_file
        # -> resolve_auth_binding) ran to completion without failing.
        assert data_dir.exists() and data_dir.is_dir()
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)


# ── T-32 (R6.2): argv self-test path — zero stdout bytes, exit 0, no leaks ─

def test_t032_selftest_browser_stdout_flag_writes_nothing_to_stdout():
    proc = subprocess.run(
        [sys.executable, "-m", "mcp104.main", "--selftest-browser-stdout"],
        capture_output=True,
        timeout=60,
    )
    if proc.returncode != 0 and (
        b"Executable doesn't exist" in proc.stderr
        or b"playwright" in proc.stderr.lower()
        or b"patchright" in proc.stderr.lower()
    ):
        pytest.skip(
            "browser not installed in this environment "
            f"(stderr: {proc.stderr[:500]!r})"
        )
    assert proc.stdout == b"", f"stdout must be zero bytes, got {proc.stdout!r}"
    assert proc.returncode == 0, (
        f"self-test path must exit zero, got {proc.returncode}, "
        f"stderr={proc.stderr!r}"
    )
    # The subprocess.run call above only returns once the process has fully
    # exited (bounded by the 60s timeout) — a lingering watcher, auth site,
    # or browser child process holding the event loop open would show up as
    # a hang against that timeout rather than a clean return.


# ── T-33 (R6.3): initialization failure -> zero stdout bytes ───────────────

def test_t033_init_failure_writes_nothing_to_stdout(tmp_path):
    env = _base_env(tmp_path)
    env["MCP104_ACCOUNT_LABEL"] = ""  # required, deliberately left unset

    proc = subprocess.run(
        [sys.executable, "-m", "mcp104.main"],
        input=b"",
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert proc.stdout == b"", f"stdout must be zero bytes, got {proc.stdout!r}"


# ── T-35 (R7.1): INFO-level messages reach diagnostic output ───────────────

def test_t035_info_level_messages_reach_diagnostic_output():
    from mcp104.main import configure_logging

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    fake_stderr = io.StringIO()
    real_stderr = sys.stderr
    try:
        sys.stderr = fake_stderr
        configure_logging()
        logging.getLogger("mcp104.test_t035").info("sentinel-info-message-t035")
        assert "sentinel-info-message-t035" in fake_stderr.getvalue(), (
            "an INFO-level message must not be silently discarded"
        )
    finally:
        sys.stderr = real_stderr
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# ── T-36 (R7.2, R7.3): init failure -> readable reason + nonzero exit ──────

def test_t036_init_failure_readable_reason_and_nonzero_exit(tmp_path):
    env = _base_env(tmp_path)
    env["MCP104_ACCOUNT_LABEL"] = "   "  # whitespace-only, deliberately invalid

    proc = subprocess.run(
        [sys.executable, "-m", "mcp104.main"],
        input=b"",
        capture_output=True,
        env=env,
        timeout=30,
    )
    assert proc.returncode != 0, "startup failure must exit with a nonzero status"
    assert proc.stderr.strip() != b"", (
        "startup failure must write a readable reason to stderr"
    )
    assert b"MCP104_ACCOUNT_LABEL" in proc.stderr, (
        "the failure reason should name the offending setting"
    )


# ── T-37 (R7.4, R7.5): diagnostic output doesn't leak input/credential/token ─

def test_t037_diagnostic_output_does_not_contain_full_token(tmp_path):
    data_dir = tmp_path / "data"
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp104.main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_base_env(data_dir),
        cwd=str(tmp_path),
    )
    fake_token = "sentinel-full-token-should-never-appear-verbatim-in-logs-88421"
    try:
        _handshake(proc)
        _write_jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "check_login", "arguments": {"token": fake_token}},
            },
        )
        _read_jsonrpc_line(proc)
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            _, stderr = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            _, stderr = proc.communicate(timeout=10)

    assert fake_token not in stderr.decode("utf-8", errors="replace"), (
        "diagnostic output must not contain the full login token"
    )


# ── T-49 (interface, main.configure_logging) ───────────────────────────────

def test_t049_configure_logging_routes_all_levels_to_diagnostics_not_stdout():
    from mcp104.main import configure_logging

    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    fake_stderr = io.StringIO()
    fake_stdout = io.StringIO()
    real_stderr, real_stdout = sys.stderr, sys.stdout
    try:
        sys.stderr = fake_stderr
        sys.stdout = fake_stdout
        configure_logging()

        for handler in root.handlers:
            stream = getattr(handler, "stream", None)
            assert stream is not fake_stdout, (
                "no handler may be configured to write to standard output"
            )

        logger = logging.getLogger("mcp104.test_t049")
        for level_name, log_fn, marker in [
            ("DEBUG", logger.debug, "sentinel-debug-t049"),
            ("INFO", logger.info, "sentinel-info-t049"),
            ("WARNING", logger.warning, "sentinel-warning-t049"),
            ("ERROR", logger.error, "sentinel-error-t049"),
        ]:
            log_fn(marker)

        diagnostics = fake_stderr.getvalue()
        # DEBUG's threshold is a level-setting decision outside this case's
        # scope; INFO and above must reach diagnostics per R7.1.
        assert "sentinel-info-t049" in diagnostics
        assert "sentinel-warning-t049" in diagnostics
        assert "sentinel-error-t049" in diagnostics
        assert fake_stdout.getvalue() == "", (
            "nothing must be written to standard output by logging"
        )
    finally:
        sys.stderr = real_stderr
        sys.stdout = real_stdout
        root.handlers[:] = saved_handlers
        root.setLevel(saved_level)


# ── T-50 (interface, main.AppContext) ───────────────────────────────────────

def test_t050_appcontext_has_no_display_manager_or_process_level_browser_field():
    import dataclasses

    from mcp104.main import AppContext

    assert dataclasses.is_dataclass(AppContext), "AppContext should be a dataclass"
    field_names = {f.name for f in dataclasses.fields(AppContext)}

    forbidden = {"vnc_manager", "browser", "_cleanup_task"}
    hit = field_names & forbidden
    assert not hit, f"AppContext must not carry these removed fields: {hit}"

    expected = {
        "config",
        "db",
        "session_pool",
        "_pending_logins",
        "_finished_logins",
        "_watcher_tasks",
        "auth_site",
        "logout_epoch",
    }
    missing = expected - field_names
    assert not missing, f"AppContext is missing expected fields: {missing}"


# ── T-91 (R6.1): full stdio handshake + one browser-free tool call ─────────

def test_t091_stdio_transport_carries_only_framed_protocol_messages(tmp_path):
    data_dir = tmp_path / "data"
    proc = subprocess.Popen(
        [sys.executable, "-m", "mcp104.main"],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_base_env(data_dir),
        cwd=str(tmp_path),
    )
    lines = []
    try:
        lines.extend(_handshake(proc))
        _write_jsonrpc(
            proc,
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                # check_login is explicitly exempt from requiring a browser
                # or a prior login() per CLAUDE.md's tool contract.
                "params": {
                    "name": "check_login",
                    "arguments": {"token": "no-such-token"},
                },
            },
        )
        lines.append(_read_jsonrpc_line(proc))
    finally:
        proc.stdin.close()
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=10)

    assert len(lines) == 2
    for raw in lines:
        assert raw.endswith(b"\n"), "every protocol message must be newline-framed"
        text = raw.decode("utf-8")
        parsed = json.loads(text)  # raises if any stray byte breaks framing
        assert parsed.get("jsonrpc") == "2.0"


# ── T-111 (main.run, e2e): imports succeed on the floor interpreter ────────

def _requires_python_floor():
    data = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
    spec = data["project"]["requires-python"]
    assert spec.startswith(">="), (
        f"unexpected requires-python spec shape: {spec!r}"
    )
    return spec[2:].strip()


def _find_floor_interpreter(version):
    import shutil

    if shutil.which("uv"):
        probe = subprocess.run(
            ["uv", "python", "find", version],
            capture_output=True,
            text=True,
        )
        if probe.returncode == 0 and probe.stdout.strip():
            path = probe.stdout.strip()
            if Path(path).exists():
                return path
    return None


def test_t111_package_imports_succeed_on_requires_python_floor(tmp_path):
    version = _requires_python_floor()
    interpreter = _find_floor_interpreter(version)
    if interpreter is None:
        pytest.skip(
            f"no {version} interpreter available in this environment "
            "(tried `uv python find`); cannot exercise the requires-python "
            "floor without one — skipping explicitly rather than silently "
            "passing"
        )

    venv_dir = tmp_path / "floor-venv"
    subprocess.run(
        ["uv", "venv", "--python", interpreter, str(venv_dir)],
        check=True,
        capture_output=True,
    )
    venv_python = (
        venv_dir / "Scripts" / "python.exe"
        if os.name == "nt"
        else venv_dir / "bin" / "python"
    )
    subprocess.run(
        ["uv", "pip", "install", "--python", str(venv_python), "-e", str(REPO_ROOT)],
        check=True,
        capture_output=True,
        cwd=str(REPO_ROOT),
    )

    modules = (
        "mcp104.tools.messaging, mcp104.tools.search, mcp104.tools.helpers, "
        "mcp104.db.database, mcp104.config, mcp104.browser.cdp_stream, "
        "mcp104.web.auth_server, mcp104.main"
    )
    proc = subprocess.run(
        [str(venv_python), "-c", f"import {modules}"],
        capture_output=True,
        cwd=str(tmp_path),  # deliberately outside the repo root
    )
    assert proc.returncode == 0, (
        f"imports failed on the requires-python floor ({version}):\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr!r}"
    )

    entry_probe = subprocess.run(
        [str(venv_python), "-c", "import mcp104.main; assert callable(mcp104.main.run)"],
        capture_output=True,
        cwd=str(tmp_path),
    )
    assert entry_probe.returncode == 0, (
        f"console-script entry point mcp104.main:run failed to import/resolve "
        f"on {version}:\nstdout={entry_probe.stdout!r}\nstderr={entry_probe.stderr!r}"
    )
