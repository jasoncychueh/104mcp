"""登入工具、完成偵測與恢復驗證：對 Agent 提供 `login`/`check_login`/`logout`；在背景
偵測登入完成（雙因子：主 frame 精確落在 vip.104.com.tw、且應用 session cookie 出現）；
在恢復路徑上以一次真實的 API 呼叫驗證登入狀態確實可用，不憑 cookie 自己的到期屬性判斷。

一次登入的完整生命週期跨三個元件——`browser/cdp_stream.py`（串流本身）、
`web/auth_server.py`（路由與 admission）、本模組（pending pool 與終局紀錄）——但狀態
表本身只有一個出處：`AppContext._pending_logins[token].state`（`LoginState`，定義在
本模組）。這個 dict 由 `main.py` 的 `AppContext` 持有，本模組是它唯一的讀寫者。

`AppContext.logout_epoch` 是 `logout()` 用來讓一個逾時取消未能在預算內結束的 watcher
自行放棄的機制：`logout()` 在做任何其他事之前把它加一；每個 watcher 在建立時捕獲當下的
值，並在寫入憑證檔的正前方重讀它，不同就整段放棄——這段重讀與寫入之間刻意不含任何
`await`，讓「已經讀過、還沒寫入」的中間狀態在單一事件迴圈上不可能被觀察到。
"""

import asyncio
import logging
import os
import secrets
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, AsyncIterator
from urllib.parse import urlparse

from mcp.server.fastmcp import Context, FastMCP

from mcp104.browser.api_client import ENDPOINTS
from mcp104.browser.cdp_stream import CdpLoginStream, POST_SUCCESS_SETTLE_SECONDS
from mcp104.browser.session import (
    PendingLogin,
    SessionInfo,
    clear_cookies,
    load_cookies,
    save_cookies,
)
from mcp104.browser.stealth import (
    MissingBrowserError,
    create_stealth_context,
    install_browser,
    launch_browser,
)
from mcp104.tools.helpers import (
    ERROR_BLOCKED_API_RESTORE_VERIFY,
    GuardAbort,
    get_session_id,
    guarded_api,
)
from mcp104.web.auth_server import create_auth_app, start_auth_site

if TYPE_CHECKING:
    from patchright.async_api import Browser, BrowserContext, Page

LOGIN_URL = "https://bsignin.104.com.tw/login"

# 104 使用 ORY Hydra 做 OIDC，登入鏈為
# bsignin → boidc(OAuth2+PKCE) → bsignin/mfa → bsignin/product → vip.104.com.tw/rms/index。
# MFA（validation_type: unreliable_device）在容器內每次都會觸發，因為每次登入都是
# 全新的瀏覽器設定檔、沒有裝置指紋歷史——所以「靜默重新登入」在設計上不可能，
# 一定要真人在檢視頁上完成。詳見 docs/104-site-facts.md。

log = logging.getLogger("104-mcp.auth")

# app-session cookies that actually survive the transfer into the next
# process (its/ithp, ~半天). PHPSESSID is session-only and does not — cookie
# presence there would be a false positive for "logged in".
VIP_SESSION_COOKIE_NAMES = ("its", "ithp")
COOKIE_POLL_INTERVAL = 1.0  # seconds
WATCHER_CANCEL_TIMEOUT = 10.0  # seconds — see _finalize_pending_login

# 一次工具呼叫在伺服器端最多等多久——兩個數字都刻意壓在 90 秒：本 repo 建議的 MCP client
# 逾時是 120 秒（README／.mcp.json 的 "timeout"），Claude Code 文件說這個欄位對 stdio
# server 同樣生效、且是硬性牆鐘時間；90 秒對 120 秒留有餘裕，一次完整真人登入（約 265
# 秒）由 Agent 連續呼叫幾次 check_login 湊成，每次都在單一呼叫的預算內。
BROWSER_INSTALL_WAIT_SECONDS = 90.0  # login() 一次呼叫最多等背景安裝多久
CHECK_LOGIN_MAX_WAIT_SECONDS = 90  # check_login 的 wait_seconds 上限
CHECK_LOGIN_POLL_INTERVAL = 0.5  # seconds

INSTALLING_BROWSER_MESSAGE = (
    "首次使用：正在背景下載安裝 login() 需要的 Chromium（約 300 MB，通常幾十秒到幾分鐘）。"
    "請稍後再呼叫一次 login()——每次呼叫最多等 90 秒，裝好就會直接接著開登入，不需要"
    "使用者做任何事。"
)


class LoginState(str, Enum):
    """一次登入在本行程記憶體裡的內部生命週期狀態——只活在這裡，刻意與線上 `state`
    值（"waiting"/"completed"）用不相交的字串集合，讓任何一處寫錯都是讀得出來的
    錯字。`handed_off`/`abandoned` 兩個終端狀態不出現在這裡：到達它們的同時，項目
    就從 `AppContext._pending_logins` 移除了——見 `_finalize_pending_login`。"""

    AWAITING_HUMAN = "awaiting_human"
    SETTLING = "settling"


@dataclass
class PendingLoginResources:
    """`AppContext._pending_logins` 的值：一次登入自己擁有的一切，與其他登入互不
    共用。`state` 是這裡的必要欄位（不是註記）——admission 與 `login()` 的分支選擇
    都讀它，兩者都不得改用「項目在不在」推導。"""

    browser: "Browser"
    context: "BrowserContext"
    page: "Page"
    stream: CdpLoginStream
    state: LoginState


@dataclass(frozen=True)
class RestoreVerdict:
    """`verify_restored_session` 的回傳值——無損地帶出 `guarded_api` 的判定，不折成
    一個小列舉。`alive` 為 `True` 時 `kind`/`payload` 沒有意義（唯一的存活路徑，見
    `verify_restored_session`）；為 `False` 時 `kind` 是 `guarded_api` 逐字帶出的
    `GuardAbort.kind`，`payload` 是要回給 Agent 的形狀（唯一例外：`kind == "blocked"`
    時換成這條路徑專屬的措辭），`keep_jar` 說這個 `kind` 底下憑證檔該不該保留——只有
    `expired` 為 `False`，其餘每一個 kind、以及任何未涵蓋的 kind，一律保留。"""

    alive: bool
    kind: str
    payload: dict
    keep_jar: bool


@dataclass(frozen=True)
class ServerLogoutResult:
    """`request_server_logout` 的回傳值。`state` 只有兩個值："unconfirmed"（請求送出
    去了，但 104 那一側的 session 沒有任何已知路徑會因此作廢）或 "not_sent"（請求根本
    沒有送出，因為沒有 session 可用、或本行程自己的前置條件壞掉）。`kind` 是
    `guarded_api` 的 `GuardAbort.kind`，逐字帶出，恆非空。`detail` 是給操作者的說明，
    直接構成 `logout()` 的 `warning`，恆非空。"""

    state: str
    kind: str
    detail: str


# 每一次登出都要說的那句話，不論 kind 是什麼——104 那一側的 session 本專案無法作廢
# （[M §8.8-4]：/oidc/logout 不作廢 vip 的 its/ithp session，104 前端也沒有第二條會
# 作廢它的路由），它會活到下一次「在任何地方」的登入把它頂掉（104 的單一登入規則）。
_SERVER_SESSION_PERSISTS_NOTE = (
    "104 那一側的 session 本專案無法作廢，它會持續有效，直到下一次"
    "「在任何地方」的登入把它頂掉（104 的單一登入規則）。"
)

# logout_unconfirmed 標記存在時，附在 restored/already_logged_in 回應上的警告——
# 讀取側見 login() 內的 _with_logout_unconfirmed_warning；寫入側見 logout()。
_LOGOUT_UNCONFIRMED_WARNING = (
    "這個登入可能就是你上一次要求登出、而我們沒能向 104 確認的那一個"
    "（104 的單一登入規則使伺服器端 session 無法被本專案主動作廢，"
    "見 logout() 回傳的 warning 說明）。"
)


def _has_vip_session_cookie(cookies: list[dict]) -> bool:
    for c in cookies:
        if c.get("domain") not in (".vip.104.com.tw", "vip.104.com.tw"):
            continue
        if c.get("name") in VIP_SESSION_COOKIE_NAMES:
            return True
    return False


# ── Restore verification ──────────────────────────────────────────────


async def verify_restored_session(ctx: Context) -> RestoreVerdict:
    """一次已認證的 API 呼叫，用來證明一份 cookie jar（不論是 pool 裡既有的、還是剛從
    憑證檔暫時登錄的）現在還能用。判定只依據 104 這次回應本身——不看 cookie 自己的到期
    屬性（那個判準曾經量到「依屬性未過期、事實上已經死了」）。走 `guarded_api` 與
    `verify_session` 端點，不啟動瀏覽器；該端點已量測為不消耗履歷瀏覽額度，送出空
    參數（同一份憑證下與送滿十個參數同樣是 200 + SUCCESS）。
    """
    app = ctx.request_context.lifespan_context
    try:
        async with guarded_api(ctx, ENDPOINTS["verify_session"]) as (_payload, _info):
            pass
    except GuardAbort as e:
        kind = e.kind
        payload = ERROR_BLOCKED_API_RESTORE_VERIFY if kind == "blocked" else e.payload
        keep_jar = kind != "expired"
        if not keep_jar:
            clear_cookies(app.config.cookies_path)
        return RestoreVerdict(alive=False, kind=kind, payload=payload, keep_jar=keep_jar)
    return RestoreVerdict(alive=True, kind="", payload={}, keep_jar=True)


class _ProvisionalRegistration:
    """The object `provisional_session` yields — its only affordance is
    `commit()`, which keeps the registration alive past the context
    manager's exit instead of it being torn down."""

    def __init__(self) -> None:
        self._committed = False

    def commit(self) -> None:
        self._committed = True


@asynccontextmanager
async def provisional_session(ctx: Context, cookies: list[dict]) -> AsyncIterator[_ProvisionalRegistration]:
    """暫時把 `cookies` 登錄為這個 MCP session 的 `SessionInfo`，讓 `guarded_api`
    （它必須從 pool 解析 session）可以在恢復驗證期間使用它。除非呼叫端呼叫
    `commit()`，離開時（含拋出例外、含被取消）一律把這次登錄解除，不留下任何東西。

    典型用法：

        async with provisional_session(ctx, cookies) as reg:
            verdict = await verify_restored_session(ctx)
            if verdict.alive:
                reg.commit()

    `CancelledError` 不被吞掉——`finally` 完成解除登錄之後原樣往上拋，取消發生時已經
    沒有人在等這次呼叫的回傳值了。
    """
    app = ctx.request_context.lifespan_context
    session_id = get_session_id(ctx)
    info = SessionInfo(cookies=cookies, account_label=app.config.account_label)
    app.session_pool.activate_direct(session_id, info)
    registration = _ProvisionalRegistration()

    try:
        yield registration
    finally:
        if not registration._committed:
            app.session_pool.remove(session_id)


# ── Server-side logout (best-effort) ──────────────────────────────────


async def request_server_logout(ctx: Context) -> ServerLogoutResult:
    """向 104 送出那一次順帶的登出請求（`logout_session` 端點；不經過節流判定閘門，
    但仍計入節流帳本）。絕不拋出——它的每一種失敗都是 `logout()` 要回報的資訊，不是
    `request_server_logout` 自己的失敗。回傳值不是任何保證：104 那一側的 session 沒有
    任何已知路徑會因為這次請求而作廢，見模組頂端 `_SERVER_SESSION_PERSISTS_NOTE`。
    """
    try:
        async with guarded_api(ctx, ENDPOINTS["logout_session"]) as (_payload, _info):
            # 量測顯示這條路徑實務上到不了：/oidc/logout 的第一個回應是 302 導向
            # boidc.104.com.tw，guarded_api 的認證主機檢查在 classify() 之前就會
            # 以 GuardAbort(kind="expired") 中止。仍然寫出這個分支，讓一個沒有
            # 已知成因的成功回應也有一個誠實的答案，而不是讓程式碼隱含「這裡到不了」。
            kind = "success"
            detail = "104 回應了看起來成功的信封，但沒有任何已知路徑能確認 vip session 真的被作廢。"
            return ServerLogoutResult(state="unconfirmed", kind=kind, detail=f"{detail}{_SERVER_SESSION_PERSISTS_NOTE}")
    except GuardAbort as e:
        kind = e.kind
        state = "not_sent" if kind in ("not_logged_in", "internal_config") else "unconfirmed"
        cause = _SERVER_LOGOUT_CAUSE_SENTENCES.get(kind)
        if cause is None:
            cause = f"104 對這次登出請求給出一個未預期的回應（kind={kind}）。"
        return ServerLogoutResult(state=state, kind=kind, detail=f"{cause}{_SERVER_SESSION_PERSISTS_NOTE}")
    except Exception:
        # 絕不拋出：任何未預期的例外都必須落地成一個誠實、保守（"unconfirmed"，不是
        # "not_sent"）的答案，而不是讓 logout() 連本機那一半都因此放棄。
        log.exception("request_server_logout: unexpected exception, not a GuardAbort")
        cause = "送出這次登出請求時發生未預期的內部錯誤。"
        return ServerLogoutResult(state="unconfirmed", kind="internal_config", detail=f"{cause}{_SERVER_SESSION_PERSISTS_NOTE}")


_SERVER_LOGOUT_CAUSE_SENTENCES = {
    "not_logged_in": "這段期間沒有可用的 session（真人可能仍在完成登入），因此沒有送出這次請求。",
    "internal_config": "本行程自己的前置條件出了問題，因此沒有送出這次請求。",
    "expired": (
        "104 的回應是一次重新導向到認證主機（HTTP 302）——這是本專案自己的認證主機"
        "檢查對這個重導向的反應，不是 104 對這次登出的判決；同一份憑證的複驗顯示"
        "session 仍然有效。"
    ),
    "transport": "這次請求逾時或連線失敗，無法確認它是否已經送達 104。",
    "challenge": "104 對這次登出請求回應了機器人驗證挑戰。",
    "blocked": "104 以 403 拒絕了這次登出請求。",
}


# ── login() ────────────────────────────────────────────────────────────


def _with_logout_unconfirmed_warning(app, result: dict) -> dict:
    """只在判定為 alive、且上一次 logout() 留下的標記存在時附上警告——這是跨行程
    殘留（一份被要求登出、卻仍然活著的憑證檔）唯一的告知管道。標記本身不因為這次
    讀取而被清除：清除規則只有一條，見 _watch_for_login 裡對它的處理。"""
    if app.config.logout_unconfirmed_path.exists():
        result = dict(result)
        result["warning"] = _LOGOUT_UNCONFIRMED_WARNING
    return result


def _shape_restore_failure_payload(verdict: RestoreVerdict) -> dict:
    """處置表對 `kind == "transport"` 這一列規定的回傳形狀是
    `{"status": "unknown", "error": …}`，不是守衛原樣的 `{"error": …}`——這是
    `RestoreVerdict.payload` 未涵蓋的那一層轉換（`RestoreVerdict.payload` 只對
    `blocked` 換過一次，見該 dataclass 的 docstring），由呼叫端在組出最終回應時補上。
    其餘每一個 kind 原樣回傳守衛（或 `blocked` 專屬措辭）給的 payload。"""
    if verdict.kind == "transport":
        return {"status": "unknown", **verdict.payload}
    return verdict.payload


async def _login_via_pool_session(app, ctx: Context, session_id: str) -> dict | None:
    """分支 B：記憶體中已有 session。回傳一個要交給 Agent 的 dict，或 `None` 表示
    verdict.kind == "expired"——此時憑證檔已刪（由 `verify_restored_session` 執行）、
    pool 裡的 session 已移除，呼叫端應落到真人登入流程。"""
    verdict = await verify_restored_session(ctx)
    if verdict.alive:
        return _with_logout_unconfirmed_warning(app, {"status": "already_logged_in"})
    if verdict.kind == "expired":
        app.session_pool.remove(session_id)
        return None
    return _shape_restore_failure_payload(verdict)


async def _login_via_cookie_file(app, ctx: Context, cookies: list[dict]) -> dict | None:
    """分支 C：讀到憑證檔。回傳形狀同 `_login_via_pool_session`。"""
    async with provisional_session(ctx, cookies) as reg:
        verdict = await verify_restored_session(ctx)
        if verdict.alive:
            reg.commit()
    if verdict.alive:
        return _with_logout_unconfirmed_warning(app, {"status": "restored"})
    if verdict.kind == "expired":
        return None
    return _shape_restore_failure_payload(verdict)


def _make_get_admissible_stream(app):
    """`web/auth_server.py` 需要的查表函式：`token -> CdpLoginStream | None`，只在
    該項目的 `state` 為 `awaiting_human` 時回傳串流——`settling` 期間項目雖然還在，
    一律回 `None`，讓新連線 404 而既有連線不受影響。"""

    def get_admissible_stream(token: str) -> "CdpLoginStream | None":
        resource = app._pending_logins.get(token)
        if resource is None or resource.state != LoginState.AWAITING_HUMAN:
            return None
        return resource.stream

    return get_admissible_stream


def _open_in_local_browser(url: str) -> bool:
    """只在「真人與本行程同機」的組態（臨時埠形態，`auth_bind_port is None`）下由呼叫端
    決定要不要呼叫：用作業系統的預設瀏覽器打開 login_url，省掉「Agent 貼網址、真人
    複製貼上」那一步。子行程的 stdin/stdout/stderr 一律接 DEVNULL——本行程的 stdout 是
    MCP 協定通道，xdg-open 之類的工具會往上面寫字。失敗回 False、不拋出：打不開只是
    少了一個便利，login_url 照樣回給 Agent 轉交真人。"""
    devnull = subprocess.DEVNULL
    try:
        if sys.platform.startswith("win"):
            os.startfile(url)  # type: ignore[attr-defined]
        elif sys.platform == "darwin":
            subprocess.Popen(["open", url], stdin=devnull, stdout=devnull, stderr=devnull)
        else:
            subprocess.Popen(["xdg-open", url], stdin=devnull, stdout=devnull, stderr=devnull)
        return True
    except Exception as exc:
        log.info("Could not open the login page in a local browser: %s", exc)
        return False


async def _open_login_browser() -> tuple:
    """開一顆無頭 stealth 瀏覽器、導覽到登入頁、啟動 CDP 串流；回 (browser, context,
    page, stream)。任何一步失敗都先關掉已經開出來的東西再往上拋——這時還沒有登錄任何
    pending 項目，沒有別人會替這些資源收尾。"""
    browser: "Browser | None" = None
    context: "BrowserContext | None" = None
    try:
        browser = await launch_browser(headless=True)
        context = await create_stealth_context(browser)
        page = await context.new_page()
        await page.goto(LOGIN_URL)
        stream = CdpLoginStream(page)
        await stream.start()
        return browser, context, page, stream
    except Exception:
        # narrowest-scope-first, each step swallowing its own exception so
        # one failed close does not hide another.
        if context is not None:
            try:
                await context.close()
            except Exception:
                log.exception("Failed closing context after a failed login start")
        if browser is not None:
            try:
                await browser.close()
            except Exception:
                log.exception("Failed closing browser after a failed login start")
        raise


async def _await_browser_install(app) -> dict | None:
    """缺瀏覽器時的自動安裝：整個行程最多一個安裝子行程（`app._browser_install`），
    沒有就啟動一個；然後在本次呼叫的預算（BROWSER_INSTALL_WAIT_SECONDS）內等它。
    回 None 表示裝好了、呼叫端可以重試啟動瀏覽器；回 dict 表示這次 login() 要原樣回給
    Agent 的形狀——還在裝（status: installing_browser）或裝失敗（error；下一次 login()
    會重新啟動一次安裝）。本次呼叫的等待被取消（client 斷線）時安裝子行程照跑不誤。"""
    task = app._browser_install
    if task is None or (task.done() and (task.cancelled() or task.exception() is not None)):
        log.warning("Chromium is missing — starting `patchright install chromium` in the background")
        task = asyncio.create_task(install_browser())
        app._browser_install = task
    done, _ = await asyncio.wait({task}, timeout=BROWSER_INSTALL_WAIT_SECONDS)
    if not done:
        return {"status": "installing_browser", "message": INSTALLING_BROWSER_MESSAGE}
    app._browser_install = None
    if task.cancelled():
        return {"error": "自動安裝 Chromium 被中止（行程關閉中）。再呼叫一次 login() 會重新安裝。"}
    exc = task.exception()
    if exc is not None:
        return {
            "error": (
                f"自動安裝 Chromium 失敗：{exc}。再呼叫一次 login() 會重試；若持續失敗，"
                "請使用者在裝有本套件的環境手動執行 `python -m patchright install chromium`。"
            )
        }
    return None


async def _start_human_login(app, ctx: Context, session_id: str) -> dict:
    """開一次全新的真人登入：開一顆無頭瀏覽器（畫面由 CDP screencast 送出，不需要
    顯示器；缺瀏覽器時先走自動安裝）、延後啟動監聽端（第一次真的需要它才啟動）、
    登錄 pending 項目、啟動背景 watcher，並在同機組態下順手用預設瀏覽器打開 login_url。

    不需要在這裡清掉「這個 session 的舊 pending 項目」：能走到這個函式，代表
    login() 上面的分支 A 已經確認這個 session 沒有任何 `state == awaiting_human`
    的 pending token——而一個離開 `awaiting_human` 的 token，`SessionPool.activate()`
    已經在同一個同步函式體內把它從 `_token_to_session` 彈掉，所以不存在一個「非
    awaiting_human、卻仍然掛在這個 session 底下」的孤兒 token 需要額外收拾。
    """
    try:
        browser, context, page, stream = await _open_login_browser()
    except MissingBrowserError:
        outcome = await _await_browser_install(app)
        if outcome is not None:
            return outcome
        # 裝好了：再試一次；這次再缺就是真的壞了，原樣往上拋給 Agent 看。
        browser, context, page, stream = await _open_login_browser()

    if app.auth_site is None:
        auth_app = create_auth_app(_make_get_admissible_stream(app))
        app.auth_site = await start_auth_site(auth_app, app.config)

    token = secrets.token_urlsafe(32)
    app.session_pool.add_pending(token, PendingLogin(mcp_session_id=session_id))
    app._pending_logins[token] = PendingLoginResources(
        browser=browser, context=context, page=page, stream=stream,
        state=LoginState.AWAITING_HUMAN,
    )

    watcher_epoch = app.logout_epoch
    watcher_task = asyncio.create_task(_watch_for_login(app, token, watcher_epoch))
    app._watcher_tasks[token] = watcher_task
    watcher_task.add_done_callback(lambda t, tok=token: _on_watcher_done(app, tok, t))

    login_url = f"{app.auth_site.base_url}/auth/{token}"
    opened = _open_in_local_browser(login_url) if app.config.auth_bind_port is None else False
    return {"login_url": login_url, "token": token, "browser_opened": opened}


# ── Background completion watcher ─────────────────────────────────────


def _on_watcher_done(app, token: str, task: asyncio.Task) -> None:
    """Done-callback for the watcher task. Its only job is to log an
    unexpected crash — _watch_for_login's own try/except already runs
    teardown for every exit path, so this does not duplicate that."""
    app._watcher_tasks.pop(token, None)
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        log.error("Login watcher for token %s ended with an exception: %s", token[:8], exc)


async def _finalize_pending_login(app, token: str, reason: str) -> bool:
    """收尾函式：本模組四個進入點（watcher 逾時、watcher 自己的取消處理、行程關閉、
    `logout()`）共用的同一個冪等函式。讀 `PendingLoginResources.state` 決定自己正在
    執行哪一個轉換——`awaiting_human` 時是 `abandoned`（寫入 `_finished_logins`）、
    `settling` 時是（提早的）`handed_off`（不寫入）。

    先取消 watcher 並等它結束（有界，`WATCHER_CANCEL_TIMEOUT`）才動任何瀏覽器資源
    ——一個尚未被此函式呼叫的呼叫端不會與仍在原子區塊裡的 watcher 競爭。回傳值是
    這次取消是否在預算內確認完成，成為 `logout()` 的 `teardown_confirmed`。

    對自己所屬的 watcher（即目前正在執行這段程式碼的那個 task）跳過取消/等待——
    `_watch_for_login` 自己的例外處理路徑也會呼叫本函式，asyncio 不允許一個 task
    await 它自己。
    """
    log.warning("Tearing down pending login for token %s: %s", token[:8], reason)

    task = app._watcher_tasks.pop(token, None)
    current = asyncio.current_task()
    cancel_confirmed = True
    if task is not None and task is not current and not task.done():
        task.cancel()
        try:
            await asyncio.wait_for(task, timeout=WATCHER_CANCEL_TIMEOUT)
        except asyncio.CancelledError:
            pass
        except asyncio.TimeoutError:
            cancel_confirmed = False
            log.error(
                "Watcher task for token %s did not finish within %.0fs of cancellation "
                "— proceeding with teardown regardless; the watcher task itself is left "
                "to finish (or discard itself against the logout-epoch gate) on its own",
                token[:8], WATCHER_CANCEL_TIMEOUT,
            )
        except Exception:
            log.exception("Watcher task for token %s raised while being cancelled", token[:8])

    resource = app._pending_logins.pop(token, None)
    app.session_pool.discard_pending(token)
    if resource is not None:
        if resource.state == LoginState.AWAITING_HUMAN:
            app._finished_logins[token] = "abandoned"
        # SETTLING → (early) handed_off: 依既有規則不寫入 _finished_logins——一個
        # 完成的登入在 session 還在時由 check_login 的優先順序第 1 條回答。
        try:
            await resource.stream.stop()
        except Exception:
            log.exception("Failed stopping CDP stream for token %s", token[:8])
        try:
            await resource.context.close()
        except Exception:
            log.exception("Failed closing context for token %s", token[:8])
        try:
            await resource.browser.close()
        except Exception:
            log.exception("Failed closing browser for token %s", token[:8])

    if (
        not app._pending_logins
        and app.config.auth_bind_port is None
        and app.auth_site is not None
    ):
        # 臨時埠形態：清空即釋放。先 detach 欄位、再 await close()——反過來寫會在
        # 兩個動作之間留下一個懸掛點，讓一個併發的 login() 重用一個正在被拆掉的
        # 監聽端。固定埠形態保留監聽端到行程關閉（main.py 的收尾負責）。
        endpoint = app.auth_site
        app.auth_site = None
        await endpoint.close()

    return cancel_confirmed


async def _watch_for_login(app, token: str, watcher_epoch: int) -> None:
    """Background task: detect login completion by watching for the main
    frame to actually settle on vip.104.com.tw, then confirming the
    app-session cookies have arrived (both conditions share one deadline,
    app.config.login_timeout_seconds).

    On success it runs the lifecycle table's atomic block — reread
    AppContext.logout_epoch, save_cookies, SessionPool.activate(), flip
    this resource's state, CdpLoginStream.mark_completed() — with no
    `await` between the epoch reread and mark_completed(): a logout() that
    increments the epoch either lands before this block starts (this
    watcher discards itself) or after it has already finished (logout()
    then tears the now-`settling` resource down through its own path).
    There is no third case a single-threaded event loop could observe.

    Every exit path — timeout, cancellation, or an unexpected crash — runs
    _finalize_pending_login exactly once via the try/except below, unless
    the atomic block already completed (`succeeded`), in which case
    cancellation lets the caller that cancelled this task own the
    teardown instead of this function racing it.
    """
    resource = app._pending_logins.get(token)
    if resource is None:
        return

    context, page = resource.context, resource.page
    succeeded = False

    def on_frame_navigated(frame):
        if frame == page.main_frame:
            hostname = urlparse(frame.url).hostname or ""
            if hostname == "vip.104.com.tw":
                log.info("Main frame settled on vip.104.com.tw: %s", frame.url)
                login_detected.set()

    login_detected = asyncio.Event()

    try:
        loop = asyncio.get_running_loop()
        overall_deadline = loop.time() + app.config.login_timeout_seconds

        page.on("framenavigated", on_frame_navigated)
        try:
            remaining = max(0.0, overall_deadline - loop.time())
            await asyncio.wait_for(login_detected.wait(), timeout=remaining)
        except asyncio.TimeoutError:
            await _finalize_pending_login(app, token, "timed out waiting for vip.104.com.tw navigation")
            return
        finally:
            page.remove_listener("framenavigated", on_frame_navigated)

        if app._pending_logins.get(token) is not resource:
            # Already superseded/torn down by another path while we were
            # waiting — nothing left to do.
            return

        cookies = await context.cookies()
        while not _has_vip_session_cookie(cookies):
            if app._pending_logins.get(token) is not resource:
                return
            if loop.time() >= overall_deadline:
                await _finalize_pending_login(app, token, "vip.104.com.tw session cookie never appeared")
                return
            await asyncio.sleep(COOKIE_POLL_INTERVAL)
            cookies = await context.cookies()

        # ── Atomic block: no `await` between the epoch reread below and
        # mark_completed() at the end of it. ──────────────────────────
        if app.logout_epoch != watcher_epoch:
            # logout() has revoked every watcher's authorization since this
            # one was created — discard this login without writing
            # anything (credential file, pool registration, or state).
            return
        save_cookies(app.config.cookies_path, cookies)
        activated = app.session_pool.activate(token, SessionInfo(
            cookies=cookies, account_label=app.config.account_label,
        ))
        resource.state = LoginState.SETTLING
        resource.stream.mark_completed()
        # ── end atomic block ──────────────────────────────────────────

        succeeded = True

        if not activated:
            # The pending registration was already consumed by a
            # concurrent teardown that ran ahead of this watcher (the
            # logout-epoch gate above only protects against logout() —
            # see this module's docstring). resource.state above still
            # flips for consistency, but nothing external observes it any
            # more; mark_completed() itself is a no-op if that teardown
            # already called stream.stop() first.
            log.warning(
                "Login for token %s completed but its pending registration was "
                "already gone by the time it did — credentials were still saved "
                "and are recoverable via the next login()'s restore path",
                token[:8],
            )

        log.info("Login auto-completed for token %s", token[:8])

        # 清除 logout_unconfirmed 標記——唯一的清除條件：一次真人登入完成並寫下
        # 新憑證檔。排在原子區塊之外（是一次檔案 I/O，會引入 await）。
        app.config.logout_unconfirmed_path.unlink(missing_ok=True)

        await resource.stream.announce_completed()
        await asyncio.sleep(POST_SUCCESS_SETTLE_SECONDS)
        await _finalize_pending_login(app, token, "settle window elapsed")

    except asyncio.CancelledError:
        # Cancellation is the one path where someone else may already own
        # teardown: a task is only cancelled from the outside, and the only
        # caller that cancels this watcher (_finalize_pending_login) is
        # already mid-teardown for this token when it does so. Any other
        # exception below has no such other owner and must always finalize,
        # `succeeded` or not — a resource that failed after the atomic
        # block completed (e.g. removing the logout_unconfirmed marker)
        # would otherwise leave the pending entry stuck in `settling`
        # forever with no one left to clean it up.
        if not succeeded:
            await _finalize_pending_login(app, token, "watcher task cancelled")
        raise
    except Exception:
        log.exception("Login watcher for token %s crashed", token[:8])
        await _finalize_pending_login(app, token, "unexpected exception in watcher")


# ── MCP tools ──────────────────────────────────────────────────────────


async def login(ctx: Context) -> dict:
    """啟動 104 人力銀行登入流程（立即回傳，不等真人）。

    先用既有狀態（記憶體中的 session 或上次的憑證檔）做一次真實的 104 驗證，通過就回
    {"status": "already_logged_in"|"restored"}，不需要真人。否則開一次真人登入，回
    {"login_url", "token", "browser_opened"}。**接下來 Agent 要做的事：**
    1. browser_opened 為 True 時，伺服器已用使用者的預設瀏覽器打開登入頁，只要請使用者
       在跳出的視窗完成 104 登入（含 MFA、產品選擇、「此帳號已登入」確認）；為 False 時
       把 login_url 給使用者請他打開。
    2. 立刻呼叫 check_login(token, wait_seconds=90)，回 pending 就再呼叫，直到 status
       不是 pending 為止——每次最多等 90 秒、一完成就立刻回傳。**不要停下來等使用者說
       「登入了」**，也不要要求使用者回報；真人完整登入約 3–5 分鐘。
    對一次已在進行中的登入重複呼叫，會原樣回傳那一次的 login_url／token，不會取代它。
    首次使用若尚未安裝 Chromium，回 {"status": "installing_browser", "message"}：伺服器
    正在背景下載，稍後再呼叫一次 login() 即可（每次最多等 90 秒，裝好會直接接著開登入）。
    """
    app = ctx.request_context.lifespan_context
    session_id = get_session_id(ctx)

    if app.session_pool.get_session(session_id) is not None:
        result = await _login_via_pool_session(app, ctx, session_id)
        if result is not None:
            return result
        # verdict.kind == "expired": pool session removed and the
        # credential file cleared above — fall through to a fresh
        # human login below.
    else:
        for pending_token in app.session_pool.find_pending_tokens_for_session(session_id):
            resource = app._pending_logins.get(pending_token)
            if resource is not None and resource.state == LoginState.AWAITING_HUMAN:
                return {
                    "login_url": f"{app.auth_site.base_url}/auth/{pending_token}",
                    "token": pending_token,
                    "browser_opened": False,
                }

        cookies = load_cookies(app.config.cookies_path)
        if cookies:
            result = await _login_via_cookie_file(app, ctx, cookies)
            if result is not None:
                return result
            # verdict.kind == "expired": credential file already
            # cleared — fall through to a fresh human login below.

    return await _start_human_login(app, ctx, session_id)


def _check_login_now(app, session_id: str, token: str) -> dict:
    """check_login 的單次判定（零 I/O）。四種狀態依序：本次執行已有可用 session 一律
    success（不論帶的是哪個 token）；token 是本次執行仍在進行中的登入 → pending；token 是
    本次執行自己鑄造、而本次執行親眼看著它逾時或被放棄 → failed；其餘（含每一個由先前
    執行鑄造的 token）→ unknown。"""
    if app.session_pool.get_session(session_id) is not None:
        return {"status": "success"}

    if token in app._pending_logins:
        return {"status": "pending", "message": "登入偵測中，使用者完成登入後會自動生效"}

    if token in app._finished_logins:
        return {"status": "failed", "error": "本次執行看著這次登入逾時或被放棄，請重新呼叫 login()"}

    return {
        "status": "unknown",
        "error": (
            "這次執行沒有發出過這個 token——可能來自先前一次執行，那次登入很可能"
            "已經成功。若要取得確定的答案，請呼叫 login()：它會驗證目前真正的"
            "登入狀態並回報。"
        ),
    }


async def check_login(token: str, ctx: Context, wait_seconds: int = 0) -> dict:
    """等待／查詢一次登入的進度。

    Args:
        token: login() 回傳的 token。
        wait_seconds: 最多等幾秒（0–90，超過以 90 計；預設 0 = 立刻回答）。狀態是
            pending 時會在伺服器端等到登入完成或時間用完才回傳，一完成就立刻回傳。

    建議用法：login() 回傳 login_url 之後反覆呼叫 check_login(token, wait_seconds=90)，
    直到 status 不是 "pending"。四種狀態：success（可以開始用其他工具）、pending（真人
    還在登入中，再呼叫一次）、failed（本次執行看著這次登入逾時或被放棄，重新 login()）、
    unknown（token 不是本次執行發出的，例如來自行程重啟之前；那次登入很可能已經成功，
    呼叫 login() 會驗證真正的狀態並回報）。
    """
    app = ctx.request_context.lifespan_context
    session_id = get_session_id(ctx)

    wait = max(0, min(int(wait_seconds), CHECK_LOGIN_MAX_WAIT_SECONDS))
    loop = asyncio.get_running_loop()
    deadline = loop.time() + wait
    while True:
        result = _check_login_now(app, session_id, token)
        remaining = deadline - loop.time()
        if result["status"] != "pending" or remaining <= 0:
            return result
        await asyncio.sleep(min(CHECK_LOGIN_POLL_INTERVAL, remaining))


async def logout(ctx: Context) -> dict:
    """登出 104 人力銀行：刪除本機登入憑證、清空記憶體中的 session，並收掉每一個
    仍在進行中的登入（含真人正在操作的那一個——它會被立即收掉，不等視窗走完）。

    回傳固定四鍵：success（本機那一半是否完成，恆為 True——一次沒有送出的伺服器
    端登出請求不會讓本機收尾中止）、server_logout（"unconfirmed" 或 "not_sent"，
    104 那一側的 session 是否真的作廢無從得知——見 warning）、warning（恆非空，
    說明 104 那一側的 session 為什麼無法被本專案作廢，以及它會活到什麼時候）、
    teardown_confirmed（收掉進行中登入的 watcher 是否在預算內確認完成；為 False
    時仍然安全，只是還有一顆瀏覽器/watcher 尚未證實已經關閉）。
    """
    app = ctx.request_context.lifespan_context
    session_id = get_session_id(ctx)

    # Step 0: 遞增登出世代——必須排在最前面，讓一個逾時取消未能在預算內結束的
    # watcher 有辦法在寫入憑證檔之前自行發現並放棄（見模組頂端說明）。
    app.logout_epoch += 1

    # Step 1: 伺服器端登出，必須在移除 pool session（Step 4）之前——它要用那個
    # session 的憑證。
    server_result = await request_server_logout(ctx)

    # Step 2: 收掉每一個仍在進行中的登入。stdio 下一個行程只有一個 client
    # session，所以「每一個」與「屬於本次連線」是同一個集合。
    teardown_confirmed = True
    for pending_token in list(app._pending_logins.keys()):
        confirmed = await _finalize_pending_login(app, pending_token, "logout() called")
        teardown_confirmed = teardown_confirmed and confirmed

    # Step 3.
    clear_cookies(app.config.cookies_path)
    # Step 4.
    app.session_pool.remove(session_id)
    # Step 5: 每一次登出都寫，與 server_result 的值無關——值域裡已經沒有一個
    # 代表「104 那一側真的登出了」的值，這一步不再是一個依值分支的動作。
    app.config.logout_unconfirmed_path.parent.mkdir(parents=True, exist_ok=True)
    app.config.logout_unconfirmed_path.write_text("")

    return {
        "success": True,
        "server_logout": server_result.state,
        "warning": f"本機登入憑證已刪除、記憶體中的 session 已清空。{server_result.detail}",
        "teardown_confirmed": teardown_confirmed,
    }


def register_auth_tools(mcp: FastMCP):
    mcp.tool()(login)
    mcp.tool()(check_login)
    mcp.tool()(logout)
