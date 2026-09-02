"""AP (Round I8, narrowed this cycle): `.gitignore` must exclude every
personal-data path.

**`.dockerignore` and `Dockerfile` are both gone this cycle (§C9, Phase 4 task
1) — the container packaging/deployment shape is retired along with them, not
merely edited.** This file used to check a SECOND protection line: that
`.dockerignore` also excluded every personal-data path, because `docker build`
uploads the entire directory tree (minus `.dockerignore`'s exclusions) to the
daemon before any `COPY` line runs — so a personal-data path missing from
`.dockerignore` still left the machine even though `Dockerfile`'s allowlist
`COPY` lines would have kept it out of the final image. With no `Dockerfile`
and no Docker build context, that specific defect class (personal data leaving
the machine via an unfiltered *build context* upload) cannot occur any more —
there is no longer a second directory tree, separately filtered by a second
engine, for a personal-data path to go missing from. It is not being treated
as "fixed" so much as "the mechanism it depended on no longer exists."

The replacement concern for a stdio-launched, pip/uvx-distributed process is
narrower and different in kind: not "what does the build context upload" but
"what does the published PACKAGE (sdist/wheel) contain" — `pyproject.toml`'s
own `[tool.setuptools.package-data]`/`packages` configuration is what decides
that now, not an ignore file at all. That check — the actual built artifact's
member list carries no real candidate data, login state, or measurement
captures — is **T-88**, owned by Phase 4 task 1's own verification (the
`pyproject.toml`/packaging task), not this file; it is a distinct assertion
against a distinct artifact (a built sdist/wheel's file list) and duplicating
it here against `.gitignore`'s text would test the wrong object.

What remains this file's job, unchanged from before: **T-47** — `.gitignore`
alone must still exclude real candidate data, login state, and measurement
captures, because git history is still a way personal data could leak (a
`git add -A` from a directory MCP104_DATA_DIR happens to point at, an
accidental commit before this file existed, etc). **T-48** is this docstring
itself — the check for the second protection line is gone, and this paragraph
is where that fact is recorded, not silently dropped.

    *** ADD TO PERSONAL_DATA_PATHS WHEN A NEW PERSONAL-DATA DIRECTORY IS
    INTRODUCED *** — same standing instruction as before this cycle. This is
    the one place in the repository where that update depends on a human
    remembering, which is exactly why this docstring and the assertion
    failure messages below both say so.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE_PATH = REPO_ROOT / ".gitignore"

# The DIRECTORY PREFIXES excluded from .gitignore because they hold personal data
# (real candidates' résumés, names, e-mail addresses, phone numbers, or live 104
# session identifiers) -- see the module docstring for why this list is explicit
# rather than derived, and for the instruction to extend it.
PERSONAL_DATA_PATHS = (
    "research/captures/",
    "data/",
)


def _ignore_lines(text: str) -> set[str]:
    """Every non-comment, non-blank line, exactly as written -- no glob
    expansion. `.gitignore` narrows `data/` to `data/*.db`, `data/*.json`, etc
    (the file types that actually exist there and actually carry live
    session/candidate state) rather than excluding the whole directory -- so
    this returns raw lines for prefix matching in `_covers_prefix` below,
    rather than attempting to reproduce git's full glob semantics.
    """
    return {
        line.strip()
        for line in text.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }


def _covers_prefix(ignore_lines: set[str], directory_prefix: str) -> bool:
    """True if some line in `ignore_lines` excludes something under
    `directory_prefix` -- either the prefix itself (`research/captures/`) or a
    narrower pattern reaching inside it (`data/*.db`). Deliberately a prefix
    check, not full glob matching: precise enough to answer "is ANYTHING under
    this personal-data directory excluded at all", which is the only question
    that distinguishes "present" from "silently absent" for this test's
    purpose.
    """
    return any(line.startswith(directory_prefix) for line in ignore_lines)


def test_personal_data_paths_are_excluded_from_gitignore():
    gitignore_lines = _ignore_lines(GITIGNORE_PATH.read_text(encoding="utf-8"))

    missing = [
        path for path in PERSONAL_DATA_PATHS if not _covers_prefix(gitignore_lines, path)
    ]

    assert missing == [], (
        f".gitignore has no rule covering personal-data path(s) {missing} -- "
        f"these would be visible in `git status`/history if this is really a "
        f"gap, or PERSONAL_DATA_PATHS in tests/test_ignore_files.py needs "
        f"updating if the path moved"
    )


def test_personal_data_paths_are_wildcard_free():
    """The list's safety rests on being literal, fully-spelled paths from the
    repo root -- a wildcard entry would make `_covers_prefix`'s plain
    string-prefix match unreliable (it could match a rule that only narrows
    something ELSE beginning with the same characters). Same premise this
    file checked before the Docker-side depth-safety check was retired; kept
    on its own because it is still meaningful for the one ignore file left.
    """
    wildcard_chars = frozenset("*?[")
    for prefix in PERSONAL_DATA_PATHS:
        assert not any(ch in prefix for ch in wildcard_chars), (
            f"PERSONAL_DATA_PATHS entry {prefix!r} contains a wildcard "
            f"character ({sorted(wildcard_chars & set(prefix))}) -- this list "
            f"is meant to hold literal, fully-spelled paths from the repo "
            f"root; a wildcard here means the prefix match above can no "
            f"longer be trusted to mean what it appears to mean"
        )
