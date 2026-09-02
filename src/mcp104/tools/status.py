from mcp.server.fastmcp import Context, FastMCP

from mcp104.db.database import ID_SOURCE_MESSAGE, ID_SOURCE_RESUME
from mcp104.tools.helpers import get_session_id, require_login

VALID_STATUSES = {"contacted", "replied", "interested", "not_interested", "scheduled"}

# candidate_id is not one key space — see db/database.py's ID_SOURCE_*
# constants and CLAUDE.md. The Agent must say which system the id came
# from; there is no reliable default. Built from the same constants
# search.py/messaging.py write with, rather than re-declared as literals,
# so the validator that gates every write can't silently drift from them.
VALID_ID_SOURCES = {ID_SOURCE_RESUME, ID_SOURCE_MESSAGE}


def register_status_tools(mcp: FastMCP):

    @mcp.tool()
    @require_login
    async def check_already_contacted(candidate_id: str, id_source: str, ctx: Context) -> bool | dict:
        """檢查是否已聯繫過此候選人（根據 Agent 自己的紀錄）。

        Args:
            candidate_id: 候選人 id。
            id_source: candidate_id 的來源 —— "resume"（來自 search_resumes /
                get_resume_detail 的履歷卡片 id）或 "message"（來自
                read_messages / send_message 的對話串 id）。兩者尚未確認是
                同一個 key space（見 CLAUDE.md、docs/104-site-facts.md），
                必須明確指定，不會自動猜測。
        """
        if id_source not in VALID_ID_SOURCES:
            return {"error": f"id_source 必須為 {sorted(VALID_ID_SOURCES)} 之一"}

        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)
        info = app.session_pool.get_session(session_id)
        candidate = await app.db.get_candidate(candidate_id, id_source, info.account_label)
        return candidate is not None and candidate.get("status") is not None

    @mcp.tool()
    @require_login
    async def update_candidate_status(candidate_id: str, id_source: str, status: str, ctx: Context) -> dict:
        """更新候選人狀態。

        Args:
            candidate_id: 候選人 id。
            id_source: candidate_id 的來源 —— "resume" 或 "message"（同上，
                見 check_already_contacted 的說明）。
            status: contacted, replied, interested, not_interested, scheduled
        """
        if id_source not in VALID_ID_SOURCES:
            return {"error": f"id_source 必須為 {sorted(VALID_ID_SOURCES)} 之一"}
        if status not in VALID_STATUSES:
            return {"success": False, "error": f"Invalid status. Must be one of: {VALID_STATUSES}"}

        app = ctx.request_context.lifespan_context
        session_id = get_session_id(ctx)
        info = app.session_pool.get_session(session_id)
        await app.db.upsert_candidate(candidate_id, id_source, info.account_label, status=status)
        return {"success": True}
