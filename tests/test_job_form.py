"""Tests for the job-form core (tools/job_form.py): cases T-47 to T-55, T-60 and T-72.

The core is reached through its module attributes (importlib, so a missing name fails only
the test that needs it, not the whole module), and every expectation comes from the core's
contract (the READ_PATHS table, the
form rules, the matching and redaction rules, the forbidden-word texts and verdicts, the
save-response table) plus docs/104-site-facts.md §8.31 (the jobUpdate key tree) and §8.34
(the jobInsert differences). No fixture is read: every read result is synthesised in this
file, and every contact value carries the SYNTHETIC-CONTACT- prefix.

Where the contract names a behaviour but not the attribute or the wire spelling that carries
it, the test takes the most direct reading and says so next to the assertion. Those
readings are:
  * ChangeError exposes .kind and .field; ForbiddenVerdict exposes .verdict and .hits;
    SaveOutcome exposes .outcome and .jobno, and its two reminder flags are counted as
    True-valued fields (their names are not given);
  * a list-valued form field may go out as key[], so T-50 compares wire keys with
    the [] suffix stripped; majorId, specialty, skill and skill_cert have no pinned bracket
    form, and an empty specialty/skill/skill_cert may be absent or "";
  * T-50 assumes salary_is_above is absent outside the 以上 case, that role 2 sends neither
    business_trip[] nor business_trip_period, and that the dialect entries map to
    locallanno_N and t_N;
  * the salary_type_id code for 月薪 is not pinned by the contract, so T-52 checks the minimum-salary
    rule over the candidate codes and requires that at least one of them refuses it.
"""

import importlib

import pytest

from mcp104.tools.helpers import MalformedResponseError

JOBNO = "1000001"
CONTACT_TOKEN = "［聯絡資料］"
ABC_PREFIX = "ABC上班地點XYZ"
DAY_ELEMENT = '{"type":1,"periods":[{"startHour":9,"startMinute":0,"endHour":18,"endMinute":0}]}'
MID_ELEMENT = '{"type":16,"periods":[{"startHour":14,"startMinute":0,"endHour":22,"endMinute":0}]}'
LANGUAGE_KEYS = tuple(
    f"{prefix}_{n}" for prefix in ("language", "l", "c", "r", "w", "locallanno", "t") for n in (1, 2, 3)
)
DISABLE_KEYS = ("dis_role_1", "dis_role_2", "dis_role_3", "dis_level_1", "dis_level_2", "dis_level_3")
# The 24 measured option dictionaries minus autoClose (autoClose is set per test where it matters).
OPTION_NAMES = (
    "role", "salaryType", "workType", "manage", "businessTrip", "businessTripPeriod",
    "workInterval", "workShift", "holiday", "onBoard", "identity", "disableRole",
    "disableLevel", "workExperience", "education", "language", "languageLevel",
    "dialect", "dialectLevel", "driver", "applyType", "replyDate", "matchEmail",
)


def _core():
    return importlib.import_module("mcp104.tools.job_form")


def _cats():
    return importlib.import_module("mcp104.tools.categories")


def _res_field(resolution, field):
    if hasattr(resolution, field):
        return getattr(resolution, field)
    return resolution[field]


class _Expect:
    """Collects every mismatch in one test, so one run reports all of them."""

    def __init__(self):
        self.failures: list[str] = []

    def equal(self, label, actual, expected):
        if actual != expected:
            self.failures.append(f"{label}\n    expected: {expected!r}\n    actual:   {actual!r}")

    def check(self):
        assert not self.failures, "\n".join(self.failures)


def _change_error(ex, label, fn, kind):
    """Run fn; it must raise ChangeError with this kind. Returns the exception or None."""
    error_type = _core().ChangeError
    try:
        fn()
    except error_type as exc:
        ex.equal(f"{label}: kind", exc.kind, kind)
        return exc
    ex.failures.append(f"{label}: accepted, expected ChangeError({kind!r})")
    return None


def _diff(actual: dict, expected: dict) -> str:
    lines = []
    for key in sorted(set(expected) | set(actual)):
        if key not in actual:
            lines.append(f"  missing {key}: expected {expected[key]!r}")
        elif key not in expected:
            lines.append(f"  unexpected {key}: {actual[key]!r}")
        elif actual[key] != expected[key]:
            lines.append(f"  {key}: expected {expected[key]!r}, actual {actual[key]!r}")
    return "\n".join(lines)


def _period(start_hour, end_hour, start_minute=0, end_minute=0):
    return {"startHour": start_hour, "startMinute": start_minute,
            "endHour": end_hour, "endMinute": end_minute}


def _options():
    return {name: {"全職": 1, "兼職": 2} for name in OPTION_NAMES}


def _baseline(role=2):
    """A synthetic jobUpdate result in 104's own camelCase spelling, shaped on docs §8.31."""
    return {
        "jobName": "SYNTH-JOB-NAME",
        "switch": "on",
        "appearDate": "2026-01-01 00:00:00",
        "openDate": "2026-01-01 00:00:00",
        "canDelete": False,
        "isHunter": False,
        "isDispatch": False,
        "isNewModel": False,
        "isTheNetwork": False,
        "isGeneral": False,
        "shareLink": [{"linkName": "copy", "linkUrl": "https://www.104.com.tw/job/SYNTH-LINK"}],
        "detail": {
            "roleId": role,
            "needEmp": {"low": 1, "high": 3},
            "jobcat": [{"no": "2007001004", "des": "軟體工程師"}],
            "description": "SYNTH-DESCRIPTION-TEXT",
            "salary": {"salaryTypeId": 20, "salaryLow": 30000, "salaryHigh": 50000,
                       "salaryOld": "SYNTH-OLD-SALARY"},
            "workType": [1],
            "jobAddress": {"addrNo": {"no": "6001001000", "des": "台北市"},
                           "address": "SYNTH-ADDRESS", "mapAddress": "SYNTH-MAP",
                           "latitude": 25.0, "longitude": 121.5},
            "industoryArea": None,
            "manage": 0,
            "businessTrip": [],
            "workTime": {"interval": [{"type": 1, "periods": [_period(9, 18)]}]},
            "holiday": "SYNTH-HOLIDAY",
            "onboardDate": 2,
            "remoteWork": {"option": 0, "description": "SYNTH-REMOTE-TEXT"},
            "shifts": [],
            "department": None,
            "negotiableSalary": {"type": 0, "salaryLow": 0, "salaryHigh": 0},
        },
        "needCondition": {
            "disables": {"disable": [], "proof": False},
            "identities": [2],
            "experience": 3,
            "education": [2],
            "majors": [{"no": "3011011000", "des": "機械工程相關"}],
            "language": [],
            "tools": [],
            "skills": [],
            "licenses": [],
            "drivers": [1],
            "otherCondition": "SYNTH-OTHER-CONDITION",
        },
        "contaction": {
            "contacter": "SYNTHETIC-CONTACT-CONTACTER",
            "emails": ["SYNTHETIC-CONTACT-EMAIL@example.invalid"],
            "usingTypes": [1],
            "contactInfo": {
                "applyAddrNo": None,
                "applyAddr": "SYNTHETIC-CONTACT-ADDRESS",
                "ext": "SYNTHETIC-CONTACT-EXT",
                "phoneArea": "SYNTHETIC-CONTACT-AREA",
                "phone": "SYNTHETIC-CONTACT-PHONE",
                "cellphone": "SYNTHETIC-CONTACT-CELL",
                "note": "SYNTHETIC-CONTACT-NOTE",
            },
        },
        "otherSetting": {"closeDate": "", "reply": {"noReply": False, "days": 3}, "subscript": 1},
        "additionalInfo": {
            "loadTime": "2026-01-01 00:00:00",
            "poEndDate": "",
            "minimumSalary": {"monthly": 28000, "hourly": 180},
            "competitors": "SYNTH-COMPETITOR-A,SYNTH-COMPETITOR-B",
            "competitorsEmail": {"blackList": [], "whiteList": []},
            "ordinanceKeywords": {},
            "regId": "SYNTH-REG-ID",
            "operations": {
                "buttons": [
                    {"linkName": "jobUpdate", "permit": True, "url": "SYNTH-UPDATE-URL",
                     "isAjax": True, "isNewTab": False, "message": "", "switch": None},
                    {"linkName": "jobPreview", "permit": True, "url": "SYNTH-PREVIEW-URL",
                     "isAjax": False, "isNewTab": True, "message": "", "switch": None},
                ],
                "links": [],
            },
            "options": _options(),
        },
        "department": {"id": "9", "code": "SYNTH-DEPT-CODE", "name": "SYNTH-DEPT-NAME"},
    }


def _insert_result(role=2):
    """The jobInsert?act=copy shape: the jobUpdate read minus openDate and additionalInfo.regId,
    switch and every button switch null, and the two jobInsert/jobPreview buttons (docs §8.34)."""
    result = _baseline(role)
    del result["openDate"]
    result["switch"] = None
    del result["additionalInfo"]["regId"]
    result["additionalInfo"]["operations"]["buttons"] = [
        {"linkName": "jobInsert", "permit": True, "url": "SYNTH-INSERT-URL",
         "isAjax": True, "isNewTab": False, "message": "", "switch": None},
        {"linkName": "jobPreview", "permit": True, "url": "SYNTH-PREVIEW-URL",
         "isAjax": False, "isNewTab": True, "message": "", "switch": None},
    ]
    return result


def _contaction(*, contacter="", emails=(), using=(), **contact_info):
    info = {"applyAddrNo": None, "applyAddr": "", "ext": "", "phoneArea": "",
            "phone": "", "cellphone": "", "note": ""}
    info.update(contact_info)
    return {"contacter": contacter, "emails": list(emails), "usingTypes": list(using), "contactInfo": info}


def _read(result):
    return _core().read_job(result)


def _build(result, *, act="edit", jobno=JOBNO):
    core = _core()
    return core.build_form(core.read_job(result), act=act, jobno=jobno)


def _exact(form):
    """Wire pairs grouped by the exact wire key, bracket suffix included."""
    grouped: dict[str, list[str]] = {}
    for key, value in _core().qs_pairs(form):
        grouped.setdefault(key, []).append(value)
    return grouped


def _read_mut(mutate):
    result = _baseline()
    mutate(result)
    return _read(result)


def _precondition_codes():
    cats = _cats()
    jobcat = cats.load_dataset("JobCat.json", "jobcat")
    major = cats.load_dataset("Major.json", "major")
    assert _res_field(cats.resolve(jobcat, "2007001004"), "status") == "resolved"
    # jobcat accepts a branch as a category: the path to 2010001000 ends on a node that has children.
    assert cats.path(jobcat, "2010001000")[-1].has_children
    assert _res_field(cats.resolve(major, "3011011000"), "status") == "resolved"


# ── T-47 (qs_pairs, interface) ───────────────────────────────────────────────────

def test_qs_pairs_writes_lists_in_brackets_and_keeps_empty_strings_but_drops_none_and_empty_lists():
    core = _core()
    form = {"a": "x", "n": 3, "t": True, "f": False, "e": "", "l": ["1", 2], "z": [], "none": None}
    assert core.qs_pairs(form) == [
        ("a", "x"), ("n", "3"), ("t", "true"), ("f", "false"), ("e", ""),
        ("l[]", "1"), ("l[]", "2"),
    ]
    with pytest.raises(TypeError):
        core.qs_pairs({"nested": {"k": "v"}})
    with pytest.raises(TypeError):
        core.qs_pairs({"ratio": 1.5})


# ── T-48 (build_form, interface): sentinels, JavaScript fallbacks, the 以上 rule ──

def test_build_form_applies_the_sentinels_the_javascript_fallbacks_and_the_above_rule():
    ex = _Expect()

    r = _baseline()
    r["detail"]["needEmp"] = {"low": -1, "high": -1}
    f = _exact(_build(r))
    ex.equal("need_emp -1/-1: need_emp", f.get("need_emp"), [""])
    ex.equal("need_emp -1/-1: need_emp1", f.get("need_emp1"), [""])
    ex.equal("need_emp -1/-1: need_emp_status", f.get("need_emp_status"), [""])

    r = _baseline()
    r["detail"]["needEmp"] = {"low": 0, "high": 0}
    f = _exact(_build(r))
    ex.equal("need_emp 0/0: need_emp", f.get("need_emp"), ["0"])
    ex.equal("need_emp 0/0: need_emp1", f.get("need_emp1"), ["0"])
    ex.equal("need_emp 0/0: need_emp_status", f.get("need_emp_status"), ["unlimit"])

    # 以上 on a non-面議 salary: salary_high is deleted, salary_is_above 1, negotiable high -1.
    # The negotiable high is 60000 so that a pass-through would be visibly wrong.
    r = _baseline()
    r["detail"]["salary"] = {"salaryTypeId": 20, "salaryLow": 30000, "salaryHigh": 9999999,
                             "salaryOld": "SYNTH-OLD-SALARY"}
    r["detail"]["negotiableSalary"] = {"type": 1, "salaryLow": 45000, "salaryHigh": 60000}
    f = _exact(_build(r))
    ex.equal("以上 (non-面議): salary_high is deleted", f.get("salary_high"), None)
    ex.equal("以上 (non-面議): salary_is_above", f.get("salary_is_above"), ["1"])
    ex.equal("以上 (non-面議): negotiable_salary_high", f.get("negotiable_salary_high"), ["-1"])

    # negotiable fallbacks: 0, null and absent all become the sentinel values.
    r = _baseline()
    r["detail"]["negotiableSalary"] = {"type": None, "salaryLow": 0, "salaryHigh": None}
    f = _exact(_build(r))
    ex.equal("fallback null/0/null: negotiable_salary_type", f.get("negotiable_salary_type"), ["0"])
    ex.equal("fallback null/0/null: negotiable_salary_low", f.get("negotiable_salary_low"), ["-1"])
    ex.equal("fallback null/0/null: negotiable_salary_high", f.get("negotiable_salary_high"), ["-1"])

    r["detail"]["negotiableSalary"] = {}
    f = _exact(_build(r))
    ex.equal("fallback with all three keys absent: negotiable_salary_type", f.get("negotiable_salary_type"), ["0"])
    ex.equal("fallback with all three keys absent: negotiable_salary_low", f.get("negotiable_salary_low"), ["-1"])
    ex.equal("fallback with all three keys absent: negotiable_salary_high", f.get("negotiable_salary_high"), ["-1"])

    r["detail"]["negotiableSalary"] = {"type": 1, "salaryLow": 45000, "salaryHigh": 60000}
    f = _exact(_build(r))
    ex.equal("real negotiable values pass through: type", f.get("negotiable_salary_type"), ["1"])
    ex.equal("real negotiable values pass through: low", f.get("negotiable_salary_low"), ["45000"])
    ex.equal("real negotiable values pass through: high", f.get("negotiable_salary_high"), ["60000"])

    # 面議 (salary_type_id 10): salary_low/high are sent as 0 and the negotiable fallbacks are 0.
    r = _baseline()
    r["detail"]["salary"] = {"salaryTypeId": 10, "salaryLow": 30000, "salaryHigh": 50000,
                             "salaryOld": "SYNTH-OLD-SALARY"}
    r["detail"]["negotiableSalary"] = {"type": 0, "salaryLow": 0, "salaryHigh": None}
    f = _exact(_build(r))
    ex.equal("面議: negotiable_salary_low is 0", f.get("negotiable_salary_low"), ["0"])
    ex.equal("面議: negotiable_salary_high is 0", f.get("negotiable_salary_high"), ["0"])
    ex.equal("面議: salary_low is sent as 0", f.get("salary_low"), ["0"])
    ex.equal("面議: salary_high is sent as 0", f.get("salary_high"), ["0"])

    # 面議 with the 以上 value: negotiable high 0 and no salary_is_above.
    r["detail"]["salary"] = {"salaryTypeId": 10, "salaryLow": 0, "salaryHigh": 9999999,
                             "salaryOld": "SYNTH-OLD-SALARY"}
    r["detail"]["negotiableSalary"] = {"type": 0, "salaryLow": 0, "salaryHigh": 0}
    f = _exact(_build(r))
    ex.equal("面議 + 以上: negotiable_salary_high is 0", f.get("negotiable_salary_high"), ["0"])
    ex.equal("面議 + 以上: no salary_is_above", f.get("salary_is_above"), None)

    # Code 8 on using_types: the reverse-checkbox net effect is "sent when read".
    r = _baseline()
    r["contaction"]["usingTypes"] = [8, 1]
    ex.equal("using_types with 8: apply_type[] keeps 8", _exact(_build(r)).get("apply_type[]"), ["1", "8"])
    r["contaction"]["usingTypes"] = [1]
    ex.equal("using_types without 8: apply_type[] has no 8", _exact(_build(r)).get("apply_type[]"), ["1"])

    # department takes id, never code; a null department sends "".
    r = _baseline()
    r["department"] = {"id": "7", "code": "SYNTH-CODE-X", "name": "SYNTH-NAME-Y"}
    ex.equal("department: id, not code", _exact(_build(r)).get("department"), ["7"])
    r["department"] = None
    ex.equal("department null: empty string", _exact(_build(r)).get("department"), [""])

    # work_interval element is one JSON string with the front end's key names and order.
    r = _baseline()
    r["detail"]["workTime"] = {"interval": [{"type": 1, "periods": [_period(9, 18)]}]}
    ex.equal("work_interval element is the exact JSON text",
             _exact(_build(r)).get("work_interval[]"), [DAY_ELEMENT])

    # An empty code array sends no key at all (the brackets form of an empty list).
    r = _baseline()
    r["needCondition"]["education"] = []
    ex.equal("empty education: edu[] absent", _exact(_build(r)).get("edu[]"), None)

    ex.equal("job takes the top-level jobName", _exact(_build(_baseline())).get("job"), ["SYNTH-JOB-NAME"])
    ex.check()


# ── T-49 (build_form, interface): role-conditional fields and code-array conversions ──

def test_build_form_sends_role_conditional_fields_and_converts_code_arrays_in_table_order():
    ex = _Expect()

    # role 2 sends worktype[] and never business_trip (its period included, by the reading in the header).
    r = _baseline(role=2)
    r["detail"]["businessTrip"] = {"types": [1], "period": 2}
    f = _exact(_build(r))
    ex.equal("role 2: worktype[]", f.get("worktype[]"), ["1"])
    ex.equal("role 2: business_trip[] absent", f.get("business_trip[]"), None)
    ex.equal("role 2: business_trip_period absent", f.get("business_trip_period"), None)

    # role 3 forces worktime and sends none of the role-3 keys, even when the read has them.
    r = _baseline(role=3)
    r["detail"]["workTime"] = {"freeKeyTime": "SYNTH-FREE-KEY", "interval": [{"type": 1, "periods": [_period(9, 18)]}]}
    r["needCondition"]["identities"] = [2]
    r["detail"]["shifts"] = [1]
    r["otherSetting"]["subscript"] = 2
    f = _exact(_build(r))
    ex.equal("role 3: worktime forced", f.get("worktime"), ["依公司規定"])
    for key in ("work_interval[]", "d3", "role_status[]", "shifts[]", "noemail"):
        ex.equal(f"role 3: {key} absent", f.get(key), None)

    # language: an empty list gives 21 zeros; a filled one lands in its slots.
    r = _baseline()
    r["needCondition"]["language"] = []
    f = _exact(_build(r))
    for key in LANGUAGE_KEYS:
        ex.equal(f"language empty: {key}", f.get(key), ["0"])
    r["needCondition"]["language"] = {
        "foreign": [{"language": 1, "listen": 1, "speak": 2, "read": 4, "write": 8}],
        "dialect": [{"language": 5, "level": 2}],
    }
    f = _exact(_build(r))
    filled = {"language_1": ["1"], "l_1": ["1"], "c_1": ["2"], "r_1": ["4"], "w_1": ["8"],
              "locallanno_1": ["5"], "t_1": ["2"]}
    for key in LANGUAGE_KEYS:
        ex.equal(f"language filled: {key}", f.get(key), filled.get(key, ["0"]))

    # Code arrays: table order, duplicates merged, codes outside the table dropped.
    r = _baseline(role=2)
    r["detail"]["workType"] = [16, 1, 3]
    ex.equal("work_type [16,1,3] -> worktype[] 1,16", _exact(_build(r)).get("worktype[]"), ["1", "16"])

    r = _baseline()
    r["needCondition"]["education"] = [8, 2, 8]
    ex.equal("education [8,2,8] -> edu[] 2,8", _exact(_build(r)).get("edu[]"), ["2", "8"])

    r["needCondition"]["drivers"] = [64, 1]
    ex.equal("drivers [64,1] -> driver[] 1,64", _exact(_build(r)).get("driver[]"), ["1", "64"])

    r["contaction"]["usingTypes"] = [16, 2, 8, 1]
    ex.equal("using_types [16,2,8,1] -> apply_type[] 1,8,2,16",
             _exact(_build(r)).get("apply_type[]"), ["1", "8", "2", "16"])

    r = _baseline(role=1)
    r["detail"]["businessTrip"] = {"types": [2, 1], "period": 2}
    f = _exact(_build(r))
    ex.equal("business_trip types [2,1] -> business_trip[] 1,2", f.get("business_trip[]"), ["1", "2"])
    ex.equal("business_trip period 2 -> business_trip_period", f.get("business_trip_period"), ["2"])

    r = _baseline(role=2)
    r["detail"]["shifts"] = [5, 1]
    ex.equal("shifts [5,1] -> shifts[] 1,5", _exact(_build(r)).get("shifts[]"), ["1", "5"])
    r["detail"]["shifts"] = [4, 5]
    ex.equal("shifts [4,5] -> shifts[] only 5", _exact(_build(r)).get("shifts[]"), ["5"])

    r = _baseline()
    r["needCondition"]["identities"] = [64, 2]
    ex.equal("identities [64,2] -> role_status[] keeps the read order",
             _exact(_build(r)).get("role_status[]"), ["64", "2"])

    # work_interval: classes in code order, and an empty interval still sends the day class.
    r = _baseline(role=1)
    r["detail"]["workTime"] = {"interval": [
        {"type": 16, "periods": [_period(14, 22)]},
        {"type": 1, "periods": [_period(9, 18)]},
    ]}
    ex.equal("interval types [16,1] -> day element then mid element",
             _exact(_build(r)).get("work_interval[]"), [DAY_ELEMENT, MID_ELEMENT])
    r["detail"]["workTime"] = {"interval": []}
    ex.equal("empty interval (role 1) -> one day class without periods",
             _exact(_build(r)).get("work_interval[]"), ['{"type":1,"periods":[]}'])

    # reply: days 0 with no_reply true sends no d2; days not 0 sends d2 1; null days sends d2 1.
    reply_cases = [
        ({"noReply": True, "days": 0}, ["0"], None),
        ({"noReply": True, "days": 3}, ["3"], ["1"]),
        ({"noReply": True, "days": None}, ["0"], ["1"]),
        ([], ["0"], None),
    ]
    for reply, reply_day, d2 in reply_cases:
        r = _baseline()
        r["otherSetting"]["reply"] = reply
        f = _exact(_build(r))
        ex.equal(f"reply {reply!r}: reply_day", f.get("reply_day"), reply_day)
        ex.equal(f"reply {reply!r}: d2", f.get("d2"), d2)
    r = _baseline()
    del r["otherSetting"]["reply"]
    f = _exact(_build(r))
    ex.equal("reply absent: reply_day", f.get("reply_day"), ["0"])
    ex.equal("reply absent: d2", f.get("d2"), None)

    # business_trip empty (role 1): no business_trip[], period empty string.
    r = _baseline(role=1)
    f = _exact(_build(r))
    ex.equal("business_trip [] (role 1): business_trip[] absent", f.get("business_trip[]"), None)
    ex.equal("business_trip [] (role 1): business_trip_period empty", f.get("business_trip_period"), [""])

    # remote work: a zero option sends the description as "".
    for remote in ([], {"option": 0, "description": "SYNTH-REMOTE-TEXT"}):
        r = _baseline()
        r["detail"]["remoteWork"] = remote
        f = _exact(_build(r))
        ex.equal(f"remote {remote!r}: remote_work_option", f.get("remote_work_option"), ["0"])
        ex.equal(f"remote {remote!r}: remote_work_description", f.get("remote_work_description"), [""])

    # job_address empty: the three address fields are empty strings.
    r = _baseline()
    r["detail"]["jobAddress"] = []
    f = _exact(_build(r))
    ex.equal("job_address []: addr_no", f.get("addr_no"), [""])
    ex.equal("job_address []: city_name", f.get("city_name"), [""])
    ex.equal("job_address []: address", f.get("address"), [""])

    # noemail: 2 -> "0", other -> "1", and absent without the matchEmail option.
    r = _baseline()
    r["otherSetting"]["subscript"] = 2
    ex.equal("subscript 2: noemail 0", _exact(_build(r)).get("noemail"), ["0"])
    r["otherSetting"]["subscript"] = 1
    ex.equal("subscript 1: noemail 1", _exact(_build(r)).get("noemail"), ["1"])
    r = _baseline()
    del r["additionalInfo"]["options"]["matchEmail"]
    r["otherSetting"]["subscript"] = 2
    ex.equal("no matchEmail option: noemail absent", _exact(_build(r)).get("noemail"), None)

    # null sentinels.
    r = _baseline()
    r["detail"]["industoryArea"] = None
    r["contaction"]["contactInfo"]["applyAddrNo"] = None
    r["detail"]["manage"] = None
    r["detail"]["onboardDate"] = None
    f = _exact(_build(r))
    ex.equal("industory_area null: ind_area1", f.get("ind_area1"), ["0"])
    ex.equal("apply_addr_no null: apply_addrno", f.get("apply_addrno"), ["0"])
    ex.equal("manage null: manage_status", f.get("manage_status"), ["0"])
    ex.equal("onboard_date null: startby", f.get("startby"), ["3"])
    r["detail"]["onboardDate"] = 0
    ex.equal("onboard_date 0: startby", _exact(_build(r)).get("startby"), ["0"])

    # auto close: keys absent unless the option is truthy; then autoclose 0 and empty closedate.
    r = _baseline()
    f = _exact(_build(r))
    ex.equal("auto_close absent: autoclose absent", f.get("autoclose"), None)
    ex.equal("auto_close absent: closedate absent", f.get("closedate"), None)
    r["additionalInfo"]["options"]["autoClose"] = {"是": 1}
    r["otherSetting"]["closeDate"] = ""
    f = _exact(_build(r))
    ex.equal("auto_close truthy, close_date empty: autoclose", f.get("autoclose"), ["0"])
    ex.equal("auto_close truthy, close_date empty: closedate", f.get("closedate"), [""])

    # contact fields: the four phone fields, apply_addr and note are trimmed; contacter is not.
    r = _baseline()
    r["contaction"]["contacter"] = " SYNTHETIC-CONTACT-NAME "
    r["contaction"]["contactInfo"].update({
        "phoneArea": " SYNTHETIC-CONTACT-AREA ",
        "phone": " SYNTHETIC-CONTACT-PHONE ",
        "ext": " SYNTHETIC-CONTACT-EXT ",
        "cellphone": " SYNTHETIC-CONTACT-CELL ",
        "applyAddr": "  SYNTHETIC-CONTACT-ADDRESS  ",
        "note": "  SYNTHETIC-CONTACT-NOTE  ",
    })
    f = _exact(_build(r))
    ex.equal("contacter is not trimmed", f.get("contact"), [" SYNTHETIC-CONTACT-NAME "])
    ex.equal("phonearea is trimmed", f.get("phonearea"), ["SYNTHETIC-CONTACT-AREA"])
    ex.equal("phone is trimmed", f.get("phone"), ["SYNTHETIC-CONTACT-PHONE"])
    ex.equal("ext is trimmed", f.get("ext"), ["SYNTHETIC-CONTACT-EXT"])
    ex.equal("cellphone is trimmed", f.get("cellphone"), ["SYNTHETIC-CONTACT-CELL"])
    ex.equal("apply_address is trimmed", f.get("apply_address"), ["SYNTHETIC-CONTACT-ADDRESS"])
    ex.equal("apply (note) is trimmed", f.get("apply"), ["SYNTHETIC-CONTACT-NOTE"])
    ex.check()


# ── T-50 (build_form, interface): an unchanged read is the full expected form ─────

def test_build_form_for_an_unchanged_read_is_the_hand_written_full_form():
    r = _baseline(role=2)
    # Traps: code arrays out of table order, phone and address padded, reply days 0 with no_reply.
    r["detail"]["workType"] = [16, 1]
    r["detail"]["shifts"] = [3, 1]
    r["detail"]["workTime"] = {"interval": [
        {"type": 16, "periods": [_period(14, 22)]},
        {"type": 1, "periods": [_period(9, 18)]},
    ]}
    r["needCondition"]["identities"] = [64, 2]
    r["needCondition"]["education"] = [8, 2]
    r["needCondition"]["drivers"] = [64, 1]
    r["otherSetting"] = {"closeDate": "", "reply": {"noReply": True, "days": 0}, "subscript": 2}
    r["contaction"] = {
        "contacter": "SYNTHETIC-CONTACT-CONTACTER",
        "emails": ["SYNTHETIC-CONTACT-EMAIL-A@example.invalid",
                   "SYNTHETIC-CONTACT-EMAIL-B@example.invalid"],
        "usingTypes": [16, 1, 8],
        "contactInfo": {
            "applyAddrNo": None,
            "applyAddr": "  SYNTHETIC-CONTACT-ADDRESS  ",
            "ext": "SYNTHETIC-CONTACT-EXT",
            "phoneArea": " SYNTHETIC-CONTACT-AREA ",
            "phone": "  SYNTHETIC-CONTACT-PHONE  ",
            "cellphone": "SYNTHETIC-CONTACT-CELL",
            "note": " SYNTHETIC-CONTACT-NOTE ",
        },
    }

    expected = {
        "job": ["SYNTH-JOB-NAME"],
        "switch": ["on"],
        "d5": [""],
        "department": ["9"],
        "jobId": ["2007001004"],
        "description": ["SYNTH-DESCRIPTION-TEXT"],
        "role": ["2"],
        "need_emp": ["1"],
        "need_emp1": ["3"],
        "need_emp_status": [""],
        "salary_type": ["20"],
        "salary_low": ["30000"],
        "salary_high": ["50000"],
        "negotiable_salary_type": ["0"],
        "negotiable_salary_low": ["-1"],
        "negotiable_salary_high": ["-1"],
        "worktype[]": ["1", "16"],
        "addr_no": ["6001001000"],
        "city_name": ["台北市"],
        "address": ["SYNTH-ADDRESS"],
        "ind_area1": ["0"],
        "manage_status": ["0"],
        "worktime": ["SYNTH-HOLIDAY"],
        "work_interval[]": [DAY_ELEMENT, MID_ELEMENT],
        "d3": [""],
        "startby": ["2"],
        "remote_work_option": ["0"],
        "remote_work_description": [""],
        "shifts[]": ["1", "3"],
        "role_status[]": ["64", "2"],
        "period": ["3"],
        "edu[]": ["2", "8"],
        "majorId": ["3011011000"],
        "driver[]": ["1", "64"],
        "others": ["SYNTH-OTHER-CONDITION"],
        "specialty": [""],
        "skill": [""],
        "skill_cert": [""],
        "contact": ["SYNTHETIC-CONTACT-CONTACTER"],
        "email": ["SYNTHETIC-CONTACT-EMAIL-A@example.invalid,SYNTHETIC-CONTACT-EMAIL-B@example.invalid"],
        "apply_type[]": ["1", "8", "16"],
        "phonearea": ["SYNTHETIC-CONTACT-AREA"],
        "phone": ["SYNTHETIC-CONTACT-PHONE"],
        "ext": ["SYNTHETIC-CONTACT-EXT"],
        "cellphone": ["SYNTHETIC-CONTACT-CELL"],
        "apply_addrno": ["0"],
        "apply_address": ["SYNTHETIC-CONTACT-ADDRESS"],
        "apply": ["SYNTHETIC-CONTACT-NOTE"],
        "reply_day": ["0"],
        "noemail": ["0"],
        "loadTime": ["2026-01-01 00:00:00"],
        "regId": ["SYNTH-REG-ID"],
        "jobNo": [JOBNO],
        "act": ["edit"],
        "checkOverDate": ["2"],
        "document_status": ["1"],
        "jobSort": ['{"data":[{"reserve":[]}]}'],
        "automaticReservation": ["false"],
    }
    # Absent on purpose: business_trip[] (the read's businessTrip is []), handicompendium (proof is
    # false), d2 (reply days are 0), and autoclose and closedate (the account reads no autoClose option).
    expected.update({key: ["0"] for key in LANGUAGE_KEYS})
    expected.update({key: ["0"] for key in DISABLE_KEYS})

    actual = _exact(_build(r))
    diff = _diff(actual, expected)
    assert not diff, "the full form differs from the hand-written form:\n" + diff


# ── T-51 (read_job, interface): the measured tree, the optional containers, the shape refusals ──

def test_read_job_accepts_the_measured_tree_and_refuses_each_unaccounted_departure():
    core = _core()

    # The measured tree, and each optional container empty or absent.
    core.read_job(_baseline())
    r = _baseline()
    r["detail"]["businessTrip"] = []
    core.read_job(r)
    r = _baseline()
    del r["detail"]["businessTrip"]
    core.read_job(r)
    r = _baseline()
    r["otherSetting"]["reply"] = []
    core.read_job(r)
    r = _baseline()
    del r["otherSetting"]["reply"]
    core.read_job(r)
    r = _baseline()
    r["detail"]["jobAddress"] = []
    core.read_job(r)
    r = _baseline()
    del r["detail"]["jobAddress"]
    core.read_job(r)
    r = _baseline()
    r["detail"]["remoteWork"] = []
    core.read_job(r)
    r = _baseline()
    del r["detail"]["remoteWork"]
    core.read_job(r)

    # Dropped codes: education 3, using_types 32, and interval class 32; the repeated class 1 is merged, not dropped.
    r = _baseline()
    r["needCondition"]["education"] = [2, 3]
    r["contaction"]["usingTypes"] = [1, 32]
    r["detail"]["workTime"]["interval"] = [
        {"type": 1, "periods": [_period(9, 18)]},
        {"type": 32, "periods": [_period(9, 18)]},
        {"type": 1, "periods": [_period(9, 18)]},
    ]
    job = core.read_job(r)

    def dropped_under(fragment):
        return {code for path, codes in job.dropped if fragment in path for code in codes}

    assert dropped_under("education") == {3}, job.dropped
    assert dropped_under("using_types") == {32}, job.dropped
    assert dropped_under("interval") == {32}, job.dropped

    # additionalInfo.options: any Chinese label under a dictionary is accepted.
    r = _baseline()
    r["additionalInfo"]["options"]["role"]["新加的中文標籤"] = 9
    core.read_job(r)

    # Each departure on its own is refused, and the message names the path but not the value.
    faults = {}
    r = _baseline()
    r["surpriseField"] = "SYNTH-EXTRA"
    faults["surprise_field"] = r
    r = _baseline()
    del r["detail"]["description"]
    faults["detail.description"] = r
    r = _baseline()
    r["detail"]["needEmp"]["low"] = "SYNTH-BAD-LOW-VALUE"
    faults["detail.need_emp.low"] = r
    r = _baseline()
    r["needCondition"]["disables"]["disable"] = [{"type": 1, "level": 1}] * 4
    faults["need_condition.disables.disable"] = r
    for path, bad in faults.items():
        with pytest.raises(core.JobShapeError) as info:
            core.read_job(bad)
        assert path in str(info.value), (path, str(info.value))
    with pytest.raises(core.JobShapeError) as info:
        core.read_job(faults["detail.need_emp.low"])
    assert "SYNTH-BAD-LOW-VALUE" not in str(info.value)

    # All departures together: every problem path is carried in one refusal.
    r = _baseline()
    r["surpriseField"] = "SYNTH-EXTRA"
    del r["detail"]["description"]
    r["detail"]["needEmp"]["low"] = "SYNTH-BAD-LOW-VALUE"
    with pytest.raises(core.JobShapeError) as info:
        core.read_job(r)
    message = str(info.value)
    for path in ("surprise_field", "detail.description", "detail.need_emp.low"):
        assert path in message, (path, message)


# ── T-52 (check_changes and apply_changes, interface) ─────────────────────────────

_STATIC_INVALID = [
    ("job_name empty", {"job_name": ""}),
    ("job_name 121 characters", {"job_name": "甲" * 121}),
    ("description empty", {"detail.description": ""}),
    ("description 4001 characters", {"detail.description": "甲" * 4001}),
    ("need_emp.low 100", {"detail.need_emp.low": 100}),
    ("need_emp.high -2", {"detail.need_emp.high": -2}),
    ("need_emp.low a bool", {"detail.need_emp.low": True}),
    ("salary_type_id 15", {"detail.salary.salary_type_id": 15}),
    ("salary_low -1", {"detail.salary.salary_low": -1}),
    ("salary_high 10000000", {"detail.salary.salary_high": 10000000}),
    ("jobcat four entries", {"detail.jobcat": [{"no": "2007001004"}, {"no": "2007001005"},
                                               {"no": "2007001006"}, {"no": "2007001007"}]}),
    ("jobcat non-digit code", {"detail.jobcat": [{"no": "2007A01004"}]}),
    ("jobcat element carrying des", {"detail.jobcat": [{"no": "2007001004", "des": "SYNTH-DES"}]}),
    ("jobcat code not in JobCat.json", {"detail.jobcat": [{"no": "9999999999"}]}),
    ("jobcat branch code", {"detail.jobcat": [{"no": "2010001000"}]}),
    ("work_type code outside the table", {"detail.work_type": [1, 32]}),
    ("work_type repeated", {"detail.work_type": [1, 1]}),
    ("remote_work.option 3", {"detail.remote_work.option": 3}),
    ("remote_work.description 201 characters", {"detail.remote_work.description": "甲" * 201}),
    ("interval class 32", {"detail.work_time.interval": [{"type": 32, "periods": []}]}),
    ("interval start_hour 24", {"detail.work_time.interval": [
        {"type": 1, "periods": [{"start_hour": 24, "start_minute": 0, "end_hour": 18, "end_minute": 0}]}]}),
    ("interval class repeated", {"detail.work_time.interval": [
        {"type": 1, "periods": []}, {"type": 1, "periods": []}]}),
    ("shifts 4 together with 5", {"detail.shifts": [4, 5]}),
    ("shifts code 6", {"detail.shifts": [6]}),
    ("identities code 1", {"need_condition.identities": [1]}),
    ("education empty", {"need_condition.education": []}),
    ("education code 64", {"need_condition.education": [64]}),
    ("majors four entries", {"need_condition.majors": [{"no": "3011011000"}, {"no": "3011012000"},
                                                       {"no": "3011013000"}, {"no": "3011014000"}]}),
    ("majors code not in Major.json", {"need_condition.majors": [{"no": "9999999999"}]}),
    ("drivers code 3", {"need_condition.drivers": [3]}),
    ("other_condition 3001 characters", {"need_condition.other_condition": "甲" * 3001}),
    ("negotiable_salary.type 3", {"detail.negotiable_salary.type": 3}),
    ("negotiable_salary.salary_low -2", {"detail.negotiable_salary.salary_low": -2}),
]

# Changes whose refusal must also name the rejected code in its text.
_CODE_IN_MESSAGE = [
    ({"detail.work_type": [1, 32]}, "32"),
    ({"need_condition.education": [64]}, "64"),
    ({"need_condition.drivers": [3]}, "3"),
    ({"need_condition.identities": [1]}, "identities"),
    ({"detail.shifts": [6]}, "6"),
]

_UNKNOWN = [
    ("detail.nonexistent", "SYNTH-VALUE"),
    ("detail.salary", {"salary_low": 1}),
    ("detail.jobcat[0].no", "2007001004"),
]

_READ_ONLY = [
    ("switch", "off"),
    ("department.id", "7"),
    ("detail.role_id", 1),
    ("contaction.contacter", "SYNTH-VALUE"),
    ("contaction.contact_info.phone", "SYNTH-VALUE"),
    ("detail.job_address.address", "SYNTH-VALUE"),
    ("detail.salary.salary_old", "SYNTH-VALUE"),
    ("need_condition.skills", []),
    ("other_setting.close_date", ""),
]


def test_check_and_apply_changes_refuse_each_rule_and_keep_the_changed_and_unchanged_sets():
    core = _core()
    _precondition_codes()
    ex = _Expect()

    # check_changes: the static rules, one refusal each.
    for label, changes in _STATIC_INVALID:
        _change_error(ex, label, lambda changes=changes: core.check_changes(changes), "invalid")
    for changes, code in _CODE_IN_MESSAGE:
        exc = _change_error(ex, f"{list(changes)[0]}: code refusal", lambda changes=changes: core.check_changes(changes), "invalid")
        ex.equal(f"{list(changes)[0]}: message names the code {code!r}", exc is not None and code in str(exc), True)

    for path, value in _UNKNOWN:
        _change_error(ex, f"unknown path {path}", lambda path=path, value=value: core.check_changes({path: value}), "unknown")
    for path, value in _READ_ONLY:
        _change_error(ex, f"read-only path {path}", lambda path=path, value=value: core.check_changes({path: value}), "read_only")

    # check_changes: positive path resolution, and the des that comes from the packaged dataset.
    checked = core.check_changes({"job_name": "SYNTH-NEW-NAME", "detail.need_emp.low": 2})
    ex.equal("accepted changes keep the caller's order and values",
             tuple(checked.items), (("job_name", "SYNTH-NEW-NAME"), ("detail.need_emp.low", 2)))
    checked = core.check_changes({"detail.jobcat": [{"no": "2007001004"}]})
    ex.equal("jobcat des comes from JobCat.json", dict(checked.items)["detail.jobcat"],
             [{"no": "2007001004", "des": "軟體工程師"}])
    checked = core.check_changes({"need_condition.majors": [{"no": "3011011000"}]})
    ex.equal("majors des comes from Major.json", dict(checked.items)["need_condition.majors"],
             [{"no": "3011011000", "des": "機械工程相關"}])

    # apply_changes against a read.
    base = _read(_baseline())

    marked = _baseline()
    marked["detail"]["description"] = "<b>SYNTH-BOLD</b> 說明"
    _change_error(ex, "markup description",
                  lambda: core.apply_changes(_read(marked), core.check_changes({"detail.description": "SYNTH-NEW"})),
                  "markup")

    absent = _baseline()
    del absent["detail"]["remoteWork"]
    _change_error(ex, "absent remote_work.option",
                  lambda: core.apply_changes(_read(absent), core.check_changes({"detail.remote_work.option": 1})),
                  "absent")

    exc = _change_error(ex, "competitor word in job_name",
                        lambda: core.apply_changes(base, core.check_changes({"job_name": "SYNTH-COMPETITOR-A 招募"})),
                        "invalid")
    ex.equal("competitor word: message names the matched word", exc is not None and "SYNTH-COMPETITOR-A" in str(exc), True)

    # Minimum salary, pinned per type: 50 is monthly, 60 is twelve times monthly,
    # 40 is eight times hourly, 30 is hourly. Each refusal names its figure, and the figure is accepted.
    for code, figure in ((50, "28000"), (60, "336000"), (40, "1440"), (30, "180")):
        r = _baseline()
        r["detail"]["salary"] = {"salaryTypeId": code, "salaryLow": 30000, "salaryHigh": 50000,
                                 "salaryOld": "SYNTH-OLD-SALARY"}
        job = _read(r)
        below = _change_error(
            ex, f"salary code {code}: below its minimum",
            lambda job=job: core.apply_changes(job, core.check_changes(
                {"detail.salary.salary_low": 1, "detail.salary.salary_high": 9999999})),
            "invalid")
        ex.equal(f"salary code {code}: message names the minimum {figure}",
                 below is not None and figure in str(below), True)
        try:
            core.apply_changes(job, core.check_changes(
                {"detail.salary.salary_low": int(figure), "detail.salary.salary_high": 9999999}))
            accepted_at_minimum = True
        except core.ChangeError:
            accepted_at_minimum = False
        ex.equal(f"salary code {code}: the minimum {figure} itself is accepted", accepted_at_minimum, True)

    # Equal values are unchanged, not changed; the changed set holds only the real edit.
    applied = core.apply_changes(base, core.check_changes({"job_name": "SYNTH-JOB-NAME",
                                                           "detail.description": "SYNTH-NEW-DESC"}))
    ex.equal("equal value goes to unchanged_paths", applied.unchanged_paths, ("job_name",))
    ex.equal("only the real edit is in changed_paths", applied.changed_paths, ("detail.description",))
    ex.equal("changed_wire has the description field", "description" in applied.changed_wire, True)
    ex.equal("changed_wire has no field for the unchanged job_name", "job" in applied.changed_wire, False)

    # A change that does not alter the sent form is refused, whichever rule makes it so.
    _change_error(ex, "remote description with option 0",
                  lambda: core.apply_changes(base, core.check_changes({"detail.remote_work.description": "SYNTH-NEW-REMOTE"})),
                  "invalid")
    salary_type_10 = _baseline()
    salary_type_10["detail"]["salary"] = {"salaryTypeId": 10, "salaryLow": 30000, "salaryHigh": 50000,
                                          "salaryOld": "SYNTH-OLD-SALARY"}
    _change_error(ex, "salary_low under 面議",
                  lambda: core.apply_changes(_read(salary_type_10), core.check_changes({"detail.salary.salary_low": 20000})),
                  "invalid")
    role_1 = _baseline(role=1)
    _change_error(ex, "work_type under role 1",
                  lambda: core.apply_changes(_read(role_1), core.check_changes({"detail.work_type": [2]})),
                  "invalid")

    # Two changes that only work together: both reach the form.
    applied = core.apply_changes(base, core.check_changes({"detail.remote_work.option": 1,
                                                           "detail.remote_work.description": "SYNTH-NEW-REMOTE"}))
    ex.equal("both remote changes are in changed_paths",
             set(applied.changed_paths), {"detail.remote_work.option", "detail.remote_work.description"})
    form = _exact(core.build_form(applied.job, act="edit", jobno=JOBNO))
    ex.equal("option 1 reaches the form", form.get("remote_work_option"), ["1"])
    ex.equal("description reaches the form", form.get("remote_work_description"), ["SYNTH-NEW-REMOTE"])
    ex.check()


# ── T-53 (compare_forms and compare_unsent, interface) ────────────────────────────

def test_compare_forms_and_compare_unsent_split_changed_damaged_and_volatile_fields():
    core = _core()
    base = {"job": "SYNTH-JOB", "description": "SYNTH-DESC", "edu": ["2", "8"],
            "loadTime": "t1", "regId": "r1", "jobSort": '{"data":[{"reserve":[]}]}', "switch": "on"}

    same = core.compare_forms(dict(base), dict(base), changed=frozenset())
    assert same.not_applied == () and same.damaged == ()

    volatile = dict(base, loadTime="t2", regId="r2", jobSort='{"data":[{"reserve":[1]}]}')
    r = core.compare_forms(base, volatile, changed=frozenset())
    assert r.not_applied == () and r.damaged == ()

    r = core.compare_forms(base, dict(base, description="SYNTH-DESC-2"), changed=frozenset({"description"}))
    assert r.not_applied == ("description",) and r.damaged == ()

    r = core.compare_forms(base, dict(base, job="SYNTH-JOB-DAMAGED"), changed=frozenset({"description"}))
    assert r.damaged == ("job",) and r.not_applied == ()

    r = core.compare_forms(base, dict(base, switch="off"), changed=frozenset())
    assert r.damaged == ("switch",) and r.not_applied == ()

    # An array in another order is a different value (the wire sequence is compared).
    r = core.compare_forms(base, dict(base, edu=["8", "2"]), changed=frozenset())
    assert r.damaged == ("edu",) and r.not_applied == ()

    before = _read(_baseline())

    def moved_location(result):
        result["detail"]["jobAddress"]["latitude"] = 25.1
        result["department"]["name"] = "SYNTH-DEPT-NAME-2"
        result["detail"]["salary"]["salaryOld"] = "SYNTH-OLD-SALARY-2"

    after = _read_mut(moved_location)
    assert core.compare_unsent(before, after, changed_paths=()) == (
        "department.name", "detail.salary.salary_old", "detail.job_address.latitude",
    )

    def volatile_only(result):
        result["appearDate"] = "2026-02-02 00:00:00"
        result["openDate"] = "2026-02-02 00:00:00"
        result["additionalInfo"]["loadTime"] = "2026-02-02 00:00:01"

    assert core.compare_unsent(before, _read_mut(volatile_only), changed_paths=()) == ()

    def jobcat_des(result):
        result["detail"]["jobcat"][0]["des"] = "SYNTH-DES-2"

    assert core.compare_unsent(before, _read_mut(jobcat_des), changed_paths=("detail.jobcat",)) == ()

    def share_name(result):
        result["shareLink"][0]["linkName"] = "SYNTH-LINK-NAME-2"

    moved_share = _read_mut(share_name)
    assert core.compare_unsent(before, moved_share, changed_paths=("job_name",)) == ()
    assert core.compare_unsent(before, moved_share, changed_paths=()) == ("share_link",)


# ── T-54 (redact, interface) ──────────────────────────────────────────────────────

def test_redact_replaces_contact_values_by_length_and_script_longest_first():
    core = _core()
    ex = _Expect()

    # ASCII of 4 or more, every occurrence; 3-character ASCII kept; a Chinese name in a note replaced;
    # the longer value replaced whole before the shorter value inside it.
    r = _baseline()
    r["contaction"] = _contaction(contacter="SYNTHETIC-CONTACT-CONTACTER", phone="2345",
                                  cellphone="0912345678", ext="123", note="王大明")
    job = _read(r)
    text = "手機0912345678，再撥0912345678；電話2345；分機123；請找王大明"
    ex.equal("ASCII >= 4 replaced everywhere, 3-character ASCII kept, Chinese name replaced",
             core.redact(text, job),
             f"手機{CONTACT_TOKEN}，再撥{CONTACT_TOKEN}；電話{CONTACT_TOKEN}；分機123；請找{CONTACT_TOKEN}")

    # A two-character ASCII contacter is replaced.
    r = _baseline()
    r["contaction"] = _contaction(contacter="Al")
    ex.equal("two-character ASCII contacter replaced", core.redact("Al 你好", _read(r)),
             f"{CONTACT_TOKEN} 你好")

    # A two-character Chinese contacter is replaced.
    r = _baseline()
    r["contaction"] = _contaction(contacter="王小")
    ex.equal("two-character Chinese contacter replaced", core.redact("王小姐", _read(r)),
             f"{CONTACT_TOKEN}姐")

    # With no contact values at all, the text comes back as it went in.
    r = _baseline()
    r["contaction"] = _contaction()
    ex.equal("no contact values: text unchanged", core.redact("一般說明 abc 123", _read(r)),
             "一般說明 abc 123")
    ex.check()


# ── T-55 (save_outcome, interface) ────────────────────────────────────────────────

def test_save_outcome_follows_the_front_end_order_and_carries_jobno_and_reminder_flags():
    core = _core()
    ex = _Expect()
    cases = [
        ({"code": "001004", "status": "SUCCESS", "message": "", "result": {"jobno": "1000009"}},
         "create", "limit_notice"),
        ({"status": "FAILED", "message": {"description": "SYNTH-FIELD-MSG"},
          "errorFieldMap": {"description": "SYNTH-FIELD-LABEL"}}, "update", "failed_fields"),
        ({"status": "FAILED", "message": "SYNTH-PLAIN-FAILURE"}, "update", "failed"),
        ({"status": "NO_REMAIN_JOB", "message": ""}, "create", "no_remaining_job"),
        ({"status": "JOB_NOT_FOUND", "message": ""}, "update", "job_not_found"),
        ({"status": "ACCESS_DENIED", "message": ""}, "update", "unrecognised"),
        ({}, "update", "unrecognised"),
    ]
    for payload, action, expected in cases:
        ex.equal(f"outcome for status {payload.get('status', payload)!r}",
                 core.save_outcome(payload, action=action).outcome, expected)

    # SUCCESS carries result.jobno. The web-order alert and the focus isDifferent flag are both True here.
    flagged = {
        "status": "SUCCESS", "message": "",
        "result": {"jobno": "1000009", "showWebOrderPurchaseAlert": True},
        "data": {"focus": {"isDifferent": True}, "pickup": {"isDifferent": False}},
    }
    outcome = core.save_outcome(flagged, action="create")
    ex.equal("SUCCESS outcome", outcome.outcome, "success")
    ex.equal("SUCCESS carries result.jobno as hinted_jobno", outcome.hinted_jobno, "1000009")
    ex.equal("SUCCESS raises the purchase alert", outcome.purchase_alert, True)
    ex.equal("SUCCESS raises the location-differs notice", outcome.location_differs, True)

    unflagged = {"status": "SUCCESS", "message": "", "result": {"jobno": "1000010"}}
    quiet = core.save_outcome(unflagged, action="update")
    ex.equal("SUCCESS without reminders: no purchase alert", bool(quiet.purchase_alert), False)
    ex.equal("SUCCESS without reminders: location matches", bool(quiet.location_differs), False)
    ex.check()


# ── T-60 (forbidden_word_texts and forbidden_verdict, interface) ──────────────────

def test_forbidden_word_texts_keep_the_seven_segment_order_and_the_one_location_prefix():
    core = _core()
    ex = _Expect()
    job = _read(_baseline())
    base_form = {
        "job": "SYNTH-JOB", "description": "SYNTH-DESC", "address": "SYNTH-ADDR",
        "addr_no": "6001001000", "remote_work_description": "SYNTH-REMOTE", "d3": "SYNTH-FREE",
        "others": "SYNTH-OTHERS", "apply": "SYNTH-APPLY",
    }
    plain = ["SYNTH-JOB", "SYNTH-DESC", "SYNTH-ADDR", "SYNTH-REMOTE", "SYNTH-FREE", "SYNTH-OTHERS", "SYNTH-APPLY"]
    ex.equal("seven segments in order, no prefix for a non-code location",
             core.forbidden_word_texts(base_form), plain)

    for code in ("6003000000", "6003001000", "6003002000", "6003003000"):
        texts = core.forbidden_word_texts(dict(base_form, addr_no=code))
        ex.equal(f"code {code}: address gets the prefix exactly once",
                 texts[2], ABC_PREFIX + "SYNTH-ADDR")
        ex.equal(f"code {code}: other segments untouched",
                 texts[:2] + texts[3:], plain[:2] + plain[3:])

    ex.equal("empty address: no prefix",
             core.forbidden_word_texts(dict(base_form, addr_no="6003001000", address=""))[2], "")
    for addr_no in ("6003001000,6001001000", "6001001000,6003001000", "6001001000"):
        ex.equal(f"addr_no {addr_no!r}: no prefix",
                 core.forbidden_word_texts(dict(base_form, addr_no=addr_no))[2], "SYNTH-ADDR")

    clean = {"http_status": 200, "texts": [""] * 7}
    verdict = core.forbidden_verdict(clean, job=job)
    ex.equal("all-empty texts: pass", verdict.verdict, "pass")
    ex.equal("all-empty texts: no hits", list(verdict.hits), [])

    warn_texts = [""] * 7
    warn_texts[1] = "SYNTH-HIT-DESC"
    verdict = core.forbidden_verdict({"http_status": 200, "texts": warn_texts}, job=job)
    ex.equal("200 with a hit: warn", verdict.verdict, "warn")
    ex.equal("200 with a hit: the hit names the segment",
             list(verdict.hits), [{"field": "detail.description", "message": "SYNTH-HIT-DESC"}])

    trailing = [""] * 7
    trailing[1] = "SYNTH-HIT-DESC\n"
    verdict = core.forbidden_verdict({"http_status": 200, "texts": trailing}, job=job)
    ex.equal("a hit's trailing newline is not reported",
             list(verdict.hits), [{"field": "detail.description", "message": "SYNTH-HIT-DESC"}])

    block_texts = [""] * 7
    block_texts[0] = "SYNTH-BLOCK-JOB"
    verdict = core.forbidden_verdict({"http_status": 400, "texts": block_texts}, job=job)
    ex.equal("400: block", verdict.verdict, "block")
    ex.equal("400: the hit names the job-name segment",
             list(verdict.hits), [{"field": "job_name", "message": "SYNTH-BLOCK-JOB"}])

    apply_texts = [""] * 7
    apply_texts[6] = "SYNTH-APPLY-HIT"
    verdict = core.forbidden_verdict({"http_status": 200, "texts": apply_texts}, job=job)
    ex.equal("seventh segment hit carries no message", list(verdict.hits), [{"field": "應徵方式說明"}])
    ex.check()

    for bad_length in (6, 8):
        with pytest.raises(MalformedResponseError):
            core.forbidden_verdict({"http_status": 200, "texts": [""] * bad_length}, job=job)


# ── T-72 (read_job and build_form, interface): the two read shapes ─────────────────

def test_job_insert_and_job_update_shapes_both_read_and_the_copy_form_blanks_switch_and_reg_id():
    core = _core()
    ex = _Expect()
    update = _baseline()
    insert = _insert_result()
    core.read_job(update)
    core.read_job(insert)

    ex.equal("jobInsert shape: switch blank in copy form", _exact(_build(insert, act="copy")).get("switch"), [""])
    ex.equal("jobInsert shape: regId blank in copy form", _exact(_build(insert, act="copy")).get("regId"), [""])
    ex.equal("jobUpdate shape: switch read in copy form", _exact(_build(update, act="copy")).get("switch"), ["on"])
    ex.equal("jobUpdate shape: regId read in copy form", _exact(_build(update, act="copy")).get("regId"),
             ["SYNTH-REG-ID"])

    off = _insert_result()
    off["switch"] = "off"
    core.read_job(off)

    extra = _insert_result()
    extra["surpriseField"] = "SYNTH-EXTRA"
    with pytest.raises(core.JobShapeError):
        _build(extra, act="copy")

    for bad in ("x", 1):
        bad_switch = _insert_result()
        bad_switch["switch"] = bad
        with pytest.raises(core.JobShapeError):
            _build(bad_switch, act="copy")

    null_switch_update = _baseline()
    null_switch_update["switch"] = None
    with pytest.raises(core.JobShapeError):
        _build(null_switch_update, act="edit")
    ex.check()
