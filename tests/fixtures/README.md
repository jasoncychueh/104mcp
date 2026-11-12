# tests/fixtures/ — building your own test data

The JSON files in this directory are **not in version control**. They are 104 responses (or
shapes derived from them), and real 104 responses carry candidates' personal data: names,
schools, employers, free text, contact details. Every machine that runs the tests builds its own
copy; `.gitignore` excludes `tests/fixtures/*.json` so a stray `git add` cannot publish them
(do not use `git add -f` on them).

When a file is missing, the tests that need it fail — they are not skipped — and the failure names
the file and points here. The simplest way to build a file is to take a real response from your own
104 enterprise account and replace everything a person supplied: candidate names, contact details,
schools, employers, free text, photo and attachment URLs, colleagues' names inside `remark`, and,
in the wrapper files below, the `Cookie` header. The values do not have to be real. They only have to
meet the conditions listed per file. The maintainer builds the files with a script that is not
published.

The one file that **is** committed is `describe_result_fields_resume_baseline.json` (last section).

## Two file formats

**Bare body.** The file is the JSON body 104 returned, unchanged in shape:
`rows_*`, `resume_*`, `zero_row_*`, `jobs_apply`, `refreshable_jobs`.

**Wrapper.** The file records a whole HTTP response, so the tests can replay an error that has no
JSON body. All `failure_*` files and `refreshable_jobs_no_permit` use it. The tests read exactly
these keys:

| Key | Type | Meaning |
|---|---|---|
| `http_status` | int | the HTTP status code |
| `content_type` | string | the `Content-Type` header value, exactly as sent |
| `body_text` | string | the response body as text |
| `body_json` | object or `null` | `body_text` parsed as JSON; `null` when the body is not JSON |

Any other key is optional and no test reads it (`request_url`, `request_headers_sent`,
`body_bytes`, `captured_at`, `note`, `derived_unmeasured`, `derived_from`, `derivation`,
`not_measured`). Existing files do not all carry the same optional keys.

## Files

Each section gives the route and request, the response shape, and what the tests depend on. A
condition listed here is one a test asserts or relies on; if your file breaks it, the test goes
red with a message that points back to this file or fails on the missing structure.

### `rows_search.json` — bare body

- Route: `GET vip.104.com.tw/api/search/searchResult`, a keyword search.
- Shape: `{status: "SUCCESS", message, result: {...}, url, data}`. Rows are in `result.data`.
  `result.pageInfo` is `{this_page, total_page, total}`; `result.browseLimit` is
  `{resumeMax: int, onThatDayCount: "<digits as a string>"}`; `result.searchForm` echoes the search
  that was sent.
- Conditions:
  - at least 1 row;
  - `pageInfo.this_page`, `total_page` and `total` are three different numbers (a test checks that
    each one is delivered under its own name);
  - row 1 has an `idNo`, `age` and `sex`;
  - `result.searchForm.kws` is `應用工程師` (the tests search for that keyword and the tool compares
    the echo with what was sent); other `searchForm` keys must be 104's own defaults, and
    `searchForm` has a `jobcat` key;
  - at least one row has a `masterUrl`, and at least one row has `latestMemo: null`;
  - at least one row has an `expJobArr` entry with an empty field (`null`, `""` or `[]`) and at
    least one entry whose `expJobNote` contains HTML;
  - every key on row 1 is one of the fields 104 really sends for résumé rows (the field-description
    tool must document every key it finds there).

### `rows_recommend.json` — bare body

- Route: `GET vip.104.com.tw/api/recommend/resumeListAll?jobno=<jobno>`.
- Shape: like `rows_search.json`; rows in `result.data`, `result.pageInfo` is
  `{this_page, total_page, total}`, `result.browseLimit` as above.
- Conditions: at least 1 row; `pageInfo.this_page`, `total_page` and `total` are three different
  numbers; `expJobArr` entries follow the same two conditions as `rows_search.json` (an empty
  per-job field, and an `expJobNote` with HTML, each somewhere in the file).

### `rows_match.json` — bare body

- Route: `GET vip.104.com.tw/api/match/matchResult?jobno=<jobno>`.
- Shape: different from the other two routes. Rows are in `result.resumes` (there is **no**
  `result.data`); `result.pageInfo` is `{total, start, end, pageRow, total_page, thisPage, prePage,
  nextPage}` (there is **no** `this_page`); `result.browseLimit` as above.
- Conditions: at least 1 row; row 1 has an `idNo` of at least 12 digits; `expJobArr` entries follow
  the same two conditions as `rows_search.json`.

### `rows_applicants.json` — bare body

- Route: `GET auth.vip.104.com.tw/api/apply/applyDetail?jobno=<jobno>`, the applicants of one job
  over the last 60 days.
- Shape: `{status: "SUCCESS", ..., result: {...}}`. `result` has `jobNo` (int or string), `jobName`,
  `jobSwitch`, `jobRole`, `resumeGroups: {today: [...rows], others: [{inputDate, resumes: [...rows]}]}`,
  `pageInfo`, and `resumeNumber: {match, notMatch, isBlack, recycle}` (all strings of digits).
  `nccLogBaseData` (the operator's account) may be any placeholder.
- Conditions:
  - at least 1 applicant row; every row has `idNo` and `pId`;
  - `result.jobNo` is the job number the applicants belong to (tests call `list_applicants` with
    it, and the tool rejects a response whose `jobNo` differs from the request);
  - `resumeNumber.notMatch`, `isBlack` and `recycle` are all `"0"`;
  - `pageInfo` is `{total, start, end, pageRow, total_page: 1, thisPage: 1, prePage, nextPage}`;
  - at least one entry of `resumeGroups.others` holds 2 or more applicants;
  - `resumeGroups.today` is an empty list (no test needs a populated one).

### `zero_row_applicants.json` — bare body

- Route: same as `rows_applicants.json`, for a job with no applicants.
- Shape: same envelope; `resumeGroups` is `{today: [], others: []}`.
- Conditions: `result.pageInfo` is `null`; both groups empty; `result.jobNo` present;
  `resumeNumber` has the four keys.

### `jobs_apply.json` — bare body

- Route: `GET vip.104.com.tw/job-api/jobs/apply?page=1`, the company's job list.
- Shape: `{data: [jobs], metadata: {total, page, hasSubDepartment}}`. Each job is `{jobno, job, switch,
  role, applyCount, applyCount60, department: {id, code, name}}`.
- Conditions:
  - between 21 and 40 jobs (the tool looks counts up 20 jobs per request, and tests expect exactly
    two such requests);
  - `jobno` values are digit strings, unique;
  - `applyCount60` is an int on every job; at least one job carries an `applyCount` key.

### `refreshable_jobs.json` — bare body

- Route: `GET vip.104.com.tw/vipapi/job/over-seven-day-job`.
- Shape: `{data: {jobs: [{jobNo, jobName, appearDate}], userPermit}, metadata}`.
- Conditions: `data.userPermit` is `true`; every job has `jobNo`, `jobName` and `appearDate`.

### `refreshable_jobs_no_permit.json` — wrapper

- A variant of `refreshable_jobs.json` for an account that may not refresh jobs.
- Conditions: `http_status` 200, `content_type` `application/json`; `body_json` is the body of
  `refreshable_jobs.json` with `data.userPermit` set to `false`; `body_text` is `body_json`
  serialised.

### `resume_unrestricted.json` — bare body

- Route: `GET auth.vip.104.com.tw/vipapi/resume/search/<idNo>`, a résumé whose contact details are
  public.
- Shape: `{data: {resume: {...}, ...}, metadata}`. The candidate is in `data.resume`; `data` also has
  operator-side siblings such as `userName` and `companyName` (the signed-in operator, not the
  candidate).
- Conditions:
  - `data.resume.contactPrivacy` is `"public"`; `data.resume` has `idNo` and `userName`;
  - `data.resume.auto` is an HTML string (the self-introduction);
  - `data.resume.callTimeDesc` is a non-empty string (the restricted file's must be longer);
  - nested values go to arbitrary depth (`achievement.list`, `customResume`, `expJobArr`, ...);
    the tests check that every key is converted at every depth, so use a résumé with several
    levels.

### `resume_restricted.json` — bare body

- Route: same as `resume_unrestricted.json`, for a candidate who hides contact details.
- Shape: same.
- Conditions:
  - `data.resume.contactPrivacy` is `"private"`; `data.resume` has `idNo`;
  - `email`, `phoneDescAll`, `callTimeDesc` and `addressDesc` are 104's human-readable placeholder
    texts, not real contact data; `email` contains no `@`;
  - `callTimeDesc` is **longer** than `callTimeDesc` in `resume_unrestricted.json`;
  - `phone` is an object whose fields are empty.

### `zero_row_search.json` — bare body

- Route: same as `rows_search.json`, for a search that matches nobody.
- Conditions: `status` `"SUCCESS"`; `result.data` empty or null; `result.searchForm.kws` is
  `zzqxvkwj9871nonexistentkeyword`; `result.searchForm.sex` is the number `2` (104's "no filter"
  default); `result.searchForm` has the same keys as `rows_search.json`.

### `zero_row_recommend.json` — bare body

- Route: same as `rows_recommend.json`, for a job with no recommendations.
- Conditions: `status` `"SUCCESS"`; `result.data` empty; `result.pageInfo` is `{this_page, total_page, total}`
  with `total` 0.

### `failure_family_a_logged_out.json` — wrapper

- Route: `GET vip.104.com.tw/api/search/getSearchRsNum?kws=...` with no login cookie (any family-A
  search route behaves the same).
- Conditions: `http_status` 200; `content_type` starts with `text/html`; `body_json` is `null`;
  `body_text` is an HTML/JavaScript redirect page that contains the string
  `/company/status/switchCompany`.

### `failure_family_a_expired.json` — wrapper

- Route: same request with an expired login cookie.
- Conditions: same as `failure_family_a_logged_out.json` (HTML, `body_json` `null`, contains
  `/company/status/switchCompany`). The two files do not have to be byte-identical.

### `failure_applicants_company_switch.json` — wrapper

- Route: `GET auth.vip.104.com.tw/api/apply/applyDetail` with no cookie.
- Conditions: `http_status` 200; `content_type` starts with `text/html`; `body_json.status` is
  `"COMPANY_SWITCH"`; `body_text` contains `COMPANY_SWITCH` and the **JSON-escaped** form of the
  redirect target: the characters `\/company\/status` (backslash before each slash); the unescaped
  redirect target is `/company/status/switchCompany`.

### `failure_applicants_not_found.json` — wrapper

- Route: `GET auth.vip.104.com.tw/api/apply/applyDetail?jobno=<a job that does not exist>`.
- Conditions: `http_status` 404; JSON content type; `body_json.status` `"DATA_NOT_FOUND"`;
  `body_json.message` is `此職務不存在`.

### `failure_data_not_found.json` — wrapper

- Route: `GET vip.104.com.tw/api/match/matchResult?jobno=<a closed job>`.
- Conditions: `http_status` 404; JSON content type; `body_json.status` `"DATA_NOT_FOUND"`; the
  message contains `此職務已關閉`.

### `failure_access_denied.json` — wrapper

- Route: `GET vip.104.com.tw/api/match/matchResult?jobno=<jobno>` rejected by 104 with `ACCESS_DENIED`
  (message `invalid referer`).
- Conditions: `http_status` 200; JSON content type; `body_json.status` `"ACCESS_DENIED"`.

### `failure_family_b_unauthenticated.json` — wrapper

- Route: `GET auth.vip.104.com.tw/job-api/jobs/recommend?page=1` called without a login cookie (any
  `auth.vip.104.com.tw` route behaves the same).
- Conditions: `http_status` 401; JSON content type; `body_json` is an object with an `error` key and
  no `status` key.

### `failure_send_validation.json` — wrapper

- Route: `POST auth.vip.104.com.tw/bc-comm/message/<jobno>-<pId>` with a body that has no `content`.
- Conditions: `http_status` 400; JSON content type; `body_json` is
  `{code, message, detail}` where `detail` is an object whose `content` entry is a list of messages
  containing the word `required` (any letter case).

## `describe_result_fields_resume_baseline.json` — committed

This is **not** a 104 response. It is a snapshot of what `describe_result_fields("resume")` returns:
this package's own field-description text. It holds no candidate data, so it is committed. It also
cannot be "built locally": generating it from the current code would make the test compare the code
with itself. The test checks that the résumé descriptions do not change; when you change them on
purpose, regenerate this file in the same commit:

```
python -c "import json; from mcp104.tools.discovery import _describe_result_fields as d; f = open('tests/fixtures/describe_result_fields_resume_baseline.json', 'w', encoding='utf-8', newline='\n'); f.write(json.dumps(d('resume'), sort_keys=True, ensure_ascii=False, indent=2) + '\n'); f.close()"
```

(Check the output with `git diff` first: the diff is the change you are announcing.)
