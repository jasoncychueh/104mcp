"""The job editor tools: update_job (change a job's posting) and create_job (add a job by
copying an existing one), with the gates and the outer shell they share.

Answers: in what order each tool talks to 104 (job list, the editor's read, the forbidden-word
check, the save, the re-read), which states make 104's own front end refuse the operation and so
make this tool refuse it with the front end's words, how the checks, the save response and the
re-read are turned into one of the closed return shapes, and what each tool's description says.
Deliberately does not: map read values to form fields, check or apply a caller's changes,
compare forms, redact contact data or interpret a save response — tools/job_form.py's pure
functions own all of that — nor issue HTTP requests, decide session failures or run the
throttle: tools/helpers.py's `guarded_sequence` owns those. The tools only check their arguments,
go through the guard in order and hand each response to a pure function.

Evidence: the job list, jobUpdate, jobInsert and job/more reads are measured; the mapping from
read values to the saved form, the forbidden-word check and the two save routes' bodies and
responses are front-end code evidence only (docs/104-site-facts.md and the module docstring of
job_form.py). Whether a save took effect is therefore read from a measured route — the re-read
of the job (update) or of the job list (create) — never from the save's own response. The logs
here carry paths and classifications, never a value.

Must never gain `from __future__ import annotations`: that import turns annotations into
strings, so mcp's `issubclass(param.annotation, Context)` check silently fails and `ctx` leaks
into the published inputSchema.
"""

import json
import logging
import os
import re
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal

from mcp.server.fastmcp import Context, FastMCP

from mcp104.browser.api_client import ENDPOINTS
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    guarded_sequence,
    jobno_error,
    malformed_response_payload,
)
from mcp104.tools.registration import register_tool
from mcp104.tools.job_form import (
    EDITABLE_PATHS,
    AppliedChanges,
    CheckedChanges,
    ChangeError,
    ForbiddenVerdict,
    JobRead,
    JobShapeError,
    SaveOutcome,
    account_mode_violations,
    apply_changes,
    build_form,
    check_changes,
    compare_forms,
    compare_unsent,
    failed_field_rows,
    field_label,
    forbidden_verdict,
    forbidden_word_texts,
    form_fields,
    qs_pairs,
    read_job,
    redact,
    save_outcome,
)
from mcp104.tools.job_switch import NO_PERMISSION, WEB_NO_MORE_MENU, no_job_points, truthy
from mcp104.tools.jobs import (
    JobList,
    JobListRow,
    parse_job_list,
    permit_state,
    write_failure_disposition,
)

log = logging.getLogger("104-mcp.job_edit")

_NO_JOB_POINTS = "您目前可以使用的職務數為0"
_VERIFYING = "職務確認中，目前無法修改"
_ADD_ON_LABELS = {
    "focus": "焦點職缺",
    "pick_up": "精選工作",
    "priority": "優先推薦",
    "job_sort": "自動排序",
}
_UNEDITABLE_KINDS = ("unknown", "read_only")


# ── Run state and the outer shell ────────────────────────────────────────


@dataclass
class _Run:
    """What one tool call has learned so far that the outer shell needs: whether a write was
    attempted, the job read last (its contact strings are what `redact` blanks out of any
    wording that came from 104), and the warnings collected."""

    action: str  # "修改" | "新增"
    write_attempted: bool = False
    job: JobRead | None = None
    warnings: list[str] = field(default_factory=list)
    rows: list[dict] = field(default_factory=list)

    def redacted(self, text: str) -> str:
        return redact(text, self.job) if self.job is not None else text


def _abort_payload(exc: GuardAbort, run: _Run) -> dict:
    """The abort's own payload, its error wording redacted."""
    payload = dict(exc.payload)
    error = payload.get("error")
    if isinstance(error, str):
        payload["error"] = run.redacted(error)
    return payload


def _location(exc: BaseException) -> str:
    """Where an exception came from, without its message (a message can carry a value)."""
    frames = traceback.extract_tb(exc.__traceback__)
    if not frames:
        return type(exc).__name__
    last = frames[-1]
    return f"{type(exc).__name__} at {os.path.basename(last.filename)}:{last.lineno} in {last.name}"


def _change_error_payload(exc: ChangeError, *, stage: str) -> dict:
    payload: dict = {"error": f"{exc.field}：{exc.detail}。{stage}"}
    if any(kind in _UNEDITABLE_KINDS for kind in exc.kinds):
        payload["editable_fields"] = list(EDITABLE_PATHS)
    return payload


def _pre_write_error(exc: Exception, run: _Run, tool: str) -> dict:
    """The error shape for a failure before anything was written."""
    if isinstance(exc, ChangeError):
        return _change_error_payload(exc, stage=f"沒有送出{run.action}")
    if isinstance(exc, JobShapeError):
        return {
            "error": run.redacted(
                f"104 的職缺資料形狀與本工具的對映不一致（{'、'.join(exc.paths)}），可能是 104 介面已變更，請回報。"
                f"沒有送出{run.action}"
            )
        }
    if isinstance(exc, MalformedResponseError):
        log.error("%s: 回應結構異常: %s", tool, exc.detail)
        return malformed_response_payload(exc)
    log.error("%s: 非預期例外 (%s)", tool, _location(exc))
    return {"error": f"內部錯誤，這是程式問題，請回報 —— 沒有送出{run.action}"}


def _failure_reason(exc: Exception, run: _Run) -> str:
    if isinstance(exc, GuardAbort):
        return run.redacted(str(exc.payload.get("error")))
    if isinstance(exc, JobShapeError):
        return f"讀取的形狀與對映不一致（{'、'.join(exc.paths)}）"
    if isinstance(exc, MalformedResponseError):
        return f"回應結構異常（{exc.detail}）"
    return type(exc).__name__


# ── Gates: what 104's own front end refuses ──────────────────────────────


def _is_one_loose(value: object) -> bool:
    """`value == 1` the way the front end compares it (a numeric string counts)."""
    if isinstance(value, bool):
        return False
    return value == 1 or value == "1"


def _verifying(row: JobListRow) -> bool:
    return row.switch == "new" or _is_one_loose(row.is_in_verify)


def _permit_refusal(permit_read, name: str) -> str | None:
    """The refusal for a permit entry that is not granted, or None when it is."""
    state = permit_read
    if state.state == "allowed":
        return None
    if state.state == "denied":
        reason = f"（104 回報：{state.message}）" if state.message else ""
        return f"{NO_PERMISSION}{reason}"
    return (
        f"{NO_PERMISSION}。權限資訊（{name}）缺席或認不出來，本工具不猜測；"
        "104 網頁在這個狀態沒有提供這個操作，請到 104 後台"
    )


_NUMERIC_TEXT = re.compile(r"-?[0-9]+(\.[0-9]+)?")


def _count(entry: object) -> str:
    """How an add-on table entry reads in the refusal: a number, or a numeric string, as 104
    gave it (it is the count of ads); the length only for a list or object."""
    if isinstance(entry, (list, dict)):
        return str(len(entry))
    if isinstance(entry, str) and _NUMERIC_TEXT.fullmatch(entry.strip()):
        return entry.strip()
    return str(entry)


def update_list_gate(job_list: JobList, row: JobListRow) -> str | None:
    """The refusal for a job the job list shows 104's front end would not let be edited, or
    None. The web-only check is the caller's, ahead of the lookup."""
    refusal = _permit_refusal(permit_state(job_list.account.permit, "job__5"), "job__5")
    if refusal is not None:
        return refusal
    if _verifying(row):
        return _VERIFYING
    add_ons = job_list.add_ons(row.jobno)
    if add_ons:
        kinds = "、".join(f"{_ADD_ON_LABELS[name]} {_count(entry)} 則" for name, entry in add_ons.items())
        return (
            f"這個職缺掛有加值廣告（{kinds}）：104 前端修改前會先查加值廣告的歸還或改動預約，"
            "本工具沒有實作這段流程，請到 104 後台修改這個職缺"
        )
    if _is_one_loose(row.fields.get("autoclose")) or truthy(row.fields.get("closedate")):
        return "這個職缺設定了自動關閉日：104 前端修改前會先查加購衝突，本工具沒有實作，請到 104 後台修改這個職缺"
    if truthy(row.fields.get("d5")):
        return "這個職缺設定了預約開啟日：本工具沒有實作預約開啟的排序資料，請到 104 後台修改這個職缺"
    return None


def more_gate(more: object) -> str | None:
    """The refusal for the copy menu of one job, read from job/more's `result`. Raises
    MalformedResponseError when `result` or `result.render` is not an object."""
    if not isinstance(more, Mapping):
        raise MalformedResponseError("job/more 的 result 缺失或非物件")
    render = more.get("render")
    if not isinstance(render, Mapping):
        raise MalformedResponseError("job/more 的 result.render 缺失或非物件")
    refusal = _permit_refusal(permit_state(more.get("permit"), "job__4"), "job__4")
    if refusal is not None:
        return refusal
    if truthy(render.get("isNewModel")) and no_job_points(render.get("poNum")):
        return _NO_JOB_POINTS
    return None


def _buttons(job: JobRead) -> list[Mapping]:
    info = job.raw.get("additional_info")
    operations = info.get("operations") if isinstance(info, Mapping) else None
    buttons = operations.get("buttons") if isinstance(operations, Mapping) else None
    return [button for button in buttons if isinstance(button, Mapping)] if isinstance(buttons, list) else []


def _nested(job: JobRead, *keys: str) -> object:
    current: object = job.raw
    for key in keys:
        if not isinstance(current, Mapping):
            return None
        current = current.get(key)
    return current


def read_gate(job: JobRead, *, act: str) -> str | None:
    """The refusal for what the editor's own read says: the confirm button 104 would not
    show or enable, an account mode the form core does not handle, a close date, a scheduled
    opening (edit only) and an empty work location."""
    if act == "edit":
        entry = next((b for b in _buttons(job) if b.get("link_name") == "jobUpdate"), None)
        if entry is None:
            return "104 的編輯資料沒有「確定修改」按鈕，104 網頁在這個狀態沒有提供修改這個職缺的操作，請到 104 後台修改"
        if entry.get("permit") is not True:
            message = entry.get("message")
            reason = f"（104 回報：{message}）" if isinstance(message, str) and message else ""
            return f"104 的編輯器停用了「確定修改」{reason}，請到 104 後台修改"
    elif not any(b.get("link_name") == "jobInsert" for b in _buttons(job)):
        return "104 的預填資料沒有「確定新增」按鈕，這個職缺在 104 前端不能複製，請到 104 後台複製"
    violations = account_mode_violations(job)
    if violations:
        return (
            f"這個帳號的模式（{'、'.join(violations)}）本工具不支援（獵頭、派遣、新制或刊登期數帳號），"
            "請到 104 後台操作"
        )
    if truthy(_nested(job, "other_setting", "close_date")):
        if act == "edit":
            return "這個職缺設定了自動關閉日：104 前端修改前會先查加購衝突，本工具沒有實作，請到 104 後台修改這個職缺"
        return "底稿職缺設定了自動關閉日：複製時前端會帶著它，本工具只送空的自動關閉日，兩者不等價，請到 104 後台複製這個職缺"
    if act == "edit" and truthy(_nested(job, "scheduled_open_date")):
        return "這個職缺設定了預約開啟日：本工具沒有實作預約開啟的排序資料，請到 104 後台修改這個職缺"
    if not form_fields(job.raw)["addr_no"]:
        return "這個職缺的上班地點是空的，104 前端不會儲存這樣的內容，請到 104 後台補上上班地點"
    return None


# ── The forbidden-word check ─────────────────────────────────────────────


class _CheckAnswerMalformed(Exception):
    """The forbidden-word check answered with something that is not one text per sent text."""

    def __init__(self, detail: str):
        self.detail = detail
        super().__init__(detail)


async def _forbidden_check(request, form, job: JobRead, jobno: str, run: _Run) -> ForbiddenVerdict:
    """Send the check and read its verdict. A failed request or an answer that does not have
    one text per sent text stops the call: the front end does not save then either."""
    try:
        payload = await request(
            ENDPOINTS["forbidden_words_check"], body={"texts": forbidden_word_texts(form), "jobNo": jobno}
        )
    except GuardAbort as exc:
        stopped = dict(exc.payload)
        stopped["error"] = f"禁用詞檢查失敗，沒有送出{run.action}：{exc.payload.get('error')}"
        raise GuardAbort(
            stopped,
            kind=exc.kind,
            code=exc.code,
            envelope_detail=exc.envelope_detail,
            envelope_message=exc.envelope_message,
        ) from exc
    try:
        return forbidden_verdict(payload, job=job)
    except MalformedResponseError as exc:
        log.error("禁用詞檢查回應結構異常: %s", exc.detail)
        raise _CheckAnswerMalformed(exc.detail) from exc


def _hit_text(hit: Mapping) -> str:
    message = hit.get("message")
    return f"禁用詞提醒（{hit['field']}）：{message}" if message else f"禁用詞提醒（{hit['field']}）"


def _forbidden_refusal(verdict: ForbiddenVerdict, run: _Run, *, accepted: bool) -> dict | None:
    """The refusal of a confirmed call by the check's verdict, or None when the call may
    send. A block cannot be waived; a warning needs the caller's explicit consent."""
    hits = [dict(hit) for hit in verdict.hits]
    if verdict.verdict == "block":
        return {
            "error": f"104 的禁用詞檢查封鎖了這些內容，必須修改，不能略過。沒有送出{run.action}",
            "forbidden_words": hits,
        }
    if verdict.verdict == "warn" and not accepted:
        return {
            "error": (
                "104 的禁用詞檢查對這些內容提出提醒；前端在這時要求確認「我已確認符合法令」。"
                f"請真人看過之後帶 accept_forbidden_word_warnings=true 重新呼叫。沒有送出{run.action}"
            ),
            "forbidden_words": hits,
        }
    return None


def _forbidden_notes(verdict: ForbiddenVerdict, *, accepted: bool) -> list[str]:
    """The reminders a confirmed, allowed call carries."""
    if verdict.verdict == "warn":
        return [_hit_text(hit) for hit in verdict.hits]
    if verdict.verdict == "pass" and accepted:
        return ["104 的禁用詞檢查沒有提醒，accept_forbidden_word_warnings 沒有使用"]
    return []


def _preview_forbidden_notes(verdict: ForbiddenVerdict) -> list[str]:
    if verdict.verdict == "block":
        return ["104 的禁用詞檢查會封鎖這些內容，必須修改才能送出"]
    if verdict.verdict == "warn":
        return ["104 的禁用詞檢查對這些內容提出提醒；送出時需要真人確認後給 accept_forbidden_word_warnings=true"]
    return []


# ── Shared pieces of the two flows ───────────────────────────────────────


def _published_value(job: JobRead, path: str) -> object:
    for tree in (job.published, job.raw):
        current: object = tree
        for key in path.split("."):
            if isinstance(current, Mapping) and key in current:
                current = current[key]
            else:
                break
        else:
            return current
    return None


def _change_rows(before: JobRead, applied: AppliedChanges) -> list[dict]:
    return [
        {"field": path, "old": _published_value(before, path), "new": _published_value(applied.job, path)}
        for path in applied.changed_paths
    ]


def _change_warnings(job: JobRead, applied: AppliedChanges) -> list[str]:
    """The reminders about the applied changes and the read: changes equal to the current
    value, codes the editor drops on save."""
    notes = []
    if applied.unchanged_paths:
        notes.append(f"這些改動等於目前的值，沒有改動：{'、'.join(applied.unchanged_paths)}")
    for path, codes in applied.job.dropped:
        notes.append(
            f"104 編輯器儲存時會去掉 {path} 裡它不認得的代碼 {list(codes)}：儲存後這些代碼會消失，送出的內容不含它們"
        )
    return notes


async def _read_job(request, endpoint_key: str, params, run: _Run, jobno: str) -> JobRead:
    job = read_job(await request(ENDPOINTS[endpoint_key], params=params), jobno=jobno)
    run.job = job
    return job


async def _send_save(
    request,
    endpoint_key: str,
    form,
    run: _Run,
    *,
    action: Literal["update", "create"],
    noun: str,
    params=None,
) -> tuple[SaveOutcome | None, dict | None]:
    """Send the save once. Returns (outcome, error): an `error` payload when 104 refused in a
    way that is certain (field errors, a failure, no remaining job, job not found, or an abort
    that says 104 did not take the request); otherwise the outcome (None when the request
    aborted ambiguously), with the reminders already in `run.warnings` — the caller re-reads
    either way."""

    async def _before_write(_info) -> None:
        run.write_attempted = True

    kwargs = {"params": params} if params else {}
    try:
        payload = await request(ENDPOINTS[endpoint_key], body=qs_pairs(form), before_request=_before_write, **kwargs)
    except GuardAbort as exc:
        if write_failure_disposition(exc, batch_size=1) == "raise":
            raise
        run.warnings.append(f"{noun}請求逾時或回應無法辨識，結果以重讀為準")
        return None, None
    outcome = save_outcome(payload, action=action)
    job = run.job
    assert job is not None
    detail = run.redacted(outcome.message) if outcome.message else None
    if outcome.outcome == "failed_fields":
        return outcome, {
            "error": f"104 拒絕了這次{run.action}：有欄位錯誤，沒有儲存",
            "failed_fields": failed_field_rows(outcome, job),
        }
    if outcome.outcome == "failed":
        return outcome, {"error": f"104 拒絕了這次{run.action}" + (f"：{detail}" if detail else "") + "，沒有儲存"}
    if outcome.outcome == "no_remaining_job":
        return outcome, {
            "error": "104 回報沒有剩餘的職務數" + (f"：{detail}" if detail else "") + f"，沒有{run.action}"
        }
    if outcome.outcome == "job_not_found":
        return outcome, {"error": "104 回報找不到這個職缺" + (f"：{detail}" if detail else "") + f"，沒有{run.action}"}
    if outcome.outcome == "unrecognised":
        shown = f"（{detail}）" if detail else ""
        run.warnings.append(f"{noun}請求的回應無法辨識{shown}，結果以重讀為準")
    run.warnings.extend(outcome.notices)
    return outcome, None


def _args_error(changes: object, *, required: bool) -> dict | None:
    """Argument check of `changes`, ahead of any request, or the error."""
    if changes is None and not required:
        return None
    if not isinstance(changes, dict) or (required and not changes):
        need = "必須是非空的物件（路徑 → 新值）" if required else "必須是物件（路徑 → 新值），或省略"
        return {"error": f"changes {need}，收到 {type(changes).__name__}。路徑請取自 get_job_detail 的欄位名稱以 . 串接"}
    if not all(isinstance(key, str) for key in changes):
        return {"error": "changes 的鍵必須是字串（欄位路徑）"}
    try:
        json.dumps(changes)
    except (TypeError, ValueError):
        return {"error": "changes 的值必須是可以序列化成 JSON 的值"}
    return None


def _flags_error(**flags: object) -> dict | None:
    bad = [name for name, value in flags.items() if not isinstance(value, bool)]
    return {"error": f"{'、'.join(bad)} 必須是布林值（true 或 false）"} if bad else None


def _find_row(job_list: JobList, jobno: str, *, run: _Run) -> JobListRow | dict:
    """The job's row on the one list page this tool reads, or the error."""
    if truthy(job_list.account.is_web):
        return {"error": f"{WEB_NO_MORE_MENU}，沒有送出{run.action}"}
    row = next((r for r in job_list.rows if r.jobno == jobno), None)
    if row is None:
        if job_list.total_page() != 1:
            return {
                "error": f"職缺 {jobno} 不在職缺清單第 1 頁；本工具只處理職缺在清單第 1 頁的帳號，沒有送出{run.action}"
            }
        return {"error": f"職缺 {jobno} 不在職缺清單上，職缺編號請取自 list_jobs 的 jobno。沒有送出{run.action}"}
    return row


# ── update_job ───────────────────────────────────────────────────────────


def _update_unconfirmed(jobno: str, rows: Sequence[dict], run: _Run, extra: str) -> dict:
    return {
        "jobno": jobno,
        "saved": "unconfirmed",
        "changes": [dict(row) for row in rows],
        "mismatched_fields": {"not_applied": [], "damaged": []},
        "message": _UPDATE_MESSAGES["unconfirmed"],
        "warnings": [*run.warnings, extra],
    }


_UPDATE_MESSAGES = {
    "confirmed": "已確認：重讀職缺的編輯資料後，每個會送出的欄位都與送出的內容一致（依據是重讀，不是儲存回應）。",
    "mismatch": (
        "重讀的內容與送出的不一致，不要重送。請到 104 後台，或用 get_job_detail 檢查這個職缺；"
        "mismatched_fields 列出不一致的欄位。"
    ),
    "unconfirmed": "無法確認是否已儲存，不要重送。請到 104 後台，或用 get_job_detail 檢查這個職缺。",
}


def _label_list(wire_keys: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(field_label(key) for key in wire_keys))


async def _update_flow(request, run: _Run, jobno, checked: CheckedChanges, confirm, accept_words, accept_reset) -> dict:
    job_list = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
    row = _find_row(job_list, jobno, run=run)
    if isinstance(row, dict):
        return row
    refusal = update_list_gate(job_list, row)
    if refusal is not None:
        return {"error": f"{refusal}。沒有送出修改"}

    read_params = [("act", "edit"), ("jobno", jobno)]
    job = await _read_job(request, "get_job_detail", read_params, run, jobno)
    refusal = read_gate(job, act="edit")
    if refusal is not None:
        return {"error": run.redacted(f"{refusal}。沒有送出修改")}
    applied = apply_changes(job, checked)
    if confirm and not applied.changed_paths:
        return {"error": f"這些改動都等於目前的值（{'、'.join(applied.unchanged_paths)}），沒有實際改動，沒有送出修改"}
    if "detail.jobcat" in applied.changed_paths and job.raw.get("switch") == "on" and not accept_reset:
        return {
            "error": (
                "改開放中職缺的職務類別時，104 前端可能提醒「因職類條件異動，過去招募進度的累計資料已不適用，"
                "確定送出後將歸零重新累計」。這個代價需要真人同意；同意後請帶 accept_progress_reset=true 重新呼叫。"
                "沒有送出修改"
            )
        }
    form = build_form(applied.job, act="edit", jobno=jobno)
    run.warnings.extend(_change_warnings(job, applied))
    rows = run.rows = _change_rows(job, applied)

    verdict = await _forbidden_check(request, form, job, jobno, run)
    if not confirm:
        return {
            "preview": {
                "jobno": jobno,
                "changes": rows,
                "forbidden_words": [dict(hit) for hit in verdict.hits],
            },
            "warnings": ["這是預覽，沒有送出；真人看過差異後帶 confirm=true 才會儲存", *run.warnings, *_preview_forbidden_notes(verdict)],
        }
    refused = _forbidden_refusal(verdict, run, accepted=accept_words)
    if refused is not None:
        return refused
    run.warnings.extend(_forbidden_notes(verdict, accepted=accept_words))

    _, error = await _send_save(request, "update_job", form, run, action="update", noun="儲存")
    if error is not None:
        return error

    saved = "confirmed"
    not_applied: list[str] = []
    damaged: list[str] = []
    read_back: JobRead | None = None
    try:
        read_back = read_job(await request(ENDPOINTS["get_job_detail"], params=read_params), jobno=jobno)
        comparison = compare_forms(
            form, build_form(read_back, act="edit", jobno=jobno), changed=applied.changed_wire
        )
        unsent = compare_unsent(job, read_back, changed_paths=applied.changed_paths)
    except (GuardAbort, MalformedResponseError, JobShapeError) as exc:
        read_back = None
        saved = "unconfirmed"
        run.warnings.append(f"重讀職缺失敗（{_failure_reason(exc, run)}），無法確認是否已儲存")
    else:
        not_applied = _label_list(comparison.not_applied)
        damaged = _label_list(comparison.damaged)
        if not_applied or damaged:
            saved = "mismatch"
        if damaged:
            run.warnings.insert(
                0,
                f"⚠ 沒有要改的欄位在儲存後變了（{'、'.join(damaged)}）：這可能是職缺被改壞的訊號，請立刻到 104 後台檢查這個職缺",
            )
        if unsent:
            run.warnings.append(
                f"重讀時這些本工具不會送出的欄位與修改前不同：{'、'.join(unsent)}；它們不在送出的內容裡，變動來自 104 端，請到 104 後台確認"
            )
    if read_back is not None:
        for change in rows:
            change["read_back"] = _published_value(read_back, change["field"])
    return {
        "jobno": jobno,
        "saved": saved,
        "changes": rows,
        "mismatched_fields": {"not_applied": not_applied, "damaged": damaged},
        "message": _UPDATE_MESSAGES[saved],
        "warnings": run.warnings,
    }


# ── create_job ───────────────────────────────────────────────────────────


@dataclass(frozen=True)
class CreateConfirmation:
    """What the re-read job list says about a create: `created` "confirmed" or "unconfirmed";
    the new job's number and `switch` (as 104 gave it) only when confirmed; `candidates`: the
    jobnos that were not on the list before, in list order; `warnings`: which condition did
    not hold."""

    created: str
    new_jobno: str | None
    new_job_switch: object
    candidates: tuple[str, ...]
    warnings: tuple[str, ...]


def confirm_created(
    before: Sequence[str], job_list_after: JobList, *, job_name: str, hinted_jobno: str | None
) -> CreateConfirmation:
    """Whether the re-read job list shows exactly one new job, with the name that was sent
    and (when 104's response named one) the number 104 named. Raises MalformedResponseError
    when the list gives no page count."""
    known = set(before)
    new_rows = [row for row in job_list_after.rows if row.jobno not in known]
    candidates = tuple(row.jobno for row in new_rows)

    def _no(reason: str) -> CreateConfirmation:
        return CreateConfirmation("unconfirmed", None, None, candidates, (reason,))

    pages = job_list_after.total_page()
    if pages != 1:
        return _no(f"重讀的職缺清單有 {pages} 頁，看不出新職缺是否在其他頁，無法確認")
    if not new_rows:
        return _no("重讀的職缺清單上沒有新的職缺（104 可能有延遲，或沒有建立），無法確認")
    if len(new_rows) > 1:
        return _no(f"重讀的職缺清單上有 {len(new_rows)} 個新的職缺（可能有人同時新增），無法確認哪一個是這次新增的")
    row = new_rows[0]
    if row.fields.get("job") != job_name:
        return _no("重讀的職缺清單上新出現的職缺名稱與送出的不符，無法確認")
    if hinted_jobno is not None and hinted_jobno != row.jobno:
        return _no("重讀的職缺清單上新出現的職缺編號與 104 在儲存回應中回報的不同，無法確認")
    return CreateConfirmation("confirmed", row.jobno, row.switch, candidates, ())


def _create_message(created: str, new_jobno: str | None) -> str:
    if created == "confirmed":
        return (
            f"已確認：重讀的職缺清單多了新職缺 {new_jobno}（依據是重讀，不是儲存回應）；開關狀態見 new_job_switch，"
            f"以 104 回報為準。新職缺的內容請用 get_job_detail({new_jobno!r}) 檢查。"
        )
    return (
        "無法確認新職缺是否建立，不要重送。請到 104 後台，或用 list_jobs 檢查；"
        "candidates 列出新出現的職缺編號，內容可用 get_job_detail 檢查。"
    )


def _create_unconfirmed(source_jobno: str, run: _Run, extra: str, candidates: Sequence[str] = ()) -> dict:
    return {
        "source_jobno": source_jobno,
        "created": "unconfirmed",
        "new_jobno": None,
        "new_job_switch": None,
        "candidates": list(candidates),
        "message": _create_message("unconfirmed", None),
        "warnings": [*run.warnings, extra],
    }


async def _create_flow(request, run: _Run, source_jobno, checked: CheckedChanges, confirm, accept_words) -> dict:
    job_list = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
    if truthy(job_list.account.is_web):
        return {"error": f"{WEB_NO_MORE_MENU}，沒有送出新增"}
    if job_list.total_page() != 1:
        return {
            "error": f"職缺清單有 {job_list.total_page()} 頁；新增後要靠一次讀到全部職缺來確認，本工具只處理清單只有一頁的帳號，沒有送出新增"
        }
    row = next((r for r in job_list.rows if r.jobno == source_jobno), None)
    if row is None:
        return {"error": f"底稿職缺 {source_jobno} 不在職缺清單上，職缺編號請取自 list_jobs 的 jobno。沒有送出新增"}
    before = [r.jobno for r in job_list.rows]

    more_params = [("jobno", str(row.raw_jobno))]
    if row.job_setting is not None:
        more_params.append(("job_setting", str(row.job_setting)))
    refusal = more_gate(await request(ENDPOINTS["job_more"], params=more_params))
    if refusal is not None:
        return {"error": f"{refusal}。沒有送出新增"}

    job = await _read_job(
        request, "job_insert", [("act", "copy"), ("name", ""), ("jobno", source_jobno)], run, source_jobno
    )
    refusal = read_gate(job, act="copy")
    if refusal is not None:
        return {"error": run.redacted(f"{refusal}。沒有送出新增")}
    applied = apply_changes(job, checked)
    form = build_form(applied.job, act="copy", jobno=source_jobno)
    run.warnings.extend(_change_warnings(job, applied))
    rows = _change_rows(job, applied)
    job_name = str(form["job"])

    verdict = await _forbidden_check(request, form, job, source_jobno, run)
    if not confirm:
        return {
            "preview": {
                "source_jobno": source_jobno,
                "job_name": job_name,
                "changes": rows,
                "forbidden_words": [dict(hit) for hit in verdict.hits],
            },
            "warnings": ["這是預覽，沒有送出；真人看過之後帶 confirm=true 才會新增", *run.warnings, *_preview_forbidden_notes(verdict)],
        }
    refused = _forbidden_refusal(verdict, run, accepted=accept_words)
    if refused is not None:
        return refused
    run.warnings.extend(_forbidden_notes(verdict, accepted=accept_words))

    outcome, error = await _send_save(
        request, "new_job", form, run, action="create", noun="新增", params=[("fast", "0")]
    )
    if error is not None:
        return error

    hinted = outcome.hinted_jobno if outcome is not None else None
    try:
        after = parse_job_list(await request(ENDPOINTS["job_list"], params=[("page", "1")]))
        confirmation = confirm_created(before, after, job_name=job_name, hinted_jobno=hinted)
    except (GuardAbort, MalformedResponseError) as exc:
        return _create_unconfirmed(
            source_jobno, run, f"重讀職缺清單失敗（{_failure_reason(exc, run)}），無法確認新職缺是否建立"
        )
    run.warnings.extend(confirmation.warnings)
    return {
        "source_jobno": source_jobno,
        "created": confirmation.created,
        "new_jobno": confirmation.new_jobno,
        "new_job_switch": confirmation.new_job_switch,
        "candidates": list(confirmation.candidates),
        "message": _create_message(confirmation.created, confirmation.new_jobno),
        "warnings": run.warnings,
    }


# ── Registration ─────────────────────────────────────────────────────────


# Each becomes the tool's actual __doc__. Claude Code cuts a description to 2,048 characters,
# so the evidence and every branch live in CLAUDE.md and only what decides a call is here.

_UPDATE_JOB_DESCRIPTION = """修改一個職缺的刊登內容（走 JSON API；寫入）。⚠ 儲存立刻生效、求職者看得到、無法撤回。先不帶 confirm 呼叫取得預覽（改前→改後與禁用詞檢查），讓真人看過差異，再帶 confirm=true 送出。

Args:
    jobno: 職缺代碼（全數字字串，來自 list_jobs）。
    changes: 必填，{欄位路徑: 新值}。路徑是 get_job_detail 的欄位名稱以 "." 串接（如 "detail.description"、"detail.salary.salary_low"），值是整個欄位的新值；detail.jobcat 與 need_condition.majors 的元素只給 {"no": 代碼}。不開放修改的欄位與理由會在錯誤裡列出（editable_fields）；不會改變送出內容的改動會被拒絕。
    confirm: true 才送出；預設只預覽。
    accept_forbidden_word_warnings: 104 的禁用詞「提醒」經真人確認後才給 true；「封鎖」不能略過。
    accept_progress_reset: 改開放中職缺的職務類別時必填 true（104 可能把招募進度累計歸零）。

104 的編輯器整份重送表單：聯絡資料等不開放修改的欄位照原樣回送，不會出現在回傳裡。有加值廣告、自動關閉日、預約開啟日、「職務確認中」的職缺，或 104 網頁上沒有修改功能的帳號，請到 104 後台修改。欄位對映、禁用詞檢查與儲存路由都只有前端碼證據、未量測；是否生效只看重讀職缺編輯資料的全欄比對。

一次呼叫送出 5 個請求（職缺清單、讀職缺、禁用詞檢查、儲存、重讀），預覽 3 個。成功回傳 {"jobno","saved","changes","mismatched_fields","message","warnings"}（預覽回 {"preview","warnings"}）：saved 只有 confirmed（重讀後全部欄位一致）、mismatch（有欄位不一致；mismatched_fields.damaged 是沒要改的欄位卻變了，代表 104 損壞了職缺，請立刻到 104 後台檢查）、unconfirmed（無法重讀），照值判斷。不是 confirmed 時不要重送。其餘回傳 {"error": str}（節流拒絕另帶 retry_after_seconds；禁用詞另帶 forbidden_words；欄位錯誤另帶 failed_fields），沒有 saved。
"""

_CREATE_JOB_DESCRIPTION = """以一個既有職缺為底稿複製新增職缺（走 JSON API；寫入）。⚠ 儲存立刻生效、求職者看得到、無法撤回；只有新職缺的開關可能不是開放。先不帶 confirm 取得預覽，讓真人看過，再帶 confirm=true 送出。只支援從既有職缺複製，不能從空白表單新增。

Args:
    source_jobno: 底稿職缺代碼（全數字字串，來自 list_jobs）。
    changes: 選填，{欄位路徑: 新值}，格式同 update_job（路徑取自 get_job_detail，值是整個欄位的新值）；不給就是原樣複製。
    confirm: true 才送出；預設只預覽。
    accept_forbidden_word_warnings: 104 的禁用詞「提醒」經真人確認後才給 true；「封鎖」不能略過。

新職缺開不開放以 104 回報為準（new_job_switch）：複製時的表單不帶底稿的開關，104 怎麼決定新職缺的開關未量測；可能用掉職務數額度（未量測）；之後可用 open_jobs／close_jobs 改變。聯絡資料等照底稿原樣帶過去，不會出現在回傳裡。有自動關閉日的底稿、104 網頁上沒有複製功能的帳號、沒有「確定新增」的職缺，請到 104 後台複製。欄位對映、禁用詞檢查與儲存路由都只有前端碼證據、未量測；是否建立只看重讀職缺清單。

一次呼叫送出 6 個請求（職缺清單、更多選單權限、讀底稿、禁用詞檢查、儲存、重讀職缺清單），預覽 4 個。成功回傳 {"source_jobno","created","new_jobno","new_job_switch","candidates","message","warnings"}（預覽回 {"preview","warnings"}）：created 只有 confirmed（重讀清單恰好多一個同名新職缺，回傳 new_jobno 與 new_job_switch）、unconfirmed（看不出來；candidates 是新出現的 jobno），照值判斷。不是 confirmed 時不要重送，請用 list_jobs 或 get_job_detail 檢查。逾時後不要重送：用戶端逾時時儲存可能已送出，重送會建立第二個公開職缺；先用 list_jobs 確認新職缺有沒有出現。其餘回傳 {"error": str}（節流拒絕另帶 retry_after_seconds；禁用詞另帶 forbidden_words；欄位錯誤另帶 failed_fields），沒有 created。
"""


def register_job_edit_tools(mcp: FastMCP) -> None:

    async def update_job(
        jobno: str,
        changes: dict,
        ctx: Context,
        confirm: bool = False,
        accept_forbidden_word_warnings: bool = False,
        accept_progress_reset: bool = False,
    ) -> dict:
        error = jobno_error(jobno) or _args_error(changes, required=True) or _flags_error(
            confirm=confirm,
            accept_forbidden_word_warnings=accept_forbidden_word_warnings,
            accept_progress_reset=accept_progress_reset,
        )
        if error is not None:
            return error
        try:
            checked = check_changes(changes)
        except ChangeError as exc:
            return _change_error_payload(exc, stage="沒有送出任何請求")

        run = _Run(action="修改")
        try:
            async with guarded_sequence(ctx, slots_needed=5 if confirm else 3) as (request, _info):
                return await _update_flow(
                    request, run, jobno, checked, confirm, accept_forbidden_word_warnings, accept_progress_reset
                )
        except GuardAbort as exc:
            return _abort_payload(exc, run)
        except _CheckAnswerMalformed as exc:
            return {"error": f"禁用詞檢查的回應結構異常（{exc.detail}），可能是介面已變更，請回報。沒有送出修改"}
        except Exception as exc:
            if run.write_attempted:
                log.error("update_job: 寫入之後的例外 (%s)", _location(exc))
                return _update_unconfirmed(
                    jobno, run.rows, run, "發生內部錯誤，無法判斷是否已寫入；請到 104 後台確認，不要重送"
                )
            return _pre_write_error(exc, run, "update_job")

    register_tool(mcp, update_job, _UPDATE_JOB_DESCRIPTION)

    async def create_job(
        source_jobno: str,
        ctx: Context,
        changes: dict | None = None,
        confirm: bool = False,
        accept_forbidden_word_warnings: bool = False,
    ) -> dict:
        error = jobno_error(source_jobno) or _args_error(changes, required=False) or _flags_error(
            confirm=confirm, accept_forbidden_word_warnings=accept_forbidden_word_warnings
        )
        if error is not None:
            return error
        try:
            checked = check_changes(changes or {})
        except ChangeError as exc:
            return _change_error_payload(exc, stage="沒有送出任何請求")

        run = _Run(action="新增")
        try:
            async with guarded_sequence(ctx, slots_needed=6 if confirm else 4) as (request, _info):
                return await _create_flow(request, run, source_jobno, checked, confirm, accept_forbidden_word_warnings)
        except GuardAbort as exc:
            return _abort_payload(exc, run)
        except _CheckAnswerMalformed as exc:
            return {"error": f"禁用詞檢查的回應結構異常（{exc.detail}），可能是介面已變更，請回報。沒有送出新增"}
        except Exception as exc:
            if run.write_attempted:
                log.error("create_job: 寫入之後的例外 (%s)", _location(exc))
                return _create_unconfirmed(
                    source_jobno, run, "發生內部錯誤，無法判斷是否已寫入；請到 104 後台確認，不要重送"
                )
            return _pre_write_error(exc, run, "create_job")

    register_tool(mcp, create_job, _CREATE_JOB_DESCRIPTION)
