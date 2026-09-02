"""AP (Round I8): `.gitignore` and `.dockerignore` must agree on which paths are
excluded BECAUSE they hold personal data.

**Context hygiene, not image gatekeeping — since the package-layout restructure.**
`.dockerignore` no longer decides what reaches the final image: `Dockerfile` carries
three explicit `COPY` lines (`requirements.txt`, `pyproject.toml`, `src/`) and no
`COPY . .`, so an allowlist bounds the image regardless of `.dockerignore`'s contents.
`.dockerignore` keeps exactly one remaining job: personal
data must not leave the machine at all, and the build context sent to the Docker daemon
must stay small. `docker build` uploads the entire directory tree (minus whatever
`.dockerignore` excludes) to the daemon BEFORE any `COPY` line runs, independent of
which files a Dockerfile later chooses to copy out of that context — so a personal-data
path missing from `.dockerignore` still leaves the machine even though the allowlist
would keep it out of the image's layers. This test's core question — does a
personal-data path excluded from git also stay out of the Docker build context — holds
exactly as before; only the consequence of a gap changed shape (an oversized/leaky build
context, not necessarily a baked-in image layer). It was not hypothetical: an earlier
image was found to contain 9.4 MB of real candidates' résumés and a live 104 session
cookie jar, excluded from git but not from the image, back when `COPY . .` was still how
the image was built.

There is no LIVE gap today -- the only two personal-data paths (`research/captures/`
and `data/`) are excluded in both files as of this writing. The risk is near-term
rather than theoretical: the next feature migrates the messaging tools, which means
new measurement captures, and a new capture directory added to `.gitignore` and not
to `.dockerignore` reproduces this exact defect with a new path.

**This test deliberately does NOT diff the two files.** They legitimately differ
for unrelated reasons: `.dockerignore` excludes `tests/` and `docs/` for image
size; `.gitignore` excludes `*.pyc` and `.pytest_cache/`, neither of which
`.dockerignore` needs to name because `.dockerignore` already excludes broader
patterns that subsume them. A wholesale diff would be red on day one for reasons
that have nothing to do with personal data.

What must agree is narrower, and nothing in either file marks a rule as
"personal-data" versus "housekeeping" in a machine-readable way -- so there is no
way to DERIVE the list below from the files themselves without a marker neither
file currently carries, and this test's own scope (`tests/` only, this round) means
it cannot add one. `PERSONAL_DATA_PATHS` is therefore an explicit, hand-maintained
list, and that is a real limitation stated honestly rather than an oversight:

    *** ADD TO PERSONAL_DATA_PATHS WHEN A NEW PERSONAL-DATA DIRECTORY IS
    INTRODUCED *** (e.g. the messaging-tools migration's new capture directory).
    This is the one place in the repository where that update depends on a human
    remembering -- which is exactly why this docstring and the assertion failure
    messages below both say so, so whoever adds the new path has a chance to find
    this file before the gap ships.
"""
from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
GITIGNORE_PATH = REPO_ROOT / ".gitignore"
DOCKERIGNORE_PATH = REPO_ROOT / ".dockerignore"
DOCKERFILE_PATH = REPO_ROOT / "Dockerfile"  # the assertion below must open THIS name —
# it is the only filename Docker looks for by default (an earlier, differently-named
# build file needed an explicit -f flag on every invocation, and is gone), so a test
# opening the wrong name would silently stop checking the file `docker build` actually
# reads.

# The DIRECTORY PREFIXES excluded from both files because they hold personal data
# (real candidates' résumés, names, e-mail addresses, phone numbers, or live 104
# session identifiers) -- see the module docstring for why this list is explicit
# rather than derived, and for the instruction to extend it.
PERSONAL_DATA_PATHS = (
    "research/captures/",
    "data/",
)


def _ignore_lines(text: str) -> set[str]:
    """Every non-comment, non-blank line, exactly as written -- no glob expansion.
    `.gitignore` and `.dockerignore` do not need to spell a personal-data
    directory identically (`.gitignore` narrows `data/` to `data/*.db` and
    `data/*.json`, the two file types that actually exist there and actually
    carry live session/candidate state, while `.dockerignore` excludes the whole
    `data/` directory for a different, image-freshness reason stated in its own
    comment) -- so this returns raw lines for prefix matching in `_covers_prefix`
    below, rather than attempting to reproduce either file's full glob semantics.
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
    that distinguishes "present" from "silently absent" for this test's purpose.

    **What this cannot answer, and why a second check exists below.** A line
    passing this check proves a rule EXISTS whose text starts with the expected
    path -- it does not prove that rule actually excludes anything at the depth
    the real directory lives at. `.dockerignore`'s own "Local noise" section is a
    measured example of the gap: `__pycache__/`, root-scoped, textually looks like
    a real rule and is one, but matched nothing under `src/mcp104/**/__pycache__/`
    until it gained a `**/` prefix. `_is_depth_safe_coverage` below is the
    narrower, structural check that catches that shape of gap for the
    personal-data rules specifically.
    """
    return any(line.startswith(directory_prefix) for line in ignore_lines)


_WILDCARD_CHARS = frozenset("*?[")


def _is_depth_safe_coverage(line: str, prefix: str) -> bool:
    """Whether `line`, taken as an ignore-file pattern, is STRUCTURALLY trustworthy
    to exclude `prefix` regardless of what depth `prefix` actually sits at.

    Deliberately NOT a glob interpreter -- this repo's two ignore files use two
    DIFFERENT matching engines (git's own, and Docker's filepath.Match-based one;
    see .gitignore's own comment on why a bare `__pycache__/` needs no `**/` in
    git but does in `.dockerignore`) and this function does not simulate either.
    It recognises exactly two shapes as safe:

    - `line` starts with `**/` -- the Docker idiom for "cross any number of
      directories", explicitly depth-anchored regardless of engine.
    - `line` starts with `prefix` AND no wildcard character (`*`/`?`/`[`) appears
      anywhere in the shared span (the first `len(prefix)` characters). If
      `prefix` itself is wildcard-free (asserted separately -- see
      `test_personal_data_dockerignore_rules_are_depth_safe`), this means `line`
      spells out `prefix`'s own path LITERALLY: depth is baked into the string
      itself, so there is no glob to misinterpret, independent of which engine
      reads it. Anything `line` does with wildcards AFTER that shared span (e.g.
      `data/*.db`) narrows what's excluded INSIDE `prefix`; it cannot change
      whether `prefix` itself is reached.

    **What a True result proves, and what it does not.** It proves the rule's
    SHAPE is capable of surviving `prefix` moving to a different depth than it
    sits at today. It does NOT prove Docker or git actually honours the rule at
    runtime -- only a real build (this project's Docker verification step 14) or
    `git check-ignore -v` proves that; this function is a cheap, offline
    structural proxy for it, not a replacement.
    """
    if line.startswith("**/"):
        return True
    if not line.startswith(prefix):
        return False
    shared_span = line[:len(prefix)]
    return not any(ch in shared_span for ch in _WILDCARD_CHARS)


def test_personal_data_paths_are_excluded_in_both_ignore_files():
    gitignore_lines = _ignore_lines(GITIGNORE_PATH.read_text(encoding="utf-8"))
    dockerignore_lines = _ignore_lines(DOCKERIGNORE_PATH.read_text(encoding="utf-8"))

    missing_from_gitignore = [
        path for path in PERSONAL_DATA_PATHS if not _covers_prefix(gitignore_lines, path)
    ]
    missing_from_dockerignore = [
        path for path in PERSONAL_DATA_PATHS if not _covers_prefix(dockerignore_lines, path)
    ]

    assert missing_from_gitignore == [], (
        f".gitignore has no rule covering personal-data path(s) "
        f"{missing_from_gitignore} -- these are committed as excluded from the "
        f"Docker build (.dockerignore) but would be visible in `git status`/history "
        f"if this is really a gap, or PERSONAL_DATA_PATHS in "
        f"tests/test_ignore_files.py needs updating if the path moved"
    )
    assert missing_from_dockerignore == [], (
        f".dockerignore has no rule covering personal-data path(s) "
        f"{missing_from_dockerignore} -- this is the exact defect class "
        f".dockerignore's own header describes: a path git excludes but the "
        f"Docker build context does not, so it still leaves the machine even though "
        f"Dockerfile's allowlist COPY lines would keep it out of the image itself. Add "
        f"the path to .dockerignore, or update PERSONAL_DATA_PATHS in "
        f"tests/test_ignore_files.py if it no longer holds personal data"
    )


def test_personal_data_dockerignore_rules_are_depth_safe():
    """Strengthens the test above: `_covers_prefix` only proves a rule's TEXT
    starts with the expected path, which a rule can satisfy while excluding
    nothing at the directory's real depth (see `_covers_prefix`'s own docstring
    for the measured `__pycache__/` example this is modelled on). This asserts
    two things `_covers_prefix` cannot:

    1. Every `PERSONAL_DATA_PATHS` entry is itself wildcard-free -- a literal,
       fully-spelled path from the repo root ("root-level by construction"). This
       is the premise the whole list's safety rests on: it is what makes a
       plain string-prefix match equivalent to a real directory match, for
       EITHER ignore file's matching engine, with no glob interpretation needed.
       A future entry that is instead a wildcard pattern (e.g. meant to say
       "a capture directory at any depth") would silently invalidate that
       premise, and this is the one place that gets checked.
    2. Every `.dockerignore` line that textually covers a `PERSONAL_DATA_PATHS`
       entry is depth-safe per `_is_depth_safe_coverage` -- either the literal
       path itself (or a narrower literal extension of it) or `**/`-anchored.
       Given check 1 holds, this is currently guaranteed to pass (a wildcard-free
       prefix cannot be start-of-string-matched by a line with a wildcard inside
       the shared span) -- it is written out explicitly anyway, rather than left
       implicit, so the invariant is documented and re-checked if either
       assumption ever changes, per `_is_depth_safe_coverage`'s own docstring on
       what this can and cannot prove.
    """
    for prefix in PERSONAL_DATA_PATHS:
        assert not any(ch in prefix for ch in _WILDCARD_CHARS), (
            f"PERSONAL_DATA_PATHS entry {prefix!r} contains a wildcard character "
            f"({sorted(_WILDCARD_CHARS & set(prefix))}) -- this list is meant to "
            f"hold literal, fully-spelled paths from the repo root ('root-level by "
            f"construction'); a wildcard here means _is_depth_safe_coverage can no "
            f"longer vouch for whether a covering rule actually reaches it, and "
            f"this test needs its own update before it can trust this entry again"
        )

    dockerignore_lines = _ignore_lines(DOCKERIGNORE_PATH.read_text(encoding="utf-8"))
    unsafe = {
        prefix: [
            line for line in dockerignore_lines
            if line.startswith(prefix) and not _is_depth_safe_coverage(line, prefix)
        ]
        for prefix in PERSONAL_DATA_PATHS
    }
    unsafe = {prefix: lines for prefix, lines in unsafe.items() if lines}
    assert unsafe == {}, (
        f".dockerignore has a rule that textually covers a personal-data path but "
        f"is not structurally depth-safe (see _is_depth_safe_coverage's own "
        f"docstring for the two shapes that count as safe): {unsafe}"
    )


# `COPY . .` alone would miss `COPY . /app`, `COPY . ./` and `ADD . .` -- all of
# which copy the ENTIRE build context, same risk, different spelling. This matches
# any COPY/ADD instruction whose source argument is a bare `.` (the whole context),
# regardless of destination -- what makes an instruction "unrestricted" is copying
# from `.`, not which literal destination string follows it.
_UNRESTRICTED_COPY_RE = re.compile(r"^\s*(COPY|ADD)\s+\.\s", re.MULTILINE)


def test_dockerfile_has_no_unrestricted_copy_context():
    """Dockerfile must not carry a COPY/ADD instruction whose source is the entire
    build context (`COPY . .`, `COPY . /app`, `ADD . .`, ...) — three explicit COPY
    lines (`requirements.txt`, `pyproject.toml`, `src/`) are the allowlist that bounds
    what reaches the image after the package-layout restructure (see the module
    docstring). A reintroduced whole-context copy would silently widen that allowlist
    back to the entire build context, personal-data risk included, with nothing in the
    image build itself to catch it -- this is the exact defect class that once shipped
    9.4 MB of real candidates' résumés."""
    dockerfile_text = DOCKERFILE_PATH.read_text(encoding="utf-8")
    match = _UNRESTRICTED_COPY_RE.search(dockerfile_text)
    if match is not None:
        raise AssertionError(
            f"Dockerfile contains an unrestricted whole-context COPY/ADD "
            f"instruction (matched {match.group(0)!r}) -- this defeats the "
            f"allowlist of explicit COPY lines the package-layout restructure "
            f"introduced specifically to bound what reaches the image"
        )
