# tests/fixtures/ — provenance and regeneration

Every file in this directory is derived, by `research/probes/redact_fixtures.py`, from
`research/captures/` — the gitignored measurement archive that holds real candidates' résumés,
names, e-mail addresses and phone numbers taken from a live 104 enterprise session (see
`research/README.md` and `CLAUDE.md`) — with one exception, `failure_send_validation.json`,
derived from `research/results/send_validation.json` instead (a probe's recorded output, not a
raw capture; see its own table row below and "A gap the JSON-API messaging migration could not
close" for the two messaging-page fixtures this script could not derive from either directory at
all). A clean clone has no `research/captures/`, so tests must depend only on this directory
(design.md Testing Strategy). **Nothing here is hand-typed** — every value is produced by the
script, never edited in the JSON directly — **but "produced by the script" is not the same claim
as "measured"**: a small number of fields (currently only in `failure_send_validation.json`,
flagged in its own table row and its own `not_measured` key) are synthesised because the source
recording never captured them. Look for an explicit "not measured" / "synthesised" marker before
treating any single field as evidence of what 104 actually sent.

## Regenerating

```
python research/probes/redact_fixtures.py
```

Requires `research/captures/` to be present locally. Re-run whenever a capture under
`research/captures/` is corrected — that is the entire reason the derivation is scripted rather
than hand-maintained: a hand-edited fixture silently stops tracking its source the next time a
measurement is corrected.

## A gap the JSON-API messaging migration could not close

The messaging migration's plan called for two more fixtures here — one inbox (`all-stream`) page
and one conversation — derived from `research/captures/` by the same redaction this script
already applies to `rows_*.json`. **No such raw capture exists.** `research/captures/` holds no
`all-stream` body and no conversation body; the only messaging-related artifacts under
`research/` are analysis outputs (`research/results/messaging_contract.json`,
`messaging_endpoints.json`) that record field names, counts and correlation statistics, never a
full response body with real row data. Recording the gap rather than papering over it: `git
grep -l "all-stream\|jobNo.*pId\|candidateName" research/captures/` and a recursive search for
`*message*`/`*inbox*`/`*conversation*`/`*stream*` under `research/captures/` both come back with
nothing beyond `03_bc-comm_message_setting.json` and `07_bc-comm_message_unread-stream.json` —
neither of which is a row-bearing body.

`tests/test_messaging.py` therefore follows the precedent `tests/test_search.py` already set for
`list_jobs`' missing success-body fixture (see that file's own module docstring, "No committed
fixture exists for list_jobs' success body"): synthetic inbox rows and conversation messages,
built directly in the test file rather than committed under `tests/fixtures/`, using the
MEASURED field names (docs/104-site-facts.md §6b.7/§6b.8/§6b.10, cross-checked against
`messaging_contract.json`'s own field-name sweep — 19 inbox fields, 13 message fields, both
counts agreeing independently) with invented values. This is a deviation from the migration
plan's literal instruction and is flagged here rather than silently resolved, per this project's
"measure, don't infer" rule — closing it for real requires Verification steps 8/9 (a live
`read_messages()`/`get_conversation()` call), at which point the captured response should be
archived to `research/captures/` and this script extended with `build_inbox_page`/
`build_conversation` builders mirroring `build_rows_search`'s redaction shape.

## What is here, and where it came from

| Fixture | Source | Personal data | Treatment |
|---|---|---|---|
| `access_denied.json` | `research/captures/zero_row_match.json` | none | copied verbatim |
| `zero_row_search.json` | `research/captures/zero_row_search.json` (`.body` unwrapped from the probe's own diagnostic envelope) | none — empty container | copied verbatim |
| `zero_row_recommend.json` | `research/captures/zero_row_recommend.json` | none — empty container | copied verbatim |
| `rows_search.json` | `research/captures/api_bodies/08_api_search_searchResult.json` (50 rows) | names, ages, areas, photo URLs | redacted per row |
| `rows_recommend.json` | `research/captures/reco_match_rows.json`, key `/api/recommend/resumeListAll` (50 rows) | same | redacted per row |
| `rows_match.json` | `research/captures/reco_match_rows.json`, key `/api/match/matchResult` (3 rows, container key `resumes` — deliberately different from the other two routes, see design.md Components §9 / T-11) | same | redacted per row |
| `resume_unrestricted.json` | `research/captures/resume_correct_host.json` (`contactPrivacy: "public"`) | name, contact fields, attachments, operator identity | redacted |
| `resume_restricted.json` | `research/captures/resume_private.json` (`contactPrivacy: "private"`) | name, attachments, operator identity | redacted — **except** the masked contact fields, preserved byte-for-byte (see below) |
| `failure_family_a_logged_out.json` | `research/captures/failure_family_a_logged_out.json` | none | copied verbatim |
| `failure_family_a_expired.json` | `research/captures/failure_family_a_expired.json` | none in the body; the wrapper's `request_headers_sent.Cookie` was a live session credential | body copied verbatim; `Cookie` header value replaced with a redaction placeholder |
| `failure_family_b_unauthenticated.json` | `research/captures/failure_family_b_unauthenticated.json` | same | same |
| `failure_access_denied.json` | `research/captures/failure_access_denied.json` | same | same |
| `failure_data_not_found.json` | `research/captures/failure_data_not_found.json` | same | same |
| `failure_send_validation.json` | `research/results/send_validation.json` (the `"both_bogus:empty"` entry, `research/probes/probe_send_validation.py`'s reject-and-read output) | none — every target thread used `pId=0` (non-existent), no probed body ever carried text | **partially synthesised, not "copied verbatim"** — `http_status` and `body_json` are the probe's own measured `status`/`body`, copied through unchanged; `content_type` was never recorded by the probe on this route (assumed, matched to the one auth-host route that IS measured — `failure_family_b_unauthenticated.json`'s `application/json;charset=utf-8`, no space after the semicolon; the vip-host family-A fixtures write it with a space, which is a different host, not evidence for this one) and `body_text` is `body_json` re-serialised by the script, not captured bytes. The fixture's own `not_measured` field states both gaps; see `build_failure_send_validation`'s docstring in `redact_fixtures.py` for the full account |

`access_denied.json`'s source filename is misleading: `zero_row_match.json` does not actually
hold an empty-result match body — it holds the same `ACCESS_DENIED` shape that
`failure_access_denied.json` now documents more completely (with HTTP status, content type and
request headers alongside the body). Both are kept: `access_denied.json` is the older, bare-body
fixture; `failure_access_denied.json` is the fuller wrapper. See "How these were obtained" below
for why a richer capture exists now.

## The masking pair: opposite treatment on the contact fields

`resume_unrestricted.json` / `resume_restricted.json` are 104's own measured proof
(`docs/104-site-facts.md` §6b.3d) that a privacy-restricted résumé has its contact fields filled
with placeholder text rather than emptied: the restricted résumé's `email` is a short string with
no `@`, and `callTimeDesc` is *longer* than the unrestricted résumé's — the shape that defeats a
"shorter means masked" heuristic. **Those placeholder values are 104's masking, not the
candidate's data.** `redact_fixtures.py` asserts, before writing `resume_restricted.json`, that
`email`, `phoneDescAll`, `callTimeDesc`, `addressDesc` and every `phone` sub-field are still
byte-identical to `research/captures/resume_private.json` — if a future edit to the script ever
touches one of them, the script raises instead of silently degrading the fixture that several
tests (T-15, T-16) depend on.

### Design-basis gap this script resolved conservatively

design.md's Testing Strategy states plainly: *"What is redacted is the unrestricted candidate's
real contact details and name."* Read literally, that leaves the **restricted** candidate's name
alone. But 104 does not mask the name field on a privacy-restricted résumé — only the contact
fields — so the restricted résumé's `userName`/`engName` are the candidate's real, unmasked name,
not a masking placeholder. Leaving them in a committed fixture would fail this task's own
mandatory safety check (no real candidate name in `tests/fixtures/`). This script redacts the
restricted résumé's name exactly as it redacts the unrestricted one's, while leaving every
masking-placeholder field untouched. This is very likely what design.md intended (the sentence is
almost certainly summarizing "redact real names and contact details, preserve 104's masking" —
not making a deliberate exception for one candidate's name) but it is a literal deviation from the
doc's wording, flagged here rather than silently resolved, per this project's "measure, don't
infer" standing rule and the "no assumptions" rule this task was dispatched under.

### Fields intentionally left as measured, not redacted

design.md scopes the row-set redaction to "names, ages, areas" and the résumé pair's redaction to
"name and contact details." This script matches that scope rather than guessing at a broader one.
Left untouched: `expJobArr` (employment history — employer names, job descriptions),
`eduDesc`/`major`/school fields, and free-text fields such as `characteristic`, `workDesc`,
`introduction`, `motto`, `talentDesc` (except for a literal-name safety-net scrub, below). If a
future reviewer wants these redacted too, that is a scope change to make deliberately, not a gap
found by accident.

### What manual audit found beyond design.md's explicit list, and why it's handled

Design.md's stated scope ("name and contact details") undersells what a full résumé object
actually contains. Reading every field of both captures by hand (not just the fields design.md
names) surfaced three things the literal scope would have missed, all fixed in
`redact_fixtures.py`:

- **The candidate's own name written into free text.** `resume.auto` (self-written biography) on
  the unrestricted résumé read, in part, "...我叫[真實姓名]..." ("...my name is [real name]...") —
  the candidate wrote their own name into their own biography (the real name is deliberately not
  reproduced here — this document is itself committed to version control, so quoting it would
  recreate exactly the leak this section describes catching). Field-by-field redaction of only the
  structured `userName`/`engName` fields would have left this in place. `redact_fixtures.py`'s
  `_scrub_literal` runs after the structured redaction and replaces every remaining literal
  occurrence of the candidate's real name (in every written form found) across all string fields,
  including narrative ones this script otherwise deliberately leaves alone.
- **A real external link to the candidate's own document.** `attachArr[0].link` on the
  unrestricted résumé, and `customContent.list[0].body[0].url`/`.file.url` on the restricted
  résumé, were live `docs.google.com` URLs pointing at the candidate's own uploaded
  presentation/portfolio — including, in one case, the candidate's Google Drive account id in the
  query string, and an attachment `title` with the candidate's name embedded in the filename.
  `_redact_candidate_attachments` replaces every such attachment/achievement/custom-content link
  and title with a synthetic placeholder. Left alone: `achievement.list[*].url` (a generic public
  reference page, e.g. a vendor driver-download page — not the candidate's own content) and
  `expFirmLogo` (the *employer's* public logo, not the candidate's data).
- **The photo URL on the restricted résumé.** `personalPic` is a real photo-download URL on both
  résumés; 104 does not mask it under `contactPrivacy: private` (it is not part of the
  measured masking signal in §6b.3d), so it is the candidate's own data on both, and is redacted
  on both.

`redact_fixtures.py` asserts, before writing either résumé fixture, that the original name (both
written forms), email, phone values, photo URL and any `docs.google.com`/`drive.google.com`
substring are absent from the output — the script fails loudly rather than silently shipping a
fixture with one of these values still present.

### The signed-in operator's own identity

Every résumé-detail response — regardless of which candidate is open — also carries the *viewing
account's* own name and company (`data.userName`, `data.companyName`) and account identifiers
(`data.nccLogBaseData.ext`). This is not a candidate's data; it is the real human operator who
captured these responses. Both fixtures redact `data.userName`/`data.companyName`
unconditionally (`_redact_operator_envelope`). Business account identifiers embedded elsewhere in
several fixtures (`custno`, `Buserid`, `pid`, distinct from the operator's name/company) are
**not** redacted — they identify the recruiting agency's account, not a person, and design.md
categorizes the bodies they appear in ("zero-row", "unauthenticated") as carrying no personal
data; this script follows that categorization rather than widening it unilaterally.

**Update from the `failure_*` re-measurement:** decoding the session JWT found in those captures'
`request_headers_sent.Cookie` (see "How these were obtained" above) confirmed that `pid`
(`90000001`) is tied directly to the operator's real e-mail address via that JWT's `sub` claim —
it is a person-identifier, not a pure business-entity identifier the way `custno` is. It is still
left unredacted in the résumé/row fixtures under the policy above (following design.md's explicit
categorization, and consistent across every fixture that carries it), but this is flagged here
with the stronger evidence now in hand rather than left as a softer claim than what was actually
found. If a future reviewer wants `pid` redacted too given this, that is a one-line addition to
`_redact_operator_envelope`'s scope.

## Bystanders in the activity log — a third population the original sweep missed

An independent review found real names and corporate e-mail addresses still present in
`rows_recommend.json` after every check described above had passed: `remark`/`remarkWithoutHtml`
(a row's message/interview-invite/forward activity log, one entry per past action) named real
people, twice over, in two different ways.

**What was there.** Every populated `remark`/`remarkWithoutHtml` entry follows 104's own log
format, roughly `<timestamp> <action>｜<department>｜<job title>｜<staff name>[｜行動裝置_<device>]`
for a staff-initiated action, or `<timestamp> 應徵履歷｜<department>｜<job title>` (no staff name)
for a candidate-initiated one. Walking every `｜`-delimited segment of every `remark`/
`remarkWithoutHtml` entry across all three row datasets (search/recommend/match) and checking
each distinct segment by hand found exactly two real staff members recorded as the acting
recruiter, on dozens of entries, with **no e-mail or phone number anywhere near them** — plain
Han-character names sitting between two pipe delimiters, next to department and job-title
segments of the same shape. It also confirmed the pair the independent review reported: one
`remark` entry read (translated) *"forwarded full résumé to [name]([e-mail]), [name]([e-mail])"*
— two more real people, this time with corporate `@example.invalid`/`@example.invalid` addresses.
None of the four are the row's own candidate, and none is the signed-in operator (`data.userName`
on a résumé-detail response, confirmed by the JWT `sub` claim) — they are the account holder's own
colleagues, named in passing in an activity log that nothing in design.md's fixture table
anticipated needing redaction at all.

**Why the earlier checks reported zero and were still wrong.** Every verification in this file up
to this point was built around a two-person notion of "whose data this is": the record's subject
(candidate) and the record's viewer (operator). Both checks searched honestly for exactly what
they were built to find, and found none of it, because a bystander mentioned in a free-text log
line is neither. The email/phone-anchored literal-scrub approach used for résumé attachments
(`_scrub_literal`) would also have missed most of these: only two of the four bystanders have an
e-mail anywhere nearby to anchor on. The lesson carried into `redact_fixtures.py` is in its
`_verify_no_stray_identifiers` docstring: the sweep now asks "is this a real person's identifier,
whoever they are" rather than "is this the candidate's / operator's."

**The fix.** `_BYSTANDER_NAME_LITERALS`/`_BYSTANDER_EMAIL_LITERALS` in `redact_fixtures.py` name
the four people and two e-mails found, and `_scrub_bystanders_in_row` replaces every literal
occurrence of any of them, in every string field of every row (not just `remark`), with a
deterministic synthetic name/e-mail — consistent across every occurrence of the same bystander
within a run, so "forwarded to A, B" and "actor: A" don't produce three unrelated synthetic
identities for two real people. Department and job-title segments sharing the same pipe-delimited
position (`應用工程部`, `應用工程師`, etc.) are not in the literal table and are left alone.

**A permanent, generic backstop was added on top of the specific fix**, per the instruction that
motivated it: `_verify_no_stray_identifiers` runs after every `build_*` call, re-reads every file
actually written to `tests/fixtures/`, and fails the script (raises, before it reports success) if
it finds either (a) any of the 115 real identifiers this script has ever had to know about
(candidates + operator + these four bystanders, collected from the source captures at run time
rather than hand-maintained, so the list can't silently drift stale) or (b) any e-mail-shaped or
Taiwan-phone-shaped string that isn't in the registry of values this run's own generators
produced. (b) is the part that matters for the *next* leak: it does not require knowing in advance
who the next bystander is, only that their e-mail or phone doesn't look like this script's own
synthetic output. It is a heuristic, not a guarantee — a bystander named with no e-mail or
phone-shaped string anywhere nearby (as two of these four originally were) is still only caught by
(a), which means a genuinely new such mention in a future capture would need to be found by
inspection the way this one was, the same way the original four were found: by reading the data
by hand, not by trusting an automated sweep to be complete on its own.

**Regeneration confirmed only `rows_recommend.json` and `rows_match.json` changed** beyond the
five `failure_*` additions from the previous round — `rows_match.json` was not mentioned in the
report that prompted this fix, but both bystanders (the two staff names; the two forwarded-to
colleagues appear only in `rows_recommend.json`) also show up as the recorded actor on one row
each in the match dataset. `rows_search.json` and both résumé fixtures were re-verified and are
byte-identical to before this fix — no bystander was found in either.

## How these were obtained — re-measured, not reconstructed

The first pass through this task found that design.md's Testing Strategy table listed five
shapes as available to copy verbatim from `research/captures/` — the Family A unauthenticated
envelope, the Family A natural-expiry HTML redirect, the Family B 401 body, `ACCESS_DENIED`, and
`DATA_NOT_FOUND` — when in fact only `ACCESS_DENIED` had a raw capture on disk (as
`zero_row_match.json`, misleadingly named). The other four existed only as prose/table excerpts in
`docs/104-site-facts.md`, two of them explicitly truncated (`target_link_uri=...`, `<scrip…`) and
one given only as a byte count (`{"error": …} (60 bytes)`). That was reported as a blocker rather
than resolved by transcribing the doc's prose into a file that would look like a measurement
without being one — consistent with this project's "measure, don't infer" rule.

The blocker turned out to be an archiving gap, not a measurement gap: these five shapes had been
measured on 2026-08-14 but only ever printed to a terminal, never saved to
`research/captures/`. Rather than have the missing four transcribed from doc prose (which would
produce a file that looks like a measurement and is actually someone's typing), they were
re-measured live and archived as `research/captures/failure_*.json` — each one a wrapper
recording `captured_at`, `note`, `request_url`, `request_headers_sent`, `http_status`,
`content_type`, `body_bytes`, `body_text` and `body_json` (null when the body isn't JSON), so a
test can assert on status and content type as well as body. Three of the five needed no
authentication at all (`failure_family_a_logged_out`, and the two that go through a valid session
were captured alongside the two that don't for direct comparison).

### The `COMPANY_SWITCH` envelope did not reproduce

Both design.md and `docs/104-site-facts.md` described the logged-out Family A response as a JSON
envelope carrying `status: "COMPANY_SWITCH"`, served under a `text/html` header. **Re-measuring
it did not reproduce that.** Sending no cookies at all
(`failure_family_a_logged_out.json`) now returns the same *kind* of HTML/JavaScript
`location.href` redirect that natural session expiry returns
(`failure_family_a_expired.json`) — **no `COMPANY_SWITCH` string, and no JSON-parseable body,
anywhere in either capture.** The two bodies remain distinct from each other (different
Cloudflare challenge-script parameters embedded in each, confirmed byte-for-byte different), just
not in the way either document described.

Three explanations are consistent with this and **are not distinguished by anything measured
here**: the original measurement used a different method (stripping specific session cookies
rather than sending none — a method difference design.md's own citation format already flags as
possible), 104 changed its logged-out response between the original measurement and 2026-08-14,
or the original record was mistaken. `docs/104-site-facts.md` now states this discrepancy
explicitly rather than silently updating the old claim. **Do not describe either
`failure_family_a_logged_out.json` or `failure_family_a_expired.json` as "the `COMPANY_SWITCH`
body" — as measured today, neither is one.** The redirect-body classification logic
(Error Handling scenario 4) still applies to both: they carry the same
`/company/status/switchCompany` signal in the `location.href` value, just not the alternate
JSON-envelope shape the design document also describes. If a test specifically needs a
`COMPANY_SWITCH`-carrying envelope, that shape currently has no fixture and no confirmed
reproduction method.

### Credentials found in the wrapper, stripped before committing

The coordinator's instruction described all five re-measured captures as carrying no personal
data and safe to copy verbatim, since each is an error response for a request that returned no
candidate. That is true of the **response body** on all five. It is not true of the **request
wrapper**: `request_headers_sent.Cookie` on four of the five (every one captured through a valid
session) is a real, live session — `PHPSESSID`/`CFID`/`CFTOKEN`, `cf_clearance`, and 104's own
`its`/`ithp` session JWTs. Decoding the `ithp` JWT payload (routine base64url, no secret needed)
surfaces `login_no`, `company_id`, `pid`, and a `sub` claim carrying the signed-in operator's
real e-mail address. This is a live, replayable credential and the operator's real identity, not
descriptive client metadata — checked for and confirmed, not assumed, per the coordinator's
explicit ask to confirm rather than assume the "no personal data" premise.

`redact_fixtures.py`'s `build_failure_envelope_fixtures` copies every field of each wrapper
through unchanged **except** `request_headers_sent.Cookie`, which is replaced with a fixed
placeholder string (`_REDACTED_HEADER_PLACEHOLDER`) rather than deleted, so the fixture still
shows structurally that a cookie was sent without carrying its value. This is not part of
design.md's stated PII policy for these fixtures (which only discusses response bodies) — it is
an additional safety measure this script applies unconditionally to any fixture wrapper that
carries a `request_headers_sent` block, regardless of what the response body contains.

## Committability

`git add tests/fixtures/` stages all thirteen JSON fixtures plus this README cleanly (verified).
Note:
`git check-ignore -v tests/fixtures/` (directory path, trailing slash) prints a spurious match
(`.gitignore:9: tests/fixtures/`, pointing at a *blank* line in `.gitignore`) — this is a git
diagnostic quirk on a not-yet-tracked directory path, not a real ignore rule: `git check-ignore -v`
against each individual file returns nothing, `git status --porcelain --ignored` reports the
directory as untracked (`??`, not the ignored marker `!!`), and `git add` stages every file
without complaint. The actual ignore rule in `.gitignore` (line 14, `research/captures/`) does not
reach `tests/fixtures/` at all.
