"""Packaging / release-artifact tests — feature stdio-cdp-rearchitecture, §C9.

Cases covered (design.md §Testing Strategy Test Cases table):
  T-34 (R6.4)  — runtime dependency set literally equals a reviewed allowlist.
  T-47 (R11.1) — .gitignore covers real candidate data, login-state files, and
                 measurement-capture artifacts.
  T-48 (R11.3) — the ignore check still exists after .dockerignore's removal,
                 and something explains why that line of defense disappeared.
  T-88 (R11.2) — the actual built release artifact's member list contains none
                 of the above.

Written blind to this cycle's in-flight changes to pyproject.toml,
requirements.txt, .mcp.json, .gitignore, and the Docker files — see the
spec-tester dispatch note. Assertions are grounded in design.md / CLAUDE.md /
steering docs, not in a peek at those files' new content.
"""
from __future__ import annotations

import shutil
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _load_pyproject() -> dict:
    try:
        import tomllib
    except ImportError:  # pragma: no cover - py<3.11 fallback
        import tomli as tomllib  # type: ignore[no-redef]
    with open(REPO_ROOT / "pyproject.toml", "rb") as f:
        return tomllib.load(f)


def _dependency_name(spec: str) -> str:
    """Extract the bare distribution name from a PEP 508 dependency spec."""
    import re

    m = re.match(r"^[A-Za-z0-9_.\-]+", spec.strip())
    assert m, f"could not parse dependency spec: {spec!r}"
    return m.group(0).lower()


# T-34 — reviewed allowlist. `aiosqlite` and `tzdata` were already runtime
# dependencies before this cycle; `uvicorn` is dropped this cycle (streamable-
# http server is gone) and nothing new is added.
REVIEWED_RUNTIME_DEPENDENCY_ALLOWLIST = {
    "mcp",
    "patchright",
    "aiohttp",
    "aiosqlite",
    "tzdata",
}


def test_runtime_dependencies_equal_reviewed_allowlist():
    """T-34 (R6.4): the declared [project] dependencies set is literally the
    reviewed allowlist — no more, no less. A new dependency must be reviewed
    for whether it writes to stdout by default before this list changes.

    This allowlist is the set reviewed on 2026-09-02 per design.md §C9
    (aiosqlite/tzdata already present pre-cycle; uvicorn dropped this cycle
    with the streamable-http server's removal; nothing new added). Any drift
    must be adjudicated by a human before this list changes.
    """
    data = _load_pyproject()
    deps = data.get("project", {}).get("dependencies", [])
    assert deps, "expected [project] dependencies to be declared and non-empty"
    declared = {_dependency_name(d) for d in deps}
    assert declared == REVIEWED_RUNTIME_DEPENDENCY_ALLOWLIST, (
        "runtime dependency set no longer matches the reviewed allowlist "
        f"(declared={declared!r}, allowlist={REVIEWED_RUNTIME_DEPENDENCY_ALLOWLIST!r}). "
        "This is not necessarily wrong — it may mean the allowlist itself needs "
        "a reviewed update — but it must not pass silently."
    )


# T-47 / T-48 — representative paths for each protected category, grounded in
# CLAUDE.md (data/cookies.json — login state; SQLite persistence under data/)
# and structure.md (research/captures/ — real candidate résumés, names,
# e-mails, explicitly marked "❌ .gitignore" in the target directory tree).
_LOGIN_STATE_SAMPLE = "data/cookies.json"
_CANDIDATE_DATA_SAMPLE = "data/104.db"
_MEASUREMENT_CAPTURE_SAMPLE = "research/captures/sample_page.html"

_PROTECTED_SAMPLE_PATHS = {
    "login state file": _LOGIN_STATE_SAMPLE,
    "candidate data": _CANDIDATE_DATA_SAMPLE,
    "measurement capture artifact": _MEASUREMENT_CAPTURE_SAMPLE,
}


def _git_check_ignore(rel_path: str) -> bool:
    """Return True iff `rel_path` is ignored by version control, per git's own
    resolution (not a hand-parsed reading of .gitignore)."""
    result = subprocess.run(
        ["git", "check-ignore", "-q", rel_path],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    return result.returncode == 0


@pytest.mark.parametrize(
    "label,rel_path",
    list(_PROTECTED_SAMPLE_PATHS.items()),
)
def test_gitignore_covers_pii_and_login_state_categories(label, rel_path):
    """T-47 (R11.1): version control's ignore rules cover real candidate
    data, login-state files, and measurement-capture artifacts."""
    assert _git_check_ignore(rel_path), (
        f"{label} sample path {rel_path!r} is not ignored by version control"
    )


def test_ignore_check_still_covers_pii_after_dockerignore_removed():
    """T-48 (R11.3): with .dockerignore gone (one of the two original lines
    of defense), the remaining check (.gitignore) is still automatically
    checked, and something in the repo explains why the other line
    disappeared rather than leaving the gap looking like an oversight."""
    dockerignore_path = REPO_ROOT / ".dockerignore"
    assert not dockerignore_path.exists(), (
        ".dockerignore still exists — R11.3 anchors on it having been removed "
        "as part of this cycle's architecture change"
    )

    # The remaining line of defense is still actually checked.
    for rel_path in _PROTECTED_SAMPLE_PATHS.values():
        assert _git_check_ignore(rel_path), (
            f"{rel_path!r} is not ignored — the sole remaining line of defense "
            "must still hold after .dockerignore's removal"
        )

    # Something explains the disappearance of the other line of defense —
    # search the plausible places rather than assuming one specific file.
    candidate_docs = [
        REPO_ROOT / ".gitignore",
        REPO_ROOT / "tests" / "test_ignore_files.py",
    ]
    explanation_found = False
    for doc in candidate_docs:
        if not doc.exists():
            continue
        text = doc.read_text(encoding="utf-8", errors="ignore").lower()
        if "dockerignore" in text:
            explanation_found = True
            break
    assert explanation_found, (
        "no candidate document (.gitignore, tests/test_ignore_files.py) "
        "mentions .dockerignore — R11.3 requires the remaining check to explain "
        "why the other line of defense disappeared, not just silently drop it"
    )


def test_built_release_artifact_excludes_pii_and_login_state(tmp_path):
    """T-88 (R11.2): the actual built release artifact's member list —not the
    declared inclusion rules— contains no real candidate data, login-state
    file, or measurement-capture artifact.

    Uses `--no-isolation`: a fully isolated build in this environment fails
    inside Anaconda's pip resolving a Windows registry path via `winreg`
    (unrelated to this project's packaging) — confirmed separately that
    `--no-isolation` builds both wheel and sdist successfully here. Output is
    written to `tmp_path` (pytest-managed, auto-cleaned), and any build-time
    leftovers under the repo itself (`build/`, `src/mcp104.egg-info/`) are
    removed afterward regardless of outcome so the working tree stays clean.
    """
    build_mod_available = (
        subprocess.run(
            [sys.executable, "-c", "import build"],
            capture_output=True,
        ).returncode
        == 0
    )
    if not build_mod_available:
        pytest.skip(
            "the 'build' package is not installed in this environment — cannot "
            "build a real release artifact to inspect; explicit skip, not a "
            "silent pass"
        )

    leftover_build_dir = REPO_ROOT / "build"
    leftover_egg_info = REPO_ROOT / "src" / "mcp104.egg-info"

    try:
        proc = subprocess.run(
            [
                sys.executable, "-m", "build",
                "--no-isolation",
                "--sdist", "--wheel",
                "--outdir", str(tmp_path),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=180,
        )
        assert proc.returncode == 0, (
            f"building the release artifact failed:\nstdout={proc.stdout}\nstderr={proc.stderr}"
        )

        built = list(tmp_path.glob("*"))
        assert built, "expected a built artifact in the output directory"

        members: list[str] = []
        for artifact in built:
            if artifact.name.endswith(".tar.gz"):
                with tarfile.open(artifact, "r:gz") as tf:
                    members.extend(tf.getnames())
            elif artifact.suffix in (".whl", ".zip"):
                with zipfile.ZipFile(artifact) as zf:
                    members.extend(zf.namelist())

        assert members, "could not enumerate any members of the built artifact(s)"

        forbidden_markers = [
            "cookies.json",
            "104.db",
            "research/captures/",
            "logout_unconfirmed",
        ]
        offenders = [
            m for m in members
            if any(marker in m for marker in forbidden_markers)
        ]
        assert not offenders, (
            f"built release artifact contains forbidden members: {offenders!r}"
        )
    finally:
        shutil.rmtree(leftover_build_dir, ignore_errors=True)
        shutil.rmtree(leftover_egg_info, ignore_errors=True)
