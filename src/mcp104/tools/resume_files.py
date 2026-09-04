"""The two asset tools: land a candidate's head-shot or one résumé
attachment in the data directory and hand the Agent a local path.

Answers: given a candidate id (and, for an attachment, which one), how does
this project get the FILE — not 104's URL for it — onto disk under a name
of our own choosing, and what does the caller see when there is no such
file, when 104 refuses, or when the bytes are not a format we recognise.

Deliberately does NOT: accept a URL from the caller (the tool re-reads the
résumé detail and only ever fetches URLs 104 itself handed it, which is
what stops it from becoming a general-purpose fetcher); issue its own HTTP
request (tools/helpers.py's guarded_sequence owns the lock, the throttle
gate and every request); decide what a transport failure means (the shared
per-request unit does); or cache anything (each call really re-fetches —
the retention sweep below is a deletion rule, not a cache lifetime, and
reading it as one would turn "104 replaced the photo, the tool still hands
over the old one" into a silent bug).

Named resume_files, not assets: `src/mcp104/assets/` is already this
package's own data directory, and a third unrelated thing called "assets"
in one repo is one too many.

Why nothing here is configurable by environment variable: the retention
period's failure mode is personal data staying on disk. Turning that into
a knob would hand a privacy decision to whatever an environment variable
happens to default to, and config.py's table has no retention or size
variable for any other mechanism either (browser/throttle.py's own
compaction policy is likewise a constant).
"""

import logging
import os
import shutil
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP

from mcp104.browser.api_client import ASSET_ROUTES, ENDPOINTS, hostname_for
from mcp104.tools.helpers import (
    GuardAbort,
    browse_limit_warning,
    extract_browse_limit,
    guarded_sequence,
    require_login,
)

log = logging.getLogger("104-mcp.resume_files")

# 24 hours. Grounded in the use case, not in a measurement: a landed file
# exists so that, within one working session, a human can be shown it. This
# is a per-run stdio process, so a file older than a working day necessarily
# belongs to a session that ended.
RESUME_FILE_RETENTION_SECONDS = 24 * 3600

# The extension is a pure function of the magic bytes — never of 104's own
# `filename`, which measurement records as frequently containing the
# candidate's name. There is no exception to that rule: the one an earlier
# draft carried (borrow 104's extension to tell OOXML members apart) left
# with the ZIP family, which is not in the whitelist because a PK signature
# has never been seen on the wire.
_EXTENSION_FOR_FORMAT = {"jpeg": ".jpg", "png": ".png", "pdf": ".pdf"}

# 104's own "no photo" placeholder lives on a different host entirely, with
# a /104main/vipphp path prefix. Two of twenty measured head-shots pointed
# there. That host is never fetched: the decision is made from the hostname,
# before any request, which is the only half of this that needs no guessing
# (the alternative — recognising the placeholder by its bytes — would have
# to download it first and would fail silently the day 104 changes the
# image).
_PLACEHOLDER_PHOTO_HOST = "static.104.com.tw"

# Resolved through the same public accessor the guard uses to pick
# cookies, so this module never carries its own copy of the hostname.
_ASSET_HOSTNAME = hostname_for(ASSET_ROUTES["candidate_photo"])

_NO_PHOTO_WARNING = "這位候選人沒有放大頭照。"
_PLACEHOLDER_PHOTO_WARNING = (
    "這位候選人沒有放大頭照：104 給的是它自己的預設頭像（在 static.104.com.tw 上，"
    "不是這位候選人的照片），因此本工具不下載它。"
)
_BROWSE_LIMIT_NOTE = (
    "browse_limit 是 104 在這次呼叫的履歷詳情請求裡回報的數字，不是本工具對「這次呼叫"
    "扣了幾格」的宣稱——抓照片與抓附件本身不扣履歷瀏覽配額。"
)
# Differentiated at the TOOL layer, because the shared per-request unit's
# own `except Exception` answers every transport failure with "可能是逾時或
# 網路問題，請稍後再試" — an invitation to an unbounded retry loop that costs
# another throttle slot and another résumé-detail request every time. Only
# the SECOND sub-request gets this wording, and the mechanism is the scope
# of its own `try`, not a "which sub-request is running" flag: GuardAbort
# carries only payload and kind, both sub-requests can raise `transport`,
# and a flag would go quietly wrong the next time the sequence grows.
_ASSET_TIMEOUT_ERROR = {
    "error": (
        "下載這個檔案超過 60 秒仍未完成（可能是檔案很大，或這條線路太慢）。"
        "請不要連續重試——每次重試都會再花掉一次履歷詳情請求與一個節流名額。"
        "量到最大的附件是 6.7 MB。"
    )
}


def sweep_expired_files(directory: Path) -> None:
    """Delete every ordinary file directly under `directory` whose mtime is
    older than RESUME_FILE_RETENTION_SECONDS. One `iterdir`, never
    recursive — this project creates no subdirectory here — and no filename
    pattern is consulted: the directory is ours alone, so "everything old"
    is both simpler and safer than a pattern that could go stale (orphaned
    `.part` files are swept by exactly this property).

    Both failure modes are logged and swallowed, and the READ failure being
    swallowed is a deliberate divergence from browser/throttle.py's
    compact_state_file, which re-raises its read failure. That one is a
    precondition of loading the throttle state: an unreadable ledger means
    the throttle cannot be trusted, and refusing to start is right. This
    directory is a precondition of nothing — not existing is simply what a
    first run looks like — and an unreadable one only means old files did
    not expire on time. Letting a permissions problem here block startup
    would cost the user even the ability to call login().

    The cost of swallowing it: personal data may outlive the retention
    period. The mitigation is that this sweep also runs before every write,
    so the same failure keeps resurfacing in the log rather than getting one
    chance at startup.
    """
    cutoff = time.time() - RESUME_FILE_RETENTION_SECONDS
    try:
        entries = list(directory.iterdir())
    except FileNotFoundError:
        return
    except OSError as exc:
        log.warning("resume-files: 無法讀取 %s，本次未清理過期檔案：%s", directory, exc)
        return

    for entry in entries:
        try:
            if not entry.is_file():
                continue
            if entry.stat().st_mtime >= cutoff:
                continue
            entry.unlink()
        except OSError as exc:
            log.warning("resume-files: 無法刪除過期檔案 %s：%s", entry.name, exc)


def clear_resume_files(directory: Path) -> str:
    """Remove the whole directory (tools/auth.py's logout()). Returns "" on
    success or a sentence to append to logout()'s existing warning when
    something could not be removed — logout()'s four-key return shape is a
    contract, so this reports through a key that is already always
    non-empty rather than adding a fifth."""
    if not directory.exists():
        return ""
    try:
        shutil.rmtree(directory)
    except OSError as exc:
        log.error("resume-files: logout() 未能清除 %s：%s", directory, exc)
        return (
            f"⚠ 已下載的候選人檔案未能從 {directory} 全部刪除，請自行手動刪除該目錄。"
        )
    return ""


def write_asset_atomically(directory: Path, filename: str, body_bytes: bytes) -> Path:
    """Write `body_bytes` to `directory/filename`, replacing any existing
    file at that path, without any window in which a reader could see a
    partial file. Creates `directory` lazily — a user who has never fetched
    an asset should not find an empty directory in their data dir.

    The temporary name carries the pid AND a random suffix, and that is not
    belt-and-braces: a temp name derived only from the target would let two
    concurrent fetches of the SAME asset interleave their writes into one
    temp file and then atomically publish a corrupt result. `os.replace`
    only promises nobody sees a half-swap; it promises nothing about what
    was swapped in. A unique suffix is enough — no lock is needed.

    A repeat fetch OVERWRITES rather than accumulating `-1`/`-2` copies:
    numbered copies would multiply one candidate's personal data on disk,
    the exact opposite of what the retention rule is for, and a second fetch
    means the caller wants the current file.
    """
    directory.mkdir(parents=True, exist_ok=True)
    target = directory / filename
    temp = directory / f"{filename}.{os.getpid()}-{uuid.uuid4().hex[:8]}.part"
    try:
        temp.write_bytes(body_bytes)
        os.replace(temp, target)
    except OSError:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    return target


def photo_filename(candidate_id: str, asset_format: str) -> str:
    """`photo-<candidate_id>.<ext>` — composed only of our own values."""
    return f"photo-{candidate_id}{_EXTENSION_FOR_FORMAT[asset_format]}"


def attachment_filename(candidate_id: str, sort: int, asset_format: str) -> str:
    """`attach-<candidate_id>-<sort>.<ext>` — composed only of our own
    values. 104's `filename` is not used, not even for its extension."""
    return f"attach-{candidate_id}-{sort}{_EXTENSION_FOR_FORMAT[asset_format]}"


def _invalid_candidate_id(candidate_id: str) -> dict | None:
    """None when `candidate_id` may be used, otherwise the error payload.

    All-digits, checked BEFORE any request is issued. Measured idNo values
    are 13-14 digit strings; this is also the path-traversal defence, since
    the value becomes part of a filename (`..`, `/` and `\\` all fail it).
    This is a guard rail on OUR side, not a rule 104 ever stated.
    """
    if isinstance(candidate_id, str) and candidate_id.isdigit():
        return None
    return {
        "error": (
            "candidate_id 必須是全數字（量到的履歷 idNo 是 13–14 位純數字）。"
            "這是本工具這一側的護欄，不是 104 講過的規則；請用 search_resumes / "
            "list_recommended_resumes / list_matched_resumes 回傳列上的 candidate_id。"
        )
    }


def _invalid_sort(sort: int) -> dict | None:
    """None when `sort` may be used, otherwise the error payload. A positive
    integer: it is 104's own per-attachment number and it becomes part of a
    filename."""
    if isinstance(sort, bool) or not isinstance(sort, int) or sort <= 0:
        return {"error": "sort 必須是正整數（104 自己在每筆附件上的編號，量到是 1..N）。"}
    return None


def _pick_asset_source(envelope: dict) -> dict:
    """The ONLY thing this module ever takes out of the résumé-detail
    envelope: the asset URLs and the quota. Nothing else — not the name, not
    the contact fields, not the autobiography — travels any further, into a
    return value, a warning, a log line or a filename.

    That is the whole guarantee, and it is narrower than "we did not read
    the résumé": the guard's own projection (`pick_data`) can only narrow
    the envelope one level, and `browseLimit` is absent entirely when 104
    does not send it (so a `pick_data=("resume","browseLimit")` would report
    a healthy response as malformed, and `pick_data=("resume",)` would throw
    the quota away). The whole envelope therefore does reach this process's
    memory; this function is what stops it going anywhere else.
    """
    data = envelope["data"]
    resume = data.get("resume")
    resume = resume if isinstance(resume, dict) else {}
    attachments = resume.get("attachArr")
    return {
        "personal_pic": resume.get("personalPic"),
        "attachments": attachments if isinstance(attachments, list) else [],
        "browse_limit": extract_browse_limit(data),
    }


def _base_warnings(browse_limit: dict | None) -> list[str]:
    # The note explains a number the caller is holding. With browse_limit
    # None (104 sent no browseLimit at all) there is no such number, and a
    # sentence glossing an absent field reads as if one were there.
    warnings = [_BROWSE_LIMIT_NOTE] if browse_limit else []
    quota_warning = browse_limit_warning(browse_limit)
    if quota_warning:
        warnings.append(quota_warning)
    return warnings


_GET_CANDIDATE_PHOTO_DESCRIPTION = """取得候選人的大頭照檔案本身（不是網址），寫進本機資料目錄後回傳路徑。

Args:
    candidate_id: 履歷列 / get_resume_detail 的 candidate_id（104 的 idNo，全數字）。

回傳固定形狀（鍵集合不隨結果變動、warnings 恆在）：
    {"photo": {"path": str, "bytes": int, "format": "jpeg"|"png"} | None,
     "browse_limit": {"resume_max","on_that_day_count"} | None, "warnings": [...]}

⚠ photo 為 null 是**正常狀態不是錯誤**：候選人沒放照片，或 104 給的是它自己的預設
頭像（warnings 會說明是哪一種）。失敗時回 {"error": str}，沒有 photo 這個鍵。

一次呼叫送出兩個 104 請求（先讀一次履歷詳情取網址，再抓檔案），因此佔兩個節流名額；
候選人沒有照片時只送一個。抓檔案本身不扣 104 的每日履歷瀏覽配額，但回傳的
browse_limit 來自那次履歷詳情請求。檔案落在資料目錄的 resume-files/ 底下，**24 小時
後自動清除**，logout() 會立即清除；單檔上限 32 MB。這些是候選人的個人資料。

⚠ 逾時（可能是大檔或線路慢）時不要連續重試：每次重試都會再花掉一次履歷詳情請求與一個
節流名額。⚠ 若回報「資產主機回傳了轉址頁」，那代表沒有被當成已登入（請重新 login()），
**不代表這位候選人沒有這個檔案**。"""

_GET_RESUME_ATTACHMENT_DESCRIPTION = """取得候選人履歷附件的檔案本身（不是網址），寫進本機資料目錄後回傳路徑。

Args:
    candidate_id: 履歷列 / get_resume_detail 的 candidate_id（104 的 idNo，全數字）。
    sort: 要哪一筆附件，用 104 自己的編號（get_resume_detail 的 attach_arr[].sort，正整數）。
          不接受標題、不接受網址、不接受陣列索引。

回傳固定形狀（鍵集合不隨結果變動、warnings 恆在）：
    {"attachment": {"path": str, "bytes": int, "format": "jpeg"|"png"|"pdf",
                    "sort": int, "title": str, "type": int},
     "browse_limit": {...} | None, "warnings": [...]}

失敗時回 {"error": str}，沒有 attachment 這個鍵；指定的 sort 不存在、而這份履歷確實有
其他附件時另附 "available": [{"sort","title"}, ...]。**指定一筆不存在的附件是問法有誤，
不是抓取失敗**（與照片不同：照片沒有是正常狀態，回 photo: null）。

一次呼叫送出兩個 104 請求（先讀一次履歷詳情取網址，再抓檔案），佔兩個節流名額。抓檔案
本身不扣履歷瀏覽配額，但 browse_limit 來自那次履歷詳情請求。檔案落在資料目錄的
resume-files/ 底下，**24 小時後自動清除**，logout() 立即清除；單檔上限 32 MB。這些是
候選人的個人資料。落地檔名一律由本工具自己組（104 給的原始檔名常含候選人姓名，一個位元
組都不使用），副檔名純由檔案本身的 magic bytes 決定。

⚠ 目前只認得 jpeg / png / pdf。其他型別（zip/OOXML 等）會被拒絕、不落地，錯誤訊息會帶
檔案前 8 個位元組的簽名——**請把那個簽名回報**，那就是補上這一族所需要的量測。
⚠ 逾時時不要連續重試（見錯誤訊息）。"""


def register_resume_file_tools(mcp: FastMCP):
    @require_login
    async def get_candidate_photo(candidate_id: str, ctx: Context) -> dict:
        invalid = _invalid_candidate_id(candidate_id)
        if invalid is not None:
            return invalid

        app = ctx.request_context.lifespan_context
        try:
            async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
                envelope = await request(
                    ENDPOINTS["get_resume_detail"], params=[("idno", candidate_id)]
                )
                source = _pick_asset_source(envelope)
                browse_limit = source["browse_limit"]
                warnings = _base_warnings(browse_limit)

                url = source["personal_pic"]
                if not isinstance(url, str) or not url.strip():
                    warnings.insert(0, _NO_PHOTO_WARNING)
                    return {"photo": None, "browse_limit": browse_limit, "warnings": warnings}

                hostname = urlparse(url).hostname
                if hostname == _PLACEHOLDER_PHOTO_HOST:
                    warnings.insert(0, _PLACEHOLDER_PHOTO_WARNING)
                    return {"photo": None, "browse_limit": browse_limit, "warnings": warnings}
                if hostname != _ASSET_HOSTNAME:
                    # An error, NOT photo: null. "we do not fetch from an
                    # unmeasured host" and "this candidate has no photo" are
                    # different facts. Only the hostname is named — no path,
                    # no query string (that is where the token lives).
                    return {
                        "error": (
                            f"104 給的大頭照網址在一個本工具沒有量測過的主機上"
                            f"（{hostname or '無法解析主機名'}）。本工具只抓 104 自己在已量測"
                            "資產主機上交出來的檔案，不會去抓其他主機。"
                        )
                    }

                # Only the second sub-request is wrapped: see
                # _ASSET_TIMEOUT_ERROR for why the try's SCOPE is the
                # mechanism rather than a flag.
                try:
                    asset = await request(ASSET_ROUTES["candidate_photo"], asset_url=url)
                except GuardAbort as e:
                    if e.kind == "transport":
                        return _ASSET_TIMEOUT_ERROR
                    raise

                body_bytes = asset["body_bytes"]
                sweep_expired_files(app.config.resume_files_dir)
                path = write_asset_atomically(
                    app.config.resume_files_dir,
                    photo_filename(candidate_id, asset["format"]),
                    body_bytes,
                )
                return {
                    "photo": {
                        "path": str(path),
                        "bytes": len(body_bytes),
                        "format": asset["format"],
                    },
                    "browse_limit": browse_limit,
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload

    @require_login
    async def get_resume_attachment(candidate_id: str, sort: int, ctx: Context) -> dict:
        invalid = _invalid_candidate_id(candidate_id)
        if invalid is not None:
            return invalid
        invalid = _invalid_sort(sort)
        if invalid is not None:
            return invalid

        app = ctx.request_context.lifespan_context
        try:
            async with guarded_sequence(ctx, slots_needed=2) as (request, _info):
                envelope = await request(
                    ENDPOINTS["get_resume_detail"], params=[("idno", candidate_id)]
                )
                source = _pick_asset_source(envelope)
                browse_limit = source["browse_limit"]
                warnings = _base_warnings(browse_limit)

                attachments = [a for a in source["attachments"] if isinstance(a, dict)]
                chosen = next((a for a in attachments if a.get("sort") == sort), None)
                if chosen is None:
                    if not attachments:
                        return {
                            "error": (
                                "這位候選人的履歷沒有附件。這不是抓取失敗，也不是登入問題——"
                                "這份履歷上就沒有可以下載的附件。"
                            )
                        }
                    # `available` only when there ARE attachments — same rule
                    # search_resumes' `candidates` follows. Titles are the
                    # candidate's own words and are already handed to the
                    # Agent by get_resume_detail, so listing them here is no
                    # new exposure and makes the correction one step.
                    return {
                        "error": (
                            f"這份履歷沒有第 {sort} 筆附件。這不是抓取失敗，是指名了一個"
                            "不存在的項目；請改用下列 available 裡的 sort。"
                        ),
                        "available": [
                            {"sort": a.get("sort"), "title": a.get("title")}
                            for a in attachments
                        ],
                    }

                url = chosen.get("link")
                if not isinstance(url, str) or not url.strip():
                    return {
                        "error": (
                            f"104 沒有給第 {sort} 筆附件的下載網址（link 缺席或為空），"
                            "本工具無法下載它。"
                        )
                    }

                try:
                    asset = await request(ASSET_ROUTES["resume_attachment"], asset_url=url)
                except GuardAbort as e:
                    if e.kind == "transport":
                        return _ASSET_TIMEOUT_ERROR
                    raise

                body_bytes = asset["body_bytes"]
                sweep_expired_files(app.config.resume_files_dir)
                path = write_asset_atomically(
                    app.config.resume_files_dir,
                    attachment_filename(candidate_id, sort, asset["format"]),
                    body_bytes,
                )
                return {
                    # `filename`, `link` and `preview` are deliberately NOT
                    # published. Not because the name is a secret
                    # (get_resume_detail hands it over) but because putting
                    # it here would offer a one-line "rename it back"; and a
                    # URL the Agent cannot open is the same thing master_url
                    # was dropped for. `title`/`type` pass through verbatim
                    # — `type` was 1 on all 14 measured attachments and its
                    # value domain is unknown, so it is not interpreted.
                    "attachment": {
                        "path": str(path),
                        "bytes": len(body_bytes),
                        "format": asset["format"],
                        "sort": sort,
                        "title": chosen.get("title"),
                        "type": chosen.get("type"),
                    },
                    "browse_limit": browse_limit,
                    "warnings": warnings,
                }
        except GuardAbort as e:
            return e.payload

    # Same registration discipline as the other tool modules: the runtime
    # description becomes the function's actual __doc__, never a parallel
    # `description=` argument the docstring itself does not carry.
    get_candidate_photo.__doc__ = _GET_CANDIDATE_PHOTO_DESCRIPTION
    get_resume_attachment.__doc__ = _GET_RESUME_ATTACHMENT_DESCRIPTION
    mcp.tool()(get_candidate_photo)
    mcp.tool()(get_resume_attachment)
