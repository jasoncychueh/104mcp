"""Tests for the job-edit write tools update_job and create_job.

Cases in this file:
  T-1 .. T-46, T-61 .. T-71
T-47 .. T-55, T-60 and T-72 are in tests/test_job_form.py; T-56 .. T-58 are in
tests/test_api_client.py; T-59 is in tests/test_jobs.py.

Written against the public tool signatures and the measured shapes in docs/104-site-facts.md:
§8.31 (jobList and
jobUpdate key sets), §8.32 (job/more: render and permit), §8.34 (jobInsert?act=copy: the three
differences from jobUpdate). Both tools are reached through register_job_edit_tools. Every 104
request goes through the real guard, the real classifier, the real throttle gate and the real
job_form core. Only the transport (fetch) is replaced, by the scripted _SeqFetchSpy from
tests/conftest.py.

Synthetic data and unmeasured responses (stated here, not inferred from any fixture):
- The jobList, jobUpdate, job/more and jobInsert bodies follow the key sets measured in §8.31,
  §8.32 and §8.34. Every value is synthetic. No body comes from tests/fixtures/.
- The forbidden-words check, the updateJob and newJob responses, their error envelopes and the
  button permit messages are synthesised from the front-end code shape. Nothing about them has
  been measured, so the assertions hold only as far as the tool contract specifies them.
- Contact values carry the SYNTHETIC-CONTACT- prefix; the one CJK contact name is 合成甲乙丙.
- Request bodies: the form body is read as a (key, value) pair sequence. The forbidden-words body
  is read as a JSON object.
- Salary type ids: 10 (面議) is named by the tool contract; 50 (月薪) comes from the front-end code table
  (JE@115016). The monthly minimum is covered by a 50 read with salary_low below the monthly
  minimum, and the 面議 floor (40000) is covered separately.
"""

import importlib
import json
import logging
from functools import lru_cache
from uuid import uuid4

import pytest
import pytest_asyncio

from mcp104.browser.api_client import RawResponse
from mcp104.browser.session import SessionInfo, SessionPool
from mcp104.config import get_config
from mcp104.db.database import Database
from mcp104.tools import categories
from mcp104.tools.helpers import get_session_id
from tests.conftest import _SeqFetchSpy


# ── Synthetic values ───────────────────────────────────────────────────────────

J1 = "1000001"
J2 = "1000002"
NEW_JOBNO = "1000009"
SECOND_NEW_JOBNO = "1000010"
JOBNO_BAD = "12a"

BASE_NAME = "SYNTH-JOB-NAME-1000001"
NEW_NAME = "SYNTH-NEW-NAME-7731"
BASE_DESC = "SYNTH-DESC-BASE-7731"
NEW_DESC = "SYNTH-DESC-NEW-7731"
ADDRESS = "SYNTH-ADDRESS-7731"
LOAD_TIME = "2026-11-13 10:00:07"
LOAD_A = "2026-11-13 10:00:01"
LOAD_B = "2026-11-13 10:05:09"
REG_ID = "SYNTH-REGID-7731"
OPTION_LABEL = "SYNTH-OPTION-LABEL-7731"
RIVAL = "SYNTH-RIVAL-WORD"
CLOSE_DATE = "2026-12-01 00:00:00"
DENIED = "SYNTH-DENIED-PERMIT"
BUTTON_REASON = "SYNTH-BUTTON-REASON"
JOB_SORT_EMPTY = '{"data":[{"reserve":[]}]}'

CONTACT = "SYNTHETIC-CONTACT-"
CONTACT_NAME = "SYNTHETIC-CONTACT-NAME"
CONTACT_NAME_B = "SYNTHETIC-CONTACT-NAME-B"
CONTACT_PHONE = "SYNTHETIC-CONTACT-PHONE"
CONTACT_NOTE = "SYNTHETIC-CONTACT-NOTE"
CONTACT_CJK = "合成甲乙丙"
PLACEHOLDER = "［聯絡資料］"

CHALLENGE_TEXT = "vip.104.com.tw 正在執行安全驗證\n此網站使用安全服務抵禦惡意機器人。"
SESSION_COOKIES = [
    {"name": "its", "value": "SYNTH-COOKIE-ITS-VALUE", "domain": "vip.104.com.tw", "path": "/"},
    {"name": "ithp", "value": "SYNTH-COOKIE-ITHP-VALUE", "domain": "auth.vip.104.com.tw", "path": "/"},
]
FORBIDDEN_SOURCE_WORDS = ("帳號持有人", "使用者說", "持有人說")

# Routes, by (method, path). Keys are checked by route, never by a total request count alone.
JOB_LIST = ("GET", "/api/job/jobList")
JOB_UPDATE = ("GET", "/api/job/jobUpdate")
JOB_MORE = ("GET", "/api/job/more")
JOB_INSERT = ("GET", "/api/job/jobInsert")
FORBIDDEN = ("POST", "/job-api/forbidden-words/check")
UPDATE = ("POST", "/api/job/updateJob")
NEW = ("POST", "/api/job/newJob")
WRITES = (UPDATE, NEW)

LIST_PERMIT_KEYS = ("job__1", "job__2", "job__3", "job__5", "job__10", "job__11",
                    "job__18", "job__19", "job__23", "job__27")
OPTION_KEYS = ("role", "salaryType", "workType", "manage", "businessTrip", "businessTripPeriod",
               "workInterval", "workShift", "holiday", "onBoard", "identity", "disableRole",
               "disableLevel", "workExperience", "education", "language", "languageLevel",
               "dialect", "dialectLevel", "driver", "applyType", "replyDate", "matchEmail")

TOOLS = ["update_job", "create_job"]


# ── Dataset leaves (the packaged category data, read through the public categories API) ───

@lru_cache(maxsize=None)
def _dataset(filename, condition_key):
    return categories.load_dataset(filename, condition_key)


def _jobcat():
    return _dataset("JobCat.json", "jobcat")


def _major():
    return _dataset("Major.json", "major")


def _leaves(dataset, limit):
    """The first `limit` leaf nodes of a dataset tree, as (code, name)."""
    found = []

    def walk(code):
        for node in categories.children(dataset, code):
            if len(found) >= limit:
                return
            fields = node._asdict()
            if fields["has_children"]:
                walk(fields["code"])
            else:
                found.append((fields["code"], fields["name"]))

    walk(None)
    return found


def _first_branch(dataset):
    for node in categories.children(dataset, None):
        fields = node._asdict()
        if fields["has_children"]:
            return fields["code"]
    raise AssertionError("the dataset has no branch node at the top level")


def _name_of(dataset, code):
    return categories.path(dataset, code)[-1].name


JOBCAT_LEAF, JOBCAT_LEAF2 = [code for code, _name in _leaves(_jobcat(), 2)]
JOBCAT_LEAF_NAME = _name_of(_jobcat(), JOBCAT_LEAF)
JOBCAT_LEAF2_NAME = _name_of(_jobcat(), JOBCAT_LEAF2)
MAJOR_LEAF, MAJOR_LEAF2 = [code for code, _name in _leaves(_major(), 2)]
MAJOR_LEAF2_NAME = _name_of(_major(), MAJOR_LEAF2)
JOBCAT_BRANCH = _first_branch(_jobcat())


# ── Response builders (wire shapes) ────────────────────────────────────────────

def _raw(status, content_type, body, parsed_json):
    return RawResponse(status=status, location=None, content_type=content_type, body=body,
                       body_bytes=body.encode("utf-8"), parsed_json=parsed_json)


def _json(status, obj):
    return _raw(status, "application/json; charset=utf-8", json.dumps(obj, ensure_ascii=False), obj)


def _envelope(result, *, status="SUCCESS", message="", code=None, error_field_map=None):
    obj = {"status": status, "message": message, "result": result, "url": "", "data": None}
    if code is not None:
        obj["code"] = code
    if error_field_map is not None:
        obj["errorFieldMap"] = error_field_map
    return _json(200, obj)


def _expired():
    return _json(200, {"status": "COMPANY_SWITCH", "message": "SYNTH-EXPIRED",
                       "url": "https://vip.104.com.tw/company/status/switchCompany"})


def _forbidden():
    return _json(403, {"status": "ACCESS_DENIED", "message": "SYNTH-403"})


def _challenge():
    return _raw(200, "text/html; charset=utf-8", CHALLENGE_TEXT, None)


def _non_json():
    return _raw(200, "text/html; charset=utf-8", "<html>SYNTH-NON-JSON</html>", None)


def _button(link, *, permit=True, message="", switch=None):
    return {"linkName": link, "permit": permit, "url": "", "isAjax": False, "isNewTab": False,
            "message": message, "switch": switch}


def _permit_map(overrides=None):
    permit = {key: {"result": True, "message": ""} for key in LIST_PERMIT_KEYS}
    permit.update(overrides or {})
    return permit


def _list_row(jobno, *, job=None, switch="on", in_verify=0, autoclose="0", closedate=None, d5=""):
    if job is None:
        job = BASE_NAME if jobno == J1 else f"SYNTH-JOB-{jobno}"
    return {
        "custno": "SYNTH-CUST", "jobno": jobno, "job": job, "appear_date": "2026-11-01 09:00:00",
        "switch": switch, "noemail": "0", "inputdate": "2026-10-01 09:00:00", "role": "1",
        "job_setting": f"SYNTH-SETTING-{jobno}", "top_job_setting": "0",
        "modifydate": "2026-11-01 09:00:00", "autoclose": autoclose, "closedate": closedate,
        "d5": d5, "jobcat1": "SYNTH-CAT", "jobcat2": "", "jobcat3": "", "isInVerify": in_verify,
        "clickCnt": 0, "department": {"id": "2001", "name": "SYNTH-DEPT-NAME", "code": "SYNTH-DEPT-CODE"},
        "canDelete": False,
    }


def _list_result(rows, *, total_page=1, is_web=False):
    return {
        "jobDataAry": list(rows),
        "pageInfo": {"curPage": "1", "total_page": total_page, "total": len(rows)},
        "permit": _permit_map(),
        "isWeb": is_web,
        "hotOrder": [], "selectJob": [], "priority": [], "jobSort": [],
        "isHunter": False, "isDispatch": False, "isNewModel": False, "poNum": 0, "coStatus": "on",
    }


def _list_ok(rows, **kwargs):
    return _envelope(_list_result(rows, **kwargs))


def _more_result(*, permit=None, render=None):
    if permit is None:
        permit = {"job__4": {"result": True, "message": ""}, "job__6": {"result": True, "message": ""}}
    if render is None:
        render = {"jobNo": J1, "switch": "on", "jobOpenDay": "", "role": "1", "isNewModel": False,
                  "poNum": 0}
    return {"render": render, "permit": permit}


def _more_ok(**kwargs):
    return _envelope(_more_result(**kwargs))


_DROP = object()


def _set(obj, dotted, value):
    keys = dotted.split(".")
    current = obj
    for key in keys[:-1]:
        current = current[key]
    if value is _DROP:
        del current[keys[-1]]
    else:
        current[keys[-1]] = value


def _base_read(*, insert=False):
    """A synthetic read of an open, ordinary job in wire (camelCase) keys. The jobUpdate shape
    carries openDate and additionalInfo.regId; the jobInsert shape (§8.34) drops both, sends
    switch null and the jobInsert/jobPreview buttons with switch null."""
    jobcat_code, jobcat_name = _leaves(_jobcat(), 1)[0]
    major_code, major_name = _leaves(_major(), 1)[0]
    read = {
        "jobName": BASE_NAME,
        "switch": "on",
        "appearDate": "2026-11-01 09:00:00",
        "canDelete": False,
        "isHunter": False,
        "isDispatch": False,
        "isNewModel": False,
        "isTheNetwork": False,
        "isGeneral": False,
        "shareLink": [
            {"linkName": "copy", "linkUrl": "https://www.104.com.tw/job/SYNTH-COPY"},
            {"linkName": "qrcode", "linkUrl": "https://www.104.com.tw/job/SYNTH-QR"},
            {"linkName": "fb", "linkUrl": "https://www.104.com.tw/job/SYNTH-FB"},
        ],
        "detail": {
            "roleId": 1,
            "needEmp": {"low": 3, "high": 5},
            "jobcat": [{"no": jobcat_code, "des": jobcat_name}],
            "description": BASE_DESC,
            "salary": {"salaryTypeId": 20, "salaryLow": 40000, "salaryHigh": 60000, "salaryOld": ""},
            "workType": [],
            "jobAddress": {
                "addrNo": {"no": "6001001000", "des": "SYNTH-CITY"},
                "address": ADDRESS,
                "mapAddress": "SYNTH-MAP-7731",
                "latitude": 25.0331,
                "longitude": 121.5654,
            },
            "industoryArea": None,
            "manage": 0,
            "businessTrip": {"types": [], "period": None},
            "workTime": {"interval": [{"type": 1, "periods": [
                {"startHour": 9, "startMinute": 0, "endHour": 18, "endMinute": 0}]}]},
            "holiday": "SYNTH-HOLIDAY",
            "onboardDate": 3,
            "remoteWork": {"option": 0, "description": ""},
            "shifts": [],
            "department": None,
            "negotiableSalary": {"type": 1, "salaryLow": 45000, "salaryHigh": 60000},
        },
        "needCondition": {
            "disables": {"disable": [], "proof": False},
            "identities": [],
            "experience": 0,
            "education": [8],
            "majors": [{"no": major_code, "des": major_name}],
            "language": [],
            "tools": [],
            "skills": [],
            "licenses": [],
            "drivers": [],
            "otherCondition": "SYNTH-OTHER-CONDITION-7731",
        },
        "contaction": {
            "contacter": CONTACT_NAME,
            "emails": [CONTACT + "MAIL@example.invalid"],
            "usingTypes": [1],
            "contactInfo": {
                "applyAddrNo": None,
                "applyAddr": CONTACT + "ADDR",
                "ext": CONTACT + "EXT",
                "phoneArea": CONTACT + "AREA",
                "phone": " " + CONTACT_PHONE + " ",
                "cellphone": CONTACT + "CELL",
                "note": CONTACT_NOTE,
            },
        },
        "otherSetting": {"closeDate": "", "reply": {"noReply": False, "days": 3}, "subscript": 1},
        "additionalInfo": {
            "loadTime": LOAD_TIME,
            "poEndDate": "",
            "minimumSalary": {"monthly": 30000, "hourly": 200},
            "competitors": RIVAL + ",SYNTH-OTHER-RIVAL",
            "competitorsEmail": {"blackList": [], "whiteList": []},
            "ordinanceKeywords": {},
            "operations": {"buttons": [_button("jobUpdate"), _button("jobPreview")], "links": []},
            "options": {key: {"1": OPTION_LABEL} for key in OPTION_KEYS},
        },
        "department": {"id": "2001", "code": "SYNTH-DEPT-CODE", "name": "SYNTH-DEPT-NAME"},
    }
    if insert:
        read["switch"] = None
        read["additionalInfo"]["operations"]["buttons"] = [_button("jobInsert"), _button("jobPreview")]
    else:
        read["openDate"] = "2026-10-01 09:00:00"
        read["additionalInfo"]["regId"] = REG_ID
    return read


def _read(*, insert=False, wire=None):
    read = _base_read(insert=insert)
    for dotted, value in (wire or {}).items():
        _set(read, dotted, value)
    return read


def _read_ok(read, *, insert=False):
    return _envelope(read, message="" if insert else J1)


# Published (get_job_detail) path -> wire path, for the paths the tests change and re-read.
PUB2WIRE = {
    "job_name": "jobName",
    "detail.description": "detail.description",
    "detail.need_emp.low": "detail.needEmp.low",
    "detail.need_emp.high": "detail.needEmp.high",
    "detail.salary.salary_type_id": "detail.salary.salaryTypeId",
    "detail.salary.salary_low": "detail.salary.salaryLow",
    "detail.salary.salary_high": "detail.salary.salaryHigh",
    "detail.negotiable_salary.type": "detail.negotiableSalary.type",
    "detail.negotiable_salary.salary_low": "detail.negotiableSalary.salaryLow",
    "detail.negotiable_salary.salary_high": "detail.negotiableSalary.salaryHigh",
    "detail.jobcat": "detail.jobcat",
    "need_condition.majors": "needCondition.majors",
    "detail.remote_work.option": "detail.remoteWork.option",
    "detail.remote_work.description": "detail.remoteWork.description",
    "need_condition.education": "needCondition.education",
}


def _reread(changes=None, *, load_time=None, reg_id=None, extra=None):
    """What a re-read returns after the given published changes were saved: the synthetic read
    with the changes applied in wire form. A jobcat or majors element re-reads with its own
    (different) name, which the form comparison must ignore."""
    read = _base_read()
    for path, value in (changes or {}).items():
        if path in ("detail.jobcat", "need_condition.majors"):
            value = [{"no": element["no"], "des": "SYNTH-RE-READ-NAME"} for element in value]
        _set(read, PUB2WIRE[path], value)
    if load_time is not None:
        read["additionalInfo"]["loadTime"] = load_time
    if reg_id is not None:
        read["additionalInfo"]["regId"] = reg_id
    for dotted, value in (extra or {}).items():
        _set(read, dotted, value)
    return read


def _fb_clean():
    return _json(200, {"data": [""] * 7, "metadata": {}})


def _fb_warn(texts=None):
    return _json(200, {"data": texts or ["SYNTH-WARN-HIT", "", "", "", "", "", ""], "metadata": {}})


def _fb_block(texts=None):
    return _json(400, {"data": texts or ["SYNTH-BLOCK-HIT", "", "", "", "", "", ""], "metadata": {}})


def _write_ok():
    return _envelope({})


def _new_ok(jobno=NEW_JOBNO):
    return _envelope({"jobno": jobno})


def _failed(message, error_field_map=None):
    return _envelope({}, status="FAILED", message=message, error_field_map=error_field_map)


# ── Fixtures and the fake transport plumbing (mirrors tests/test_job_switch.py) ─────────────

class _FakeSessionObj:
    pass


class _FakeRequestContext:
    def __init__(self, lifespan_context):
        self.lifespan_context = lifespan_context


class _FakeApp:
    def __init__(self, session_pool, database):
        self.session_pool = session_pool
        self.config = get_config()
        self.db = database


class _FakeCtx:
    def __init__(self, session_pool, database):
        self.session = _FakeSessionObj()
        self.request_context = _FakeRequestContext(_FakeApp(session_pool, database))


_OPENED_DATABASES: list[Database] = []


async def _new_session(tmp_path):
    pool = SessionPool()
    database = Database(str(tmp_path / f"db_{uuid4().hex}.sqlite"))
    await database.init()
    _OPENED_DATABASES.append(database)
    ctx = _FakeCtx(pool, database)
    info = SessionInfo(cookies=list(SESSION_COOKIES), account_label="test@104.com")
    pool.activate_direct(get_session_id(ctx), info)
    return ctx, info


@pytest_asyncio.fixture(autouse=True)
async def _close_opened_databases():
    yield
    while _OPENED_DATABASES:
        await _OPENED_DATABASES.pop().close()


@pytest.fixture(autouse=True)
def deterministic_throttle(monkeypatch):
    """The pacing floor's inline sleep is neutralised; pacing is tests/test_throttle.py's job."""
    async def instant_sleep(seconds):
        return None
    monkeypatch.setattr("mcp104.browser.throttle._sleep", instant_sleep)


class _FakeMCP:
    """Records each tool under its name and its effective description."""

    def __init__(self):
        self.tools: dict[str, object] = {}
        self.descriptions: dict[str, str] = {}

    def tool(self, *args, **kwargs):
        def decorator(fn):
            name = kwargs.get("name", fn.__name__)
            self.tools[name] = fn
            self.descriptions[name] = kwargs.get("description") or fn.__doc__ or ""
            return fn
        return decorator


def _je():
    return importlib.import_module("mcp104.tools.job_edit")


def _tool(name):
    mcp = _FakeMCP()
    _je().register_job_edit_tools(mcp)
    return mcp.tools[name]


def _install(monkeypatch, scripted):
    spy = _SeqFetchSpy(list(scripted))
    monkeypatch.setattr("mcp104.browser.api_client.fetch", spy)
    monkeypatch.setattr("mcp104.tools.helpers.fetch", spy, raising=False)
    return spy


async def _run(tmp_path, monkeypatch, tool_name, script, **kwargs):
    ctx, info = await _new_session(tmp_path)
    spy = _install(monkeypatch, script)
    result = await _tool(tool_name)(ctx=ctx, **kwargs)
    return result, spy, info


def _jobs_tool(name):
    mcp = _FakeMCP()
    importlib.import_module("mcp104.tools.jobs").register_job_tools(mcp)
    return mcp.tools[name]


async def _run_with_slots_left(tmp_path, monkeypatch, tool_name, script, *, slots_left, **kwargs):
    # The cap is the largest full cost of a confirmed write under test (six, for create_job), so no
    # write is refused for exceeding the cap itself. list_refreshable_jobs sends one request, so each
    # call spends exactly one slot. Spending cap - slots_left of them leaves exactly `slots_left`
    # requests in the hour.
    cap = 6
    spent = cap - slots_left
    monkeypatch.setenv("MCP104_MAX_REQUESTS_PER_HOUR", str(cap))
    ctx, info = await _new_session(tmp_path)
    spend = _install(monkeypatch, [_json(200, {}) for _ in range(spent)])
    for _ in range(spent):
        await _jobs_tool("list_refreshable_jobs")(ctx=ctx)
    assert len(_routes(spend)) == spent
    spy = _install(monkeypatch, script)
    result = await _tool(tool_name)(ctx=ctx, **kwargs)
    return result, spy, info


# ── Request and result inspection ──────────────────────────────────────────────

def _routes(spy):
    return [(endpoint.method, endpoint.path) for endpoint, _params, _body in spy.calls]


def _calls(spy, route):
    return [(params, body) for endpoint, params, body in spy.calls
            if (endpoint.method, endpoint.path) == route]


def _pairs(value):
    if value is None:
        return []
    items = value.items() if isinstance(value, dict) else value
    return [(str(k), str(v)) for k, v in items]


def _params(spy, route):
    return [_pairs(params) for params, _body in _calls(spy, route)]


def _body(spy, route):
    bodies = [body for _params, body in _calls(spy, route)]
    assert bodies, f"no request was sent to {route}"
    return bodies[-1]


def _json_body(spy, route):
    body = _body(spy, route)
    return json.loads(body) if isinstance(body, str) else body


def _form(body):
    assert isinstance(body, (list, tuple)), "the update body must be a (key, value) pair sequence"
    out: dict[str, list[str]] = {}
    for key, value in body:
        out.setdefault(key, []).append(str(value))
    return out


def _no_write(spy):
    return not any(route in WRITES for route in _routes(spy))


def _is_error(result):
    return "error" in result


def _dump(obj):
    return json.dumps(obj, ensure_ascii=False, default=str)


def _warnings_text(result):
    return " ".join(result.get("warnings", []))


def _rows_by_field(changes):
    return {row["field"]: row for row in changes}


ALLOWED_SHAPES = [
    frozenset({"preview", "warnings"}),
    frozenset({"jobno", "saved", "changes", "mismatched_fields", "message", "warnings"}),
    frozenset({"source_jobno", "created", "new_jobno", "new_job_switch", "candidates", "message", "warnings"}),
    frozenset({"error"}),
    frozenset({"error", "retry_after_seconds"}),
    frozenset({"error", "forbidden_words"}),
    frozenset({"error", "failed_fields"}),
    frozenset({"error", "editable_fields"}),
]


def _assert_closed_shape(result):
    assert frozenset(result) in ALLOWED_SHAPES, sorted(result)
    if "saved" in result:
        assert result["saved"] in {"confirmed", "mismatch", "unconfirmed"}
    if "created" in result:
        assert result["created"] in {"confirmed", "unconfirmed"}


# ── Parameter tables ───────────────────────────────────────────────────────────

INVALID_CHANGES = [
    pytest.param("job_name", "", id="job_name-empty"),
    pytest.param("job_name", "x" * 121, id="job_name-121"),
    pytest.param("detail.description", "x" * 4001, id="description-4001"),
    pytest.param("detail.need_emp.low", 100, id="need_emp-100"),
    pytest.param("detail.need_emp.low", -2, id="need_emp--2"),
    pytest.param("detail.need_emp.low", True, id="need_emp-bool"),
    pytest.param("detail.salary.salary_type_id", 15, id="salary_type-15"),
    pytest.param("detail.jobcat", [{"no": "ABC"}], id="jobcat-non-numeric"),
    pytest.param("detail.jobcat", [{"no": code} for code, _n in _leaves(_jobcat(), 4)],
                 id="jobcat-four"),
    pytest.param("detail.jobcat", [{"no": JOBCAT_LEAF, "des": "SYNTH-EXTRA-DES"}],
                 id="jobcat-element-has-des"),
    pytest.param("detail.jobcat", [{"no": "9999999999"}], id="jobcat-not-in-dataset"),
    pytest.param("detail.jobcat", [{"no": JOBCAT_BRANCH}], id="jobcat-branch-node"),
    pytest.param("detail.shifts", [4, 5], id="shifts-4-and-5"),
    pytest.param("need_condition.education", [], id="education-empty"),
    pytest.param("need_condition.majors", [{"no": code} for code, _n in _leaves(_major(), 4)],
                 id="majors-four"),
    pytest.param("need_condition.majors", [{"no": "9999999999"}], id="majors-not-in-dataset"),
    pytest.param("need_condition.other_condition", "x" * 3001, id="other-condition-3001"),
]

READ_ONLY_KEYS = [
    "switch", "detail.role_id", "detail.job_address.address", "department.id",
    "other_setting.close_date", "detail.salary.salary_old", "need_condition.skills",
    "contaction.contacter",
]


def _extra_top(read):
    read["synthExtra"] = "SYNTH-EXTRA-VALUE"


def _extra_detail(read):
    read["detail"]["synthExtra"] = "SYNTH-EXTRA-VALUE"


def _extra_jobcat(read):
    read["detail"]["jobcat"][0]["synthExtra"] = "SYNTH-EXTRA-VALUE"


def _extra_contact_info(read):
    read["contaction"]["contactInfo"]["synthExtra"] = "SYNTH-EXTRA-VALUE"


EXTRA_KEY_LOCATIONS = [
    pytest.param(_extra_top, id="top"),
    pytest.param(_extra_detail, id="detail"),
    pytest.param(_extra_jobcat, id="jobcat-element"),
    pytest.param(_extra_contact_info, id="contact-info"),
]


def _str_low(read):
    read["detail"]["needEmp"]["low"] = "3"


def _using_not_list(read):
    read["contaction"]["usingTypes"] = "1"


def _language_string(read):
    read["needCondition"]["language"] = "x"


def _disables_four(read):
    read["needCondition"]["disables"]["disable"] = [{"type": 1, "level": 1}] * 4


TYPE_MISMATCHES = [
    pytest.param(_str_low, "need_emp", id="need_emp-low-string"),
    pytest.param(_using_not_list, "using_types", id="using-types-not-list"),
    pytest.param(_language_string, "language", id="language-string"),
    pytest.param(_disables_four, "disable", id="disables-four"),
]

ACCOUNT_MODE_WIRE = [
    pytest.param({"isHunter": True}, id="is-hunter"),
    pytest.param({"additionalInfo.options.postJob": {"1": "SYNTH-OPTION"}}, id="post-job-option"),
    pytest.param({"detail.hiringType": {"1": "SYNTH-OPTION"}}, id="hiring-type"),
    pytest.param({"otherSetting.dueDateCount": 3}, id="due-date-count"),
    pytest.param({"detail.jobAddress": []}, id="job-address-empty"),
]


def _is_web(res):
    res["isWeb"] = True


def _no_permit(res):
    del res["permit"]


def _job5_denied(res):
    res["permit"]["job__5"] = {"result": False, "message": DENIED}


def _job5_not_bool(res):
    res["permit"]["job__5"] = {"result": "yes", "message": ""}


def _permit_not_object(res):
    res["permit"] = "SYNTH-NOT-OBJECT"


def _in_verify(res):
    res["jobDataAry"][0]["isInVerify"] = 1


def _switch_new(res):
    res["jobDataAry"][0]["switch"] = "new"


def _no_update_button(read):
    read["additionalInfo"]["operations"]["buttons"] = [_button("jobPreview")]


def _update_button_denied(read):
    read["additionalInfo"]["operations"]["buttons"][0] = _button("jobUpdate", permit=False,
                                                                 message=BUTTON_REASON)


def _update_button_denied_no_message(read):
    read["additionalInfo"]["operations"]["buttons"][0] = _button("jobUpdate", permit=False)


UPDATE_GATES = [
    pytest.param(_is_web, None, ["此帳號在 104 網頁上沒有這個操作", "104 後台"], [JOB_LIST], id="is-web"),
    pytest.param(_no_permit, None, ["104 後台"], [JOB_LIST], id="job5-absent"),
    pytest.param(_job5_denied, None, [DENIED], [JOB_LIST], id="job5-denied"),
    pytest.param(_job5_not_bool, None, ["104 後台"], [JOB_LIST], id="job5-not-bool"),
    pytest.param(_permit_not_object, None, ["104 後台"], [JOB_LIST], id="permit-not-object"),
    pytest.param(_in_verify, None, ["確認中"], [JOB_LIST], id="in-verify"),
    pytest.param(_switch_new, None, ["確認中"], [JOB_LIST], id="switch-new"),
    pytest.param(None, _no_update_button, ["104 後台"], [JOB_LIST, JOB_UPDATE], id="no-jobUpdate-button"),
    pytest.param(None, _update_button_denied, [BUTTON_REASON, "104 後台"], [JOB_LIST, JOB_UPDATE],
                 id="jobUpdate-button-denied"),
    pytest.param(None, _update_button_denied_no_message, ["104 後台"], [JOB_LIST, JOB_UPDATE],
                 id="jobUpdate-button-denied-no-message"),
]


def _add_on(res, key):
    res[key] = {J1: 1}


ADD_ON_KINDS = [
    pytest.param("hotOrder", "焦點職缺", id="focus-job"),
    pytest.param("selectJob", "精選工作", id="select-job"),
    pytest.param("priority", "優先推薦", id="priority"),
    pytest.param("jobSort", "自動排序", id="auto-sort"),
]


# ── T-1 .. T-10: parameter and read-shape refusals ─────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_T1_unknown_paths_are_refused_with_editable_fields_before_any_request(tmp_path, monkeypatch, tool):
    changes = {"detail.nonexistent": 1, "foo": "x", "detail.jobcat[0].no": JOBCAT_LEAF}
    kwargs = {"jobno": J1} if tool == "update_job" else {"source_jobno": J1}
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, [], changes=changes, **kwargs)
    assert _is_error(result)
    assert "editable_fields" in result
    for key in ("detail.nonexistent", "foo", "detail.jobcat[0].no"):
        assert key in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("key", READ_ONLY_KEYS)
async def test_T2_read_only_paths_are_refused_with_editable_fields_before_any_request(tmp_path, monkeypatch, tool, key):
    kwargs = {"jobno": J1} if tool == "update_job" else {"source_jobno": J1}
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, [], changes={key: "SYNTH-VALUE"}, **kwargs)
    assert _is_error(result)
    assert "editable_fields" in result
    assert key in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("path, value", INVALID_CHANGES)
async def test_T3_invalid_values_are_refused_naming_the_field_before_any_request(tmp_path, monkeypatch, tool, path, value):
    kwargs = {"jobno": J1} if tool == "update_job" else {"source_jobno": J1}
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, [], changes={path: value}, **kwargs)
    assert _is_error(result)
    assert path in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T4_an_empty_update_changes_object_is_refused_before_any_request(tmp_path, monkeypatch):
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", [], jobno=J1, changes={})
    assert _is_error(result)
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, kwargs", [
    pytest.param("update_job", {"jobno": J1, "changes": {"foo": 1}}, id="unknown-key"),
    pytest.param("update_job", {"jobno": J1, "changes": {"job_name": ""}}, id="empty-job-name"),
    pytest.param("update_job", {"jobno": J1, "changes": {}}, id="empty-changes"),
    pytest.param("update_job", {"jobno": J1, "changes": ["not", "an", "object"]}, id="changes-not-object"),
    pytest.param("update_job", {"jobno": J1, "changes": {"detail.jobcat": [{"no": JOBCAT_BRANCH}]}}, id="branch-code"),
    pytest.param("update_job", {"jobno": JOBNO_BAD, "changes": {"job_name": "x"}}, id="bad-jobno"),
    pytest.param("create_job", {"source_jobno": J1, "changes": {"foo": 1}}, id="create-unknown-key"),
])
async def test_T5_parameter_and_static_refusals_take_no_throttle_slot(tmp_path, monkeypatch, tool, kwargs):
    result, spy, info = await _run(tmp_path, monkeypatch, tool, [], **kwargs)
    assert _is_error(result)
    assert spy.calls == []
    assert list(info.throttle.request_timestamps) == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_T6_a_jobno_that_is_not_all_digits_is_refused_and_points_to_list_jobs(tmp_path, monkeypatch, tool):
    kwargs = {"jobno": JOBNO_BAD} if tool == "update_job" else {"source_jobno": JOBNO_BAD}
    changes = {"job_name": "SYNTH-NAME"}
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, [], changes=changes, **kwargs)
    assert _is_error(result)
    assert "list_jobs" in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("location", EXTRA_KEY_LOCATIONS)
async def test_T7_an_unhandled_key_in_the_read_refuses_both_tools_and_names_the_path(tmp_path, monkeypatch, tool, location):
    insert = tool == "create_job"
    read = _base_read(insert=insert)
    location(read)
    if insert:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
        kwargs = {"source_jobno": J1}
    else:
        script = [_list_ok([_list_row(J1)]), _read_ok(read)]
        kwargs = {"jobno": J1}
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, script,
                                    changes={"job_name": NEW_NAME}, **kwargs)
    assert _is_error(result)
    assert "synth_extra" in result["error"]
    assert _no_write(spy)
    dump = _dump(result)
    assert CONTACT not in dump
    assert BASE_DESC not in dump
    assert "SYNTH-EXTRA-VALUE" not in dump


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("mutate, hint", TYPE_MISMATCHES)
async def test_T8_a_wrong_type_in_the_read_refuses_both_tools_and_names_the_path(tmp_path, monkeypatch, tool, mutate, hint):
    insert = tool == "create_job"
    read = _base_read(insert=insert)
    mutate(read)
    if insert:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
        kwargs = {"source_jobno": J1}
    else:
        script = [_list_ok([_list_row(J1)]), _read_ok(read)]
        kwargs = {"jobno": J1}
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, script,
                                    changes={"job_name": NEW_NAME}, **kwargs)
    assert _is_error(result)
    assert hint in result["error"]
    assert _no_write(spy)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_T9_a_missing_required_read_path_refuses_and_missing_optional_paths_do_not(tmp_path, monkeypatch, tool):
    insert = tool == "create_job"
    kwargs = {"source_jobno": J1} if insert else {"jobno": J1}
    # The required path: detail.description is not optional.
    read = _read(insert=insert, wire={"detail.description": _DROP})
    if insert:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
    else:
        script = [_list_ok([_list_row(J1)]), _read_ok(read)]
    refused, spy, _info = await _run(tmp_path, monkeypatch, tool, script,
                                     changes={"job_name": NEW_NAME}, **kwargs)
    assert _is_error(refused)
    assert "description" in refused["error"]
    assert _no_write(spy)

    # The optional paths: the base read has no scheduledOpenDate and no freeKeyTime; shifts goes too.
    read = _read(insert=insert, wire={"detail.shifts": _DROP})
    if insert:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True), _fb_clean()]
    else:
        script = [_list_ok([_list_row(J1)]), _read_ok(read), _fb_clean()]
    preview, spy, _info = await _run(tmp_path, monkeypatch, tool, script,
                                     changes={"job_name": NEW_NAME}, **kwargs)
    assert "preview" in preview, preview
    assert _no_write(spy)


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("wire", ACCOUNT_MODE_WIRE)
async def test_T10_an_unsupported_account_mode_or_empty_address_refuses_and_points_to_the_backend(tmp_path, monkeypatch, tool, wire):
    insert = tool == "create_job"
    read = _read(insert=insert, wire=wire)
    kwargs = {"source_jobno": J1} if insert else {"jobno": J1}
    if insert:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
    else:
        script = [_list_ok([_list_row(J1)]), _read_ok(read)]
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, script,
                                    changes={"job_name": NEW_NAME}, **kwargs)
    assert _is_error(result)
    assert "104 後台" in result["error"]
    assert _no_write(spy)


# ── T-11 .. T-20: preview, confirm and the forbidden-words check ───────────────

@pytest.mark.asyncio
async def test_T11_update_without_confirm_previews_with_three_requests_and_no_write(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_clean()]
    changes = {"detail.description": NEW_DESC, "detail.need_emp.low": 4}
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1, changes=changes)
    assert "preview" in result, result
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE, FORBIDDEN]
    assert _no_write(spy)
    rows = _rows_by_field(result["preview"]["changes"])
    assert rows["detail.description"]["old"] == BASE_DESC
    assert rows["detail.description"]["new"] == NEW_DESC
    assert rows["detail.need_emp.low"]["old"] == 3
    assert rows["detail.need_emp.low"]["new"] == 4
    assert _params(spy, JOB_LIST) == [[("page", "1")]]
    assert _params(spy, JOB_UPDATE) == [[("act", "edit"), ("jobno", J1)]]


@pytest.mark.asyncio
async def test_T11_create_without_confirm_previews_with_four_requests_and_no_write(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_read(insert=True), insert=True), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script,
                                    source_jobno=J1, changes={"job_name": NEW_NAME})
    assert "preview" in result, result
    assert result["preview"]["job_name"] == NEW_NAME
    assert _routes(spy) == [JOB_LIST, JOB_MORE, JOB_INSERT, FORBIDDEN]
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T12_confirm_after_a_preview_starts_again_at_job_list_and_sends_the_second_read(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    script = [
        _list_ok([_list_row(J1)]), _read_ok(_read(wire={"additionalInfo.loadTime": LOAD_A})), _fb_clean(),
        _list_ok([_list_row(J1)]), _read_ok(_read(wire={"additionalInfo.loadTime": LOAD_B})), _fb_clean(),
        _write_ok(), _read_ok(_reread({"detail.description": NEW_DESC}, load_time=LOAD_B)),
    ]
    spy = _install(monkeypatch, script)
    tool = _tool("update_job")
    changes = {"detail.description": NEW_DESC}

    preview = await tool(ctx=ctx, jobno=J1, changes=changes)
    assert "preview" in preview
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE, FORBIDDEN]

    confirmed = await tool(ctx=ctx, jobno=J1, changes=changes, confirm=True)
    assert "error" not in confirmed, confirmed
    assert _routes(spy)[3:] == [JOB_LIST, JOB_UPDATE, FORBIDDEN, UPDATE, JOB_UPDATE]
    assert _form(_body(spy, UPDATE))["loadTime"] == [LOAD_B]


@pytest.mark.asyncio
async def test_T13_an_unchanged_value_is_named_and_dropped_from_the_changes_and_refused_when_nothing_changes(tmp_path, monkeypatch):
    # (a) one change equals the current value, one differs: the first is named, only the second is in changes.
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": BASE_DESC, "detail.need_emp.low": 4})
    assert "preview" in result, result
    assert [row["field"] for row in result["preview"]["changes"]] == ["detail.need_emp.low"]
    assert "detail.description" in _warnings_text(result)

    # (b) every change equals the current value, confirm=True: refused after two requests.
    script = [_list_ok([_list_row(J1)]), _read_ok(_read())]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": BASE_DESC}, confirm=True)
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]
    assert _no_write(spy)

    # (c) every change equals the current value, preview: returned with no change rows and a warning.
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_clean()]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": BASE_DESC})
    assert "preview" in result, result
    assert result["preview"]["changes"] == []
    assert "detail.description" in _warnings_text(result)


@pytest.mark.asyncio
async def test_T14_the_seven_forbidden_check_texts_follow_the_order_and_the_location_prefix_rule(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert "preview" in result, result
    body = _json_body(spy, FORBIDDEN)
    assert body["jobNo"] == J1
    texts = body["texts"]
    assert len(texts) == 7
    assert texts[0] == BASE_NAME
    assert texts[1] == NEW_DESC
    assert texts[2] == ADDRESS
    assert texts[3] == ""
    assert texts[4] == ""
    assert texts[5] == "SYNTH-OTHER-CONDITION-7731"
    assert texts[6] == CONTACT_NOTE


@pytest.mark.asyncio
@pytest.mark.parametrize("addr_no, expect_prefix", [
    pytest.param({"no": "6003001000", "des": "SYNTH-CITY"}, True, id="one-location-6003001000"),
    pytest.param({"no": "6001001000", "des": "SYNTH-CITY"}, False, id="one-location-other-code"),
    pytest.param([{"no": "6003001000", "des": "SYNTH-CITY"}, {"no": "6001001000", "des": "SYNTH-CITY-2"}],
                 False, id="two-locations-first-is-6003001000"),
])
async def test_T14_the_address_prefix_is_added_only_for_exactly_one_of_the_four_codes(tmp_path, monkeypatch, addr_no, expect_prefix):
    read = _read(wire={"detail.jobAddress.addrNo": addr_no})
    script = [_list_ok([_list_row(J1)]), _read_ok(read), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert "preview" in result, result
    address_text = _json_body(spy, FORBIDDEN)["texts"][2]
    assert address_text.endswith(ADDRESS)
    assert address_text.startswith("ABC上班地點XYZ") is expect_prefix


@pytest.mark.asyncio
async def test_T14_the_create_forbidden_check_carries_the_source_jobno(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_read(insert=True), insert=True), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert "preview" in result, result
    assert _json_body(spy, FORBIDDEN)["jobNo"] == J1


@pytest.mark.asyncio
async def test_T15_a_block_is_reported_in_the_preview_and_cannot_be_bypassed_on_confirm(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    script = [
        _list_ok([_list_row(J1)]), _read_ok(_read()), _fb_block(),
        _list_ok([_list_row(J1)]), _read_ok(_read()), _fb_block(),
    ]
    spy = _install(monkeypatch, script)
    tool = _tool("update_job")
    preview = await tool(ctx=ctx, jobno=J1, changes={"detail.description": NEW_DESC})
    assert "preview" in preview, preview
    assert preview["preview"]["forbidden_words"], "a block must be listed in the preview"

    result = await tool(ctx=ctx, jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True,
                        accept_forbidden_word_warnings=True)
    assert _is_error(result)
    assert "forbidden_words" in result
    assert _routes(spy)[3:] == [JOB_LIST, JOB_UPDATE, FORBIDDEN]
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T16_a_warning_without_the_acknowledgement_is_refused_with_its_hits(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_warn()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert _is_error(result)
    assert "forbidden_words" in result
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE, FORBIDDEN]
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T17_a_warning_with_the_acknowledgement_is_sent_and_listed(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_warn(), _write_ok(),
              _read_ok(_reread({"detail.description": NEW_DESC}))]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True,
                                    accept_forbidden_word_warnings=True)
    assert "error" not in result, result
    assert len(_routes(spy)) == 5
    assert UPDATE in _routes(spy)
    assert "SYNTH-WARN-HIT" in _warnings_text(result)


@pytest.mark.asyncio
async def test_T17_an_acknowledgement_with_no_warning_says_the_value_was_not_used(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_clean(), _write_ok(),
              _read_ok(_reread({"detail.description": NEW_DESC}))]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True,
                                    accept_forbidden_word_warnings=True)
    assert "error" not in result, result
    warnings = " ".join(result["warnings"])
    assert "accept_forbidden_word_warnings" in warnings or "沒有使用" in warnings


@pytest.mark.asyncio
async def test_T18_the_seventh_hit_names_only_its_segment_and_the_location_prefix_is_removed(tmp_path, monkeypatch):
    read = _read(wire={"detail.jobAddress.addrNo": {"no": "6003001000", "des": "SYNTH-CITY"}})
    texts = ["", "", "ABC上班地點XYZ" + ADDRESS + "SYNTH-ADDR-HIT", "", "", "", "SYNTH-APPLY-HIT"]
    script = [_list_ok([_list_row(J1)]), _read_ok(read), _fb_warn(texts)]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC})
    assert "preview" in result, result
    hits = {hit["field"]: hit for hit in result["preview"]["forbidden_words"]}
    assert "應徵方式說明" in hits
    assert "message" not in hits["應徵方式說明"]
    assert hits["detail.job_address.address"]["message"] == ADDRESS + "SYNTH-ADDR-HIT"
    assert "ABC上班地點XYZ" not in _dump(result)


@pytest.mark.asyncio
async def test_T18b_a_measured_warn_body_reports_the_hit_without_its_trailing_newline(tmp_path, monkeypatch):
    hit = "輸入內容含「限男性」，可能涉有就業歧視，請務必確認職務內容符合法律規範。\n"
    body = {"data": ["", hit, "", "", "", "", ""], "metadata": {}}
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _json(200, body)]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC})
    assert "preview" in result, result
    hits = result["preview"]["forbidden_words"]
    assert [item["field"] for item in hits] == ["detail.description"]
    assert hits[0]["message"] == hit.rstrip("\n")
    assert not hits[0]["message"].endswith("\n")


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    pytest.param(TimeoutError("SYNTH-FORBIDDEN-TIMEOUT"), id="timeout"),
    pytest.param(_json(500, {"status": "SYNTH-ERROR", "message": "SYNTH-500"}), id="http-500"),
    pytest.param(_json(200, {"data": [""] * 6, "metadata": {}}), id="six-elements"),
    pytest.param(_json(200, {"data": ["", 1, "", "", "", "", ""], "metadata": {}}), id="number-element"),
    pytest.param(_json(200, [""] * 7), id="bare-array"),
])
async def test_T19_a_failed_or_unrecognised_forbidden_check_refuses_the_write(tmp_path, monkeypatch, response):
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), response]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE, FORBIDDEN]
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T20_a_job_on_page_one_of_a_multi_page_list_continues(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)], total_page=2), _read_ok(_read()), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert "preview" in result, result
    assert _params(spy, JOB_LIST) == [[("page", "1")]]


@pytest.mark.asyncio
async def test_T20_a_job_not_on_page_one_of_a_multi_page_list_is_refused_after_one_request(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J2)], total_page=2)]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST]


# ── T-21 .. T-23: the update gates ─────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("list_edit, read_edit, expected, routes", UPDATE_GATES)
async def test_T21_front_end_permission_gates_refuse_the_update_and_never_send_job_more(tmp_path, monkeypatch, list_edit, read_edit, expected, routes):
    res = _list_result([_list_row(J1)])
    if list_edit is not None:
        list_edit(res)
    read = _base_read()
    if read_edit is not None:
        read_edit(read)
    script = [_envelope(res), _read_ok(read), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert _is_error(result)
    for text in expected:
        assert text in result["error"], result["error"]
    assert _routes(spy) == routes
    assert JOB_MORE not in _routes(spy)
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T21_an_absent_or_false_isWeb_continues_to_the_read(tmp_path, monkeypatch):
    res = _list_result([_list_row(J1)], is_web=False)
    del res["isWeb"]
    script = [_envelope(res), _read_ok(_base_read()), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert "preview" in result, result
    assert JOB_UPDATE in _routes(spy)


@pytest.mark.asyncio
@pytest.mark.parametrize("list_row_edit, expected", [
    pytest.param(lambda row: row.update({"isInVerify": 1}), "確認中", id="in-verify"),
    pytest.param(lambda row: row.update({"switch": "new"}), "確認中", id="switch-new"),
])
async def test_T21_an_in_verification_job_is_refused_before_the_read(tmp_path, monkeypatch, list_row_edit, expected):
    row = _list_row(J1)
    list_row_edit(row)
    script = [_list_ok([row])]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert _is_error(result)
    assert expected in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
@pytest.mark.parametrize("kind, label", ADD_ON_KINDS)
async def test_T22_an_add_on_for_this_job_refuses_and_names_the_kind(tmp_path, monkeypatch, kind, label):
    res = _list_result([_list_row(J1)])
    _add_on(res, kind)
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", [_envelope(res)], jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert _is_error(result)
    assert label in result["error"]
    assert "104 後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_T22_an_add_on_for_another_job_does_not_refuse(tmp_path, monkeypatch):
    res = _list_result([_list_row(J1)])
    res["hotOrder"] = {J2: 1}
    script = [_envelope(res), _read_ok(_base_read()), _fb_clean()]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC})
    assert "preview" in result, result


@pytest.mark.asyncio
@pytest.mark.parametrize("row_edit", [
    pytest.param({"autoclose": "1"}, id="autoclose-1"),
    pytest.param({"closedate": CLOSE_DATE}, id="closedate-set"),
    pytest.param({"d5": CLOSE_DATE}, id="d5-set"),
])
async def test_T22_an_auto_close_or_pre_open_date_on_the_list_refuses(tmp_path, monkeypatch, row_edit):
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1, **row_edit)])], jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert _is_error(result)
    assert "104 後台" in result["error"]
    assert _routes(spy) == [JOB_LIST]


@pytest.mark.asyncio
async def test_T22_a_close_date_on_the_read_refuses_after_the_read(tmp_path, monkeypatch):
    read = _read(wire={"otherSetting.closeDate": CLOSE_DATE})
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1)]), _read_ok(read)], jobno=J1,
                                    changes={"detail.description": NEW_DESC})
    assert _is_error(result)
    assert "104 後台" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]


@pytest.mark.asyncio
async def test_T23_a_category_change_on_an_open_job_needs_accept_progress_reset(tmp_path, monkeypatch):
    changes = {"detail.jobcat": [{"no": JOBCAT_LEAF2}]}
    script = [_list_ok([_list_row(J1)]), _read_ok(_read())]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1, changes=changes)
    assert _is_error(result)
    assert "招募進度" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]


@pytest.mark.asyncio
async def test_T23_a_category_change_on_a_closed_job_needs_no_accept_progress_reset(tmp_path, monkeypatch):
    changes = {"detail.jobcat": [{"no": JOBCAT_LEAF2}]}
    script = [_list_ok([_list_row(J1, switch="off")]), _read_ok(_read(wire={"switch": "off"})), _fb_clean()]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1, changes=changes)
    assert "preview" in result, result


@pytest.mark.asyncio
async def test_T23_with_accept_progress_reset_the_category_change_is_sent(tmp_path, monkeypatch):
    changes = {"detail.jobcat": [{"no": JOBCAT_LEAF2}]}
    script = [_list_ok([_list_row(J1)]), _read_ok(_read()), _fb_clean(), _write_ok(),
              _read_ok(_reread(changes))]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes=changes, confirm=True, accept_progress_reset=True)
    assert "error" not in result, result
    assert len(_routes(spy)) == 5
    assert UPDATE in _routes(spy)


# ── T-24 .. T-30: the sent body, re-read comparison and write outcomes ─────────

@pytest.mark.asyncio
async def test_T24_the_sent_form_carries_the_read_values_and_the_changed_need_emp_fields_only(tmp_path, monkeypatch):
    # Two runs, one with only the description changed and one that also changes need_emp. The
    # tool cannot send a zero-change body (a zero-change confirm is refused by design), so the
    # need_emp-only difference is the observable comparison available.
    run_a_script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                    _read_ok(_reread({"detail.description": NEW_DESC}))]
    result_a, spy_a, _info = await _run(tmp_path, monkeypatch, "update_job", run_a_script, jobno=J1,
                                        changes={"detail.description": NEW_DESC}, confirm=True)
    assert "error" not in result_a, result_a
    assert UPDATE in _routes(spy_a)
    assert _routes(spy_a)[-1] == JOB_UPDATE
    form_a = _form(_body(spy_a, UPDATE))
    assert form_a["description"] == [NEW_DESC]
    assert form_a["need_emp"] == ["3"]
    assert form_a["need_emp1"] == ["5"]
    assert form_a["need_emp_status"] == [""]
    assert form_a["jobSort"] == [JOB_SORT_EMPTY]
    assert form_a["jobNo"] == [J1]
    assert form_a["switch"] == ["on"]
    assert form_a["job"] == [BASE_NAME]
    assert form_a["loadTime"] == [LOAD_TIME]
    assert form_a["contact"] == [CONTACT_NAME]
    assert form_a["phone"] == [CONTACT_PHONE]

    # 0 and 0 is the unlimited setting: need_emp and need_emp1 become "0" and need_emp_status "unlimit",
    # so exactly the three need_emp wire fields differ from run A.
    changed_both = {"detail.description": NEW_DESC, "detail.need_emp.low": 0, "detail.need_emp.high": 0}
    run_b_script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                    _read_ok(_reread(changed_both))]
    result_b, spy_b, _info = await _run(tmp_path, monkeypatch, "update_job", run_b_script, jobno=J1,
                                        changes=changed_both, confirm=True)
    assert "error" not in result_b, result_b
    form_b = _form(_body(spy_b, UPDATE))
    differing = {key for key in set(form_a) | set(form_b) if form_a.get(key) != form_b.get(key)}
    assert differing == {"need_emp", "need_emp1", "need_emp_status"}
    assert form_b["description"] == [NEW_DESC]
    assert form_b["need_emp"] == ["0"]
    assert form_b["need_emp1"] == ["0"]
    assert form_b["need_emp_status"] == ["unlimit"]


@pytest.mark.asyncio
async def test_T25_a_matching_re_read_is_confirmed_with_the_read_back_values(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
              _read_ok(_reread({"detail.description": NEW_DESC}, load_time=LOAD_B, reg_id="SYNTH-REGID-B"))]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert result["saved"] == "confirmed", result
    assert len(_routes(spy)) == 5
    rows = _rows_by_field(result["changes"])
    assert rows["detail.description"]["read_back"] == NEW_DESC


@pytest.mark.asyncio
async def test_T26_a_change_that_did_not_apply_is_a_mismatch_naming_the_path(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
              _read_ok(_reread({}))]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC}, confirm=True)
    assert result["saved"] == "mismatch", result
    assert "detail.description" in result["mismatched_fields"]["not_applied"]
    assert result["mismatched_fields"]["damaged"] == []


@pytest.mark.asyncio
async def test_T26_an_unchanged_field_that_moved_is_damage_and_the_contact_is_named_only(tmp_path, monkeypatch):
    reread = _reread({}, extra={"needCondition.education": [2], "contaction.contacter": CONTACT_NAME_B})
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(), _read_ok(reread)]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC}, confirm=True)
    assert result["saved"] == "mismatch", result
    assert "detail.description" in result["mismatched_fields"]["not_applied"]
    damaged = result["mismatched_fields"]["damaged"]
    assert "need_condition.education" in damaged
    assert "聯絡人" in damaged
    assert "改壞" in result["warnings"][0]
    dump = _dump(result)
    assert CONTACT not in dump
    assert CONTACT_NAME_B not in dump


@pytest.mark.asyncio
@pytest.mark.parametrize("reread_response", [
    pytest.param(TimeoutError("SYNTH-REREAD-TIMEOUT"), id="reread-timeout"),
    pytest.param(_expired(), id="reread-expired"),
    pytest.param("extra-key", id="reread-extra-key"),
])
async def test_T27_a_failed_or_unreadable_re_read_is_unconfirmed_without_a_read_back(tmp_path, monkeypatch, reread_response):
    if reread_response == "extra-key":
        read = _reread({"detail.description": NEW_DESC})
        read["synthExtra"] = "SYNTH-EXTRA"
        reread_response = _read_ok(read)
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(), reread_response]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert result["saved"] == "unconfirmed", result
    assert result["warnings"]
    assert len(_routes(spy)) == 5
    assert all("read_back" not in row for row in result["changes"])


@pytest.mark.asyncio
async def test_T28_a_field_error_from_104_is_refused_with_the_failed_fields_and_no_re_read(tmp_path, monkeypatch):
    bad = _failed(
        {"description": "SYNTH-DESC-FAIL 請修改", "email": CONTACT + "MAIL@example.invalid 格式錯誤"},
        error_field_map={"description": "職務說明有誤", "email": CONTACT + "MAIL@example.invalid 格式錯誤",
                         "apply_type_tel": "SYNTH-TEL 錯誤"},
    )
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), bad]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert _is_error(result)
    failed = result["failed_fields"]
    # Key union: the message keys come first in their own order (description, email), then the key
    # only errorFieldMap names (apply_type_tel). All three errorFieldMap keys must appear.
    assert len(failed) == 3
    assert failed[0]["field"] == "detail.description" and failed[0]["label"] == "職務說明有誤"
    assert "message" in failed[0]
    assert "message" not in failed[1]
    assert failed[2]["label"] == "SYNTH-TEL 錯誤"
    assert "message" not in failed[2]
    assert len(_routes(spy)) == 4
    assert CONTACT not in _dump(result)


@pytest.mark.asyncio
@pytest.mark.parametrize("status, http", [
    pytest.param("NO_REMAIN_JOB", 200, id="no-remain-job"),
    pytest.param("JOB_NOT_FOUND", 200, id="job-not-found"),
    pytest.param("FAILED", 200, id="failed-without-field-map"),
    pytest.param("NO_REMAIN_JOB", 400, id="no-remain-job-http-400"),
])
async def test_T29_a_plain_refusal_from_104_is_an_error_with_no_re_read(tmp_path, monkeypatch, status, http):
    if http == 400:
        bad = _json(400, {"status": status, "message": "SYNTH-104-REJECT", "result": None,
                          "url": "", "data": None})
    else:
        bad = _envelope({}, status=status, message="SYNTH-104-REJECT")
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), bad]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert _is_error(result)
    assert len(_routes(spy)) == 4


@pytest.mark.asyncio
@pytest.mark.parametrize("response", [
    pytest.param(TimeoutError("SYNTH-WRITE-TIMEOUT"), id="timeout"),
    pytest.param(_non_json(), id="non-json"),
    pytest.param(_json(500, {"status": "SYNTH-ERROR", "message": "SYNTH-500"}), id="http-500"),
    pytest.param(_envelope({}, status="SOMETHING"), id="unknown-status"),
    pytest.param(_envelope({}, status="ACCESS_DENIED"), id="access-denied-status"),
    pytest.param(_envelope({}, code="001001"), id="limit-code-001001"),
])
async def test_T30_an_ambiguous_write_response_is_re_read_and_reported_from_the_re_read(tmp_path, monkeypatch, response):
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), response,
              _read_ok(_reread({"detail.description": NEW_DESC}))]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert len(_routes(spy)) == 5
    assert result["saved"] == "confirmed", result
    assert result["warnings"]


# ── T-31: the published input schemas ──────────────────────────────────────────

@pytest.mark.asyncio
async def test_T31_the_published_input_schemas_are_exactly_the_documented_parameters_without_ctx():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("job-edit-schema")
    _je().register_job_edit_tools(mcp)
    published = {tool.name: tool for tool in await mcp.list_tools()}
    assert "update_job" in published and "create_job" in published

    update = published["update_job"].inputSchema
    assert set(update["properties"]) == {"jobno", "changes", "confirm",
                                         "accept_forbidden_word_warnings", "accept_progress_reset"}
    assert set(update.get("required", [])) == {"jobno", "changes"}
    assert "ctx" not in update["properties"]

    create = published["create_job"].inputSchema
    assert set(create["properties"]) == {"source_jobno", "changes", "confirm",
                                         "accept_forbidden_word_warnings"}
    assert set(create.get("required", [])) == {"source_jobno"}
    assert "ctx" not in create["properties"]


# ── T-32 .. T-37: create_job ───────────────────────────────────────────────────

def _render(**overrides):
    render = {"jobNo": J1, "switch": "on", "jobOpenDay": "", "role": "1", "isNewModel": False, "poNum": 0}
    render.update(overrides)
    return render


CREATE_GATES = [
    pytest.param(lambda res: res.update({"isWeb": True}), None, None,
                 ["此帳號在 104 網頁上沒有這個操作", "104 後台"], [JOB_LIST], id="is-web"),
    pytest.param(lambda res: res["pageInfo"].update({"total_page": 2}), None, None,
                 ["一頁"], [JOB_LIST], id="more-than-one-page"),
    pytest.param(lambda res: res.update({"jobDataAry": [_list_row(J2)]}), None, None,
                 [], [JOB_LIST], id="source-not-listed"),
    pytest.param(None, {"job__6": {"result": True, "message": ""}}, None,
                 ["權限", "104 後台"], [JOB_LIST, JOB_MORE], id="job4-absent"),
    pytest.param(None, {"job__4": {"result": False, "message": DENIED}}, None,
                 [DENIED], [JOB_LIST, JOB_MORE], id="job4-denied"),
    pytest.param(None, "SYNTH-NOT-OBJECT", None,
                 ["權限", "104 後台"], [JOB_LIST, JOB_MORE], id="permit-not-object"),
    pytest.param(None, None, {"isNewModel": True, "poNum": 0},
                 ["您目前可以使用的職務數為0"], [JOB_LIST, JOB_MORE], id="new-model-no-slots"),
    pytest.param(None, None, "SYNTH-NOT-OBJECT",
                 ["結構"], [JOB_LIST, JOB_MORE], id="render-not-object"),
]


@pytest.mark.asyncio
@pytest.mark.parametrize("list_edit, more_permit, more_render, expected, routes", CREATE_GATES)
async def test_T32_create_front_end_gates_refuse_before_the_copy_read(tmp_path, monkeypatch, list_edit, more_permit, more_render, expected, routes):
    res = _list_result([_list_row(J1)])
    if list_edit is not None:
        list_edit(res)
    script = [_envelope(res), _more_ok(permit=more_permit, render=more_render),
              _read_ok(_base_read(insert=True), insert=True), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert _is_error(result)
    for text in expected:
        assert text in result["error"], result["error"]
    assert _routes(spy) == routes
    assert JOB_INSERT not in _routes(spy)
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T32_the_more_request_carries_the_source_jobno_and_its_job_setting(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert "preview" in result, result
    assert _routes(spy)[:2] == [JOB_LIST, JOB_MORE]
    assert _params(spy, JOB_MORE) == [[("jobno", J1), ("job_setting", f"SYNTH-SETTING-{J1}")]]


@pytest.mark.asyncio
@pytest.mark.parametrize("list_is_web", [
    pytest.param(False, id="false"),
    pytest.param(None, id="absent"),
])
async def test_T32_an_absent_or_false_isWeb_continues_to_the_copy_read(tmp_path, monkeypatch, list_is_web):
    res = _list_result([_list_row(J1)])
    if list_is_web is None:
        del res["isWeb"]
    else:
        res["isWeb"] = list_is_web
    script = [_envelope(res), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert "preview" in result, result
    assert JOB_MORE in _routes(spy)


@pytest.mark.asyncio
@pytest.mark.parametrize("render, poNum_on_list", [
    pytest.param(_render(isNewModel=True, poNum=3), 0, id="new-model-with-slots"),
    pytest.param(_render(isNewModel=False, poNum=0), 0, id="old-model-zero-slots"),
])
async def test_T32_slot_counts_only_matter_for_a_new_model_account(tmp_path, monkeypatch, render, poNum_on_list):
    res = _list_result([_list_row(J1)])
    res["isNewModel"] = True
    res["poNum"] = poNum_on_list
    script = [_envelope(res), _more_ok(render=render), _read_ok(_base_read(insert=True), insert=True), _fb_clean()]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert "preview" in result, result


@pytest.mark.asyncio
async def test_T33_a_jobInsert_read_with_an_unhandled_key_is_refused_with_no_write(tmp_path, monkeypatch):
    read = _base_read(insert=True)
    read["synthExtra"] = "SYNTH-EXTRA-VALUE"
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert _is_error(result)
    assert _routes(spy) == [JOB_LIST, JOB_MORE, JOB_INSERT]
    assert _params(spy, JOB_INSERT) == [[("act", "copy"), ("name", ""), ("jobno", J1)]]
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T33_a_read_without_the_jobInsert_button_refuses_and_says_so(tmp_path, monkeypatch):
    read = _base_read(insert=True)
    read["additionalInfo"]["operations"]["buttons"] = [_button("jobPreview")]
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert _is_error(result)
    assert "確定新增" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_MORE, JOB_INSERT]
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T34_the_new_job_request_is_the_copy_form_with_fast_zero_and_no_automatic_reservation(tmp_path, monkeypatch):
    script = [
        _list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean(),
        _new_ok(NEW_JOBNO),
        _list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME, switch="off")]),
    ]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1,
                                    changes={"job_name": NEW_NAME}, confirm=True)
    assert "error" not in result, result
    assert _routes(spy) == [JOB_LIST, JOB_MORE, JOB_INSERT, FORBIDDEN, NEW, JOB_LIST]
    assert _params(spy, NEW) == [[("fast", "0")]]
    form = _form(_body(spy, NEW))
    assert form["act"] == ["copy"]
    assert form["jobNo"] == [J1]
    assert form["jobSort"] == [JOB_SORT_EMPTY]
    assert form["automaticReservation"] == ["false"]
    assert form["job"] == [NEW_NAME]


@pytest.mark.asyncio
@pytest.mark.parametrize("after_rows, new_switch", [
    pytest.param([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME, switch="off")], "off", id="new-job-off"),
    pytest.param([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME, switch="on")], "on", id="new-job-on"),
])
async def test_T35_one_new_job_with_the_submitted_name_is_confirmed_with_its_switch(tmp_path, monkeypatch, after_rows, new_switch):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean(),
              _new_ok(NEW_JOBNO), _list_ok(after_rows)]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1,
                                    changes={"job_name": NEW_NAME}, confirm=True)
    assert result["created"] == "confirmed", result
    assert result["new_jobno"] == NEW_JOBNO
    assert result["new_job_switch"] == new_switch
    assert len(_routes(spy)) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("after, new_result_jobno, candidates", [
    pytest.param(_list_ok([_list_row(J1)]), NEW_JOBNO, [], id="no-new-job"),
    pytest.param(_list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME),
                           _list_row(SECOND_NEW_JOBNO, job=NEW_NAME)]), NEW_JOBNO,
                 [NEW_JOBNO, SECOND_NEW_JOBNO], id="two-new-jobs"),
    pytest.param(_list_ok([_list_row(J1), _list_row(NEW_JOBNO, job="SYNTH-OTHER-NAME")]), NEW_JOBNO,
                 [NEW_JOBNO], id="name-differs"),
    pytest.param(_list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME)]), "1000099",
                 [NEW_JOBNO], id="response-jobno-differs"),
    pytest.param(_list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME)], total_page=2), NEW_JOBNO,
                 [NEW_JOBNO], id="re-read-two-pages"),
    pytest.param(TimeoutError("SYNTH-REREAD-TIMEOUT"), NEW_JOBNO, None, id="re-read-timeout"),
])
async def test_T35_anything_else_is_unconfirmed_with_the_candidates_and_a_warning(tmp_path, monkeypatch, after, new_result_jobno, candidates):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean(),
              _new_ok(new_result_jobno), after]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1,
                                    changes={"job_name": NEW_NAME}, confirm=True)
    assert result["created"] == "unconfirmed", result
    assert result["new_jobno"] is None
    assert result["new_job_switch"] is None
    assert result["warnings"]
    if candidates is not None:
        assert result["candidates"] == candidates
    assert len(_routes(spy)) == 6


@pytest.mark.asyncio
@pytest.mark.parametrize("bad", [
    pytest.param(_failed({"description": "SYNTH-DESC-FAIL"}, {"description": "職務說明有誤"}), id="failed-with-field-map"),
    pytest.param(_envelope({}, status="NO_REMAIN_JOB", message="SYNTH-104-REJECT"), id="no-remain-job"),
    pytest.param(_envelope({}, status="JOB_NOT_FOUND", message="SYNTH-104-REJECT"), id="job-not-found"),
])
async def test_T36_a_plain_refusal_from_104_on_the_new_job_is_an_error_with_no_re_read(tmp_path, monkeypatch, bad):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
              _fb_clean(), bad]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1, confirm=True)
    assert _is_error(result)
    assert len(_routes(spy)) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize("response, needle", [
    pytest.param(_envelope({"jobno": NEW_JOBNO}, code="001004"), "上線", id="001004"),
    pytest.param(_envelope({"jobno": NEW_JOBNO}, code="001006"), None, id="001006"),
    pytest.param(_envelope({"jobno": NEW_JOBNO}, code="001001"), None, id="001001"),
    pytest.param(TimeoutError("SYNTH-NEW-TIMEOUT"), None, id="new-job-timeout"),
])
async def test_T37_limit_codes_and_timeouts_on_the_new_job_are_re_read_with_a_warning(tmp_path, monkeypatch, response, needle):
    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
              _fb_clean(), response, _list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=BASE_NAME)])]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1, confirm=True)
    assert len(_routes(spy)) == 6
    assert result["created"] in {"confirmed", "unconfirmed"}
    assert result["warnings"]
    if needle is not None:
        assert any("上線" in w or "曝光" in w for w in result["warnings"])


# ── T-38 .. T-43: privacy, log, shape, messages, internal errors, unrecognised responses ─

def _privacy_scenarios():
    scenarios = []

    def add(name, tool, kwargs, script, placeholder):
        scenarios.append(pytest.param(tool, kwargs, script, placeholder, id=name))

    add("preview", "update_job", {"jobno": J1, "changes": {"detail.description": NEW_DESC}},
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean()], False)
    add("confirmed", "update_job", {"jobno": J1, "changes": {"detail.description": NEW_DESC}, "confirm": True},
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
         _read_ok(_reread({"detail.description": NEW_DESC}))], False)
    add("mismatch-contact", "update_job", {"jobno": J1, "changes": {"detail.description": NEW_DESC}, "confirm": True},
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
         _read_ok(_reread({"detail.description": NEW_DESC}, extra={"contaction.contacter": CONTACT_NAME_B}))], False)
    add("failed-message-carries-contact", "update_job",
        {"jobno": J1, "changes": {"detail.description": NEW_DESC}, "confirm": True},
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(),
         _failed({"description": CONTACT + "EXT 不可用"}, {"description": "職務說明有誤"})], True)
    add("seventh-hit", "update_job", {"jobno": J1, "changes": {"detail.description": NEW_DESC}},
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()),
         _fb_warn(["", "", "", "", "", "", CONTACT_NOTE + " 不合規"])], False)
    add("button-reason-carries-contact", "update_job", {"jobno": J1, "changes": {"detail.description": NEW_DESC}},
        [_list_ok([_list_row(J1)]),
         _read_ok(_read(wire={"additionalInfo.operations.buttons": [
             _button("jobUpdate", permit=False, message=CONTACT + "NOTE 停用"), _button("jobPreview")]}))], True)
    add("guard-error-carries-contact", "update_job", {"jobno": J1, "changes": {"detail.description": NEW_DESC}},
        [_list_ok([_list_row(J1)]),
         _json(403, {"status": "ACCESS_DENIED", "message": CONTACT + "PHONE 403"})], False)
    add("chinese-contact-name-in-104-text", "update_job",
        {"jobno": J1, "changes": {"detail.description": NEW_DESC}, "confirm": True},
        [_list_ok([_list_row(J1)]), _read_ok(_read(wire={"contaction.contacter": CONTACT_CJK})), _fb_clean(),
         _failed({"description": CONTACT_CJK + " 不可用"}, {"description": "職務說明有誤"})], True)
    return scenarios


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, kwargs, script, placeholder", _privacy_scenarios())
async def test_T38_contact_values_and_editor_helper_values_never_reach_the_result(tmp_path, monkeypatch, tool, kwargs, script, placeholder):
    result, _spy, _info = await _run(tmp_path, monkeypatch, tool, script, **kwargs)
    dump = _dump(result)
    for sentinel in (CONTACT, CONTACT_CJK, LOAD_TIME, OPTION_LABEL):
        assert sentinel not in dump, sentinel
    if placeholder:
        assert PLACEHOLDER in dump


@pytest.mark.asyncio
async def test_T39_the_project_log_carries_no_contact_value_and_no_form_value(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.DEBUG)
    # A shape refusal with a contact value under an unhandled key: the log may name the path only.
    shape_read = _read(wire={"contaction.contactInfo.synthExtra": CONTACT + "LOG-VALUE"})
    await _run(tmp_path, monkeypatch, "update_job", [_list_ok([_list_row(J1)]), _read_ok(shape_read)],
               jobno=J1, changes={"detail.description": NEW_DESC})
    # A mismatch and a preview, which carry the description and the contacter through the core.
    await _run(tmp_path, monkeypatch, "update_job",
               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                _read_ok(_reread({"detail.description": NEW_DESC}, extra={"contaction.contacter": CONTACT_NAME_B}))],
               jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True)
    await _run(tmp_path, monkeypatch, "update_job",
               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean()],
               jobno=J1, changes={"detail.description": NEW_DESC})

    project_records = [
        record for record in caplog.records
        if record.name == "104-mcp" or record.name.startswith("104-mcp.")
    ]
    assert project_records, "the update writes must leave at least one 104-mcp log record, or the leak check is vacuous"
    log_text = "\n".join(caplog.handler.format(record) for record in project_records)
    for forbidden in (CONTACT, CONTACT_NAME_B, BASE_DESC, NEW_DESC):
        assert forbidden not in log_text, f"{forbidden!r} leaked into the project log"


@pytest.mark.asyncio
async def test_T40_every_return_has_one_of_the_closed_shapes_and_closed_saved_and_created_values(tmp_path, monkeypatch):
    results = []
    # refusals
    results.append((await _run(tmp_path, monkeypatch, "update_job", [], jobno=J1, changes={"foo": 1}))[0])
    results.append((await _run(tmp_path, monkeypatch, "create_job", [], source_jobno=J1,
                               changes={"foo": 1}))[0])
    # forbidden block and forbidden hit on confirm
    results.append((await _run(tmp_path, monkeypatch, "update_job",
                               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_block()],
                               jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True))[0])
    # previews
    results.append((await _run(tmp_path, monkeypatch, "update_job",
                               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean()],
                               jobno=J1, changes={"detail.description": NEW_DESC}))[0])
    results.append((await _run(tmp_path, monkeypatch, "create_job",
                               [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
                                _fb_clean()], source_jobno=J1))[0])
    # confirmed, mismatch, unconfirmed on update
    results.append((await _run(tmp_path, monkeypatch, "update_job",
                               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                                _read_ok(_reread({"detail.description": NEW_DESC}))],
                               jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True))[0])
    results.append((await _run(tmp_path, monkeypatch, "update_job",
                               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                                _read_ok(_reread({}))],
                               jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True))[0])
    results.append((await _run(tmp_path, monkeypatch, "update_job",
                               [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                                TimeoutError("SYNTH-REREAD")],
                               jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True))[0])
    # created confirmed and unconfirmed
    results.append((await _run(tmp_path, monkeypatch, "create_job",
                               [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
                                _fb_clean(), _new_ok(), _list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=NEW_NAME)])],
                               source_jobno=J1, changes={"job_name": NEW_NAME}, confirm=True))[0])
    results.append((await _run(tmp_path, monkeypatch, "create_job",
                               [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
                                _fb_clean(), _new_ok(), _list_ok([_list_row(J1)])],
                               source_jobno=J1, changes={"job_name": NEW_NAME}, confirm=True))[0])
    for result in results:
        _assert_closed_shape(result)


@pytest.mark.asyncio
async def test_T41_every_non_confirmed_outcome_says_do_not_resend_and_where_to_check(tmp_path, monkeypatch):
    mismatch, _s, _i = await _run(
        tmp_path, monkeypatch, "update_job",
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
         _read_ok(_reread({}))],
        jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True)
    assert mismatch["saved"] == "mismatch"
    assert "不要重送" in mismatch["message"] and "104 後台" in mismatch["message"]

    unconfirmed, _s, _i = await _run(
        tmp_path, monkeypatch, "update_job",
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(), TimeoutError("SYNTH")],
        jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True)
    assert unconfirmed["saved"] == "unconfirmed"
    assert "不要重送" in unconfirmed["message"] and "104 後台" in unconfirmed["message"]

    created_unconfirmed, _s, _i = await _run(
        tmp_path, monkeypatch, "create_job",
        [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean(),
         _new_ok(), _list_ok([_list_row(J1)])],
        source_jobno=J1, changes={"job_name": NEW_NAME}, confirm=True)
    assert created_unconfirmed["created"] == "unconfirmed"
    assert "不要重送" in created_unconfirmed["message"] and "104 後台" in created_unconfirmed["message"]

    confirmed, _s, _i = await _run(
        tmp_path, monkeypatch, "update_job",
        [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
         _read_ok(_reread({"detail.description": NEW_DESC}))],
        jobno=J1, changes={"detail.description": NEW_DESC}, confirm=True)
    assert confirmed["saved"] == "confirmed"
    assert "重讀" in confirmed["message"]


@pytest.mark.asyncio
async def test_T42_an_unexpected_error_after_the_write_is_unconfirmed_and_before_it_is_an_error(tmp_path, monkeypatch):
    """The seam: compare_forms, build_form and confirm_created are the core steps after and before
    the write. Each is replaced, by scoped mock.patch blocks on both the core
    module and the tools module (create=True, so either import style is reached), with a raiser."""
    from unittest import mock

    def boom(*args, **kwargs):
        raise RuntimeError("SYNTH-INTERNAL")

    # After the write (the post-write comparison fails): unconfirmed; the write was attempted.
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
              _read_ok(_reread({"detail.description": NEW_DESC}))]
    with mock.patch("mcp104.tools.job_form.compare_forms", boom, create=True), \
            mock.patch("mcp104.tools.job_edit.compare_forms", boom, create=True):
        result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                        changes={"detail.description": NEW_DESC}, confirm=True)
    assert "error" not in result, result
    assert result["saved"] == "unconfirmed"
    assert result["warnings"]
    assert UPDATE in _routes(spy)

    # Before the write (the form build fails): an error, and the message says nothing was sent.
    with mock.patch("mcp104.tools.job_form.build_form", boom, create=True), \
            mock.patch("mcp104.tools.job_edit.build_form", boom, create=True):
        result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                        [_list_ok([_list_row(J1)]), _read_ok(_base_read())], jobno=J1,
                                        changes={"detail.description": NEW_DESC}, confirm=True)
    assert _is_error(result)
    assert "沒有送出" in result["error"]
    assert _no_write(spy)

    # The create path after the write: created is unconfirmed.
    with mock.patch("mcp104.tools.job_edit.confirm_created", boom, create=True):
        result, _spy, _info = await _run(
            tmp_path, monkeypatch, "create_job",
            [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
             _fb_clean(), _new_ok(), _list_ok([_list_row(J1)])],
            source_jobno=J1, changes={"job_name": NEW_NAME}, confirm=True)
    assert result.get("created") == "unconfirmed", result
    assert result["warnings"]


@pytest.mark.asyncio
@pytest.mark.parametrize("tool, index, bad", [
    pytest.param("update_job", 0, _expired(), id="update-list-expired"),
    pytest.param("update_job", 0, _challenge(), id="update-list-challenge"),
    pytest.param("update_job", 1, _expired(), id="update-read-expired"),
    pytest.param("update_job", 1, _forbidden(), id="update-read-403"),
    pytest.param("update_job", 1, _challenge(), id="update-read-challenge"),
    pytest.param("update_job", 3, _expired(), id="update-write-expired"),
    pytest.param("update_job", 3, _forbidden(), id="update-write-403"),
    pytest.param("update_job", 3, _challenge(), id="update-write-challenge"),
    pytest.param("create_job", 0, _expired(), id="create-list-expired"),
    pytest.param("create_job", 1, _forbidden(), id="create-more-403"),
    pytest.param("create_job", 2, _challenge(), id="create-insert-challenge"),
    pytest.param("create_job", 4, _expired(), id="create-write-expired"),
    pytest.param("create_job", 4, _forbidden(), id="create-write-403"),
    pytest.param("create_job", 4, _challenge(), id="create-write-challenge"),
])
async def test_T43_session_block_and_challenge_responses_stop_the_sequence_with_the_matching_error(tmp_path, monkeypatch, tool, index, bad):
    if tool == "update_job":
        chain = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
                 _read_ok(_reread({"detail.description": NEW_DESC}))]
        kwargs = {"jobno": J1, "changes": {"detail.description": NEW_DESC}, "confirm": True}
    else:
        chain = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True),
                 _fb_clean(), _new_ok(), _list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=BASE_NAME)])]
        kwargs = {"source_jobno": J1, "confirm": True}
    script = chain[:index] + [bad]
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, script, **kwargs)
    assert _is_error(result), result
    assert len(spy.calls) == index + 1


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_T43_a_rate_limit_refusal_at_entry_is_returned_with_retry_after(tmp_path, monkeypatch, tool):
    kwargs = {"jobno": J1, "changes": {"detail.description": NEW_DESC}} if tool == "update_job" else {"source_jobno": J1}
    result, spy, _info = await _run_with_slots_left(tmp_path, monkeypatch, tool, [], slots_left=0, **kwargs)
    assert _is_error(result)
    assert "retry_after_seconds" in result
    assert spy.calls == []


# ── T-44 .. T-46: tool descriptions, registration, the rolling window ──────────

@pytest.mark.asyncio
async def test_T44_the_two_descriptions_say_what_the_model_needs_before_a_write():
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("job-edit-descriptions")
    _je().register_job_edit_tools(mcp)
    published = {tool.name: tool.description for tool in await mcp.list_tools()}
    update_text = published["update_job"]
    create_text = published["create_job"]
    for text in (update_text, create_text):
        assert "立刻生效" in text
        assert "求職者" in text
        assert "無法撤回" in text
        assert "confirm" in text
        assert "前端碼" in text
        assert "未量測" in text
        assert not any(word in text for word in FORBIDDEN_SOURCE_WORDS)
    assert "confirmed" in update_text and "mismatch" in update_text and "unconfirmed" in update_text
    assert "損壞" in update_text
    for name in ("new_job_switch", "open_jobs", "close_jobs", "confirmed", "unconfirmed"):
        assert name in create_text
    # The timeout clause: a resend after a timeout can create a second public job.
    assert "逾時" in create_text and "第二個" in create_text


@pytest.mark.asyncio
async def test_T45_both_tools_are_registered_and_fit_the_description_budget():
    from mcp.server.fastmcp import FastMCP

    main = importlib.import_module("mcp104.main")
    mcp = FastMCP("job-edit-registration")
    main.register_all_tools(mcp)
    published = {tool.name: tool for tool in await mcp.list_tools()}
    for name in ("update_job", "create_job"):
        assert name in published
        assert len(published[name].description) <= 1948, name


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_T46_with_four_slots_free_a_confirmed_call_is_refused_at_entry(tmp_path, monkeypatch, tool):
    # Four requests fit in the window. The confirmed update needs five and the confirmed create six,
    # so each is refused before any request is sent. One call per test: a session's sends are shared.
    kwargs = ({"jobno": J1, "changes": {"detail.description": NEW_DESC}, "confirm": True} if tool == "update_job"
              else {"source_jobno": J1, "changes": {"job_name": NEW_NAME}, "confirm": True})
    result, spy, _info = await _run_with_slots_left(tmp_path, monkeypatch, tool, [], slots_left=4, **kwargs)
    assert _is_error(result)
    assert "retry_after_seconds" in result
    assert spy.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
async def test_T46_with_four_slots_free_a_preview_runs(tmp_path, monkeypatch, tool):
    # The update preview needs three requests and the create preview four; both fit in four slots.
    if tool == "update_job":
        script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean()]
        kwargs = {"jobno": J1, "changes": {"detail.description": NEW_DESC}}
        expected_requests = 3
    else:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean()]
        kwargs = {"source_jobno": J1, "changes": {"job_name": NEW_NAME}}
        expected_requests = 4
    result, spy, _info = await _run_with_slots_left(tmp_path, monkeypatch, tool, script, slots_left=4, **kwargs)
    assert "preview" in result, result
    assert len(_routes(spy)) == expected_requests


# ── T-61 .. T-71: the front-end state rules and the mapping's conversions ──────

@pytest.mark.asyncio
async def test_T61_a_remote_description_alone_is_refused_while_the_remote_option_is_off(tmp_path, monkeypatch):
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1)]), _read_ok(_base_read())], jobno=J1,
                                    changes={"detail.remote_work.description": "SYNTH-REMOTE-DESC"})
    assert _is_error(result)
    assert "detail.remote_work.description" in result["error"]
    assert "遠端" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]


@pytest.mark.asyncio
async def test_T61_switching_the_remote_option_on_lets_the_description_through(tmp_path, monkeypatch):
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.remote_work.option": 1,
                                             "detail.remote_work.description": "SYNTH-REMOTE-DESC"})
    assert "preview" in result, result
    assert len(_routes(spy)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("path, value", [
    pytest.param("detail.salary.salary_low", 45000, id="salary-low"),
    pytest.param("detail.salary.salary_high", 50000, id="salary-high"),
])
async def test_T62_a_salary_range_under_negotiable_pay_is_refused_and_points_to_the_negotiable_fields(tmp_path, monkeypatch, path, value):
    read = _read(wire={"detail.salary.salaryTypeId": 10})
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1)]), _read_ok(read)], jobno=J1,
                                    changes={path: value})
    assert _is_error(result)
    assert path in result["error"]
    assert "detail.negotiable_salary" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]


@pytest.mark.asyncio
async def test_T63_a_work_type_on_a_full_time_job_is_refused_and_a_part_time_job_passes(tmp_path, monkeypatch):
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1)]), _read_ok(_base_read())], jobno=J1,
                                    changes={"detail.work_type": [1]})
    assert _is_error(result)
    assert "detail.work_type" in result["error"]
    assert "兼職" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]

    part_time = _read(wire={"detail.roleId": 2})
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1)]), _read_ok(part_time), _fb_clean()], jobno=J1,
                                    changes={"detail.work_type": [1]})
    assert "preview" in result, result
    assert len(_routes(spy)) == 3


@pytest.mark.asyncio
@pytest.mark.parametrize("path, value", [
    pytest.param("detail.shifts", [1], id="shifts"),
    pytest.param("detail.work_time.interval",
                 [{"type": 1, "periods": [{"start_hour": 8, "start_minute": 30, "end_hour": 17, "end_minute": 30}]}],
                 id="work-time-interval"),
    pytest.param("need_condition.identities", [2], id="identities"),
])
async def test_T64_the_senior_role_refuses_shift_and_identity_changes_that_are_not_sent(tmp_path, monkeypatch, path, value):
    read = _read(wire={"detail.roleId": 3})
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job",
                                    [_list_ok([_list_row(J1)]), _read_ok(read)], jobno=J1,
                                    changes={path: value})
    assert _is_error(result)
    assert path in result["error"]
    assert "高階" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_UPDATE]


@pytest.mark.asyncio
@pytest.mark.parametrize("path, value, code", [
    pytest.param("detail.work_type", [3], "3", id="work-type-3"),
    pytest.param("need_condition.education", [64], "64", id="education-64"),
    pytest.param("need_condition.identities", [1], "1", id="identities-1"),
    pytest.param("need_condition.drivers", [3], "3", id="drivers-3"),
    pytest.param("detail.shifts", [6], "6", id="shifts-6"),
    pytest.param("detail.work_time.interval",
                 [{"type": 32, "periods": [{"start_hour": 9, "start_minute": 0, "end_hour": 18, "end_minute": 0}]}],
                 "32", id="interval-type-32"),
])
async def test_T65_a_code_outside_the_table_is_refused_naming_the_field_before_any_request(tmp_path, monkeypatch, path, value, code):
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", [], jobno=J1, changes={path: value})
    assert _is_error(result)
    assert path in result["error"]
    assert code in result["error"]
    assert spy.calls == []


@pytest.mark.asyncio
async def test_T66_codes_the_form_drops_are_named_then_do_not_count_as_damage(tmp_path, monkeypatch):
    read = _read(wire={"needCondition.education": [8, 64], "contaction.usingTypes": [1, 32]})
    script = [_list_ok([_list_row(J1)]), _read_ok(read), _fb_clean(), _write_ok(),
              _read_ok(_reread({"detail.description": NEW_DESC}))]
    result, spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                    changes={"detail.description": NEW_DESC}, confirm=True)
    assert "error" not in result, result
    warnings = _warnings_text(result)
    assert "need_condition.education" in warnings
    assert "64" in warnings and "32" in warnings
    form = _form(_body(spy, UPDATE))
    assert form["edu[]"] == ["8"]
    assert "32" not in form.get("apply_type[]", [])
    assert result["saved"] == "confirmed"
    assert result["mismatched_fields"]["damaged"] == []


@pytest.mark.asyncio
async def test_T67_a_moved_unsent_field_is_named_without_its_values_and_a_date_only_change_is_not(tmp_path, monkeypatch):
    moved = _reread({"detail.description": NEW_DESC},
                    extra={"detail.jobAddress.latitude": 25.0399, "department.name": "SYNTH-DEPT-MOVED"})
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(), _read_ok(moved)]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC}, confirm=True)
    assert result["saved"] == "confirmed", result
    warnings = _warnings_text(result)
    assert "detail.job_address.latitude" in warnings
    assert "department.name" in warnings
    assert "25.0399" not in _dump(result)
    assert "SYNTH-DEPT-MOVED" not in _dump(result)

    dates_only = _reread({"detail.description": NEW_DESC}, load_time=LOAD_B,
                         extra={"appearDate": "2026-11-20 09:00:00"})
    script = [_list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(), _read_ok(dates_only)]
    result, _spy, _info = await _run(tmp_path, monkeypatch, "update_job", script, jobno=J1,
                                     changes={"detail.description": NEW_DESC}, confirm=True)
    assert result["saved"] == "confirmed", result
    assert "appear" not in _warnings_text(result)
    assert "load" not in _warnings_text(result)


@pytest.mark.asyncio
async def test_T68_a_category_and_a_major_are_sent_as_codes_and_previewed_with_the_dataset_name(tmp_path, monkeypatch):
    ctx, _info = await _new_session(tmp_path)
    changes = {"detail.jobcat": [{"no": JOBCAT_LEAF2}], "need_condition.majors": [{"no": MAJOR_LEAF2}]}
    script = [
        _list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(),
        _list_ok([_list_row(J1)]), _read_ok(_base_read()), _fb_clean(), _write_ok(),
        _read_ok(_reread(changes)),
    ]
    spy = _install(monkeypatch, script)
    tool = _tool("update_job")
    preview = await tool(ctx=ctx, jobno=J1, changes=changes, accept_progress_reset=True)
    assert "preview" in preview, preview
    rows = _rows_by_field(preview["preview"]["changes"])
    assert rows["detail.jobcat"]["new"] == [{"no": JOBCAT_LEAF2, "des": JOBCAT_LEAF2_NAME}]
    assert rows["need_condition.majors"]["new"] == [{"no": MAJOR_LEAF2, "des": MAJOR_LEAF2_NAME}]

    confirmed = await tool(ctx=ctx, jobno=J1, changes=changes, accept_progress_reset=True, confirm=True)
    assert "error" not in confirmed, confirmed
    form = _form(_body(spy, UPDATE))
    assert form["jobId"] == [JOBCAT_LEAF2]
    assert form["majorId"] == [MAJOR_LEAF2]
    sent_values = " ".join(value for values in form.values() for value in values)
    assert JOBCAT_LEAF2_NAME not in sent_values
    assert MAJOR_LEAF2_NAME not in sent_values


@pytest.mark.asyncio
@pytest.mark.parametrize("tool", TOOLS)
@pytest.mark.parametrize("read_wire, changes, expected, route_count", [
    pytest.param({}, {"job_name": RIVAL + " 職缺"}, ["job_name", RIVAL], None, id="competitor-word"),
    pytest.param({"detail.needEmp.high": 3}, {"detail.need_emp.low": 5}, ["detail.need_emp"], None,
                 id="need-emp-low-above-high"),
    pytest.param({}, {"detail.salary.salary_low": 70000}, ["detail.salary"], None, id="salary-low-above-high"),
    pytest.param({"detail.salary.salaryTypeId": 50}, {"detail.salary.salary_low": 20000},
                 ["detail.salary", "30000"], None, id="monthly-floor"),
    pytest.param({"detail.salary.salaryTypeId": 10}, {"detail.negotiable_salary.salary_low": 39000},
                 ["detail.negotiable_salary", "40000"], None, id="negotiable-monthly-floor"),
])
async def test_T69_a_change_that_breaks_a_rule_on_this_job_is_refused_after_the_read(tmp_path, monkeypatch, tool, read_wire, changes, expected, route_count):
    insert = tool == "create_job"
    read = _read(insert=insert, wire=read_wire)
    kwargs = {"source_jobno": J1} if insert else {"jobno": J1}
    if insert:
        script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)]
        expected_routes = [JOB_LIST, JOB_MORE, JOB_INSERT]
    else:
        script = [_list_ok([_list_row(J1)]), _read_ok(read)]
        expected_routes = [JOB_LIST, JOB_UPDATE]
    result, spy, _info = await _run(tmp_path, monkeypatch, tool, script, changes=changes, **kwargs)
    assert _is_error(result)
    for text in expected:
        assert text in result["error"], result["error"]
    assert _routes(spy) == expected_routes
    assert _no_write(spy)


@pytest.mark.asyncio
async def test_T70_a_source_with_an_auto_close_date_is_refused_for_create_and_an_empty_one_passes(tmp_path, monkeypatch):
    read = _read(insert=True, wire={"otherSetting.closeDate": CLOSE_DATE,
                                    "additionalInfo.options.autoClose": {"1": OPTION_LABEL}})
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job",
                                    [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(read, insert=True)],
                                    source_jobno=J1)
    assert _is_error(result)
    assert "自動關閉" in result["error"]
    assert "104 後台" in result["error"]
    assert _routes(spy) == [JOB_LIST, JOB_MORE, JOB_INSERT]
    assert _no_write(spy)

    script = [_list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean()]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1)
    assert "preview" in result, result
    assert len(_routes(spy)) == 4


@pytest.mark.asyncio
async def test_T71_the_jobInsert_shape_from_the_measurement_is_accepted_and_sends_empty_switch_and_reg_id(tmp_path, monkeypatch):
    script = [
        _list_ok([_list_row(J1)]), _more_ok(), _read_ok(_base_read(insert=True), insert=True), _fb_clean(),
        _new_ok(NEW_JOBNO), _list_ok([_list_row(J1), _list_row(NEW_JOBNO, job=BASE_NAME, switch="off")]),
    ]
    result, spy, _info = await _run(tmp_path, monkeypatch, "create_job", script, source_jobno=J1, confirm=True)
    assert "error" not in result, result
    assert result["created"] in {"confirmed", "unconfirmed"}
    form = _form(_body(spy, NEW))
    assert form["switch"] == [""]
    assert form["regId"] == [""]
