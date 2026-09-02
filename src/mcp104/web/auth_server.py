"""登入檢視頁與本機端點：提供真人開啟的檢視頁，以及一條雙向 WebSocket（幀出、輸入事件
進）。這一層完全不知道 CDP 概念——它拿到的只有一個查表函式
`token -> CdpLoginStream | None`（`get_admissible_stream`），對外只做兩件事：把幀轉發給
瀏覽器、把瀏覽器的輸入事件轉發給那個查表函式回傳的串流物件。哪個 token 在哪個狀態可以
拿到串流，是呼叫端（`tools/auth.py`）的決定，不是這一層的。

**admission 是查表函式自己的事**：本模組不維護任何登入狀態，`get_admissible_stream(token)`
回傳 `None` 一律視為「這個 token 現在不能連」，兩條路由（檢視頁、WebSocket）對此給出完全
相同的 404（既有那句固定字串、相同狀態碼與標頭），不依成因而改變說法——一個依狀態產生的
說明會把這個回應變成 token 狀態的預言機。

**只綁 `127.0.0.1`**（安全性需求，兩種形態皆然）。埠的取得方式：自建 socket、
`bind()` 之後立刻讀出實際生效的埠，再把它交給 aiohttp 的 `web.SockSite`——這樣送出
「打開哪個網址」之前，埠號早就確定，不必事後猜測內部狀態。

**存取記錄關閉**：aiohttp 預設的 access log 會記下請求細節（含 WebSocket upgrade 請求）。
`start_auth_site` 建立 `AppRunner` 時明確傳 `access_log=None`，寧可完全沒有存取記錄，
也不要冒著記到不該記的東西的風險——stdio 下任何未經設定的輸出都是風險。

檢視頁是單一自足頁面（inline CSS/JS、零外部資源）。座標換算**不**在這裡的 Python 端做
——那是 `browser/cdp_stream.py` 的 `page_coords()` 的職責；這裡的 JS 只送原始偏移量、
當下的顯示矩形、與繪製那次點擊所看到的那一幀的幾何，四個尺寸缺一律送 `undefined`，讓
`dispatch_input` 依規則拒絕，不在客戶端偷偷 1:1 猜測。

**收場訊息依渲染優先序決定，規則寫在檢視頁 JS 裡，而不是靠伺服器再送一個線上值**：
串流關閉時 WebSocket 可能已經不可用，一則「正常結束」的訊息本來就不保證送得到；「有沒有
收到過 `"completed"`」是檢視頁自己手上、不依賴任何後續訊息的事實。收到過就顯示「登入已完成，可以關閉本頁」，沒收到過才顯示
「連線中斷，請重新呼叫 login()」。

輸入路徑的隱私規則與 `cdp_stream.dispatch_input` 相同：這裡的 WebSocket 處理常式把收到的
事件原樣轉交給 `dispatch_input`，不記錄事件內容本身；解析失敗的訊息直接丟棄，不記錄原始
內容。
"""

from __future__ import annotations

import json
import logging
import socket
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import aiohttp
from aiohttp import web

from mcp104.config import ConfigError

if TYPE_CHECKING:
    from mcp104.browser.cdp_stream import CdpLoginStream
    from mcp104.config import Config

log = logging.getLogger("104-mcp.auth_server")

# 未知、已完成或已放棄的 token，三種成因兩條路由回應完全相同的固定字串與狀態碼
# ——不依成因改變說法。沿用既有措辭。
_NOT_FOUND_TEXT = "無效或已過期的登入連結"

_GET_ADMISSIBLE_STREAM_KEY = "get_admissible_stream"


def _not_found_response() -> web.Response:
    return web.Response(text=_NOT_FOUND_TEXT, status=404)


@dataclass(frozen=True)
class Binding:
    """`resolve_auth_binding` 的回傳值：決定要綁哪個位址／埠。`host` 恆為
    `127.0.0.1`；`port` 為 `None` 時表示向作業系統要一個臨時埠。"""

    host: str
    port: int | None


def resolve_auth_binding(config: "Config") -> Binding:
    """純函式：只讀 `config.auth_bind_port`／`config.auth_base_url`，不碰 socket、不做
    任何 I/O。這兩個設定值是一對——真人與本行程同機時兩者都不設定（臨時埠 + localhost），
    不同機時兩者都要設定（執行環境負責把 `auth_base_url` 接到本行程的
    `127.0.0.1:auth_bind_port`）。**只給一半在這裡就是設定錯誤**：那會產生一個結構完全
    正常、卻接不到任何東西的登入位址，而 Agent 那一側看不出跟「真人還在打字」有什麼分別
    ——寧可在設定解析階段就大聲失敗。
    """
    has_port = config.auth_bind_port is not None
    has_url = config.auth_base_url is not None
    if has_port != has_url:
        raise ConfigError(
            "MCP104_AUTH_BIND_PORT 與 MCP104_AUTH_BASE_URL 必須同時設定或同時不設定："
            "兩者都不設定代表真人與本行程同一台機器（臨時埠 + localhost）；兩者都設定"
            "代表真人在另一台機器上，且該執行環境已負責把這個對外位址接到本行程的"
            f"127.0.0.1:{config.auth_bind_port if has_port else '<auth_bind_port>'}。"
            "目前只設定了其中一個，這會產生一個結構正常但實際接不到任何東西的登入"
            "位址，因此在啟動時就拒絕，而不是等到第一次 login() 才讓真人對著空白畫面。"
        )
    return Binding(host="127.0.0.1", port=config.auth_bind_port)


class AuthEndpoint:
    """`start_auth_site` 的回傳值：一個控制代碼，不是純值。除了 `base_url`／`port`
    兩個對外欄位，呼叫端唯一能做的事就是 `await close()`——關掉這個監聽端所需的內部物件
    （aiohttp 的 runner 與那個自建 socket）刻意不出現在宣告的介面表面上。

    `close()` 冪等（已經關過就什麼都不做）且依定義不拋出：它的兩個呼叫時機（臨時埠
    形態在 `_pending_logins` 清空時、固定埠形態到行程關閉）都不存在一個「關不掉就中止」
    的合理處置。
    """

    def __init__(self, base_url: str, port: int, runner: web.AppRunner) -> None:
        self._base_url = base_url
        self._port = port
        self._runner = runner
        self._closed = False

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def port(self) -> int:
        return self._port

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            await self._runner.cleanup()
        except Exception:
            log.exception("auth endpoint cleanup failed (swallowed: close() 依定義不拋出)")


async def start_auth_site(app: web.Application, config: "Config") -> AuthEndpoint:
    """啟動監聽並回傳實際生效的位址與埠。位址／埠的決定交給 `resolve_auth_binding`——
    半套組態在這裡會以 `ConfigError` 結束，不留下任何已建立的資源。

    埠的取得方式：先自建一個 socket 並 `bind()`，立刻讀出系統實際配的埠，再把這個 socket
    交給 `web.SockSite`——送出登入位址之前，埠號早就確定。

    設定指定的固定埠已被占用時，`bind()` 失敗，以一個指名該埠號的 `OSError` 結束，且不
    留下任何已建立的資源（socket 已關閉、`AppRunner` 從未 `setup()`）；臨時埠的那條路徑
    不受影響。
    """
    binding = resolve_auth_binding(config)

    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind((binding.host, binding.port or 0))
    except OSError as exc:
        sock.close()
        if binding.port is not None:
            raise OSError(
                f"無法綁定設定指定的埠 {binding.port}（{exc}）——最可能的成因是同一位"
                "使用者的另一次執行仍在使用它，也可能是一次早就登入完、但仍在服務中的"
                "執行仍佔著這個固定埠。"
            ) from exc
        raise
    sock.listen(128)
    actual_port = sock.getsockname()[1]

    runner = web.AppRunner(app, access_log=None)
    try:
        await runner.setup()
        site = web.SockSite(runner, sock)
        await site.start()
    except Exception:
        await runner.cleanup()
        sock.close()
        raise

    base_url = config.auth_base_url or f"http://localhost:{actual_port}"
    return AuthEndpoint(base_url=base_url, port=actual_port, runner=runner)


def create_auth_app(
    get_admissible_stream: Callable[[str], "CdpLoginStream | None"],
) -> web.Application:
    """路由：`GET /auth/{token}`（檢視頁）、`GET /auth/{token}/ws`（雙向 WebSocket）。

    `get_admissible_stream` 是唯一的資料來源：**只在該項目的 `state` 為
    `awaiting_human` 時回傳串流**，其餘一律 `None`（生命週期狀態表的 admission 欄，
    由呼叫端決定，不是本函式的判斷）——這一層拿到 `None` 就一律回既有那句固定字串的 404，
    不去問「為什麼」。
    """
    app = web.Application()
    app[_GET_ADMISSIBLE_STREAM_KEY] = get_admissible_stream
    app.router.add_get("/auth/{token}", _handle_auth_page)
    app.router.add_get("/auth/{token}/ws", _handle_auth_ws)
    return app


async def _handle_auth_page(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    get_stream = request.app[_GET_ADMISSIBLE_STREAM_KEY]
    if get_stream(token) is None:
        return _not_found_response()
    return web.Response(text=_PAGE_HTML, content_type="text/html")


async def _handle_auth_ws(request: web.Request) -> web.StreamResponse:
    token = request.match_info["token"]
    get_stream = request.app[_GET_ADMISSIBLE_STREAM_KEY]
    stream = get_stream(token)
    if stream is None:
        return _not_found_response()

    ws = web.WebSocketResponse()
    await ws.prepare(request)
    stream.add_viewer(ws)
    await stream.refresh_for_new_viewer()
    try:
        async for msg in ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                try:
                    event = json.loads(msg.data)
                except (ValueError, TypeError):
                    # 解析不出來的訊息直接丟棄——不記錄原始內容（隱私規則，見模組
                    # docstring）。
                    continue
                if isinstance(event, dict):
                    await stream.dispatch_input(event)
            elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break
    finally:
        stream.remove_viewer(ws)
    return ws


# 單一自足頁面：inline CSS/JS、零外部資源。內容不依 token 而變——token 只決定這個路由
# 回不回 404，頁面本身固定；WebSocket 網址由 JS 從 `location.pathname` 組出
# （`/auth/{token}` + `/ws`），不需要伺服器把 token 內插進 HTML。
#
# 座標換算刻意不在這裡做：JS 只送使用者在 <img> 上點的原始偏移量
# （offset_x/offset_y，相對 <img> 左上角）、<img> 當下的顯示矩形（rect_w/rect_h）、與
# 繪製那次點擊所看到的那一幀的幾何（device_width/device_height，取自最近一次收到的
# frame metadata）——換算由伺服器端 `cdp_stream.page_coords()` 完成。四個尺寸值任一
# 缺漏，伺服器會拒絕並記錄，這裡不做 1:1 猜測。
#
# 收場訊息的規則寫在這裡：`receivedCompleted` 記錄這條連線先前有沒有收到過
# `"completed"`，socket 關閉時依它二選一（生命週期狀態表底下
# 那條渲染優先序）——這是唯一出處，這裡是它的執行點。
_PAGE_HTML = """<!doctype html>
<html lang="zh-TW">
<head>
<meta charset="utf-8">
<title>104 人力銀行登入</title>
<style>
  html, body { margin: 0; padding: 0; background: #111; color: #eee; font-family: sans-serif; }
  #status { padding: 8px 12px; font-size: 14px; background: #222; }
  #screen { display: block; width: 100%; max-width: 1600px; margin: 0 auto; cursor: default; background: #000; }
</style>
</head>
<body>
<div id="status">連線中……</div>
<img id="screen" draggable="false" alt="live screencast">
<script>
(function () {
  "use strict";
  var img = document.getElementById("screen");
  var statusEl = document.getElementById("status");
  var lastMetadata = null;
  var receivedCompleted = false;
  var proto = location.protocol === "https:" ? "wss:" : "ws:";
  var ws = new WebSocket(proto + "//" + location.host + location.pathname + "/ws");

  function setStatus(text) { statusEl.textContent = text; }

  function send(obj) {
    if (ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(obj));
  }

  ws.onmessage = function (ev) {
    var msg;
    try { msg = JSON.parse(ev.data); } catch (e) { return; }
    if (msg.type === "frame") {
      lastMetadata = msg.metadata || null;
      img.src = "data:image/jpeg;base64," + msg.data;
    } else if (msg.type === "state") {
      if (msg.value === "completed") {
        receivedCompleted = true;
        setStatus("登入已完成，這段期間不需要做任何事，頁面即將自動關閉。");
      } else if (msg.value === "waiting") {
        setStatus("請在下方畫面中完成 104 登入。");
      }
    }
  };

  ws.onclose = function () {
    if (receivedCompleted) {
      setStatus("登入已完成，可以關閉本頁。");
    } else {
      setStatus("連線中斷，請重新呼叫 login()。");
    }
  };

  function geometry(e) {
    var rect = img.getBoundingClientRect();
    var md = lastMetadata || {};
    return {
      offset_x: e.clientX - rect.left,
      offset_y: e.clientY - rect.top,
      rect_w: rect.width,
      rect_h: rect.height,
      device_width: md.deviceWidth,
      device_height: md.deviceHeight
    };
  }

  img.oncontextmenu = function (e) { e.preventDefault(); };

  img.addEventListener("mousedown", function (e) {
    e.preventDefault();
    send(Object.assign({ type: "mouse", event: "mousePressed",
      button: e.button, clickCount: e.detail || 1 }, geometry(e)));
  });
  img.addEventListener("mouseup", function (e) {
    e.preventDefault();
    send(Object.assign({ type: "mouse", event: "mouseReleased",
      button: e.button, clickCount: e.detail || 1 }, geometry(e)));
  });

  // e.buttons（複數，bitmask）才是「目前正按著哪個鍵」，跟 mousedown/mouseup 用的
  // e.button（單數，哪個鍵觸發了這次事件）是不同欄位、不同編碼。
  function buttonFromButtonsBitmask(bitmask) {
    if (bitmask & 1) return 0;
    if (bitmask & 2) return 2;
    if (bitmask & 4) return 1;
    return -1;
  }

  var pendingMove = null;
  img.addEventListener("mousemove", function (e) {
    pendingMove = Object.assign({ type: "mouse", event: "mouseMoved",
      button: buttonFromButtonsBitmask(e.buttons), clickCount: 0 }, geometry(e));
  });
  (function flushMove() {
    if (pendingMove) { send(pendingMove); pendingMove = null; }
    requestAnimationFrame(flushMove);
  })();

  img.addEventListener("wheel", function (e) {
    e.preventDefault();
    send(Object.assign({ type: "mouse", event: "mouseWheel",
      button: -1, clickCount: 0, deltaX: e.deltaX, deltaY: e.deltaY }, geometry(e)));
  }, { passive: false });

  window.addEventListener("keydown", function (e) {
    e.preventDefault();
    send({ type: "keydown", key: e.key,
      shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey, metaKey: e.metaKey });
  });
  window.addEventListener("keyup", function (e) {
    e.preventDefault();
    send({ type: "keyup", key: e.key,
      shiftKey: e.shiftKey, ctrlKey: e.ctrlKey, altKey: e.altKey, metaKey: e.metaKey });
  });
})();
</script>
</body>
</html>
"""
