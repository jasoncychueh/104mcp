"""Template tools — `list_templates` and the three that write: `create_template`,
`update_template` and `delete_template` (all routes on auth.vip.104.com.tw under
`/bc-comm/template*`). Every decision is a module-level pure function — text
length, list and detail parsing, the pre-create check, the create / update
outcome comparison, the update merge, the job list of a refused delete — and the
tool bodies only order the requests.

A write here is never confirmed by its own response. Each tool sends the write
exactly once and then re-reads a route that was measured (the template list or
the single-template read); only what the re-read shows decides "confirmed". A
write whose response is lost, unreadable or refused-in-an-unknown-way is
followed by the same re-read, and the result is "unconfirmed" when it cannot be
shown. Evidence levels for each route are in docs/104-site-facts.md (§8.15,
§8.18, §8.33 measured; §8.36 front-end code).

Nothing here writes `sent_log` or a candidate's status: a template is the
account's own text, not a message to anyone.

Must never gain `from __future__ import annotations`: that import turns
annotations into strings, so mcp's `issubclass(param.annotation, Context)` check
silently fails and `ctx` leaks into the published inputSchema.
"""

import logging
from dataclasses import dataclass

from mcp.server.fastmcp import Context, FastMCP

from mcp104.browser.api_client import ENDPOINTS
from mcp104.tools.helpers import (
    GuardAbort,
    MalformedResponseError,
    convert_keys,
    guarded_api,
    guarded_sequence,
    malformed_response_payload,
    normalise_jobno,
    require_login,
)
from mcp104.tools.registration import register_tool
from mcp104.tools.messaging import _more_pages_warning, _send_verdict

log = logging.getLogger("104-mcp.templates")

TITLE_MAX = 16
DESCRIPTION_MAX = 1000

# 104's error code for "no such template" (a 404 on the single read and on the
# delete check) and for "jobs still use this template" (the delete check).
_CODE_TEMPLATE_NOT_FOUND = "00802"
_CODE_TEMPLATE_IN_USE = "00803"


# ── Pure functions ───────────────────────────────────────────────────────


def js_length(text: str) -> int:
    """The length 104's web page counts: UTF-16 code units, so a character
    outside the basic plane (an emoji) is 2."""
    return len(text.encode("utf-16-le")) // 2


def text_errors(*, title: str | None, description: str | None) -> str | None:
    """The first problem with the template text, or None. A parameter that is
    None is not checked. A text that is empty or only whitespace is "required";
    a text longer than its limit names the limit and the actual length. The
    limits are the web page's, not a measured server limit."""
    if title is not None:
        if not title.strip():
            return "標題不可為空白"
        length = js_length(title)
        if length > TITLE_MAX:
            return f"標題最多 {TITLE_MAX} 字（依 104 網頁的計數：UTF-16 編碼單位，emoji 算 2 字），收到 {length} 字"
    if description is not None:
        if not description.strip():
            return "內容不可為空白"
        length = js_length(description)
        if length > DESCRIPTION_MAX:
            return (
                f"內容最多 {DESCRIPTION_MAX} 字（依 104 網頁的計數：UTF-16 編碼單位，emoji 算 2 字），"
                f"收到 {length} 字"
            )
    return None


def template_id_error(template_id: str) -> dict | None:
    """None for a non-empty all-digit string; otherwise an error payload that
    says where a template id comes from."""
    if isinstance(template_id, str) and template_id.isascii() and template_id.isdigit():
        return None
    return {
        "error": (
            f"template_id 必須是全數字的範本編號字串（例如 \"123456\"），收到 {template_id!r}。"
            "範本編號請取自 list_templates 回傳的 id。"
        )
    }


def _id_text(value: object, what: str) -> str:
    """A typeId (or an id) as 104 gives it — a string or an integer — in its
    string form. Anything else is not an identifier."""
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise MalformedResponseError(f"{what} 不是字串或整數：{value!r}")
    return str(value)


@dataclass(frozen=True)
class TemplateListPage:
    rows: tuple[dict, ...]  # 104's own rows, `typeId` normalised to a string
    total: int
    total_pages: int
    per_page: int


def _metadata_int(metadata: dict, key: str) -> int:
    value = metadata.get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise MalformedResponseError(f"metadata.{key} 缺失或不是整數")
    return value


def parse_template_list(payload: object) -> TemplateListPage:
    """The template list's family-B payload ({data, metadata}) -> a page.
    Raises MalformedResponseError when `data` is not an array of objects with a
    string `id`, a `typeId` is neither a string nor an integer, or `metadata`
    lacks an integer `total`, `totalPage` or `perPage`."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = payload.get("metadata")
    if not isinstance(metadata, dict):
        raise MalformedResponseError("metadata 缺失或非物件")
    rows = []
    for raw in payload["data"]:
        if not isinstance(raw, dict):
            raise MalformedResponseError("範本清單含非物件項目")
        if not isinstance(raw.get("id"), str) or not raw["id"]:
            raise MalformedResponseError("範本清單的列缺少字串 id")
        row = dict(raw)
        row["typeId"] = _id_text(raw.get("typeId"), "範本列的 typeId")
        rows.append(row)
    return TemplateListPage(
        rows=tuple(rows),
        total=_metadata_int(metadata, "total"),
        total_pages=_metadata_int(metadata, "totalPage"),
        per_page=_metadata_int(metadata, "perPage"),
    )


def parse_template_types(payload: object) -> tuple[tuple[str, str], ...]:
    """The category list's payload -> ((typeId string, name), ...). Raises
    MalformedResponseError when `data` is not an array of objects with a
    typeId (string or integer) and a string name."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise MalformedResponseError("類別清單的 data 缺失或非陣列")
    types = []
    for raw in payload["data"]:
        if not isinstance(raw, dict):
            raise MalformedResponseError("類別清單含非物件項目")
        name = raw.get("name")
        if not isinstance(name, str):
            raise MalformedResponseError("類別清單的 name 缺失或不是字串")
        types.append((_id_text(raw.get("typeId"), "類別的 typeId"), name))
    return tuple(types)


class AttachmentsUnreadableError(MalformedResponseError):
    """A single-template read whose `files` is missing or not an array. It is a
    malformed response like any other, but update_template answers it
    differently: without the attachment list there is no telling whether a PUT
    would overwrite them."""


@dataclass(frozen=True)
class TemplateDetail:
    id: str
    title: str
    description: str
    type_id: str
    type_desc: str | None
    file_count: int


def parse_template_detail(payload: object, *, requested_id: str) -> TemplateDetail:
    """The single-template read's payload -> a TemplateDetail. `data.id` must
    be the requested id. A missing or non-array `files` is refused: without it
    there is no telling whether a write would drop attachments. `typeDesc` is
    read when it is a string; the measured single read has none, so it is
    otherwise None."""
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        raise MalformedResponseError("範本的 data 缺失或非物件")
    if _id_text(data.get("id"), "範本的 id") != requested_id:
        raise MalformedResponseError("回應的範本 id 與請求的不同")
    title = data.get("title")
    description = data.get("description")
    if not isinstance(title, str) or not isinstance(description, str):
        raise MalformedResponseError("範本的 title 或 description 缺失或不是字串")
    files = data.get("files")
    if not isinstance(files, list):
        raise AttachmentsUnreadableError("讀不到這則範本的附件清單（files 缺失或不是陣列）")
    type_desc = data.get("typeDesc")
    return TemplateDetail(
        id=requested_id,
        title=title,
        description=description,
        type_id=_id_text(data.get("typeId"), "範本的 typeId"),
        type_desc=type_desc if isinstance(type_desc, str) else None,
        file_count=len(files),
    )


def type_id_problem(type_id: str, types: tuple[tuple[str, str], ...]) -> str | None:
    """None when `type_id` is on the category list; otherwise the error text,
    listing every (code, name)."""
    if any(code == type_id for code, _name in types):
        return None
    listed = "、".join(f"{code}={name}" for code, name in types)
    return f"type_id {type_id!r} 不在 104 的類別清單上。目前的類別：{listed}"


def create_preflight(page: TemplateListPage) -> str | None:
    """None when a new template's arrival can be seen on this one page: the list
    is a single page, shows every row, and has room left (a full page could push
    the new template onto page 2). Otherwise the reason."""
    if page.total_pages > 1 or len(page.rows) != page.total or page.total >= page.per_page:
        return (
            "這個類別的範本清單超過一頁、已滿一頁或與總數不符，本工具只能在一頁之內確認新增的結果，"
            "沒有送出新增。請到 104 後台新增，或先整理這個類別的範本。"
        )
    return None


def create_body(title: str, description: str, type_id: str) -> dict:
    return {"title": title, "description": description, "typeId": type_id, "files": []}


def _template_row(raw: dict) -> dict:
    """One list row -> the tool-facing template, allow-listed to the five
    published keys (`id`, `title`, `description`, `typeId`, `typeDesc`) and
    converted to snake_case. `description` is the complete letter body, so a
    list row needs no separate detail read to be shown."""
    return convert_keys({k: v for k, v in raw.items() if k in _TEMPLATE_ROW_ALLOWED_FIELDS})


def template_row_from_detail(detail: TemplateDetail) -> dict:
    """A TemplateDetail as the same five keys a list row has. `type_desc` is
    present (None when the single read had none) so the key set equals the
    list's."""
    return {
        "id": detail.id,
        "title": detail.title,
        "description": detail.description,
        "type_id": detail.type_id,
        "type_desc": detail.type_desc,
    }


def _same_but_whitespace(a: str, b: str) -> bool:
    def norm(text: str) -> str:
        return text.replace("\r\n", "\n").strip()

    return a != b and norm(a) == norm(b)


_WHITESPACE_NOTE = "104 存下的文字與送出的只差在空白或換行"


def _text_difference_reason(sent: dict, title: str, description: str) -> str:
    """Why stored text is not what was sent: whitespace only, or a real
    difference."""
    pairs = [(sent["title"], title), (sent["description"], description)]
    if any(a != b for a, b in pairs) and all(a == b or _same_but_whitespace(a, b) for a, b in pairs):
        return _WHITESPACE_NOTE
    return "重讀到的內容與送出的不相符"


def create_outcome(
    before: TemplateListPage, after: TemplateListPage, sent: dict
) -> tuple[str, dict | None, str | None]:
    """(status, template row or None, reason or None) for a create. "confirmed"
    only when the re-read shows exactly one id that was not there before and its
    title, description and typeId are what was sent. Ids that disappeared
    (deleted elsewhere) are ignored."""
    if after.total_pages > 1 or len(after.rows) != after.total:
        return "unconfirmed", None, "重讀的範本清單超過一頁或與總數不符，無法在一頁之內確認新增的結果"
    known = {row["id"] for row in before.rows}
    new_rows = [row for row in after.rows if row["id"] not in known]
    if not new_rows:
        return "unconfirmed", None, "重讀的範本清單沒有出現新的範本"
    if len(new_rows) > 1:
        return "unconfirmed", None, "重讀的範本清單多出不只一則範本，無法判斷哪一則是這次新增的"
    row = new_rows[0]
    if (row.get("title"), row.get("description"), row.get("typeId")) == (
        sent["title"], sent["description"], sent["typeId"]
    ):
        return "confirmed", _template_row(row), None
    if row.get("typeId") == sent["typeId"] and isinstance(row.get("title"), str) and isinstance(
        row.get("description"), str
    ):
        return "unconfirmed", None, _text_difference_reason(sent, row["title"], row["description"])
    return "unconfirmed", None, "重讀到的新範本與送出的內容不相符"


def merge_update(detail: TemplateDetail, *, title: str | None, description: str | None, type_id: str | None) -> dict | str:
    """The PUT body for an update, or the reason not to send one. Attachments:
    a template that has any is refused, since what to send back for them has
    not been measured and a wrong body would drop them for good. An update that
    leaves all three fields as they are is refused too."""
    if detail.file_count > 0:
        return (
            "這則範本有附件，本工具尚未實測帶附件的修改，送錯會讓附件消失而且無法復原，沒有送出修改。"
            "請到 104 後台修改這一則。"
        )
    merged = {
        "title": detail.title if title is None else title,
        "description": detail.description if description is None else description,
        "typeId": detail.type_id if type_id is None else type_id,
    }
    if (merged["title"], merged["description"], merged["typeId"]) == (
        detail.title, detail.description, detail.type_id
    ):
        return "給的內容與這則範本目前的內容相同，沒有要改的地方，沒有送出修改"
    return {**merged, "files": []}


def update_outcome(sent: dict, after: TemplateDetail) -> tuple[str, str | None]:
    """("confirmed", None) when the re-read has the three fields as sent and no
    attachments; otherwise ("unconfirmed", reason)."""
    if after.file_count > 0:
        return "unconfirmed", "重讀時這則範本帶有附件，與送出的修改不符"
    if (after.title, after.description, after.type_id) == (sent["title"], sent["description"], sent["typeId"]):
        return "confirmed", None
    if after.type_id == sent["typeId"]:
        return "unconfirmed", _text_difference_reason(sent, after.title, after.description)
    return "unconfirmed", "重讀到的內容與送出的不相符"


def used_by_jobs(envelope_detail: object) -> list[dict] | None:
    """The jobs 104 names when it refuses a delete: an array whose every item is
    an object with a `jobNo`, as [{"jobno", "job"}]. `job` is None when
    `jobName` is not a string. Anything else is None."""
    if not isinstance(envelope_detail, list):
        return None
    jobs = []
    for item in envelope_detail:
        if not isinstance(item, dict) or "jobNo" not in item:
            return None
        try:
            jobno = normalise_jobno(item["jobNo"])
        except MalformedResponseError:
            return None
        name = item.get("jobName")
        jobs.append({"jobno": jobno, "job": name if isinstance(name, str) else None})
    return jobs


# ── list_templates: allow-listed row conversion ─────────────────────────

# Allow-list, not deny-list: the published row is only what is named here,
# because a single-template read also carries `files`, which no tool publishes.
_TEMPLATE_ROW_ALLOWED_FIELDS = frozenset({"id", "title", "description", "typeId", "typeDesc"})


def _build_template_list_response(envelope: object) -> dict:
    """`envelope` is list_templates' already-classified family-B payload. A
    missing `metadata` is an error — measured present even on the no-typeId
    call, so its absence is a genuine departure, handled via the existing
    MalformedResponseError precedent rather than a new failure mode."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), list):
        raise MalformedResponseError("data 缺失或非陣列")
    metadata = envelope.get("metadata")
    if not isinstance(metadata, dict):
        raise MalformedResponseError("metadata 缺失或非物件")

    pagination = {
        "page": metadata.get("page"),
        "total_pages": metadata.get("totalPage"),
        "total": metadata.get("total"),
    }
    warnings = []
    warning = _more_pages_warning(pagination)
    if warning:
        warnings.append(warning)

    return {
        "results": [_template_row(row) for row in envelope["data"]],
        "pagination": pagination,
        "browse_limit": None,
        "warnings": warnings,
    }


# ── Shared tool plumbing ─────────────────────────────────────────────────


class _WriteTracker:
    """Records that a write request is about to leave: set by the
    `before_request` hook as the last act before the request is issued, read by
    each tool's catch-all to tell "nothing was sent" from "may have been"."""

    def __init__(self) -> None:
        self.sent = False

    async def before(self, _info) -> None:
        self.sent = True


async def _issue_write(request, tracker: _WriteTracker, endpoint, *, label: str, warnings: list[str], **kwargs):
    """Send one write exactly once. A refusal that 104 certainly did not act on
    (`NOT_SENT`) re-raises for the caller to return. Anything else — a timeout,
    an unreadable body, a status nobody has seen — may have taken effect, so it
    is noted in `warnings` and the caller goes on to the re-read. Returns the
    classified payload, or None when the response was not usable."""
    try:
        return await request(endpoint, before_request=tracker.before, **kwargs)
    except GuardAbort as exc:
        if _send_verdict(exc.kind) == "not_sent":
            raise
        warnings.append(f"{label}請求逾時或回應無法辨識，結果以重讀為準")
        return None


def _abort_reason(exc: GuardAbort | MalformedResponseError) -> str:
    return exc.payload.get("error") if isinstance(exc, GuardAbort) else exc.detail


_CHECK_BACKOFFICE = "請到 104 後台確認，不要重送"

_TEMPLATE_GONE_ERROR = "範本不存在或已被刪除；請用 list_templates 重新取得範本 id"


def _is_not_found(exc: GuardAbort) -> bool:
    return exc.kind == "not_found" and exc.code == _CODE_TEMPLATE_NOT_FOUND


# ── Tool descriptions ────────────────────────────────────────────────────
#
# Each becomes the tool's actual __doc__ (see register_tool) and has to fit the
# 2,048-character slice Claude Code shows the model.

_CREATE_TEMPLATE_DESCRIPTION = """新增一則罐頭信件範本到這個帳號（走 JSON API；寫入，不會送到任何求職者）。新增後 list_templates 看得到，回傳的 template.id 也可以當事件送出工具的 template_id。

Args:
    title: 必填，≤ 16 字（依 104 網頁的計數：UTF-16 編碼單位，emoji 算 2 字）。標題可重複，104 不檢查與既有範本是否重名，所以可能與既有範本重複。
    description: 必填，信件本文，≤ 1000 字（計數同上）。標題與內容原樣送出、不去除前後空白；整段空白視為空。#jname# 代表職缺名稱，是 104 唯一的佔位符；送出事件時由誰替換未量測。
    type_id: 必填，歸檔分類代碼。已知六種："1" 詢問意願、"2" 邀約面試、"3" 感謝函、"4" 到職日期提醒、"5" 邀性格測驗、"0" 不分類；帳號也可能有自訂的類別。送出前會讀類別清單核對，不在清單上就拒絕並列出可用的類別。

新範本沒有附件。一次呼叫送四個請求：讀類別清單、讀該類別的範本清單、新增（POST）、重讀同一份清單。新增請求只有前端碼證據，本工具沒有它的量測；結果只由前後兩次清單的差集決定，回應本身不當成確認。該類別的範本超過一頁（或剛好滿一頁）時在送出前拒絕，請到 104 後台新增。

成功時固定回傳 {"created": "confirmed"|"unconfirmed", "template": {...}|null, "message", "warnings"}。confirmed：重讀恰好多出一則內容相符的範本，template 是它（id、title、description、type_id、type_desc）。unconfirmed：重讀失敗或結果不符（同時有人新增、104 改寫了文字等），template 為 null；這不代表失敗，請到 104 後台確認，不要重送，重送可能產生重複的範本。created 只有這兩個值，照值判斷。

其餘情況回傳 {"error": str}（節流拒絕另帶 retry_after_seconds）：參數不合規則、類別不存在、被 104 拒絕、session 失效或遭封鎖，確定沒有新增。
"""

_UPDATE_TEMPLATE_DESCRIPTION = """修改一則已存好的範本（走 JSON API；寫入，不會送到任何求職者）。只改有給的欄位，沒給的沿用現值；三個都不給會被拒絕。

Args:
    template_id: 必填，全數字字串，來自 list_templates 的 id。
    title: 選填，≤ 16 字（依 104 網頁的計數：UTF-16 編碼單位，emoji 算 2 字）。
    description: 選填，≤ 1000 字（計數同上）。標題與內容原樣送出、不去除前後空白。#jname# 代表職缺名稱，送出事件時由誰替換未量測。
    type_id: 選填，要改成的歸檔分類代碼；帶了才會讀類別清單核對，不在清單上就拒絕並列出類別。

帶附件的範本不修改：本工具尚未實測帶附件的修改，送錯會讓附件消失而且無法復原，所以送出前就拒絕，請到 104 後台修改。

一次呼叫送三個請求，帶 type_id 時四個：讀這則範本、（有 type_id 才讀類別清單）、修改（PUT）、重讀這則範本。修改請求量測過，但只量過沒有附件的範本；結果仍以重讀為準，回應不當成確認。

成功時固定回傳 {"updated": "confirmed"|"unconfirmed", "template": {...}|null, "message", "warnings"}。template 是重讀到的範本（id、title、description、type_id、type_desc），重讀失敗或範本已不存在時為 null；⚠ type_desc 一律為 null（單筆讀取的路由不回類別名稱），要名稱請呼叫 list_templates。confirmed：重讀的三個欄位都與送出的相符。unconfirmed：不相符、只差空白或換行、或重讀失敗；這不代表失敗，請到 104 後台確認，不要重送。updated 只有這兩個值，照值判斷。

其餘情況回傳 {"error": str}（節流拒絕另帶 retry_after_seconds）：參數不合規則、範本不存在、有附件、沒有要改的內容、被 104 拒絕，確定沒有修改。
"""

_DELETE_TEMPLATE_DESCRIPTION = """刪除一則範本（走 JSON API；寫入）。⚠ 刪除後無法復原；呼叫前必須先讓真人確認要刪哪一則（可先用 list_templates 列出來讓他看標題與內容）。沒有 dry-run。

Args:
    template_id: 必填，全數字字串，來自 list_templates 的 id。

被職缺使用的範本 104 不讓刪（依 104 的規則，本工具尚未實測）：這時回 {"error", "used_by_jobs": [{"jobno","job"}]}，沒有送出刪除。

一次呼叫送三個請求：刪除前檢查、刪除（DELETE）、重讀這則範本。刪除前檢查量測過通過時的形狀（HTTP 200 {"data": true}），只有回 true 才往下，其他值都不送刪除；被職缺使用時的回應沒有量測。刪除請求只有前端碼證據，本工具沒有它的量測；結果只由重讀決定：重讀得到 104 的「找不到指定範本」才算確認。

成功時固定回傳 {"deleted": "confirmed"|"unconfirmed", "template_id", "message", "warnings"}。unconfirmed：重讀仍讀得到這則範本，或重讀失敗；這不代表失敗，請到 104 後台確認，不要重送。deleted 只有這兩個值，照值判斷。

其餘情況回傳 {"error": str}（節流拒絕另帶 retry_after_seconds）：參數不合規則、範本不存在、被職缺使用、刪除前檢查沒有通過、被 104 拒絕，確定沒有刪除。
"""


def register_template_tools(mcp: FastMCP) -> None:

    @mcp.tool()
    @require_login
    async def list_templates(ctx: Context, type_id: str | None = None, page: int = 1) -> dict:
        """列出這個帳號已存好的罐頭信件範本（走 JSON API；一次呼叫一個請求，
        不佔履歷瀏覽配額，但仍需要登入、仍過節流閘）。用來挑出要給事件送出工具
        （send_inquiry 等）的 template_id，但不是它們的必要前置步驟——template_id
        都是選填的，完全不呼叫本工具也能送出。

        Args:
            type_id: 選填，依範本的歸檔分類篩選——這是帳號自己整理範本用的分類，
                只是歸檔，不換路由：五種招募事件（詢問意願、邀約面試、感謝函、
                到職日期提醒、邀性格測驗）各有自己的送出工具，事件種類由工具
                決定，與所帶範本的類別無關。已知六種：
                "1" 詢問意願、"2" 邀約面試、"3" 感謝函、"4" 到職日期提醒、
                "5" 邀性格測驗、"0" 不分類。省略時回全部範本。
            page: 頁碼，預設 1。

        成功時固定回傳 {"results": [{"id","title","description","type_id",
        "type_desc"}, ...], "pagination": {"page","total_pages","total"},
        "browse_limit": null, "warnings": [...]}——description 就是完整信件
        本文，不需要另外查單則範本。total_pages 大於 page 時 warnings 會提醒還有
        其餘頁面。失敗時回傳 {"error": str}，沒有 results 欄位。
        """
        params: list[tuple[str, str]] = [("page", str(page))]
        if type_id is not None:
            params.append(("typeId", type_id))
        try:
            async with guarded_api(ctx, ENDPOINTS["list_templates"], params=params) as (envelope, _info):
                return _build_template_list_response(envelope)
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("list_templates: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)

    async def create_template(ctx: Context, title: str, description: str, type_id: str) -> dict:
        problem = text_errors(title=title, description=description)
        if problem is None and not type_id.strip():
            problem = "type_id 不可為空白；已知的類別代碼見說明，也可以先用 list_templates 看帳號裡有哪些類別"
        if problem is not None:
            return {"error": problem}

        warnings: list[str] = []
        tracker = _WriteTracker()
        try:
            async with guarded_sequence(ctx, slots_needed=4) as (request, _info):
                types = parse_template_types(
                    await request(ENDPOINTS["list_template_types"], params=[("withoutPaging", "1")])
                )
                problem = type_id_problem(type_id, types)
                if problem is not None:
                    return {"error": problem}

                # No perPage: the list is read at 104's own default page size, which is
                # what create_preflight's "room left on the page" check is measured against.
                list_params = [("page", "1"), ("typeId", type_id)]
                before = parse_template_list(await request(ENDPOINTS["list_templates"], params=list_params))
                problem = create_preflight(before)
                if problem is not None:
                    return {"error": problem}

                body = create_body(title, description, type_id)
                await _issue_write(
                    request, tracker, ENDPOINTS["create_template"], label="新增", warnings=warnings, body=body
                )

                try:
                    after = parse_template_list(await request(ENDPOINTS["list_templates"], params=list_params))
                    status, template, reason = create_outcome(before, after, body)
                except (GuardAbort, MalformedResponseError) as exc:
                    status, template, reason = "unconfirmed", None, f"重讀範本清單失敗（{_abort_reason(exc)}）"
                if reason is not None:
                    warnings.append(reason)
                return {
                    "created": status,
                    "template": template,
                    "message": (
                        "新增請求已送出，重讀範本清單恰好多出一則內容相符的範本，已確認新增"
                        if status == "confirmed"
                        else f"無法確認範本已新增（原因見 warnings）。{_CHECK_BACKOFFICE}，重送可能產生重複的範本"
                    ),
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("create_template: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)
        except Exception:
            log.error("create_template: 非預期例外 (post_attempted=%s)", tracker.sent, exc_info=True)
            if tracker.sent:
                return {
                    "created": "unconfirmed",
                    "template": None,
                    "message": f"無法確認範本已新增（原因見 warnings）。{_CHECK_BACKOFFICE}，重送可能產生重複的範本",
                    "warnings": warnings + [f"發生內部錯誤，無法判斷是否已寫入；{_CHECK_BACKOFFICE}"],
                }
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出新增"}

    register_tool(mcp, create_template, _CREATE_TEMPLATE_DESCRIPTION)

    async def update_template(
        ctx: Context, template_id: str, title: str | None = None,
        description: str | None = None, type_id: str | None = None,
    ) -> dict:
        error = template_id_error(template_id)
        if error is not None:
            return error
        if title is None and description is None and type_id is None:
            return {"error": "title、description、type_id 三個都沒給，沒有要改的內容；至少要給一個"}
        problem = text_errors(title=title, description=description)
        if problem is None and type_id is not None and not type_id.strip():
            problem = "type_id 不可為空白；不改類別就不要帶這個參數"
        if problem is not None:
            return {"error": problem}

        warnings: list[str] = []
        tracker = _WriteTracker()
        id_params = [("template_id", template_id)]
        try:
            async with guarded_sequence(ctx, slots_needed=4 if type_id is not None else 3) as (request, _info):
                try:
                    detail = parse_template_detail(
                        await request(ENDPOINTS["get_template"], params=id_params), requested_id=template_id
                    )
                except GuardAbort as exc:
                    if _is_not_found(exc):
                        return {"error": _TEMPLATE_GONE_ERROR}
                    raise
                except AttachmentsUnreadableError as exc:
                    log.error("update_template: 讀不到附件清單: %s", exc.detail)
                    return {
                        "error": (
                            "讀不到這則範本的附件清單（104 回的 files 缺失或不是陣列），"
                            "為了不蓋掉附件，沒有送出修改。請到 104 後台修改這一則。"
                        )
                    }

                body = merge_update(detail, title=title, description=description, type_id=type_id)
                if isinstance(body, str):
                    return {"error": body}

                if type_id is not None:
                    types = parse_template_types(
                        await request(ENDPOINTS["list_template_types"], params=[("withoutPaging", "1")])
                    )
                    problem = type_id_problem(type_id, types)
                    if problem is not None:
                        return {"error": problem}

                response = await _issue_write(
                    request, tracker, ENDPOINTS["update_template"],
                    label="修改", warnings=warnings, params=id_params, body=body,
                )
                if response is not None and response.get("data") is False:
                    warnings.append("修改請求回的 data 是 false（它的意思未量測），結果以重讀為準")

                template = None
                try:
                    after = parse_template_detail(
                        await request(ENDPOINTS["get_template"], params=id_params), requested_id=template_id
                    )
                    template = template_row_from_detail(after)
                    status, reason = update_outcome(body, after)
                except GuardAbort as exc:
                    status = "unconfirmed"
                    reason = "範本已不存在" if _is_not_found(exc) else f"重讀範本失敗（{_abort_reason(exc)}）"
                except MalformedResponseError as exc:
                    status, reason = "unconfirmed", f"重讀範本失敗（{_abort_reason(exc)}）"
                if reason is not None:
                    warnings.append(reason)
                return {
                    "updated": status,
                    "template": template,
                    "message": (
                        "修改請求已送出，重讀這則範本，三個欄位都與送出的相符，已確認修改"
                        if status == "confirmed"
                        else f"無法確認範本已修改（原因見 warnings）。{_CHECK_BACKOFFICE}"
                    ),
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("update_template: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)
        except Exception:
            log.error("update_template: 非預期例外 (put_attempted=%s)", tracker.sent, exc_info=True)
            if tracker.sent:
                return {
                    "updated": "unconfirmed",
                    "template": None,
                    "message": f"無法確認範本已修改（原因見 warnings）。{_CHECK_BACKOFFICE}",
                    "warnings": warnings + [f"發生內部錯誤，無法判斷是否已寫入；{_CHECK_BACKOFFICE}"],
                }
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出修改"}

    register_tool(mcp, update_template, _UPDATE_TEMPLATE_DESCRIPTION)

    async def delete_template(ctx: Context, template_id: str) -> dict:
        error = template_id_error(template_id)
        if error is not None:
            return error

        warnings: list[str] = []
        tracker = _WriteTracker()
        id_params = [("template_id", template_id)]
        try:
            async with guarded_sequence(ctx, slots_needed=3) as (request, _info):
                try:
                    check = await request(ENDPOINTS["template_delete_check"], params=id_params)
                except GuardAbort as exc:
                    # Judged by 104's code whatever the HTTP status or kind: the status of
                    # the "in use" refusal has not been measured, and a 403 must not be
                    # reported as a block.
                    if exc.code == _CODE_TEMPLATE_IN_USE:
                        return _in_use_error(exc)
                    if exc.code == _CODE_TEMPLATE_NOT_FOUND:
                        return {"error": _TEMPLATE_GONE_ERROR}
                    raise
                if check.get("data") is not True:
                    return {
                        "error": "104 的刪除前檢查沒有確認這則範本可以刪除，沒有送出刪除；請到 104 後台查看"
                    }

                await _issue_write(
                    request, tracker, ENDPOINTS["delete_template"], label="刪除", warnings=warnings, params=id_params
                )

                try:
                    await request(ENDPOINTS["get_template"], params=id_params)
                    status = "unconfirmed"
                    warnings.append("刪除後重讀，這則範本仍讀得到")
                except GuardAbort as exc:
                    if _is_not_found(exc):
                        status = "confirmed"
                    else:
                        status = "unconfirmed"
                        warnings.append(f"重讀範本失敗（{_abort_reason(exc)}）")
                return {
                    "deleted": status,
                    "template_id": template_id,
                    "message": (
                        "刪除請求已送出，重讀這則範本得到 104 的「找不到指定範本」，已確認刪除"
                        if status == "confirmed"
                        else f"無法確認範本已刪除（原因見 warnings）。{_CHECK_BACKOFFICE}"
                    ),
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload
        except MalformedResponseError as exc:
            log.error("delete_template: 回應結構異常: %s", exc.detail)
            return malformed_response_payload(exc)
        except Exception:
            log.error("delete_template: 非預期例外 (delete_attempted=%s)", tracker.sent, exc_info=True)
            if tracker.sent:
                return {
                    "deleted": "unconfirmed",
                    "template_id": template_id,
                    "message": f"無法確認範本已刪除（原因見 warnings）。{_CHECK_BACKOFFICE}",
                    "warnings": warnings + [f"發生內部錯誤，無法判斷是否已刪除；{_CHECK_BACKOFFICE}"],
                }
            return {"error": "內部錯誤，這是程式問題，請回報 —— 沒有送出刪除"}

    register_tool(mcp, delete_template, _DELETE_TEMPLATE_DESCRIPTION)


def _in_use_error(exc: GuardAbort) -> dict:
    """The error for a delete refused because jobs use the template: 104's own
    message, this tool's explanation, and the jobs when 104 listed them in a
    readable form."""
    jobs = used_by_jobs(exc.envelope_detail)
    text = "這則範本正被職缺使用，104 不讓刪除，沒有送出刪除。"
    if exc.envelope_message:
        text = f"{exc.envelope_message}。{text}"
    if jobs:
        return {"error": text + "使用它的職缺見 used_by_jobs；請先改掉職缺上的範本，或保留這則範本。", "used_by_jobs": jobs}
    return {"error": text + "104 沒有附上可讀的職缺清單，請到 104 後台查看是哪些職缺在用。"}
