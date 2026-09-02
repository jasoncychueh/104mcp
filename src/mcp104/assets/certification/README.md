# Certification corpus

`certify_conditions_results.json` is the recorded output of
`research/probes/certify_conditions.py`: one baseline+treatment request pair per shipped
filter-key condition in `tools/filters.py`'s `CONDITIONS` table, run against a live 104
session. Each entry carries what was actually submitted (`filters_sent`), what 104 echoed
back (`echoed`), and the totals that certified the condition as applied — the raw evidence
`tools/drop_detection.py`'s `_derive_echo_evidence()` turns into each row's
`echo_evidence` (`"echoed"` / `"not-echoed"` / `"unmeasured"`) at import time.

## What it is not

Not real candidate data. Every value in this file is either our own filter input, a 104
wire parameter name, or a result **count** — never a résumé field, a message, or anything
identifying a jobseeker. That is what makes it safe to commit, unlike `research/captures/`
(real captured pages/bodies, out of version control by design).

## Why it is a versioned build input, not runtime data

Production code (`tools/drop_detection.py`) reads this file at import time and **raises if
it is absent** — a missing corpus would otherwise silently derive every condition as
`unmeasured`, switching drop detection off while every filtered search kept reporting
success. That failure mode is exactly what this file's presence guards against, so it
ships inside the package (`[tool.setuptools.package-data]` in `pyproject.toml`) and the
Dockerfile's `COPY src/ ./src/` line carries it into the image — there is no separate step
that copies it in.

## Who regenerates it, and how

`research/probes/certify_conditions.py` is the only tool that writes this file. It
resumes rather than starting over: existing entries are read back and only missing
conditions are re-submitted, so deleting a handful of entries (rather than the whole
file) forces a targeted re-run without re-measuring everything. It writes to this file by
**repo-root arithmetic** (`Path(__file__).resolve().parents[2] / "src" / "mcp104" /
"assets" / "certification" / "certify_conditions_results.json"`, i.e. its own
`RESULTS_PATH`), not through the installed `mcp104` package — regenerating this file must
update the copy under version control, and writing through
`importlib.resources.files("mcp104")` would, for a non-editable install, land in
`site-packages` instead, invisible to `git status`. The probe deliberately does **not**
import `tools/drop_detection.py` (the module that reads this file) — see that module's
own docstring for why importing it would deadlock regeneration when the file is missing.

Regenerating requires a live, logged-in 104 session (see `research/README.md`) and is a
production-account operation like any other search — it counts against the same daily
résumé-browse quota `CLAUDE.md` documents.
