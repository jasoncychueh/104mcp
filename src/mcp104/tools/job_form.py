"""The job editor's form core: the pure functions behind update_job and create_job.

Answers: given the data 104's job editor loads (GET /api/job/jobUpdate or jobInsert, already
classified), what form would the editor send back on save — which read paths are accounted
for and which are refused (`read_job`, `READ_PATHS`); which fields a caller may change and
whether a change is acceptable for this job (`check_changes`, `apply_changes`); the whole form
and its wire strings (`form_fields`, `build_form`, `qs_pairs`); whether a re-read agrees with
what was sent (`compare_forms`, `compare_unsent`); how contact data is kept out of anything
returned (`redact`); what the forbidden-word check is sent and what its answer means
(`forbidden_word_texts`, `forbidden_verdict`); how a save response reads (`save_outcome`).
Deliberately does not: issue a request, decide session or auth failures, or run the throttle —
tools/helpers.py's `guarded_sequence` owns all three. Everything here takes values and returns
values or raises.

Evidence. The two reads are measured (docs/104-site-facts.md §8.31 for jobUpdate, §8.34 for
jobInsert?act=copy). Everything else — which read value feeds which form field, the
transformations between load and save, the validation rules, the forbidden-word check and the
two save routes' bodies and responses — is front-end code evidence only. Offsets are UTF-8
character indexes into 104's bundles: JE = job-editor.bc97b750.js, CC = chunk-common.c6c97b14.js,
ENT = vip25Entry.b29263c7.js, AJL = all-job-list.abf62d9f.js.

The editor sends the whole form on every save; it has no "send only what changed" path and no
"omit to keep" for contact data. A field mapped one cell wrong is therefore not an error from
104 but a real job quietly altered, which is why every read key must have a place in
`READ_PATHS`, every form field a source, and why the logs below carry paths and
classifications only, never a value.
"""

import copy
import json
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

from mcp104.tools import categories
from mcp104.tools.discovery import _snake_case
from mcp104.tools.helpers import MalformedResponseError
from mcp104.tools.jobs import build_job_detail
from mcp104.tools.search import html_to_text

log = logging.getLogger("104-mcp.job_form")

FormValue = str | int | bool | list[str | int]
Form = Mapping[str, FormValue]

REDACTION_MARK = "［聯絡資料］"

# The front end prefixes the address text with this marker before the forbidden-word check
# when the job has exactly one work location and it is one of the four codes below
# (JE@341600), and deletes it from the answer (JE@401050).
_ADDRESS_MARK = "ABC上班地點XYZ"
_MARKED_ADDR_CODES = frozenset({"6003000000", "6003001000", "6003002000", "6003003000"})

# What the editor sends as the auto-sort service's booking data when the job has no booking
# to cancel or keep: no identity fields, an empty reservation list (JE@327850 serialises the
# front end's own value down to this when there is nothing booked).
JOB_SORT_EMPTY = '{"data":[{"reserve":[]}]}'

_SALARY_ABOVE = 9999999

# Code tables, in the order the front end writes them back (an object keyed by name,
# iterated in declaration order: JE@168872–169386, JE@170580).
_WORK_TYPE_ORDER = (1, 2, 4, 8, 16)
_BUSINESS_TRIP_ORDER = (1, 2)
_EDU_ORDER = (1, 2, 4, 8, 16, 32)
_DRIVER_ORDER = (1, 2, 4, 8, 16, 32, 64, 128, 256, 512, 1024)
_APPLY_TYPE_ORDER = (1, 8, 4, 2, 16)
_SHIFT_CODES = (1, 2, 3, 4, 5)
_INTERVAL_ORDER = (1, 2, 4, 8, 16)
_IDENTITY_CODES = (2, 4, 8, 16, 32, 64, 1024, 2048, 4096, 8192, 16384, 32768, 65536, 131072)
_SALARY_TYPES = (10, 20, 30, 40, 50, 60, 70)
_NEGOTIABLE = 10


class JobShapeError(Exception):
    """The read does not match what the form mapping accounts for: a path with no entry in
    `READ_PATHS`, a mapped path of the wrong type or value, a required path that is absent,
    or a value the form cannot carry. `paths` names every problem found, never a value."""

    def __init__(self, paths: Sequence[str]):
        self.paths = tuple(paths)
        super().__init__("、".join(self.paths))


class ChangeError(Exception):
    """A caller's change is refused. `kind` is one of "unknown" (not a path of the job),
    "read_only" (a path of the job this tool does not let a caller change), "invalid" (the
    value or the combination is refused), "markup" (the stored text carries markup the
    plain-text view would erase), "absent" (this job's read has no such field). When
    several changes are refused together, `field` and `detail` join them, `kind` is the
    first one's, and `kinds` lists every refusal's kind."""

    def __init__(self, kind: str, field: str, detail: str, *, kinds: Sequence[str] | None = None):
        self.kind = kind
        self.field = field
        self.detail = detail
        self.kinds = tuple(kinds) if kinds is not None else (kind,)
        super().__init__(f"{field}：{detail}")


# ── The read mapping ─────────────────────────────────────────────────────

# Type tokens a read value can carry. "empty_list" is the empty JSON array PHP writes for an
# empty associative array; every block the front end guards with "is an object and not an
# array" accepts it and is read as absent.
_S = frozenset({"str"})
_I = frozenset({"int"})
_B = frozenset({"bool"})
_O = frozenset({"obj"})
_L = frozenset({"list"})
_N = frozenset({"null"})
_E = frozenset({"empty_list"})
_ANY = frozenset({"any"})

RowKind = Literal["map", "skip", "mode", "dict", "gate", "check"]


@dataclass(frozen=True)
class ReadPath:
    """One read path (key names after the snake_case conversion; `[]` is "every element").

    kind "map": feeds a form field (`note` names it). "skip": not sent, with the reason in
    `note`; the whole subtree is covered. "mode": an account-mode flag — `note` "false" means
    the value must be False, "absent" means the key must not be there. "dict": a code
    dictionary of 104's options, not walked key by key. "gate": read by a pre-write gate,
    not sent. "check": read for a validation rule, not sent."""

    path: str
    kind: RowKind
    types: frozenset[str]
    required: bool = True
    note: str = ""
    values: frozenset | None = None
    max_items: int | None = None


def _p(path, kind, types, note="", *, required=True, values=None, max_items=None):
    return ReadPath(path, kind, types, required, note, values, max_items)


READ_PATHS: tuple[ReadPath, ...] = (
    _p("job_name", "map", _S, "job (top level, not under detail; JE@226307)"),
    _p("switch", "map", _S | _N, "switch: the value loaded (JE@353276); null reads as \"\" for a copy",
       values=frozenset({"on", "off"})),
    _p("appear_date", "skip", _S, "server-kept date, no form cell"),
    _p("open_date", "skip", _S, "server-kept date, no form cell; jobInsert has no such key", required=False),
    _p("can_delete", "skip", _B, "capability flag"),
    _p("is_general", "skip", _B, "capability flag"),
    _p("is_hunter", "mode", _B, "false"),
    _p("is_dispatch", "mode", _B, "false"),
    _p("is_new_model", "mode", _B, "false"),
    _p("is_the_network", "mode", _B, "false"),
    _p("share_link", "skip", _L, "links of the public job page"),
    _p("scheduled_open_date", "map", _S, "d5", required=False),
    _p("department", "map", _O | _N, "department", required=False),
    _p("department.id", "map", _S | _I, "department (the id, not the code; JE@25500)"),
    _p("department.code", "skip", _S, "display only"),
    _p("department.name", "skip", _S, "display only"),
    _p("detail", "map", _O),
    _p("detail.role_id", "map", _I, "role", values=frozenset({1, 2, 3})),
    _p("detail.need_emp", "map", _O),
    _p("detail.need_emp.low", "map", _I, "need_emp, need_emp_status"),
    _p("detail.need_emp.high", "map", _I, "need_emp1, need_emp_status"),
    _p("detail.jobcat", "map", _L),
    _p("detail.jobcat[]", "map", _O),
    _p("detail.jobcat[].no", "map", _S, "jobId"),
    _p("detail.jobcat[].des", "skip", _S, "label of the code"),
    _p("detail.description", "map", _S, "description"),
    _p("detail.salary", "map", _O),
    _p("detail.salary.salary_type_id", "map", _I, "salary_type"),
    _p("detail.salary.salary_low", "map", _I | _N, "salary_low"),
    _p("detail.salary.salary_high", "map", _I | _N, "salary_high, salary_is_above"),
    _p("detail.salary.salary_old", "skip", _S, "the front end does not send it"),
    _p("detail.negotiable_salary", "map", _O),
    _p("detail.negotiable_salary.type", "map", _I | _N, "negotiable_salary_type", required=False),
    _p("detail.negotiable_salary.salary_low", "map", _I | _N, "negotiable_salary_low", required=False),
    _p("detail.negotiable_salary.salary_high", "map", _I | _N, "negotiable_salary_high", required=False),
    _p("detail.work_type", "map", _L, "worktype"),
    _p("detail.work_type[]", "map", _I),
    _p("detail.job_address", "map", _O | _E, "addr_no, city_name, address", required=False),
    _p("detail.job_address.addr_no", "map", _O | _L),
    _p("detail.job_address.addr_no.no", "map", _S, "addr_no"),
    _p("detail.job_address.addr_no.des", "map", _S, "city_name"),
    _p("detail.job_address.addr_no[]", "map", _O),
    _p("detail.job_address.addr_no[].no", "map", _S, "addr_no"),
    _p("detail.job_address.addr_no[].des", "map", _S, "city_name"),
    _p("detail.job_address.address", "map", _S, "address"),
    _p("detail.job_address.map_address", "skip", _ANY, "saved by a separate write the tool never sends"),
    _p("detail.job_address.latitude", "skip", _ANY, "saved by a separate write the tool never sends"),
    _p("detail.job_address.longitude", "skip", _ANY, "saved by a separate write the tool never sends"),
    _p("detail.industory_area", "map", _N | _L, "ind_area1"),
    _p("detail.industory_area[]", "map", _O),
    _p("detail.industory_area[].no", "map", _S, "ind_area1"),
    _p("detail.industory_area[].des", "skip", _S, "label of the code"),
    _p("detail.manage", "map", _I | _N, "manage_status", required=False),
    _p("detail.business_trip", "map", _O | _E, "business_trip, business_trip_period", required=False),
    _p("detail.business_trip.types", "map", _L, "business_trip"),
    _p("detail.business_trip.types[]", "map", _I),
    _p("detail.business_trip.period", "map", _I | _N, "business_trip_period", required=False),
    _p("detail.work_time", "map", _O),
    _p("detail.work_time.interval", "map", _L, "work_interval"),
    _p("detail.work_time.interval[]", "map", _O),
    _p("detail.work_time.interval[].type", "map", _I, "work_interval"),
    _p("detail.work_time.interval[].periods", "map", _L, "work_interval"),
    _p("detail.work_time.interval[].periods[]", "map", _O),
    _p("detail.work_time.interval[].periods[].start_hour", "map", _I | _N, "work_interval", required=False),
    _p("detail.work_time.interval[].periods[].start_minute", "map", _I | _N, "work_interval", required=False),
    _p("detail.work_time.interval[].periods[].end_hour", "map", _I | _N, "work_interval", required=False),
    _p("detail.work_time.interval[].periods[].end_minute", "map", _I | _N, "work_interval", required=False),
    _p("detail.work_time.free_key_time", "map", _S, "d3", required=False),
    _p("detail.holiday", "map", _S, "worktime"),
    _p("detail.onboard_date", "map", _I | _N, "startby", required=False),
    _p("detail.remote_work", "map", _O | _E, "remote_work_option, remote_work_description", required=False),
    _p("detail.remote_work.option", "map", _I, "remote_work_option", required=False),
    _p("detail.remote_work.description", "map", _S, "remote_work_description", required=False),
    _p("detail.shifts", "map", _L, "shifts", required=False),
    _p("detail.shifts[]", "map", _I),
    _p("detail.department", "skip", _ANY, "the form's department is the top-level one"),
    _p("detail.hiring_type", "mode", _ANY, "absent", required=False),
    _p("detail.hirer", "mode", _ANY, "absent", required=False),
    _p("need_condition", "map", _O),
    _p("need_condition.disables", "map", _O),
    _p("need_condition.disables.disable", "map", _L, "dis_role_1..3, dis_level_1..3", max_items=3),
    _p("need_condition.disables.disable[]", "map", _O),
    _p("need_condition.disables.disable[].type", "map", _I, "dis_role_N"),
    _p("need_condition.disables.disable[].level", "map", _I, "dis_level_N"),
    _p("need_condition.disables.proof", "map", _B, "handicompendium"),
    _p("need_condition.identities", "map", _L, "role_status"),
    _p("need_condition.identities[]", "map", _I),
    _p("need_condition.experience", "map", _I, "period"),
    _p("need_condition.education", "map", _L, "edu"),
    _p("need_condition.education[]", "map", _I),
    _p("need_condition.majors", "map", _L, "majorId"),
    _p("need_condition.majors[]", "map", _O),
    _p("need_condition.majors[].no", "map", _S, "majorId"),
    _p("need_condition.majors[].des", "skip", _S, "label of the code"),
    _p("need_condition.language", "map", _O | _E, "language_N, l_N, c_N, r_N, w_N, locallanno_N, t_N"),
    _p("need_condition.language.foreign", "map", _L, "language_N, l_N, c_N, r_N, w_N", required=False, max_items=3),
    _p("need_condition.language.foreign[]", "map", _O),
    _p("need_condition.language.foreign[].language", "map", _I, "language_N"),
    _p("need_condition.language.foreign[].listen", "map", _I, "l_N"),
    _p("need_condition.language.foreign[].speak", "map", _I, "c_N"),
    _p("need_condition.language.foreign[].read", "map", _I, "r_N"),
    _p("need_condition.language.foreign[].write", "map", _I, "w_N"),
    _p("need_condition.language.dialect", "map", _L, "locallanno_N, t_N", required=False, max_items=3),
    _p("need_condition.language.dialect[]", "map", _O),
    _p("need_condition.language.dialect[].language", "map", _I, "locallanno_N"),
    _p("need_condition.language.dialect[].level", "map", _I, "t_N"),
    _p("need_condition.tools", "map", _L, "specialty"),
    _p("need_condition.tools[]", "map", _O),
    _p("need_condition.tools[].no", "map", _S, "specialty"),
    _p("need_condition.tools[].des", "skip", _S, "label of the code"),
    _p("need_condition.skills", "map", _L, "skill"),
    _p("need_condition.skills[]", "map", _O),
    _p("need_condition.skills[].no", "map", _S, "skill"),
    _p("need_condition.skills[].des", "skip", _S, "label of the code"),
    _p("need_condition.licenses", "map", _L, "skill_cert"),
    _p("need_condition.licenses[]", "map", _O),
    _p("need_condition.licenses[].no", "map", _S, "skill_cert"),
    _p("need_condition.licenses[].des", "skip", _S, "label of the code"),
    _p("need_condition.drivers", "map", _L, "driver"),
    _p("need_condition.drivers[]", "map", _I),
    _p("need_condition.other_condition", "map", _S, "others"),
    _p("contaction", "map", _O),
    _p("contaction.contacter", "map", _S, "contact"),
    _p("contaction.emails", "map", _L, "email"),
    _p("contaction.emails[]", "map", _S),
    _p("contaction.using_types", "map", _L, "apply_type"),
    _p("contaction.using_types[]", "map", _I),
    _p("contaction.contact_info", "map", _O),
    _p("contaction.contact_info.phone_area", "map", _S, "phonearea"),
    _p("contaction.contact_info.phone", "map", _S, "phone"),
    _p("contaction.contact_info.ext", "map", _S, "ext"),
    _p("contaction.contact_info.cellphone", "map", _S, "cellphone"),
    _p("contaction.contact_info.apply_addr_no", "map", _N | _L, "apply_addrno"),
    _p("contaction.contact_info.apply_addr_no[]", "map", _O),
    _p("contaction.contact_info.apply_addr_no[].no", "map", _S, "apply_addrno"),
    _p("contaction.contact_info.apply_addr_no[].des", "skip", _S, "label of the code"),
    _p("contaction.contact_info.apply_addr", "map", _S, "apply_address"),
    _p("contaction.contact_info.note", "map", _S, "apply"),
    _p("other_setting", "map", _O),
    _p("other_setting.close_date", "map", _S, "autoclose, closedate (only with options.auto_close)"),
    _p("other_setting.reply", "map", _O | _E, "reply_day, d2", required=False),
    _p("other_setting.reply.days", "map", _I | _N | _S, "reply_day, d2", required=False),
    _p("other_setting.reply.no_reply", "map", _B, "d2"),
    _p("other_setting.subscript", "map", _I, "noemail"),
    _p("other_setting.due_date_count", "mode", _ANY, "absent", required=False),
    _p("additional_info", "map", _O),
    _p("additional_info.load_time", "map", _S, "loadTime"),
    _p("additional_info.reg_id", "map", _S, "regId (jobInsert has no such key; sent as \"\")", required=False),
    _p("additional_info.options", "dict", _O | _E, "only post_job, auto_close and match_email are read"),
    _p("additional_info.operations", "map", _O),
    _p("additional_info.operations.buttons", "gate", _L),
    _p("additional_info.operations.buttons[]", "gate", _O),
    _p("additional_info.operations.buttons[].link_name", "gate", _S),
    _p("additional_info.operations.buttons[].permit", "gate", _B),
    _p("additional_info.operations.buttons[].message", "gate", _S),
    _p("additional_info.operations.buttons[].url", "skip", _ANY, "button front-end setting"),
    _p("additional_info.operations.buttons[].is_ajax", "skip", _ANY, "button front-end setting"),
    _p("additional_info.operations.buttons[].is_new_tab", "skip", _ANY, "button front-end setting"),
    _p("additional_info.operations.buttons[].switch", "skip", _ANY, "button front-end setting; null on jobInsert"),
    _p("additional_info.operations.links", "skip", _ANY, "help-page links"),
    _p("additional_info.minimum_salary", "check", _O),
    _p("additional_info.minimum_salary.monthly", "check", _I),
    _p("additional_info.minimum_salary.hourly", "check", _I),
    _p("additional_info.competitors", "check", _S),
    _p("additional_info.competitors_email", "skip", _ANY, "on-screen reminders only"),
    _p("additional_info.ordinance_keywords", "skip", _ANY, "on-screen reminders only"),
    _p("additional_info.po_end_date", "skip", _ANY, "date picker bound only"),
)

_RULES: dict[str, ReadPath] = {row.path: row for row in READ_PATHS}


def _parent_path(path: str) -> str:
    if path.endswith("[]"):
        return path[:-2]
    if "." in path:
        return path.rsplit(".", 1)[0]
    return ""


def _build_children() -> dict[str, tuple[ReadPath, ...]]:
    children: dict[str, list[ReadPath]] = {}
    for row in READ_PATHS:
        if row.path.endswith("[]"):
            continue
        children.setdefault(_parent_path(row.path), []).append(row)
    return {parent: tuple(rows) for parent, rows in children.items()}


# Rows directly under an object path (list-element rows are reached through the list).
_CHILD_ROWS = _build_children()

# Names inside the options dictionary the form reads (the dictionary's other keys are 104's
# own option lists, labelled in Chinese).
_OPT_POST_JOB = "post_job"
_OPT_AUTO_CLOSE = "auto_close"
_OPT_MATCH_EMAIL = "match_email"


def _tokens(value: object) -> frozenset[str]:
    if value is None:
        return _N
    if isinstance(value, bool):
        return _B
    if isinstance(value, int):
        return _I
    if isinstance(value, float):
        return frozenset({"float"})
    if isinstance(value, str):
        return _S
    if isinstance(value, dict):
        return _O
    if isinstance(value, list):
        return _L | _E if not value else _L
    return frozenset({"other"})


def _walk(value: object, path: str, problems: list[str]) -> None:
    rule = _RULES.get(path)
    if rule is None:
        problems.append(path)
        return
    if "any" not in rule.types and not (_tokens(value) & rule.types):
        problems.append(path)
        return
    if rule.values is not None and value is not None and value not in rule.values:
        problems.append(path)
        return
    if rule.kind in ("skip", "mode", "dict"):
        return
    if isinstance(value, dict):
        for key, child in value.items():
            _walk(child, f"{path}.{key}" if path else key, problems)
        for child_rule in _CHILD_ROWS.get(path, ()):
            name = child_rule.path.rsplit(".", 1)[-1]
            if child_rule.required and name not in value:
                problems.append(child_rule.path)
    elif isinstance(value, list):
        if rule.max_items is not None and len(value) > rule.max_items:
            problems.append(path)
        for element in value:
            _walk(element, f"{path}[]", problems)


def _convert_checked(value: object, path: str, problems: list[str]):
    """convert_keys, except that two different keys landing on one name is a problem rather
    than a silent overwrite."""
    if isinstance(value, dict):
        out: dict = {}
        for key, child in value.items():
            name = _snake_case(key)
            child_path = f"{path}.{name}" if path else name
            if name in out:
                problems.append(child_path)
                continue
            out[name] = _convert_checked(child, child_path, problems)
        return out
    if isinstance(value, list):
        return [_convert_checked(element, f"{path}[]", problems) for element in value]
    return value


@dataclass(frozen=True)
class JobRead:
    """One job as the editor loaded it.

    `raw`: the read with keys converted to snake_case and every value untouched — every form
    value is taken from here, so what goes back is the bytes 104 gave (plus the conversions
    the front end itself makes). `published`: what get_job_detail publishes for the same read
    — used for showing "before" and re-read values, never for building the form (its strings
    went through html_to_text). `dropped`: (path, codes) the front end would drop on save
    because its code table does not know them. `contact_values`: the contact strings
    `redact` blanks out; it stays inside this module's callers' reach only through `redact`."""

    raw: Mapping
    published: Mapping
    dropped: tuple[tuple[str, tuple[int, ...]], ...]
    contact_values: frozenset[str]


def _lookup(tree: object, path: str) -> tuple[bool, object]:
    current = tree
    for key in path.split("."):
        if isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            return False, None
    return True, current


def _store(tree: dict, path: str, value: object) -> None:
    *parents, last = path.split(".")
    current = tree
    for key in parents:
        current = current[key]
    current[last] = value


def _collect_dropped(raw: Mapping) -> tuple[tuple[str, tuple[int, ...]], ...]:
    checks: tuple[tuple[str, Sequence[int]], ...] = (
        ("detail.work_type", _WORK_TYPE_ORDER),
        ("detail.business_trip.types", _BUSINESS_TRIP_ORDER),
        ("detail.shifts", _SHIFT_CODES),
        ("need_condition.education", _EDU_ORDER),
        ("need_condition.drivers", _DRIVER_ORDER),
        ("contaction.using_types", _APPLY_TYPE_ORDER),
    )
    found: list[tuple[str, tuple[int, ...]]] = []
    for path, known in checks:
        present, codes = _lookup(raw, path)
        if present and isinstance(codes, list):
            unknown = tuple(dict.fromkeys(code for code in codes if code not in known))
            if unknown:
                found.append((path, unknown))
    present, interval = _lookup(raw, "detail.work_time.interval")
    if present and isinstance(interval, list):
        unknown = tuple(dict.fromkeys(
            item["type"] for item in interval if item["type"] not in _INTERVAL_ORDER
        ))
        if unknown:
            found.append(("detail.work_time.interval", unknown))
    return tuple(found)


def _contact_values(raw: Mapping) -> frozenset[str]:
    """The strings of the `contaction` subtree that `redact` replaces: the contact person's
    name whatever its length, any value with a non-ASCII character (a Chinese name or
    address fragment identifies someone at any length), and ASCII values of four or more
    characters (a shorter one — an extension, "0" — identifies no one and would be replaced
    inside unrelated text)."""
    contact = raw.get("contaction")
    values: set[str] = set()

    def visit(node: object, is_contacter: bool) -> None:
        if isinstance(node, str):
            text = node.strip()
            if text and (is_contacter or not text.isascii() or len(text) >= 4):
                values.add(text)
        elif isinstance(node, Mapping):
            for key, child in node.items():
                visit(child, False)
        elif isinstance(node, list):
            for child in node:
                visit(child, False)

    if isinstance(contact, Mapping):
        for key, child in contact.items():
            visit(child, key == "contacter")
    return frozenset(values)


def read_job(result: object, *, jobno: str = "") -> JobRead:
    """The one entry through which a read reaches the form core: every key of `result` must
    be accounted for by `READ_PATHS`, every mapped value of the type the mapping needs, every
    required path present. Raises JobShapeError (all problem paths together, no values) when
    not, MalformedResponseError when `result` is not an object or its job name is empty.
    `jobno` is only the label of the published view."""
    if not isinstance(result, dict):
        raise MalformedResponseError("result 缺失或非物件")
    problems: list[str] = []
    raw = _convert_checked(result, "", problems)
    for key, child in raw.items():
        _walk(child, key, problems)
    for row in _CHILD_ROWS.get("", ()):
        if row.required and row.path not in raw:
            problems.append(row.path)
    if problems:
        paths = tuple(dict.fromkeys(problems))
        log.warning("job read does not match the form mapping: %s", "、".join(paths))
        raise JobShapeError(paths)
    published = build_job_detail(result, requested_jobno=jobno)
    return JobRead(
        raw=raw,
        published=published,
        dropped=_collect_dropped(raw),
        contact_values=_contact_values(raw),
    )


def account_mode_violations(job: JobRead) -> tuple[str, ...]:
    """The paths that say this account runs a mode the form core does not handle (hunter,
    dispatch, new-model, network accounts, due-date listing): an account flag that is not
    False, or a key that only those modes carry. Empty for a supported account."""
    found: list[str] = []
    for row in READ_PATHS:
        if row.kind != "mode":
            continue
        present, value = _lookup(job.raw, row.path)
        if row.note == "false":
            if value is not False:
                found.append(row.path)
        elif present:
            found.append(row.path)
    options = _as_obj(_get(job.raw, "additional_info", "options"))
    if _OPT_POST_JOB in options:
        found.append(f"additional_info.options.{_OPT_POST_JOB}")
    return tuple(found)


# ── Reading values out of a validated read ───────────────────────────────

def _get(obj: object, *keys: str, default=None):
    current = obj
    for key in keys:
        if isinstance(current, Mapping) and key in current:
            current = current[key]
        else:
            return default
    return current


def _as_obj(value: object) -> Mapping:
    """A block guarded by "is an object and not an array": `[]`, null and absent all read
    as an empty object."""
    return value if isinstance(value, Mapping) else {}


def _ordered(codes: Sequence[int], order: Sequence[int]) -> list[int]:
    """The codes the table knows, once each, in the table's order."""
    present = set(codes)
    return [code for code in order if code in present]


def _join_nos(items: object, key: str = "no") -> str:
    if not isinstance(items, list):
        return ""
    return ",".join(item[key] for item in items)


def _shift_codes(codes: Sequence[int]) -> list[int]:
    present = set(codes)
    out = [code for code in (1, 2, 3) if code in present]
    if 5 in present:
        out.append(5)
    elif 4 in present:
        out.append(4)
    return out


def _work_intervals(interval: Sequence[Mapping]) -> list[str]:
    """One JSON string per recognised shift, in code order; a repeated shift keeps its last
    entry (JE@215220); no recognised shift at all sends the day shift with no periods
    (JE@227560); a period with any of its four cells empty is dropped."""
    by_type: dict[int, Mapping] = {}
    for item in interval:
        if item["type"] in _INTERVAL_ORDER:
            by_type[item["type"]] = item
    if not by_type:
        return [json.dumps({"type": 1, "periods": []}, separators=(",", ":"), ensure_ascii=False)]
    out = []
    for code in _INTERVAL_ORDER:
        if code not in by_type:
            continue
        periods = []
        for period in by_type[code]["periods"]:
            cells = (
                period.get("start_hour"), period.get("start_minute"),
                period.get("end_hour"), period.get("end_minute"),
            )
            if any(cell is None for cell in cells):
                continue
            periods.append({
                "startHour": int(cells[0]), "startMinute": int(cells[1]),
                "endHour": int(cells[2]), "endMinute": int(cells[3]),
            })
        out.append(json.dumps({"type": code, "periods": periods}, separators=(",", ":"), ensure_ascii=False))
    return out


def _addr_items(addr_no: object) -> list[Mapping]:
    if isinstance(addr_no, Mapping):
        return [addr_no]
    if isinstance(addr_no, list):
        return list(addr_no)
    return []


def _is_zero_days(days: object) -> bool:
    return days == 0 or days == "0"


# ── Building the form ────────────────────────────────────────────────────

def form_fields(raw: Mapping) -> Form:
    """The form fields the read decides, in the order the editor assembles them (job
    details, conditions, contact, other settings). Not included: the fields that are the
    same for every save or belong to one request (`build_form` adds them)."""
    detail = raw["detail"]
    need = raw["need_condition"]
    contact = raw["contaction"]
    contact_info = contact["contact_info"]
    other = raw["other_setting"]
    info = raw["additional_info"]
    options = _as_obj(info.get("options"))
    role = str(detail["role_id"])

    f: dict[str, FormValue] = {}

    # job details
    f["role"] = role
    f["job"] = raw["job_name"]
    if role != "3" and need["identities"]:
        f["role_status"] = list(need["identities"])
    disables = need["disables"]["disable"]
    for i in range(3):
        f[f"dis_role_{i + 1}"] = disables[i]["type"] if i < len(disables) else 0
    for i in range(3):
        f[f"dis_level_{i + 1}"] = disables[i]["level"] if i < len(disables) else 0
    if need["disables"]["proof"]:
        f["handicompendium"] = "1"

    low, high = detail["need_emp"]["low"], detail["need_emp"]["high"]
    f["need_emp"] = str(low) if low >= 0 else ""
    f["need_emp1"] = str(high) if high >= 0 else ""
    f["need_emp_status"] = "unlimit" if low == 0 and high == 0 else ""
    f["jobId"] = _join_nos(detail["jobcat"])
    f["description"] = detail["description"]

    salary = detail["salary"]
    negotiable = salary["salary_type_id"] == _NEGOTIABLE
    above = salary["salary_high"] == _SALARY_ABOVE
    f["salary_type"] = str(salary["salary_type_id"])
    salary_low, salary_high = salary["salary_low"], salary["salary_high"]
    f["salary_low"] = salary_low if salary_low is not None and salary_low >= 0 else ""
    if salary_high is not None and 0 <= salary_high < _SALARY_ABOVE:
        f["salary_high"] = salary_high
    else:
        f["salary_high"] = ""
    ns = _as_obj(detail["negotiable_salary"])
    # JavaScript `||`: 0, null and absent all become the fallback (JE@228697)
    n_type = ns.get("type") or 0
    n_low = ns.get("salary_low") or (0 if negotiable else -1)
    n_high = ns.get("salary_high") or (0 if negotiable else -1)
    if negotiable:
        f["salary_low"] = 0
        f["salary_high"] = 0
    if above:
        if negotiable:
            n_high = 0
        else:
            del f["salary_high"]
            f["salary_is_above"] = "1"
            n_high = -1
    f["negotiable_salary_type"] = n_type
    f["negotiable_salary_low"] = n_low
    f["negotiable_salary_high"] = n_high

    if role == "2":
        work_type = _ordered(detail["work_type"], _WORK_TYPE_ORDER)
        if work_type:
            f["worktype"] = work_type

    address = detail.get("job_address")
    if isinstance(address, Mapping):
        items = _addr_items(address["addr_no"])
        f["addr_no"] = _join_nos(items)
        f["address"] = address["address"]
        f["city_name"] = _join_nos(items, "des")
    else:
        f["addr_no"] = ""
        f["address"] = ""
        f["city_name"] = ""

    remote = detail.get("remote_work")
    if isinstance(remote, Mapping) and remote.get("option"):
        f["remote_work_option"] = remote["option"]
        f["remote_work_description"] = remote.get("description") or ""
    else:
        f["remote_work_option"] = 0
        f["remote_work_description"] = ""

    f["ind_area1"] = _join_nos(detail["industory_area"]) or "0"
    manage = detail.get("manage")
    f["manage_status"] = "0" if manage is None else str(manage)

    if role != "2":
        trip = detail.get("business_trip")
        types: list[int] = []
        period = None
        if isinstance(trip, Mapping):
            types = _ordered(trip["types"], _BUSINESS_TRIP_ORDER)
            period = trip.get("period")
        if types:
            f["business_trip"] = types
        f["business_trip_period"] = "" if period is None else str(period)

    f["worktime"] = "依公司規定" if role == "3" else detail["holiday"]
    if role != "3":
        f["work_interval"] = _work_intervals(detail["work_time"]["interval"])
        f["d3"] = detail["work_time"].get("free_key_time") or ""
        shifts = _shift_codes(detail.get("shifts") or [])
        if shifts:
            f["shifts"] = shifts
    onboard = detail.get("onboard_date")
    f["startby"] = "3" if onboard is None else str(onboard)

    # conditions
    f["period"] = str(need["experience"])
    education = _ordered(need["education"], _EDU_ORDER)
    if education:
        f["edu"] = education
    f["majorId"] = _join_nos(need["majors"])
    language = need["language"]
    foreign = list(language.get("foreign") or []) if isinstance(language, Mapping) else []
    dialect = list(language.get("dialect") or []) if isinstance(language, Mapping) else []
    for key, source in (("language", "language"), ("l", "listen"), ("c", "speak"), ("r", "read"), ("w", "write")):
        for i in range(3):
            f[f"{key}_{i + 1}"] = foreign[i][source] if i < len(foreign) else 0
    for key, source in (("locallanno", "language"), ("t", "level")):
        for i in range(3):
            f[f"{key}_{i + 1}"] = dialect[i][source] if i < len(dialect) else 0
    f["specialty"] = _join_nos(need["tools"])
    f["skill"] = _join_nos(need["skills"])
    f["skill_cert"] = _join_nos(need["licenses"])
    drivers = _ordered(need["drivers"], _DRIVER_ORDER)
    if drivers:
        f["driver"] = drivers
    f["others"] = need["other_condition"]

    # contact
    department = raw.get("department")
    f["department"] = str(department["id"]) if isinstance(department, Mapping) else ""
    f["contact"] = contact["contacter"]
    f["email"] = ",".join(contact["emails"])
    reply = other.get("reply")
    days = reply.get("days") if isinstance(reply, Mapping) else None
    f["reply_day"] = "0" if days is None or days == "" else str(days)
    if isinstance(reply, Mapping) and reply["no_reply"] and not _is_zero_days(days):
        f["d2"] = "1"
    apply_type = _ordered(contact["using_types"], _APPLY_TYPE_ORDER)
    if apply_type:
        f["apply_type"] = apply_type
    f["phonearea"] = contact_info["phone_area"].strip()
    f["phone"] = contact_info["phone"].strip()
    f["ext"] = contact_info["ext"].strip()
    f["cellphone"] = contact_info["cellphone"].strip()
    f["apply_addrno"] = _join_nos(contact_info["apply_addr_no"]) or "0"
    f["apply_address"] = contact_info["apply_addr"].strip()
    f["apply"] = contact_info["note"].strip()

    # other settings
    if options.get(_OPT_AUTO_CLOSE):
        if other["close_date"]:
            raise JobShapeError(("other_setting.close_date",))
        f["autoclose"] = "0"
        f["closedate"] = ""
    if _OPT_MATCH_EMAIL in options and role != "3":
        f["noemail"] = "0" if other["subscript"] == 2 else "1"
    return f


# Fields every save carries, so that a mapping that forgets one is caught here instead of
# being sent. List-valued fields are left out of the first two sets because an empty list
# has no key on the wire.
_ALWAYS_SCALARS = frozenset({
    "role", "job", "need_emp", "need_emp1", "need_emp_status", "jobId", "description",
    "salary_type", "salary_low", "negotiable_salary_type", "negotiable_salary_low",
    "negotiable_salary_high", "addr_no", "address", "city_name", "remote_work_option",
    "remote_work_description", "ind_area1", "manage_status", "worktime", "startby", "period",
    "majorId", "specialty", "skill", "skill_cert", "others", "document_status", "department",
    "contact", "email", "reply_day", "phonearea", "phone", "ext", "cellphone", "apply_addrno",
    "apply_address", "apply", "jobSort", "loadTime", "jobNo", "switch", "act", "checkOverDate",
    "d5", "automaticReservation", "regId",
    *(f"dis_role_{i}" for i in (1, 2, 3)), *(f"dis_level_{i}" for i in (1, 2, 3)),
    *(f"{k}_{i}" for k in ("language", "l", "c", "r", "w", "locallanno", "t") for i in (1, 2, 3)),
})


def _assert_complete(form: Mapping[str, FormValue], raw: Mapping) -> None:
    role = form["role"]
    options = _as_obj(_get(raw, "additional_info", "options"))
    expected = set(_ALWAYS_SCALARS)
    if role != "2":
        expected.add("business_trip_period")
    if role != "3":
        expected.update(("d3", "work_interval"))
    if options.get(_OPT_AUTO_CLOSE):
        expected.update(("autoclose", "closedate"))
    if _OPT_MATCH_EMAIL in options and role != "3":
        expected.add("noemail")
    missing = sorted(key for key in expected if key not in form)
    if missing:
        raise JobShapeError(tuple(missing))


def build_form(job: JobRead, *, act: Literal["edit", "copy"], jobno: str) -> Form:
    """The whole form the editor sends on save, in the editor's order. `jobno` is the job
    being edited, or for a copy the source job. A null `switch` (what a copy read carries)
    is sent as an empty string for a copy and refused for an edit: an edit's progress-reset
    consent is decided on `switch == "on"`, and a null would let it be skipped unnoticed."""
    if act not in ("edit", "copy"):
        raise ValueError(f"act must be 'edit' or 'copy', got {act!r}")
    raw = job.raw
    switch = raw["switch"]
    if switch is None:
        if act == "edit":
            raise JobShapeError(("switch",))
        switch = ""
    fields = form_fields(raw)
    form: dict[str, FormValue] = {}
    for key, value in fields.items():
        if key == "department":
            form["document_status"] = 1
        form[key] = value
    scheduled = _get(raw, "scheduled_open_date")
    form["jobSort"] = JOB_SORT_EMPTY
    form["loadTime"] = raw["additional_info"]["load_time"]
    form["jobNo"] = jobno
    form["switch"] = switch
    form["act"] = act
    form["checkOverDate"] = "2"
    form["d5"] = scheduled.replace("-", "/") if isinstance(scheduled, str) and scheduled else ""
    form["automaticReservation"] = False
    form["regId"] = _get(raw, "additional_info", "reg_id") or ""
    _assert_complete(form, raw)
    return form


def qs_pairs(form: Form) -> list[tuple[str, str]]:
    """The wire strings: what `qs.stringify(form, {arrayFormat: "brackets"})` writes (CC@636617).
    Scalars as `(key, value)` (booleans "true"/"false", an empty string kept); a list as one
    `(key + "[]", element)` per element in order, an empty list writing nothing; None writes
    nothing. Any other type is a bug in the mapping and raises TypeError. Percent-encoding is
    the HTTP layer's."""
    pairs: list[tuple[str, str]] = []
    for key, value in form.items():
        if value is None:
            continue
        if isinstance(value, bool):
            pairs.append((key, "true" if value else "false"))
        elif isinstance(value, (str, int)):
            pairs.append((key, str(value)))
        elif isinstance(value, list):
            for element in value:
                if isinstance(element, bool) or not isinstance(element, (str, int)):
                    raise TypeError(f"form field {key!r} has a list element of type {type(element).__name__}")
                pairs.append((f"{key}[]", str(element)))
        else:
            raise TypeError(f"form field {key!r} has type {type(value).__name__}")
    return pairs


def _wire_values(form: Form) -> dict[str, tuple[str, ...]]:
    grouped: dict[str, list[str]] = {}
    for key, value in qs_pairs(form):
        grouped.setdefault(key[:-2] if key.endswith("[]") else key, []).append(value)
    return {key: tuple(values) for key, values in grouped.items()}


def _differing_keys(a: Form, b: Form) -> list[str]:
    left, right = _wire_values(a), _wire_values(b)
    keys = list(dict.fromkeys([*left, *right]))
    return [key for key in keys if left.get(key, ()) != right.get(key, ())]


# ── Changes ──────────────────────────────────────────────────────────────

class _Invalid(Exception):
    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _text_rule(max_len: int, *, non_empty: bool) -> Callable[[object], object]:
    def check(value: object) -> object:
        if not isinstance(value, str):
            raise _Invalid("必須是字串")
        if non_empty and not value.strip():
            raise _Invalid("不可為空")
        if len(value) > max_len:
            raise _Invalid(f"不可超過 {max_len} 字元（目前 {len(value)}）")
        return value
    return check


def _int_rule(low: int, high: int, *, allow: Sequence[int] = ()) -> Callable[[object], object]:
    def check(value: object) -> object:
        if not _is_int(value):
            raise _Invalid("必須是整數")
        if value in allow:
            return value
        if not low <= value <= high:
            raise _Invalid(f"必須在 {low}～{high} 之間")
        return value
    return check


def _choice_rule(allowed: Sequence[int]) -> Callable[[object], object]:
    def check(value: object) -> object:
        if not _is_int(value) or value not in allowed:
            raise _Invalid(f"不認得的代碼，合法值：{'、'.join(str(c) for c in allowed)}")
        return value
    return check


def _code_list_rule(allowed: Sequence[int], *, min_items: int = 0, no_pair: tuple[int, int] | None = None):
    def check(value: object) -> object:
        if not isinstance(value, list):
            raise _Invalid("必須是整數代碼的陣列")
        if not all(_is_int(code) for code in value):
            raise _Invalid("元素必須都是整數代碼")
        unknown = [code for code in value if code not in allowed]
        if unknown:
            raise _Invalid(
                f"不認得的代碼：{'、'.join(str(c) for c in unknown)}（合法代碼：{'、'.join(str(c) for c in allowed)}）"
            )
        if len(set(value)) != len(value):
            raise _Invalid("代碼不可重複")
        if len(value) < min_items:
            raise _Invalid(f"至少要有 {min_items} 個")
        if no_pair is not None and no_pair[0] in value and no_pair[1] in value:
            raise _Invalid(f"{no_pair[0]} 與 {no_pair[1]} 不能同時存在")
        return list(value)
    return check


def _leaf_of(filename: str, condition: str, code: str) -> str:
    """The name of dataset leaf `code`; _Invalid when it is not in the dataset or is a branch
    (a branch code's acceptance by the editor's selector has no evidence)."""
    # `condition` is a search-filter key; load_dataset needs one only to pick that filter's
    # branch-code acceptance, which is ignored here — only the tree is used, and a branch
    # is refused below whatever the filter accepts.
    dataset = categories.load_dataset(filename, condition)
    try:
        node = categories.path(dataset, code)[-1]
    except KeyError:
        raise _Invalid(f"代碼 {code} 不在 {filename} 裡") from None
    if node.has_children:
        raise _Invalid(f"代碼 {code} 是分支節點，只接受沒有子節點的代碼")
    return node.name


def _coded_items_rule(filename: str, condition: str, *, min_items: int, max_items: int, digits: bool):
    def check(value: object) -> object:
        if not isinstance(value, list):
            raise _Invalid("必須是 [{\"no\": 代碼}] 的陣列")
        if not min_items <= len(value) <= max_items:
            raise _Invalid(f"必須有 {min_items}～{max_items} 個")
        out = []
        seen: set[str] = set()
        for item in value:
            if not isinstance(item, dict) or set(item) != {"no"}:
                raise _Invalid("每個元素必須恰好是 {\"no\": 代碼}（名稱由本工具帶出）")
            code = item["no"]
            if not isinstance(code, str) or not code:
                raise _Invalid("no 必須是非空字串")
            if digits and not (code.isascii() and code.isdigit()):
                raise _Invalid(f"no 必須是全數字字串，收到 {code!r}")
            if code in seen:
                raise _Invalid(f"代碼 {code} 重複")
            seen.add(code)
            out.append({"no": code, "des": _leaf_of(filename, condition, code)})
        return out
    return check


_PERIOD_KEYS = ("start_hour", "start_minute", "end_hour", "end_minute")
_PERIOD_RANGES = {"start_hour": 23, "start_minute": 59, "end_hour": 23, "end_minute": 59}


def _interval_rule(value: object) -> object:
    if not isinstance(value, list):
        raise _Invalid("必須是 [{\"type\", \"periods\"}] 的陣列")
    out = []
    seen_types: set[int] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"type", "periods"}:
            raise _Invalid("每個班別必須恰好是 {\"type\", \"periods\"}")
        code = item["type"]
        if not _is_int(code) or code not in _INTERVAL_ORDER:
            raise _Invalid(f"不認得的班別代碼：{code!r}（合法代碼：1、2、4、8、16）")
        if code in seen_types:
            raise _Invalid(f"班別 {code} 重複")
        seen_types.add(code)
        if not isinstance(item["periods"], list):
            raise _Invalid("periods 必須是陣列")
        periods = []
        for period in item["periods"]:
            if not isinstance(period, dict) or set(period) != set(_PERIOD_KEYS):
                raise _Invalid("每個時段必須恰好是 start_hour、start_minute、end_hour、end_minute")
            for key in _PERIOD_KEYS:
                cell = period[key]
                if not _is_int(cell) or not 0 <= cell <= _PERIOD_RANGES[key]:
                    raise _Invalid(f"{key} 必須是 0～{_PERIOD_RANGES[key]} 的整數")
            normal = {key: period[key] for key in _PERIOD_KEYS}
            if (normal["start_hour"], normal["start_minute"]) == (normal["end_hour"], normal["end_minute"]):
                raise _Invalid("時段的起訖時間不可相同")
            if normal in periods:
                raise _Invalid("同一個班別不可有相同的時段")
            periods.append(normal)
        out.append({"type": code, "periods": periods})
    return out


def _negotiable_amount(value: object) -> object:
    if not _is_int(value) or value < -1:
        raise _Invalid("必須是 0 以上的整數，或 -1（未填）")
    return value


@dataclass(frozen=True)
class EditableField:
    """A path a caller may change. `check` validates a value without needing the job and
    returns it normalised (job category and major elements get their names); `text` marks the
    four free-text fields that are refused when they hold a competitor's word and when the
    stored text carries markup."""

    path: str
    summary: str
    check: Callable[[object], object]
    text: bool = False


def _editable_table() -> dict[str, EditableField]:
    rows = (
        EditableField("job_name", "職務名稱：非空字串，≤ 120 字元", _text_rule(120, non_empty=True), text=True),
        EditableField("detail.description", "職務說明：非空字串，≤ 4000 字元", _text_rule(4000, non_empty=True), text=True),
        EditableField("detail.need_emp.low", "需求人數下限：0～99 的整數（與 high 同為 0 表示不限）", _int_rule(0, 99)),
        EditableField("detail.need_emp.high", "需求人數上限：0～99 的整數", _int_rule(0, 99)),
        EditableField("detail.salary.salary_type_id", "薪資類型代碼：10 面議、20 論件、30 時薪、40 日薪、50 月薪、60 年薪、70 部分月薪", _choice_rule(_SALARY_TYPES)),
        EditableField("detail.salary.salary_low", "薪資下限：0～9999999 的整數", _int_rule(0, _SALARY_ABOVE)),
        EditableField("detail.salary.salary_high", "薪資上限：0～9999999 的整數（9999999 表示「以上」）", _int_rule(0, _SALARY_ABOVE)),
        EditableField("detail.negotiable_salary.type", "面議薪資類型：0 無、1 月薪、2 年薪", _choice_rule((0, 1, 2))),
        EditableField("detail.negotiable_salary.salary_low", "面議薪資下限：0 以上的整數或 -1（未填）", _negotiable_amount),
        EditableField("detail.negotiable_salary.salary_high", "面議薪資上限：0 以上的整數或 -1（未填）", _negotiable_amount),
        EditableField("detail.jobcat", "職務類別：1～3 個 [{\"no\": 代碼}]，代碼取自套件的 JobCat.json 葉節點", _coded_items_rule("JobCat.json", "jobcat", min_items=1, max_items=3, digits=True)),
        EditableField("detail.work_type", "工作型態代碼（只有兼職職缺送出）：1、2、4、8、16", _code_list_rule(_WORK_TYPE_ORDER)),
        EditableField("detail.remote_work.option", "遠端工作：0 無、1 全遠端、2 部分遠端", _choice_rule((0, 1, 2))),
        EditableField("detail.remote_work.description", "遠端工作說明：≤ 200 字元", _text_rule(200, non_empty=False), text=True),
        EditableField("detail.work_time.interval", "上班班別與時段：[{\"type\": 班別, \"periods\": [{start_hour, start_minute, end_hour, end_minute}]}]，班別代碼 1、2、4、8、16", _interval_rule),
        EditableField("detail.shifts", "輪班方式代碼（高階職缺不送出）：1～5，4 與 5 不能並存", _code_list_rule(_SHIFT_CODES, no_pair=(4, 5))),
        EditableField("need_condition.identities", "身分條件代碼（高階職缺不送出）", _code_list_rule(_IDENTITY_CODES)),
        EditableField("need_condition.education", "學歷代碼：1、2、4、8、16、32，至少一個", _code_list_rule(_EDU_ORDER, min_items=1)),
        EditableField("need_condition.majors", "科系：最多 3 個 [{\"no\": 代碼}]，代碼取自套件的 Major.json 葉節點", _coded_items_rule("Major.json", "major", min_items=0, max_items=3, digits=False)),
        EditableField("need_condition.drivers", "駕照代碼：1、2、4、…、1024", _code_list_rule(_DRIVER_ORDER)),
        EditableField("need_condition.other_condition", "其他條件：≤ 3000 字元", _text_rule(3000, non_empty=False), text=True),
    )
    return {row.path: row for row in rows}


EDITABLE: Mapping[str, EditableField] = _editable_table()
EDITABLE_PATHS: tuple[str, ...] = tuple(EDITABLE)

_READ_ONLY_REASONS: tuple[tuple[str, str], ...] = (
    ("switch", "開關請用 open_jobs／close_jobs"),
    ("detail.role_id", "職缺類別（全職、兼職、高階）在 104 編輯畫面上唯讀"),
    ("detail.job_address", "上班地點不開放修改（地點代碼的來源沒有量測），照讀到的值回送"),
    ("detail.industory_area", "產業別不開放修改，照讀到的值回送"),
    ("detail.manage", "管理責任的代碼表沒有量測對應，不開放修改，照讀到的值回送"),
    ("detail.onboard_date", "可上班日的代碼表沒有量測對應，不開放修改，照讀到的值回送"),
    ("detail.business_trip", "出差外派的期間代碼表沒有量測對應，不開放修改，照讀到的值回送"),
    ("detail.holiday", "休假制度不開放修改，照讀到的值回送"),
    ("detail.work_time.free_key_time", "時段補充說明不開放修改，照讀到的值回送"),
    ("detail.salary.salary_old", "104 前端不送這個欄位"),
    ("detail", "這個欄位不開放修改，照讀到的值回送"),
    ("department", "部門不開放修改（部門代碼的來源沒有量測），照讀到的值回送"),
    ("other_setting", "自動關閉日、回覆期限、配對信不開放修改，照讀到的值回送"),
    ("need_condition.skills", "技能代碼沒有可以檢查的資料集，不開放修改，照讀到的值回送"),
    ("need_condition.tools", "擅長工具代碼沒有可以檢查的資料集，不開放修改，照讀到的值回送"),
    ("need_condition.licenses", "證照代碼沒有可以檢查的資料集，不開放修改，照讀到的值回送"),
    ("need_condition.language", "外語與方言非空時的形狀沒有量測，不開放修改，照讀到的值回送"),
    ("need_condition.disables", "身心障礙條件非空時的形狀沒有量測，不開放修改，照讀到的值回送"),
    ("need_condition.experience", "工作經歷的代碼表沒有量測對應，不開放修改，照讀到的值回送"),
    ("need_condition", "這個欄位不開放修改，照讀到的值回送"),
    ("contaction", "聯絡資料不開放修改，只會原樣回送（本工具不讓呼叫端碰同仁的聯絡資料）"),
)


def _read_only_reason(path: str) -> str:
    for prefix, reason in _READ_ONLY_REASONS:
        if path == prefix or path.startswith(prefix + ".") or path.startswith(prefix + "["):
            return reason
    return "這個欄位不開放修改，照讀到的值回送"


def _related_editable(path: str) -> str | None:
    """The editable path above or below `path`, if there is one."""
    for editable in EDITABLE:
        if editable.startswith(path + "."):
            return editable
        if path.startswith(editable + ".") or path.startswith(editable + "["):
            return editable
    return None


def _path_error(path: str) -> tuple[str, str]:
    related = _related_editable(path)
    if related is not None:
        return "unknown", f"不是可以修改的欄位層級；可修改的路徑是 {related}"
    if path in _RULES:
        return "read_only", _read_only_reason(path)
    return "unknown", "不是這個職缺的欄位"


@dataclass(frozen=True)
class CheckedChanges:
    """Changes that passed the checks that need no job content: (path, new value) in the
    caller's order, job category and major elements carrying their dataset names."""

    items: tuple[tuple[str, object], ...]


def check_changes(changes: Mapping[str, object]) -> CheckedChanges:
    """The checks that need no job content: the path is a changeable one, the value has the
    right type, is in its code table, within its length and count, free of duplicates, and a
    job category or major is a leaf of the packaged dataset. Raises ChangeError naming every
    refused path."""
    if not isinstance(changes, Mapping):
        raise ChangeError("invalid", "changes", "必須是物件（路徑 → 新值）")
    items: list[tuple[str, object]] = []
    errors: list[tuple[str, str, str]] = []
    for key, value in changes.items():
        path = key if isinstance(key, str) else repr(key)
        spec = EDITABLE.get(path)
        if spec is None:
            kind, detail = _path_error(path)
            errors.append((kind, path, detail))
            continue
        try:
            items.append((path, spec.check(value)))
        except _Invalid as exc:
            errors.append(("invalid", path, exc.detail))
    if errors:
        if len(errors) == 1:
            raise ChangeError(*errors[0])
        raise ChangeError(
            errors[0][0],
            "、".join(error[1] for error in errors),
            "；".join(f"{error[1]}：{error[2]}" for error in errors),
            kinds=[error[0] for error in errors],
        )
    return CheckedChanges(items=tuple(items))


@dataclass(frozen=True)
class AppliedChanges:
    """`job`: the read with the changes applied. `changed_paths`: the paths that actually
    changed, in the caller's order. `unchanged_paths`: the paths whose new value equals the
    read one. `changed_wire`: the form fields whose sent value differs before and after."""

    job: JobRead
    changed_paths: tuple[str, ...]
    unchanged_paths: tuple[str, ...]
    changed_wire: frozenset[str]


def _same_value(path: str, old: object, new: object) -> bool:
    if path in ("detail.jobcat", "need_condition.majors"):
        return [item["no"] for item in old] == [item["no"] for item in new]
    return old == new


def _competitor_words(raw: Mapping) -> list[str]:
    text = _get(raw, "additional_info", "competitors", default="")
    return [word.strip() for word in text.split(",") if word.strip()]


_NO_EFFECT_DEFAULT = "這個改動不會改變送出給 104 的內容（例如只改了順序：104 前端以固定順序送出這個欄位）"


def _no_effect_reason(path: str, raw: Mapping) -> str:
    role = _get(raw, "detail", "role_id")
    if path == "detail.remote_work.description" and not _get(raw, "detail", "remote_work", "option"):
        return "沒有遠端工作時，遠端說明不會送出"
    if path in ("detail.salary.salary_low", "detail.salary.salary_high") and \
            _get(raw, "detail", "salary", "salary_type_id") == _NEGOTIABLE:
        return "面議時薪資範圍不會送出（104 前端送 0），面議金額請改 detail.negotiable_salary"
    if path == "detail.work_type" and role != 2:
        return "只有兼職職缺會送出工作型態"
    if path in ("detail.shifts", "detail.work_time.interval", "need_condition.identities") and role == 3:
        return "高階職缺不會送出這個欄位"
    return _NO_EFFECT_DEFAULT


def _cross_field_checks(raw: Mapping, changed: Sequence[str]) -> None:
    """The rules that look at the job's other content after the changes are applied."""
    words = _competitor_words(raw)
    for path in changed:
        if EDITABLE[path].text:
            _, text = _lookup(raw, path)
            hits = [word for word in words if word in text]
            if hits:
                raise ChangeError("invalid", path, f"內容含公司設定的競爭對手字詞「{'、'.join(hits)}」，建議修改以避免求職者投遞錯誤")
    detail = raw["detail"]
    if any(path.startswith("detail.need_emp.") for path in changed):
        low, high = detail["need_emp"]["low"], detail["need_emp"]["high"]
        both_zero = low == 0 and high == 0
        in_range = 1 <= low <= 99 and 1 <= high <= 99 and low <= high
        if not (both_zero or in_range):
            field = next(path for path in changed if path.startswith("detail.need_emp."))
            raise ChangeError("invalid", field, f"需求人數套用後是 {low}～{high}：必須兩者皆為 0（不限），或皆為 1～99 且下限不大於上限")
    salary = detail["salary"]
    salary_type = salary["salary_type_id"]
    salary_changed = [path for path in changed if path.startswith("detail.salary.")]
    if salary_changed and salary_type != _NEGOTIABLE:
        low, high = salary["salary_low"], salary["salary_high"]
        field = salary_changed[0]
        if high is not None and low is not None and high != _SALARY_ABOVE and low > high:
            raise ChangeError("invalid", field, f"最低工作待遇 {low} 不可大於最高工作待遇 {high}")
        minimum = _get(raw, "additional_info", "minimum_salary", default={})
        monthly, hourly = minimum.get("monthly"), minimum.get("hourly")
        limits = {
            50: ("月薪", monthly), 60: ("年薪", 12 * monthly if monthly is not None else None),
            40: ("日薪", 8 * hourly if hourly is not None else None), 30: ("時薪", hourly),
        }
        label, floor = limits.get(salary_type, (None, None))
        if floor is not None and low is not None and low < floor:
            raise ChangeError("invalid", field, f"依勞動部規定，{label}最低 {floor} 元，下限 {low} 低於它")
    negotiable_changed = [
        path for path in changed
        if path.startswith("detail.negotiable_salary.") or path == "detail.salary.salary_type_id"
    ]
    if negotiable_changed and salary_type == _NEGOTIABLE:
        ns = _as_obj(detail["negotiable_salary"])
        n_type, n_low = ns.get("type"), ns.get("salary_low")
        floor = {1: 40000, 2: 480000}.get(n_type)
        if floor is not None and isinstance(n_low, int) and n_low < floor:
            raise ChangeError("invalid", negotiable_changed[0], f"依勞動部規定，面議需經常性薪資達 4 萬元（面議月薪最低 40000、年薪最低 480000），目前 {n_low}")
    if "detail.work_time.interval" in changed and detail["role_id"] in (1, 2) and not detail["work_time"]["interval"]:
        raise ChangeError("invalid", "detail.work_time.interval", "全職與兼職職缺至少要有一個班別")


def apply_changes(job: JobRead, checked: CheckedChanges) -> AppliedChanges:
    """Apply the changes to this job's content, checking what only the content can decide:
    the field exists on this job, the stored text carries no markup the plain-text view would
    erase, no competitor's word, the number and salary rules, and that each change really
    alters what is sent. Raises ChangeError (kind "absent", "markup" or "invalid")."""
    raw = copy.deepcopy(job.raw)
    published = copy.deepcopy(job.published)
    changed: list[str] = []
    unchanged: list[str] = []
    originals: dict[str, object] = {}
    for path, new in checked.items:
        present, old = _lookup(job.raw, path)
        if not present:
            raise ChangeError("absent", path, "這個職缺的讀取沒有這個欄位，無法修改")
        if EDITABLE[path].text and isinstance(old, str) and html_to_text(old) != old:
            raise ChangeError("markup", path, "104 存的這段文字帶有標記（HTML），get_job_detail 顯示的是轉成純文字的版本，以它為底稿改寫會抹掉原本的格式；請到 104 後台修改這個欄位")
        if _same_value(path, old, new):
            unchanged.append(path)
            continue
        originals[path] = old
        changed.append(path)
        _store(raw, path, copy.deepcopy(new))
        present_published, _ = _lookup(published, path)
        if present_published:
            _store(published, path, copy.deepcopy(new))
    _cross_field_checks(raw, changed)
    for path in changed:
        reverted = copy.deepcopy(raw)
        _store(reverted, path, copy.deepcopy(originals[path]))
        if not _differing_keys(form_fields(raw), form_fields(reverted)):
            raise ChangeError("invalid", path, _no_effect_reason(path, raw))
    changed_wire = frozenset(_differing_keys(form_fields(job.raw), form_fields(raw)))
    applied = JobRead(
        raw=raw,
        published=published,
        dropped=_collect_dropped(raw),
        contact_values=job.contact_values,
    )
    return AppliedChanges(
        job=applied,
        changed_paths=tuple(changed),
        unchanged_paths=tuple(unchanged),
        changed_wire=changed_wire,
    )


# ── Comparing a re-read with what was sent ───────────────────────────────

# Fields that differ on every request or every read, so they are never compared: the load
# time changes with each read, the registration id may be recomputed after a category
# change, and the rest are the same for every save or belong to one request.
_NEVER_COMPARED = frozenset({
    "loadTime", "regId", "jobSort", "automaticReservation", "act", "checkOverDate", "jobNo",
})


@dataclass(frozen=True)
class FormComparison:
    """Form fields (without the list suffix) whose re-read value differs from the sent one:
    `not_applied` when the caller changed them, `damaged` when the caller did not."""

    not_applied: tuple[str, ...]
    damaged: tuple[str, ...]


def compare_forms(sent: Form, read_back: Form, *, changed: frozenset[str]) -> FormComparison:
    """Compare two `build_form` results field by field on their wire strings (so a different
    array order counts as different), leaving out the fields in `_NEVER_COMPARED`."""
    differing = [key for key in _differing_keys(sent, read_back) if key not in _NEVER_COMPARED]
    return FormComparison(
        not_applied=tuple(key for key in differing if key in changed),
        damaged=tuple(key for key in differing if key not in changed),
    )


# Paths the save may move on 104's side, so they are not compared.
_UNSENT_MAY_CHANGE = frozenset({"appear_date", "open_date"})


def _extract(tree: object, path: str) -> list[object]:
    """Every value at `path`; `[]` walks the elements. A missing part contributes a marker so
    that a path present on one side only compares as different."""
    nodes: list[object] = [tree]
    for part in path.split("."):
        each = part.endswith("[]")
        key = part[:-2] if each else part
        next_nodes: list[object] = []
        for node in nodes:
            value = node[key] if isinstance(node, Mapping) and key in node else _MISSING
            if each:
                next_nodes.extend(value if isinstance(value, list) else [_MISSING])
            else:
                next_nodes.append(value)
        nodes = next_nodes
    return nodes


_MISSING = object()


def compare_unsent(before: JobRead, after: JobRead, *, changed_paths: Sequence[str]) -> tuple[str, ...]:
    """The "not sent" paths whose re-read value differs from the one before the change —
    the form comparison cannot see them, and a mapping pointed the wrong way could show up
    there (for example 104 recomputing map coordinates from a sent address). Not compared:
    the server-kept dates, `additional_info` (editor and account settings, not job content),
    the labels of a code the caller changed, and `share_link` when the job name changed (the
    link names may carry it). Paths come back in `READ_PATHS` order; a list is compared as a
    whole subtree."""
    covered = tuple(changed_paths)
    out: list[str] = []
    for row in READ_PATHS:
        if row.kind != "skip" or row.path.startswith("additional_info"):
            continue
        if row.path in _UNSENT_MAY_CHANGE:
            continue
        if any(row.path.startswith(path + ".") or row.path.startswith(path + "[") for path in covered):
            continue
        if row.path == "share_link" and "job_name" in covered:
            continue
        left, right = _extract(before.raw, row.path), _extract(after.raw, row.path)
        if len(left) != len(right) or any(
            (a is _MISSING) != (b is _MISSING) or (a is not _MISSING and a != b) for a, b in zip(left, right)
        ):
            out.append(row.path)
    return tuple(out)


# A form field's published name when its source is a path get_job_detail publishes, else a
# fixed label: contact data, reply window, match mail and editor data are never named by their
# path and never come with a value.
_PUBLISHED_FIELDS: dict[str, str] = {
    "role": "detail.role_id", "job": "job_name", "role_status": "need_condition.identities",
    "handicompendium": "need_condition.disables.proof", "need_emp": "detail.need_emp.low",
    "need_emp1": "detail.need_emp.high", "need_emp_status": "detail.need_emp",
    "jobId": "detail.jobcat", "description": "detail.description",
    "salary_type": "detail.salary.salary_type_id", "salary_low": "detail.salary.salary_low",
    "salary_high": "detail.salary.salary_high", "salary_is_above": "detail.salary.salary_high",
    "negotiable_salary_type": "detail.negotiable_salary.type",
    "negotiable_salary_low": "detail.negotiable_salary.salary_low",
    "negotiable_salary_high": "detail.negotiable_salary.salary_high",
    "worktype": "detail.work_type", "addr_no": "detail.job_address.addr_no",
    "city_name": "detail.job_address.addr_no", "address": "detail.job_address.address",
    "remote_work_option": "detail.remote_work.option",
    "remote_work_description": "detail.remote_work.description",
    "ind_area1": "detail.industory_area", "manage_status": "detail.manage",
    "business_trip": "detail.business_trip.types",
    "business_trip_period": "detail.business_trip.period", "worktime": "detail.holiday",
    "work_interval": "detail.work_time.interval", "d3": "detail.work_time.free_key_time",
    "shifts": "detail.shifts", "startby": "detail.onboard_date",
    "period": "need_condition.experience", "edu": "need_condition.education",
    "majorId": "need_condition.majors", "specialty": "need_condition.tools",
    "skill": "need_condition.skills", "skill_cert": "need_condition.licenses",
    "driver": "need_condition.drivers", "others": "need_condition.other_condition",
    "department": "department.id", "switch": "switch",
    "autoclose": "other_setting.close_date", "closedate": "other_setting.close_date",
    **{f"dis_role_{i}": "need_condition.disables.disable" for i in (1, 2, 3)},
    **{f"dis_level_{i}": "need_condition.disables.disable" for i in (1, 2, 3)},
    **{f"{k}_{i}": "need_condition.language" for k in ("language", "l", "c", "r", "w", "locallanno", "t") for i in (1, 2, 3)},
}
_FIELD_LABELS: dict[str, str] = {
    "contact": "聯絡人", "email": "聯絡 E-mail", "apply_type": "應徵方式",
    "phonearea": "聯絡電話", "phone": "聯絡電話", "ext": "聯絡電話", "cellphone": "聯絡電話",
    "apply_addrno": "應徵地點", "apply_address": "應徵地點", "apply": "應徵方式說明",
    "reply_day": "回覆期限", "d2": "回覆期限", "noemail": "配對信", "d5": "預約開啟日",
    "document_status": "文件狀態", "loadTime": "編輯器資料", "regId": "編輯器資料",
    "jobSort": "送出參數", "jobNo": "送出參數", "act": "送出參數", "checkOverDate": "送出參數",
    "automaticReservation": "送出參數",
}


def _bare(wire: str) -> str:
    return wire[:-2] if wire.endswith("[]") else wire


def field_label(wire: str) -> str:
    """The name a returned message uses for a form field: its published path when the field
    comes from one, a fixed label when it is contact data or editor data, the field's own
    name when it is not one this tool sends (104 has error keys of its own)."""
    key = _bare(wire)
    return _PUBLISHED_FIELDS.get(key) or _FIELD_LABELS.get(key) or key


def field_is_published(wire: str) -> bool:
    """Whether the form field's value comes from a path get_job_detail publishes — the only
    fields whose text may be quoted back."""
    return _bare(wire) in _PUBLISHED_FIELDS


# ── Contact data ─────────────────────────────────────────────────────────

def redact(text: str, job: JobRead) -> str:
    """`text` with every occurrence of this job's contact strings replaced by the redaction
    mark. Run on any wording that came from 104 (error messages, field errors, forbidden-word
    reminders, button reasons) before it is returned or quoted; longer values go first so a
    short value does not cut a long one in two."""
    for value in sorted(job.contact_values, key=len, reverse=True):
        text = text.replace(value, REDACTION_MARK)
    return text


# ── The forbidden-word check ─────────────────────────────────────────────

_FORBIDDEN_SEGMENT_NAMES = (
    "job_name", "detail.description", "detail.job_address.address", "detail.remote_work.description",
    "detail.work_time.free_key_time", "need_condition.other_condition", "應徵方式說明",
)


def forbidden_word_texts(form: Form) -> list[str]:
    """The seven texts the front end sends the forbidden-word check, in its order (JE@341665):
    job name, description, address, remote-work description, time-slot note, other conditions,
    application note. The address gets the marker prefix when the whole `addr_no` is exactly
    one of four codes and the address is not empty (JE@341600)."""
    address = str(form.get("address", ""))
    if str(form.get("addr_no", "")) in _MARKED_ADDR_CODES and address:
        address = _ADDRESS_MARK + address
    return [
        str(form.get("job", "")), str(form.get("description", "")), address,
        str(form.get("remote_work_description", "")), str(form.get("d3", "")),
        str(form.get("others", "")), str(form.get("apply", "")),
    ]


@dataclass(frozen=True)
class ForbiddenVerdict:
    """`verdict`: "pass" (every answer empty), "warn" (a non-empty answer on HTTP 200) or
    "block" (HTTP 400). `hits`: one {"field", "message"?} per non-empty answer, the message
    redacted; the application note's answer is named but never quoted."""

    verdict: Literal["pass", "warn", "block"]
    hits: tuple[dict, ...]

    @property
    def kind(self) -> str:
        return self.verdict


def forbidden_verdict(payload: Mapping, *, job: JobRead) -> ForbiddenVerdict:
    """The verdict of a classified forbidden-word answer (`{"http_status", "texts"}`).
    Raises MalformedResponseError when the answer does not have one text per sent text."""
    texts = payload.get("texts") if isinstance(payload, Mapping) else None
    if not isinstance(texts, list) or len(texts) != len(_FORBIDDEN_SEGMENT_NAMES) \
            or not all(isinstance(text, str) for text in texts):
        raise MalformedResponseError("禁用詞檢查的回應不是 7 段文字")
    cleaned = [text.replace(_ADDRESS_MARK, "") for text in texts]
    hits: list[dict] = []
    for index, text in enumerate(cleaned):
        if not text:
            continue
        hit = {"field": _FORBIDDEN_SEGMENT_NAMES[index]}
        if index != len(cleaned) - 1:
            hit["message"] = redact(text, job).rstrip()
        hits.append(hit)
    if not hits:
        return ForbiddenVerdict("pass", ())
    if payload.get("http_status") == 400:
        return ForbiddenVerdict("block", tuple(hits))
    return ForbiddenVerdict("warn", tuple(hits))


# ── The save response ────────────────────────────────────────────────────

# 104's own wording for the three codes that make the front end show a quota dialog
# (JE@166397, JE@131800). Whether a new job was created in the 001006 case is not visible in
# the front end's code.
LIMIT_NOTICE_TEXT: Mapping[str, str] = {
    "001001": "104 回報：職務已達可開啟的數量上限",
    "001004": "104 回報：職務新增完成，但已達可開啟的數量上限，無法曝光職務——新職缺可能已建立，但沒有上線",
    "001006": "104 回報：職務數已達上限——是否已建立新職缺從前端碼看不出來，以重讀為準",
}
_PURCHASE_ALERT_TEXT = "104 回報：刊登天數已用完，無法曝光職務"
_LOCATION_DIFFERS_TEXT = "104 回報：加強曝光地點與職缺不同"

SaveKind = Literal[
    "limit_notice", "failed_fields", "failed", "success", "no_remaining_job", "job_not_found", "unrecognised",
]


@dataclass(frozen=True)
class SaveOutcome:
    """How a save response reads. `outcome` is one of the `SaveKind` values; `kind` is the
    same value. `code`: the response code for "limit_notice". `message`: 104's text when it
    is a string. `field_errors`: (field key, 104's label, 104's text or None) for
    "failed_fields". `hinted_jobno`: a create success's `result.jobno` when it is all digits.
    `purchase_alert`, `location_differs`: the two flags the front end alerts on after a
    success. `notices`: the reminder wording for "limit_notice" and the flags."""

    outcome: SaveKind
    code: str | None = None
    message: str | None = None
    field_errors: tuple[tuple[str, str, str | None], ...] = ()
    hinted_jobno: str | None = None
    purchase_alert: bool = False
    location_differs: bool = False
    notices: tuple[str, ...] = ()

    @property
    def kind(self) -> str:
        return self.outcome


def save_outcome(payload: Mapping, *, action: Literal["update", "create"]) -> SaveOutcome:
    """The first matching case of the front end's response handling wins (JE@355680): a
    quota code, a failure with field errors, a failure, a success, no remaining job, job not
    found, anything else (including ACCESS_DENIED)."""
    if not isinstance(payload, Mapping):
        return SaveOutcome("unrecognised")
    status = payload.get("status")
    message = payload.get("message")
    text = message if isinstance(message, str) else None
    code = payload.get("code")
    if isinstance(code, str) and code in LIMIT_NOTICE_TEXT:
        return SaveOutcome("limit_notice", code=code, message=text, notices=(LIMIT_NOTICE_TEXT[code],))
    if status == "FAILED":
        field_map = payload.get("errorFieldMap")
        if isinstance(field_map, Mapping) and field_map:
            message_keys = list(message) if isinstance(message, Mapping) else []
            keys = message_keys + [key for key in field_map if key not in message_keys]
            rows = []
            for key in keys:
                label = field_map.get(key)
                detail = message.get(key) if isinstance(message, Mapping) else None
                rows.append((
                    str(key),
                    label if isinstance(label, str) else "",
                    detail if isinstance(detail, str) else None,
                ))
            return SaveOutcome("failed_fields", message=text, field_errors=tuple(rows))
        return SaveOutcome("failed", message=text)
    if status == "SUCCESS":
        result = payload.get("result")
        data = payload.get("data")
        purchase = isinstance(result, Mapping) and bool(result.get("showWebOrderPurchaseAlert"))
        location = isinstance(data, Mapping) and any(
            isinstance(data.get(key), Mapping) and bool(data[key].get("isDifferent"))
            for key in ("focus", "pickup")
        )
        hinted = None
        if action == "create" and isinstance(result, Mapping):
            jobno = result.get("jobno")
            if isinstance(jobno, str) and jobno.isascii() and jobno.isdigit():
                hinted = jobno
        notices = tuple(
            notice for flag, notice in ((purchase, _PURCHASE_ALERT_TEXT), (location, _LOCATION_DIFFERS_TEXT)) if flag
        )
        return SaveOutcome(
            "success", message=text, hinted_jobno=hinted,
            purchase_alert=purchase, location_differs=location, notices=notices,
        )
    if status == "NO_REMAIN_JOB":
        return SaveOutcome("no_remaining_job", message=text)
    if status == "JOB_NOT_FOUND":
        return SaveOutcome("job_not_found", message=text)
    return SaveOutcome("unrecognised", message=text)


def failed_field_rows(outcome: SaveOutcome, job: JobRead) -> list[dict]:
    """`failed_fields` as returned to the caller: {"field", "label", "message"?} per key.
    The label and message are redacted; a message is attached only when the field's source
    is a published path — contact fields, unpublished fields and keys this tool does not
    recognise (104 has error keys such as `apply_type_tel` that are in no form) get none,
    because a key that cannot be placed might be contact data."""
    rows = []
    for key, label, detail in outcome.field_errors:
        row = {"field": field_label(key), "label": redact(label, job)}
        if detail is not None and field_is_published(key):
            row["message"] = redact(detail, job)
        rows.append(row)
    return rows
