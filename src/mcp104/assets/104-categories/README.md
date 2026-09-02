# 104 category datasets

Eight of 104's public, unauthenticated category-tree JSON files, bundled so that
`tools/categories.py` can resolve a caller's category name to 104's code **offline** — no
network access, no login, no request guard, no throttle. `tools/filters.py`'s condition
table records which filter condition each dataset serves; this README covers only the
files themselves — provenance, format and how to refresh them.

## Why here and not under `data/`

`data/` is the runtime persistence directory (SQLite database, the live session cookie file) and
its `.gitignore` rule (`data/*.json`) exists to keep that file's contents — real candidate data —
out of version control. These eight files are the opposite: public 104 taxonomy data, meant to be
committed and shipped with the code. A directory whose name states that it holds committed assets
removes the ambiguity, rather than relying on the `data/*.json` pattern happening not to reach a
subdirectory (it doesn't today, but tightening it to a recursive pattern is a plausible future edit
that would silently drop these files from the repository). Confirmed with
`git check-ignore -v src/mcp104/assets/104-categories/*` returning nothing (2026-08-14).

Packaged under `src/mcp104/assets/` (not the repository root) because setuptools can only ship
package data that lives inside the package, and the container image's allowlist `COPY` lines carry
nothing outside `src/mcp104/` — see `.spec/steering/tech.md`'s package-layout note. Production code
(`tools/categories.py`) reads these files via `importlib.resources.files("mcp104")`, addressing the
installed package tree, not this directory by a repo-relative path.

## Provenance

- **Source:** `https://static.104.com.tw/category-tool/json/<FileName>.json` — the URL pattern
  recorded in `docs/104-site-facts.md` (§ Technology Stack in `.spec/steering/tech.md`: "104 公開
  分類資料（`static.104.com.tw/category-tool/json/*.json`）— 免登入、不在受保護主機上、零反爬蟲
  曝險"). Unauthenticated, public, zero anti-bot exposure — this is why fetching them (then, and on
  refresh) is not routed through `guarded_page`/`guarded_api` or the throttle.
- **Fetched:** 2026-08-13, copied verbatim from `research/captures/category_json/` (that directory
  holds the original captures alongside several datasets this feature does not bundle; only the
  eight below are copied here).
- **Node counts, depth and byte size measured against these exact files** (`docs/104-site-facts.md`
  §6b.3g, 2026-08-14 offline verification, zero requests):

| File | Serves condition(s) | Nodes | Depth |
|---|---|---:|---:|
| `JobCat.json` | `jobcat` | 681 | 3 |
| `AreaWork.json` | `city` | 126 | 2 |
| `Area.json` | `home` | 1056 | 3 |
| `Indust.json` | `workExpInd`, `expectInd` | 366 | 3 |
| `Major.json` | `major` | 156 | 2 |
| `Tool.json` | `goodTools` | 799 | 3 |
| `Skill.json` | `certificates` | 2661 | 3 |
| `Abroad.json` | `studyAbroad`, `nationality` | 211 | 2 |

Every mapping above was verified by resolving one known name in the file to its documented code
(`docs/104-site-facts.md` §6b.3g "八份分類資料集對應到哪個條件") — the mapping was previously only
inferred from filenames, and that inference turned out wrong once for `Skill.json` (its first
branch is language-related *certificates*, not language ability, which is why it looks at a glance
like a language dataset and is not one).

## Format

Each file is a JSON array of tree nodes. A node carries at least:

```json
{"no": "2010001000", "des": "操作／技術類人員", "eng": "...", "n": [ ... child nodes ... ]}
```

`no` is 104's code (string), `des` is the Chinese display name, `n` is the (optional) array of
child nodes — its absence or emptiness marks a leaf. `Tool.json` and `Skill.json` carry a few extra
per-node keys (`icon`, `issuing_authority*`) that `tools/categories.py` does not read.

## Refresh procedure

1. Fetch each URL above with a plain HTTP client (`curl`, `requests`, or a browser's network tab
   suffices) — no 104 login is required and no throttle applies, per the provenance note above.
2. Save each response byte-for-byte as `<FileName>.json` in this directory, overwriting the
   previous copy.
3. Re-run `research/probes/sweep_resolution_precision.py` (offline, zero requests) against the
   refreshed files and compare its precision table against the one recorded in
   `docs/104-site-facts.md` §6b.3g — a refresh could change node counts, decoration patterns, or
   the name-prefix-family census that section measured.
4. Update the node/depth table above and the fetch date if anything changed.
5. Re-run `python -m pytest tests/test_categories.py -q` — T-51 asserts each dataset's measured
   codes against fixed expected values, so a refresh that renumbers or renames a node this project
   already depends on will fail loudly here rather than silently in production.

No measurement exists yet for how often 104 changes these trees, so there is no recommended refresh
cadence — refresh on demand when a resolution looks stale, not on a schedule.
