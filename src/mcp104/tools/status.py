"""Candidate status: the four tools that read and write what the Agent has recorded about a
candidate, and the recording of the id pairs 104 hands over.

Answers: what does the local database hold about this person, across the two id spaces
(resume `idNo`, message `pId`) and the early rows whose space was never recorded; which
candidates carry which status; and which (idNo, pId) pairs in a 104 response are safe to
remember. Deliberately does not: call 104, infer a pairing from anything but a pair 104
itself delivered on one piece of data, merge a person's rows, or change a status on its
own.

The decisions (which records count as contacted, the Taipei time format, the warnings,
which rows of a response yield a pair) are module-level pure functions; the tools only
check parameters, call the database and hand the result to them.

This module must never gain `from __future__ import annotations`: that import turns
annotations into strings, so mcp's `issubclass(param.annotation, Context)` check
silently fails and `ctx` leaks into the published inputSchema.
"""

import logging
from collections.abc import Iterable
from datetime import datetime

from mcp.server.fastmcp import Context, FastMCP

from mcp104.db.database import (
    ID_SOURCE_MESSAGE,
    ID_SOURCE_RESUME,
    ID_SOURCE_UNKNOWN,
    TAIPEI_TZ,
    UTC_TZ,
)
from mcp104.tools.helpers import get_session_id, require_login

log = logging.getLogger("104-mcp.status")

VALID_STATUSES = {"contacted", "replied", "interested", "not_interested", "scheduled"}

# candidate_id is not one key space — see db/database.py's ID_SOURCE_*
# constants and CLAUDE.md. The Agent must say which system the id came
# from; there is no reliable default. Built from the same constants
# search.py/messaging.py write with, rather than re-declared as literals,
# so the validator that gates every write can't silently drift from them.
VALID_ID_SOURCES = {ID_SOURCE_RESUME, ID_SOURCE_MESSAGE}

CANDIDATE_LIST_PAGE_SIZE = 50


# ── Recording the id pairs 104 hands over ────────────────────────────────

def id_link_pairs(items: Iterable[object], *, id_no_key: str, p_id_key: str) -> list[tuple[str, str]]:
    """(idNo, pId) pairs from response rows, first-seen order, duplicates dropped.
    An item contributes only when it is a dict whose two values are non-empty strings
    of ASCII digits; anything else (not a dict, a missing key, None, an empty string, a
    non-string, a non-digit or a full-width digit) is skipped rather than guessed at."""
    pairs: list[tuple[str, str]] = []
    seen: set[tuple[str, str]] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        id_no, p_id = item.get(id_no_key), item.get(p_id_key)
        if not all(isinstance(v, str) and v.isascii() and v.isdigit() for v in (id_no, p_id)):
            continue
        if (id_no, p_id) not in seen:
            seen.add((id_no, p_id))
            pairs.append((id_no, p_id))
    return pairs


async def remember_id_links(app, account_label: str | None, pairs: list[tuple[str, str]], source: str) -> None:
    """Write the pairs to the id-link table. NOT pure. A local side effect must never
    change a tool's response, so a storage failure is logged (source and pair count only
    — never an id) and swallowed; the pair is recorded the next time 104 delivers it.
    Does nothing without an account label or without pairs."""
    if account_label is None or not pairs:
        return
    try:
        await app.db.record_id_links(account_label, pairs, source)
    except Exception:
        log.error(
            "status: failed to record %d id link(s) from source %r (the tool's response is unaffected)",
            len(pairs), source, exc_info=True,
        )


# ── Shaping what the status tools return ─────────────────────────────────

def taipei_time(value: str | None) -> str | None:
    """The database's UTC "YYYY-MM-DD HH:MM:SS" as Taipei-time ISO 8601 with a +08:00
    offset; None stays None."""
    if value is None:
        return None
    return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC_TZ).astimezone(TAIPEI_TZ).isoformat()


def public_record(row: dict, *, with_matched_by: bool) -> dict:
    """A database row as the tool delivers it: a fixed key set, no account_email, both
    times (and those inside linked_ids) in Taipei time."""
    record = {
        "candidate_id": row["candidate_id"],
        "id_source": row["id_source"],
        "name": row["name"],
        "status": row["status"],
        "created_at": taipei_time(row["created_at"]),
        "status_updated_at": taipei_time(row["status_updated_at"]),
    }
    if with_matched_by:
        record["matched_by"] = row["matched_by"]
    record["linked_ids"] = [
        {**linked, "status_updated_at": taipei_time(linked["status_updated_at"])}
        for linked in row["linked_ids"]
    ]
    return record


def is_contacted(records: list[dict]) -> bool:
    """The one place that decides "already contacted": any related record carries a status."""
    return any(record["status"] is not None for record in records)


def _describe_record(record: dict) -> str:
    when = record["status_updated_at"]
    return (
        f"{record['id_source']} {record['candidate_id']}：{record['status']}"
        f"（{'狀態時間 ' + when if when is not None else '狀態時間不可考'}）"
    )


def status_difference_warning(records: list[dict]) -> str | None:
    """A note listing each stated record when two or more different statuses exist among
    them. States the facts and their times; moving from contacted to scheduled is ordinary
    progress, so it is not called an error."""
    stated = [record for record in records if record["status"] is not None]
    if len({record["status"] for record in stated}) < 2:
        return None
    return (
        "本機記下的這幾筆相關紀錄狀態不同，請依狀態時間判斷哪一筆較新"
        "（例如從 contacted 走到 scheduled 是正常的進展）："
        + "；".join(_describe_record(record) for record in stated)
    )


def unknown_source_warning(records: list[dict]) -> str | None:
    """A note when any record has id_source "unknown": early versions stored the id without
    saying which system it came from, and that cannot be recovered."""
    count = sum(1 for record in records if record["id_source"] == ID_SOURCE_UNKNOWN)
    if not count:
        return None
    return (
        f"有 {count} 筆紀錄的 id_source 是 unknown：這是早期版本記下的紀錄，當時沒有記下"
        "這個 id 來自履歷還是訊息系統，來源已不可考；它依 id 字串相同而被找到，不保證是同一位候選人。"
    )


def candidate_status_response(rows: list[dict]) -> dict:
    records = [public_record(row, with_matched_by=True) for row in rows]
    warnings = [
        warning
        for warning in (status_difference_warning(records), unknown_source_warning(records))
        if warning is not None
    ]
    return {"contacted": is_contacted(records), "records": records, "warnings": warnings}


def candidate_list_response(rows: list[dict], total: int, page: int) -> dict:
    results = [public_record(row, with_matched_by=False) for row in rows]
    warning = unknown_source_warning(results)
    return {
        "results": results,
        "pagination": {
            "page": page,
            "total_pages": -(-total // CANDIDATE_LIST_PAGE_SIZE),
            "total": total,
        },
        "browse_limit": None,
        "warnings": [warning] if warning is not None else [],
    }


def check_id_source(id_source: str) -> dict | None:
    if id_source not in VALID_ID_SOURCES:
        return {"error": f"id_source 必須為 {sorted(VALID_ID_SOURCES)} 之一"}
    return None


def check_list_arguments(status: str | None, page: object) -> dict | None:
    if status is not None and status not in VALID_STATUSES:
        return {"error": f"status 必須為 {sorted(VALID_STATUSES)} 之一，或省略以列出所有帶狀態的紀錄"}
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        return {"error": f"page 必須是 1 以上的整數，收到 {page!r}"}
    return None


def register_status_tools(mcp: FastMCP):

    @mcp.tool()
    @require_login
    async def get_candidate_status(candidate_id: str, id_source: str, ctx: Context) -> dict:
        """讀回本機對一位候選人記下的紀錄（狀態、名字、時間），只讀、不呼叫 104、不佔節流。

        Args:
            candidate_id: 候選人 id。
            id_source: candidate_id 的來源 —— "resume"（履歷列 / get_resume_detail 的 candidate_id，
                即 104 的 idNo）或 "message"（訊息系統的 pId：read_messages 列的 candidate_id、
                履歷列與應徵者列的 p_id）。必須明確指定，不會猜。

        同一位候選人在兩個空間各有一個 id。本工具除了這個 id 自己的紀錄，也會沿著本機記下的
        對應找另一個空間的紀錄：104 把兩個 id 放在同一筆資料上交出時（履歷列、應徵者列、
        履歷詳情、事件工具的反向換算），本機就記下這組對應。沒被觀察到的對應不會被建立，
        所以還沒讀過任何帶兩個 id 的資料時，另一個空間的紀錄找不到。各來源的證據強度不一：
        履歷列量過 6 個樣本，應徵者列沿用這個結論、未另外量測。

        成功時固定回傳：
          {"contacted": bool,
           "records": [{"candidate_id", "id_source", "name", "status", "created_at",
                        "status_updated_at", "matched_by", "linked_ids"}, ...],
           "warnings": [...]}
        contacted 與 check_already_contacted 用同樣參數的結果相同：records 裡任一筆帶狀態即為 True。
        matched_by："same_id"（這個 id 自己）｜"same_id_unknown_source"（同一個 id、但這筆是早期版本
        記下的、來源已不可考）｜"linked"（經對應找到的）。id_source 可能是 "unknown"。
        linked_ids 是這一筆已記下的另一個空間的 id：[{"candidate_id","id_source","status",
        "status_updated_at"}]，可直接拿去用另一組工具，並看得到那個 id 下記著什麼。時間是台北時間
        ISO 8601（帶 +08:00）；status_updated_at 為 null 表示狀態寫下的時間不可考或從未寫過狀態，
        name／status 為 null 表示沒記。沒有任何紀錄時 records 為 []、contacted 為 False，不是錯誤。
        紀錄之間狀態不同時照列並在 warnings 列出各筆的狀態與時間，不替你挑一個。失敗時回 {"error": str}。
        """
        error = check_id_source(id_source)
        if error is not None:
            return error
        if not candidate_id.strip():
            return {"error": "candidate_id 不可為空白"}

        app = ctx.request_context.lifespan_context
        info = app.session_pool.get_session(get_session_id(ctx))
        rows = await app.db.find_candidate_records(candidate_id, id_source, info.account_label)
        return candidate_status_response(rows)

    @mcp.tool()
    @require_login
    async def list_tracked_candidates(ctx: Context, status: str | None = None, page: int = 1) -> dict:
        """列出本機記下狀態的候選人（只限目前登入的帳號），只讀、不呼叫 104、不佔節流。

        Args:
            status: "contacted" | "replied" | "interested" | "not_interested" | "scheduled"；
                省略則列出所有帶狀態的紀錄（只記了名字、沒有狀態的不列）。
            page: 頁碼，從 1 開始；每頁固定 50 筆。

        成功時固定回傳：
          {"results": [{"candidate_id", "id_source", "name", "status", "created_at",
                        "status_updated_at", "linked_ids"}, ...],
           "pagination": {"page", "total_pages", "total"}, "browse_limit": null, "warnings": [...]}
        依狀態時間由新到舊排序，時間不可考的排最後。每一列是一筆紀錄，不是一個人：同一個人
        在履歷 id 與訊息 id 下各有狀態時兩筆都會出現。⚠ 以 status 篩選時，請看每列 linked_ids
        裡對方 id 的 status——同一個人可能在另一個 id 下已經走得更遠（例如已排面試）。linked_ids
        只列直接配對的 id；同一個人的完整紀錄（含經多組對應相連的 id）請用 get_candidate_status。
        id_source 可能是 "unknown"（早期版本記下、來源已不可考的紀錄）。欄位意義與時間格式同
        get_candidate_status。頁碼超出範圍回空的 results，不是錯誤。browse_limit 固定 null（本機
        資料，不扣 104 的瀏覽配額）。失敗時回 {"error": str}。
        """
        error = check_list_arguments(status, page)
        if error is not None:
            return error

        app = ctx.request_context.lifespan_context
        info = app.session_pool.get_session(get_session_id(ctx))
        rows, total = await app.db.list_candidates(
            info.account_label, status,
            limit=CANDIDATE_LIST_PAGE_SIZE, offset=(page - 1) * CANDIDATE_LIST_PAGE_SIZE,
        )
        return candidate_list_response(rows, total, page)

    @mcp.tool()
    @require_login
    async def check_already_contacted(candidate_id: str, id_source: str, ctx: Context) -> bool | dict:
        """檢查是否已聯繫過此候選人（根據 Agent 自己的紀錄）。回 True/False；參數不合法時回 {"error"}。

        Args: 同 get_candidate_status。

        除了這個 id 自己的紀錄，也會查本機已記下對應的另一個空間的 id，以及早期版本記下、來源
        已不可考的同一個 id 的紀錄；任一筆帶狀態即為 True。要看是哪一筆、什麼狀態、何時記下，
        改用 get_candidate_status（它的 contacted 與本工具的結果相同）。
        """
        error = check_id_source(id_source)
        if error is not None:
            return error

        app = ctx.request_context.lifespan_context
        info = app.session_pool.get_session(get_session_id(ctx))
        rows = await app.db.find_candidate_records(candidate_id, id_source, info.account_label)
        return is_contacted(rows)

    @mcp.tool()
    @require_login
    async def update_candidate_status(candidate_id: str, id_source: str, status: str, ctx: Context) -> dict:
        """更新候選人狀態。只寫入指定 id 與來源的那一筆，同時記下狀態時間；
        不改寫另一個空間或來源不可考的紀錄。

        Args:
            candidate_id: 候選人 id。
            id_source: candidate_id 的來源 —— "resume" 或 "message"（同
                get_candidate_status 的說明）。
            status: contacted, replied, interested, not_interested, scheduled
        """
        error = check_id_source(id_source)
        if error is not None:
            return error
        if status not in VALID_STATUSES:
            return {"success": False, "error": f"Invalid status. Must be one of: {VALID_STATUSES}"}

        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)
        info = app.session_pool.get_session(session_id)
        await app.db.upsert_candidate(candidate_id, id_source, info.account_label, status=status)
        return {"success": True}
