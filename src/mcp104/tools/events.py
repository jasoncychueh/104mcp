"""104's structured recruiting events — one tool per event (inquiry, interview
invite, thank-you, start-date reminder, personality-test invite), all built on one
shared send skeleton (`send_event`) that every event route plugs into.

An event send is three requests in a fixed order — reverse bridge (pId -> idNo),
`event/last-info`, the event POST — issued through `tools/helpers.py`'s
`guarded_sequence` (one lock, one throttle-gate check, one `_issue_one` per
sub-request). What differs between routes is declared once per route
(`EventRoute`) and the route's body assembler; the sequence, the daily-cap
checks, the send conclusion, the send log and the candidate-status write live in
`send_event` only.

The pre-send validators (`check_*`) and the body assemblers are module-level pure
functions, testable with no session, no HTTP and no MCP `Context`.

This module must never gain `from __future__ import annotations`: that import turns
annotations into strings, so mcp's `issubclass(param.annotation, Context)` check
silently fails and `ctx` leaks into the published inputSchema.
"""

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from functools import partial

from mcp.server.fastmcp import Context, FastMCP

import mcp104.tools.discovery as discovery_mod
from mcp104.browser.api_client import ENDPOINTS
from mcp104.db.database import ID_SOURCE_MESSAGE, LINK_SOURCE_EVENT_BRIDGE
from mcp104.browser.session import SessionInfo
from mcp104.tools.helpers import GuardAbort, ToolAbort, guarded_sequence, require_login
from mcp104.tools.messaging import (
    _RESUME_ID_GUARD_MESSAGE,
    StatusWrite as StatusWrite,
    _build_success_send_result,
    _conclude_send,
    _enforce_daily_cap,
    _log_send_attempt,
    _looks_like_resume_id,
    _write_status,
)
from mcp104.tools.status import id_link_pairs, remember_id_links

log = logging.getLogger("104-mcp.events")


# ── The route declaration ────────────────────────────────────────────────

@dataclass(frozen=True)
class LastInfo:
    """The part of `event/last-info` the event routes use."""

    # data.emailCC — must be present and a list, otherwise the send aborts
    # before the third request.
    email_cc: list
    # data.recruiters, verbatim; None when the key is absent. Parsed only by the
    # route that asks for it (`_recruiter_ids`), never by the skeleton.
    recruiters: object | None


# Called as build_body(id_no=..., last_info=...) — the tool has already bound its
# own parameters into it.
BodyBuilder = Callable[..., dict]


@dataclass(frozen=True)
class EventRoute:
    tool_name: str                                # log tag, and the tool's name
    endpoint_key: str                             # key into ENDPOINTS
    success_shape_measured: bool                  # only willingness has been recorded
    status_write: StatusWrite
    accepted_with_caveat_codes: frozenset[str]    # 104 "contact succeeded, but ..." codes


_WILLINGNESS_ROUTE = EventRoute(
    tool_name="send_inquiry",
    endpoint_key="send_willingness_event",
    success_shape_measured=True,
    status_write="contacted_if_unset",
    accepted_with_caveat_codes=frozenset(),
)


async def send_event(
    ctx: Context, route: EventRoute, *, job_id: str, candidate_id: str, build_body: BodyBuilder,
) -> dict:
    """The one event-send sequence. The caller has already run its pre-send
    validation and bound its own parameters into `build_body`. Returns one of the
    five send shapes (`{"success": False, ...}`, `{"error": ...}`, `confirmed`,
    `unconfirmed` + `failed`, `unconfirmed`).

    `build_body(id_no=<reverse bridge>, last_info=<LastInfo>)` runs before the
    third request; a `ToolAbort` it raises is concluded while `send_attempted` is
    still `False`, so nothing is logged and nothing is sent.
    """
    app = ctx.request_context.lifespan_context

    # `send_attempted` is set only as the LAST statement of the SEND request's own
    # before_request hook, never by `_before_first`: the first two sub-requests
    # are GETs that may fail for reasons that have nothing to do with whether the
    # event went out. Both are initialised before the guard is entered so the
    # except handlers can read them on every path, including an expired session.
    account_label: str | None = None
    send_attempted = False

    async def _before_first(info: SessionInfo) -> None:
        nonlocal account_label
        account_label = info.account_label
        await _enforce_daily_cap(app, info)

    async def _before_send(info: SessionInfo) -> None:
        nonlocal send_attempted
        await _enforce_daily_cap(app, info)
        send_attempted = True  # LAST statement — proof the send request is about to be issued

    try:
        async with guarded_sequence(ctx, slots_needed=3, before_first=_before_first) as (request, info):
            idno_payload = await request(
                ENDPOINTS["resolve_candidate_idno"],
                params=[("job_no", job_id), ("p_id", candidate_id)],
                pick_data=("idNo",), pick_metadata=(),
            )
            id_no = idno_payload["data"]["idNo"]
            # 104 itself just converted this pId to this idNo; remember it whether or not the
            # event is sent afterwards.
            await remember_id_links(
                app, info.account_label,
                id_link_pairs([{"id_no": id_no, "p_id": candidate_id}], id_no_key="id_no", p_id_key="p_id"),
                LINK_SOURCE_EVENT_BRIDGE,
            )

            last_info_payload = await request(
                ENDPOINTS["event_last_info"],
                pick_data=("emailCC",), pick_data_optional=("recruiters",), pick_metadata=(),
            )
            email_cc = last_info_payload["data"]["emailCC"]
            if not isinstance(email_cc, list):
                # Present but not a list — the projection only checks presence,
                # the type check is this function's own job. Aborts BEFORE the
                # third request is issued; send_attempted is still False here.
                raise ToolAbort(
                    {"error": "104 回應結構異常（emailCC 非陣列），可能是介面已變更，請回報"},
                    kind="malformed",
                )
            last_info = LastInfo(email_cc=email_cc, recruiters=last_info_payload["data"]["recruiters"])

            body = build_body(id_no=id_no, last_info=last_info)
            payload = await request(
                ENDPOINTS[route.endpoint_key], body=body, before_request=_before_send,
            )
            await _log_send_attempt(app, info.account_label, candidate_id, ID_SOURCE_MESSAGE)
            await _write_status(app, candidate_id, info.account_label, route.status_write)
            result = _build_success_send_result(
                payload, success_shape_measured=route.success_shape_measured,
            )
            result["warnings"] = []
            return result
    except GuardAbort as e:
        return await _conclude_send(
            app, route.tool_name, e, send_attempted, account_label, candidate_id,
            status_write=route.status_write,
            accepted_with_caveat_codes=route.accepted_with_caveat_codes,
        )
    except Exception as exc:
        return await _conclude_send(
            app, route.tool_name, exc, send_attempted, account_label, candidate_id,
            status_write=route.status_write,
            accepted_with_caveat_codes=route.accepted_with_caveat_codes,
        )


# ── Pre-send validation (pure) ───────────────────────────────────────────
#
# Each returns an error message naming the rule that failed, or None.

# Taiwan observes no daylight saving, so a fixed offset is exact and needs no
# system timezone database.
TAIPEI = timezone(timedelta(hours=8))

MAX_MESSAGE_LENGTH = 1000

_REPLY_DAYS_CHOICES = (1, 2, 3, 5, 7, 14)
_PHASE_CHOICES = (1, 2, 3)
_MEETING_TIME_FORMAT = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2} [0-9]{2}:[0-9]{2}", re.ASCII)


def check_message(message: str) -> str | None:
    if not message.strip():
        return "訊息內容不可為空白"
    if len(message) > MAX_MESSAGE_LENGTH:
        return (
            f"訊息內容過長（{len(message)} 字元），上限 {MAX_MESSAGE_LENGTH} 字元"
            "（來源：104 對話框介面上限，API 側真正的上限未量測，故往嚴的方向擋下）"
        )
    return None


def check_reply_days(reply_days: int | None) -> str | None:
    if reply_days is None:
        return None
    if isinstance(reply_days, bool) or reply_days not in _REPLY_DAYS_CHOICES:
        return f"reply_days 只能是 {', '.join(str(d) for d in _REPLY_DAYS_CHOICES)} 其中之一，或省略（不要求回覆期限），收到 {reply_days!r}"
    return None


def check_meeting_time(value: str, *, now: datetime, name: str) -> str | None:
    """`value` must be `YYYY-MM-DD HH:mm`, a date-time that exists, and — read as
    Taipei local time — not earlier than `now` (an aware datetime, injected so
    the boundary is testable without a clock)."""
    if not _MEETING_TIME_FORMAT.fullmatch(value):
        return f"{name} 格式必須是 YYYY-MM-DD HH:mm（台北當地時間），收到 {value!r}"
    try:
        parsed = datetime.strptime(value, "%Y-%m-%d %H:%M")
    except ValueError:
        return f"{name} 不是存在的日期時間：{value!r}"
    if parsed.replace(tzinfo=TAIPEI) < now:
        return f"{name} 不得早於現在（台北當地時間）：{value!r}"
    return None


def check_text(value: str, *, name: str, max_len: int) -> str | None:
    stripped = value.strip()
    if not stripped:
        return f"{name} 不可為空白"
    if len(stripped) > max_len:
        return f"{name} 過長（{len(stripped)} 字元），上限 {max_len} 字元"
    return None


def check_phase(phase: int) -> str | None:
    if isinstance(phase, bool) or phase not in _PHASE_CHOICES:
        return f"phase 只能是 1（一面）、2（二面）、3（三面），收到 {phase!r}"
    return None


# ── Body assembly (pure) ─────────────────────────────────────────────────

# The wire key set is measured verbatim [M §8.13/§8.14-1/§8.19] — every key
# except candidate[0].idNo (the reverse bridge's value), contactJobNo (the
# caller's own job_id), content (the caller's message) and templateId is a
# fixed literal, not a parameter. `isWithDetail` is hardcoded True rather than
# opened up.
_WILLINGNESS_RC = "13011211"


def _willingness_body(id_no: str, job_id: str, message: str, template_id: str | None, email_cc: list) -> dict:
    """Pure. Assembles the POST /bc-comm/event/willingness body. `id_no` comes
    from the reverse bridge (sub-request 1), never from the caller's own
    candidate_id (which is a pId, a different key space). `templateId` is the
    caller's value verbatim, or the measured empty-string shape when omitted —
    the key is ALWAYS present, there is no third shape. `email_cc` is
    last-info's `data.emailCC`, sent back verbatim including `[]`."""
    return {
        "candidate": [{"idNo": id_no}],
        "contactJobNo": job_id,
        "content": message,
        "templateId": template_id if template_id is not None else "",
        "isRequiredReplyDay": False,
        "replyDay": 1,
        "contact": "",
        "contactTel": "",
        "isWithDetail": True,
        "file": [],
        "ec": "",
        "rc": _WILLINGNESS_RC,
        "emailCC": email_cc,
    }


# The four routes below share this base; their keys and values come from the same
# sources as the willingness body's. `ec` / `rc` are sent with the same literals on
# every event route the front end builds.
def _base_body(*, id_no: str, last_info: LastInfo, job_id: str, message: str, template_id: str | None) -> dict:
    return {
        "candidate": [{"idNo": id_no}],
        "emailCC": last_info.email_cc,
        "contactJobNo": job_id,
        "content": message,
        "templateId": template_id if template_id is not None else "",
        "ec": "",
        "rc": _WILLINGNESS_RC,
    }


def _reply_day_fields(reply_days: int | None) -> dict:
    # The front end sends replyDay's default (1) even when the deadline box is unticked.
    return {
        "isRequiredReplyDay": reply_days is not None,
        "replyDay": reply_days if reply_days is not None else 1,
    }


def _recruiter_ids(raw: object | None) -> list:
    """Pure. `last-info`'s `data.recruiters` -> the ids, in order, forwarded as 104
    gave them (no type check on the id). `None` or `[]` -> `[]`; any other shape (not a
    list, an element that is not an object, an element without `id`) is a departure
    from what was recorded and aborts before the send request."""
    if raw is None:
        return []
    if not isinstance(raw, list) or not all(isinstance(r, dict) and "id" in r for r in raw):
        raise ToolAbort(
            {"error": "104 回應結構異常（recruiters 形狀異常），可能是介面已變更，請回報"},
            kind="malformed",
        )
    return [r["id"] for r in raw]


def _thank_body(
    *, id_no: str, last_info: LastInfo, job_id: str, message: str, template_id: str | None,
) -> dict:
    return _base_body(
        id_no=id_no, last_info=last_info, job_id=job_id, message=message, template_id=template_id,
    )


def _exam_body(
    *, id_no: str, last_info: LastInfo, job_id: str, message: str, template_id: str | None,
    reply_days: int | None,
) -> dict:
    return {
        **_base_body(
            id_no=id_no, last_info=last_info, job_id=job_id, message=message, template_id=template_id,
        ),
        **_reply_day_fields(reply_days),
        # The front end offers exactly one test ("104職業適性測驗"); not a parameter.
        "examNo": 1,
    }


def _appointment_fields(
    *, last_info: LastInfo, reply_days: int | None, meeting_time: str, location: str,
    contact_name: str, contact_phone: str, include_recruiters: bool,
) -> dict:
    """The keys the interview and start-date routes share. The calendar and
    recruiter sync flags are fixed literals, not parameters: they are outward actions
    this tool never takes. `recruiters` stays empty unless the caller asked for the
    account's saved recruiters."""
    return {
        **_reply_day_fields(reply_days),
        "contact": contact_name.strip(),
        "contactTel": contact_phone.strip(),
        "isWithDetail": True,
        "file": [],
        "syncToCalendar": False,
        "meetingTime": meeting_time,
        "location": location.strip(),
        "syncRecruiters": False,
        "recruiters": _recruiter_ids(last_info.recruiters) if include_recruiters else [],
    }


def _interview_body(
    *, id_no: str, last_info: LastInfo, job_id: str, message: str, template_id: str | None,
    reply_days: int | None, meeting_time: str, location: str, contact_name: str,
    contact_phone: str, phase: int, include_recruiters: bool,
) -> dict:
    return {
        **_base_body(
            id_no=id_no, last_info=last_info, job_id=job_id, message=message, template_id=template_id,
        ),
        **_appointment_fields(
            last_info=last_info, reply_days=reply_days, meeting_time=meeting_time, location=location,
            contact_name=contact_name, contact_phone=contact_phone, include_recruiters=include_recruiters,
        ),
        "syncContacts": False,
        "contacts": [],
        "phase": phase,
    }


def _inquire_body(
    *, id_no: str, last_info: LastInfo, job_id: str, message: str, template_id: str | None,
    reply_days: int | None, start_time: str, location: str, contact_name: str,
    contact_phone: str, include_recruiters: bool,
) -> dict:
    return {
        **_base_body(
            id_no=id_no, last_info=last_info, job_id=job_id, message=message, template_id=template_id,
        ),
        **_appointment_fields(
            last_info=last_info, reply_days=reply_days, meeting_time=start_time, location=location,
            contact_name=contact_name, contact_phone=contact_phone, include_recruiters=include_recruiters,
        ),
    }


# 104's own front end treats these three error codes as "contact succeeded, but ...".
_CAVEAT_CODES = frozenset({"00305", "00310", "00311"})

_INTERVIEW_ROUTE = EventRoute(
    tool_name="send_interview_invite",
    endpoint_key="send_interview_event",
    success_shape_measured=False,
    status_write="scheduled",
    accepted_with_caveat_codes=_CAVEAT_CODES,
)
_START_DATE_ROUTE = EventRoute(
    tool_name="send_start_date_reminder",
    endpoint_key="send_inquire_event",
    success_shape_measured=False,
    status_write="contacted_if_unset",
    accepted_with_caveat_codes=_CAVEAT_CODES,
)
_EXAM_ROUTE = EventRoute(
    tool_name="send_personality_test_invite",
    endpoint_key="send_exam_event",
    success_shape_measured=False,
    status_write="contacted_if_unset",
    accepted_with_caveat_codes=_CAVEAT_CODES,
)
_THANK_ROUTE = EventRoute(
    tool_name="send_thank_you_letter",
    endpoint_key="send_thank_event",
    success_shape_measured=False,
    status_write="contacted_if_unset",
    accepted_with_caveat_codes=_CAVEAT_CODES,
)


def _first_error(*errors: str | None) -> str | None:
    return next((e for e in errors if e is not None), None)


# ── Registration ─────────────────────────────────────────────────────────

def _with_notes(func: Callable) -> Callable:
    """Substitute the shared description fragments into `func.__doc__`. The
    constants are read from the discovery module HERE, at registration time —
    not bound once at import — so a caller (e.g. a test) that monkeypatches a
    constant BEFORE registering sees the patched text reach the description."""
    func.__doc__ = (
        func.__doc__
        .replace("{{MESSAGING_CANDIDATE_ID_NOTE}}", discovery_mod.MESSAGING_CANDIDATE_ID_NOTE)
        .replace("{{UNMEASURED_EVENT_ROUTE_NOTE}}", discovery_mod.UNMEASURED_EVENT_ROUTE_NOTE)
    )
    return func


def register_event_tools(mcp: FastMCP) -> None:

    async def send_inquiry(
        ctx: Context, job_id: str, candidate_id: str, message: str, template_id: str | None = None,
    ) -> dict:
        """送出 104 的「詢問意願」事件——UI 上那個邀約對話框送出的東西，會立刻送到
        一位真實求職者手上、無法撤回，必須先讓真人看過內容再呼叫，沒有
        dry-run。純文字訊息請改用 send_message；其他招募事件各有自己的工具——
        send_interview_invite（邀約面試）、send_thank_you_letter（感謝函）、
        send_start_date_reminder（到職日期提醒）、send_personality_test_invite
        （邀性格測驗），不會加到本工具的參數上。

        一次呼叫送出**三個**請求（反向橋 → last-info → 事件本體）——五個事件
        工具都是這樣（get_candidate_photo／get_resume_attachment 各送兩個）；
        正常情況下幾秒內完成，最壞情況（三個子請求都逾時）約 50 秒。⚠ 若 MCP 客戶端在最後一個 POST 已送出後才逾時，這裡看
        不到任何回傳，但對方可能已經收到那封信——請改用
        read_messages(job_nos=[job_id]) 或 104 後台確認，不要重送。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）——這封信會掛在
                這個職缺底下。
            candidate_id: 與 send_message 完全一樣。{{MESSAGING_CANDIDATE_ID_NOTE}}
                事件本文實際要的履歷 idNo 由本工具自己用反向橋換算，呼叫端從頭到尾
                只需要認得這一個 id。
            message: 信件本文，純文字，**最多 1000 字元**（超過在送出前拒絕，
                一個請求都不送——這個上限來自 104 對話框介面，API 側真正的上限
                未量測，故往嚴的方向擋下）。與所選範本的內容完全脫鉤：送出的是
                這裡給的文字，不是範本的 description。
            template_id: 選填。省略時送出「不帶範本」的形狀（`templateId` 這個
                鍵仍然會送，值是空字串）；給了就原樣送出，104 自己判斷合不合法
                ——本工具不做範本類型查核。可用 list_templates 找一則範本的 id。
                事件種類由工具決定，範本類別不換路由（104 前端碼）：本工具一律
                送詢問意願。

        每日上限由 MCP104_MAX_DAILY_MESSAGES 控制（per 帳號，與 send_message 共用）。
        回傳形狀與 send_message 完全相同（"success": False、{"error": ...}、
        "sent": "confirmed"/"unconfirmed" 三大類，見 send_message 的說明）——
        兩者共用同一套判定與同一份程式碼。
        """
        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}
        error = check_message(message)
        if error is not None:
            return {"success": False, "error": error}

        def build_body(*, id_no: str, last_info: LastInfo) -> dict:
            return _willingness_body(id_no, job_id, message, template_id, last_info.email_cc)

        return await send_event(
            ctx, _WILLINGNESS_ROUTE, job_id=job_id, candidate_id=candidate_id, build_body=build_body,
        )

    async def send_thank_you_letter(
        ctx: Context, job_id: str, candidate_id: str, message: str, template_id: str | None = None,
    ) -> dict:
        """送出 104 的「感謝函」事件（通常用於婉拒）。其他事件請用
        send_inquiry（詢問意願）、send_interview_invite（邀約面試）、
        send_start_date_reminder（到職日期提醒）、send_personality_test_invite
        （邀性格測驗）；純文字訊息用 send_message。⚠ 會立刻送到一位真實求職者
        手上、無法撤回，必須先讓真人看過內容再呼叫，沒有 dry-run。

        {{UNMEASURED_EVENT_ROUTE_NOTE}}

        104 的履歷清單頁只對「應徵」來源的履歷提供感謝函，對話頁沒有找到這個
        限制；對非應徵來源送出時 104 是否接受未知，被拒絕時回報的是確定沒有送出。

        一次呼叫送出三個請求（反向橋 → last-info → 事件本體），最壞約 50 秒。
        ⚠ 若 MCP 客戶端在最後的 POST 送出後才逾時，這裡看不到回傳，但對方可能
        已收到——請用 read_messages(job_nos=[job_id]) 或 104 後台確認，不要重送。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）。
            candidate_id: {{MESSAGING_CANDIDATE_ID_NOTE}}
            message: 信件本文，純文字，不可空白，最多 1000 字元。
            template_id: 選填，範本 id，原樣送出；範本類別不換路由。

        不合規則的參數在送出前拒絕，一個請求都不送。每日上限與 send_message
        共用。回傳形狀同 send_message，但本工具不會回 "confirmed"。
        """
        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}
        error = check_message(message)
        if error is not None:
            return {"success": False, "error": error}
        return await send_event(
            ctx, _THANK_ROUTE, job_id=job_id, candidate_id=candidate_id,
            build_body=partial(_thank_body, job_id=job_id, message=message, template_id=template_id),
        )

    async def send_personality_test_invite(
        ctx: Context, job_id: str, candidate_id: str, message: str,
        reply_days: int | None = None, template_id: str | None = None,
    ) -> dict:
        """送出 104 的「邀性格測驗」事件（固定是「104職業適性測驗」）。其他事件請用
        send_inquiry（詢問意願）、send_interview_invite（邀約面試）、
        send_start_date_reminder（到職日期提醒）、send_thank_you_letter（感謝函）；
        純文字訊息用 send_message。⚠ 會立刻送到一位真實求職者手上、無法撤回，
        必須先讓真人看過內容再呼叫，沒有 dry-run。

        {{UNMEASURED_EVENT_ROUTE_NOTE}}

        一次呼叫送出三個請求（反向橋 → last-info → 事件本體），最壞約 50 秒。
        ⚠ 若 MCP 客戶端在最後的 POST 送出後才逾時，這裡看不到回傳，但對方可能
        已收到——請用 read_messages(job_nos=[job_id]) 或 104 後台確認，不要重送。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）。
            candidate_id: {{MESSAGING_CANDIDATE_ID_NOTE}}
            message: 信件本文，純文字，不可空白，最多 1000 字元。
            reply_days: 選填，要求幾天內回覆：1、2、3、5、7、14 其中之一；省略＝
                不要求回覆期限。
            template_id: 選填，範本 id，原樣送出；範本類別不換路由。

        不合規則的參數在送出前拒絕，一個請求都不送。每日上限與 send_message
        共用。回傳形狀同 send_message，但本工具不會回 "confirmed"。
        """
        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}
        error = _first_error(check_message(message), check_reply_days(reply_days))
        if error is not None:
            return {"success": False, "error": error}
        return await send_event(
            ctx, _EXAM_ROUTE, job_id=job_id, candidate_id=candidate_id,
            build_body=partial(
                _exam_body, job_id=job_id, message=message, template_id=template_id,
                reply_days=reply_days,
            ),
        )

    async def send_interview_invite(
        ctx: Context, job_id: str, candidate_id: str, message: str,
        meeting_time: str, location: str, phase: int, contact_name: str, contact_phone: str,
        reply_days: int | None = None, include_recruiters: bool = False,
        template_id: str | None = None,
    ) -> dict:
        """送出 104 的「邀約面試」事件。其他事件請用 send_inquiry（詢問意願）、
        send_thank_you_letter（感謝函）、send_start_date_reminder（到職日期提醒）、
        send_personality_test_invite（邀性格測驗）。⚠ 會立刻送到一位真實求職者
        手上、無法撤回，必須先讓真人看過內容、時間與地點再呼叫，沒有 dry-run。
        再呼叫一次會送出第二封、各自獨立的邀約，不會改期也不會取消前一封，前一封
        仍待回覆；改期或取消請到 104 後台操作。不同步行事曆、不要求 104 通知任何人。

        {{UNMEASURED_EVENT_ROUTE_NOTE}}

        一次呼叫送出三個請求（反向橋 → last-info → 事件本體），最壞約 50 秒。
        ⚠ 若 MCP 客戶端在最後的 POST 送出後才逾時，這裡看不到回傳，但對方可能
        已收到——請用 read_messages(job_nos=[job_id]) 或 104 後台確認，不要重送。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）。
            candidate_id: {{MESSAGING_CANDIDATE_ID_NOTE}}
            message: 信件本文，純文字，不可空白，最多 1000 字元。
            meeting_time: 面試時間，YYYY-MM-DD HH:mm，台北當地時間，不得是過去；
                原樣送出，不做時區換算。
            location: 面試地點，最多 100 字元。
            phase: 面試階段：1=一面、2=二面、3=三面。
            contact_name: 聯絡窗口姓名，最多 50 字元。
            contact_phone: 聯絡窗口電話，最多 50 字元。
            reply_days: 選填，要求幾天內回覆：1、2、3、5、7、14 其中之一；省略＝
                不要求回覆期限。
            include_recruiters: True 會把帳號在 104 存好的面試官／聯絡人列進這封
                邀約；104 會因此做什麼未實測（可能通知他們，且撤不回）。預設 False。
            template_id: 選填，範本 id，原樣送出；範本類別不換路由。

        不合規則的參數在送出前拒絕，一個請求都不送。每日上限與 send_message
        共用。回傳形狀同 send_message，但本工具不會回 "confirmed"。
        """
        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}
        now = datetime.now(TAIPEI)
        error = _first_error(
            check_message(message),
            check_meeting_time(meeting_time, now=now, name="meeting_time"),
            check_text(location, name="location", max_len=100),
            check_text(contact_name, name="contact_name", max_len=50),
            check_text(contact_phone, name="contact_phone", max_len=50),
            check_phase(phase),
            check_reply_days(reply_days),
        )
        if error is not None:
            return {"success": False, "error": error}
        return await send_event(
            ctx, _INTERVIEW_ROUTE, job_id=job_id, candidate_id=candidate_id,
            build_body=partial(
                _interview_body, job_id=job_id, message=message, template_id=template_id,
                reply_days=reply_days, meeting_time=meeting_time, location=location,
                contact_name=contact_name, contact_phone=contact_phone, phase=phase,
                include_recruiters=include_recruiters,
            ),
        )

    async def send_start_date_reminder(
        ctx: Context, job_id: str, candidate_id: str, message: str,
        start_time: str, location: str, contact_name: str, contact_phone: str,
        reply_days: int | None = None, include_recruiters: bool = False,
        template_id: str | None = None,
    ) -> dict:
        """送出 104 的「到職日期提醒」事件（通知已錄取者報到時間與地點）。其他事件請用
        send_inquiry（詢問意願）、send_interview_invite（邀約面試）、
        send_thank_you_letter（感謝函）、send_personality_test_invite（邀性格測驗）。
        ⚠ 會立刻送到一位真實求職者手上、無法撤回，必須先讓真人看過內容、時間與地點
        再呼叫，沒有 dry-run。再呼叫一次會送出第二封、各自獨立的邀約，不會改期也
        不會取消前一封，前一封仍待回覆；改期或取消請到 104 後台操作。不同步行事曆、
        不要求 104 通知任何人。

        {{UNMEASURED_EVENT_ROUTE_NOTE}}

        一次呼叫送出三個請求（反向橋 → last-info → 事件本體），最壞約 50 秒。
        ⚠ 若 MCP 客戶端在最後的 POST 送出後才逾時，這裡看不到回傳，但對方可能
        已收到——請用 read_messages(job_nos=[job_id]) 或 104 後台確認，不要重送。

        Args:
            job_id: 職缺 id（可來自 list_jobs 或 read_messages）。
            candidate_id: {{MESSAGING_CANDIDATE_ID_NOTE}}
            message: 信件本文，純文字，不可空白，最多 1000 字元。
            start_time: 報到時間，YYYY-MM-DD HH:mm，台北當地時間，不得是過去；
                原樣送出，不做時區換算。
            location: 報到地點，最多 100 字元。
            contact_name: 聯絡窗口姓名，最多 50 字元。
            contact_phone: 聯絡窗口電話，最多 50 字元。
            reply_days: 選填，要求幾天內回覆：1、2、3、5、7、14 其中之一；省略＝
                不要求回覆期限。
            include_recruiters: True 會把帳號在 104 存好的面試官／聯絡人列進這封
                提醒；104 會因此做什麼未實測（可能通知他們，且撤不回）。預設 False。
            template_id: 選填，範本 id，原樣送出；範本類別不換路由。

        不合規則的參數在送出前拒絕，一個請求都不送。每日上限與 send_message
        共用。回傳形狀同 send_message，但本工具不會回 "confirmed"。
        """
        if _looks_like_resume_id(candidate_id):
            return {"success": False, "error": _RESUME_ID_GUARD_MESSAGE}
        now = datetime.now(TAIPEI)
        error = _first_error(
            check_message(message),
            check_meeting_time(start_time, now=now, name="start_time"),
            check_text(location, name="location", max_len=100),
            check_text(contact_name, name="contact_name", max_len=50),
            check_text(contact_phone, name="contact_phone", max_len=50),
            check_reply_days(reply_days),
        )
        if error is not None:
            return {"success": False, "error": error}
        return await send_event(
            ctx, _START_DATE_ROUTE, job_id=job_id, candidate_id=candidate_id,
            build_body=partial(
                _inquire_body, job_id=job_id, message=message, template_id=template_id,
                reply_days=reply_days, start_time=start_time, location=location,
                contact_name=contact_name, contact_phone=contact_phone,
                include_recruiters=include_recruiters,
            ),
        )

    for tool in (
        send_inquiry, send_interview_invite, send_start_date_reminder,
        send_personality_test_invite, send_thank_you_letter,
    ):
        mcp.tool()(require_login(_with_notes(tool)))
