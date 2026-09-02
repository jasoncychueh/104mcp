"""CDP 登入串流：把受控瀏覽器的畫面透過 Chrome DevTools Protocol 的
`Page.startScreencast` 送出去，並把真人在檢視頁上的滑鼠／鍵盤／捲動事件轉發回同一顆
瀏覽器。這個模組完全不知道 `web/` 那一層的 WebSocket 細節，也不知道
`PendingLoginResources` 那一層的內部生命週期狀態（`awaiting_human` / `settling` /
`handed_off` / `abandoned`）——它只認得自己這三個屬性值（`state`），由呼叫者
（`browser/session.py` 的 watcher）在恰好一個地方（`mark_completed()`）推進，見該處的
生命週期狀態表。

以 `research/probes/probe_headless_cdp.py` 的 stage 2（`_forward_key_down` /
`_forward_key_up` / `_forward_mouse_event` / `_cdp_modifiers` / `_safe_send_str` 與其
watchdog）為參考實作，但不是逐行照抄——座標換算改移到伺服器端這一份純函式裡（見
`page_coords()`），理由是可測試性：探針把換算寫在檢視頁的 JS 裡，沒辦法在沒有瀏覽器的
情況下逐案驗證。

四個技術約束（design.md §C4，都在探針裡實際踩過並修好）：

1. **背壓契約**：Chromium 要收到前一幀的 `Page.screencastFrameAck` 才送下一幀。每一幀
   都必須 ack；ack 是非同步送出的，其 task 必須保有強參考直到完成（見 `_track`）；不得
   以「畫面有沒有在動」作為串流健康與否的判準——因此收尾順序是 `stop()` 先取消
   watchdog，再排空還在飛的 ack，最後才真的停掉 screencast，避免 watchdog 在一顆已經
   完成認證的瀏覽器上重新開起串流。
2. **座標換算**：只在伺服器端做一次（`page_coords()`），檢視頁送來的是原始偏移量
   （`offset_x`/`offset_y`）、它當下的顯示矩形（`rect_w`/`rect_h`）與**它實際據以繪製
   那次點擊所看到的那一幀**的幾何（`device_width`/`device_height`）——伺服器不得改用
   自己手上最新的一幀，那會引入一個客戶端版本沒有的競態。四個尺寸值任一缺漏或非正值，
   事件被拒絕並記錄（只記幾何，不記座標以外的任何內容），絕不退回 1:1 猜測；
   `offset_x`/`offset_y` 為 `0` 是合法輸入。
3. **鍵盤事件序列**：`rawKeyDown`（不帶 `text`）→ `char`（帶 `text`）→ `keyUp`，否則
   每個字元會插入兩次。非可印鍵以 `windowsVirtualKeyCode`（Win32 VK 常數）指定；沒有
   已驗證代碼的鍵略過而不猜。
4. **滑鼠 `buttons` 位元遮罩**：`button`（單數，哪個鍵觸發了這次事件）與檢視頁自己算出
   的「目前按著哪個鍵」是不同概念；沒有對應到已知代碼時退回 `"none"`，不退回 `"left"`
   ——move 事件若固定送左鍵，每一次單純的 hover 都會變成按住左鍵拖曳。

輸入路徑的隱私規則：`dispatch_input` 與它呼叫的每一個轉換函式都可能經手帳號持有人的
真實密碼字元。這條路徑上絕不 print / log / 存檔事件或其中任何一個欄位，包含 `key`
本身，也包含例外訊息裡不得夾帶它。

畫面幀的處置規則（安全需求）：幀只存活到轉發出去為止——不寫檔、不累積進任何清單、不為
新連上的檢視頁快取重播。登入成功後的畫面是真實的 104 後台，內含候選人個資。
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from patchright.async_api import Page

log = logging.getLogger("104-mcp.cdp_stream")

# 兩個呼叫點（新檢視者連上時、watchdog 逾時時）共用的啟動參數，避免兩處的
# format/quality/maxWidth/maxHeight 之後被改到不一致。
_SCREENCAST_PARAMS = {"format": "jpeg", "quality": 80, "maxWidth": 1600, "maxHeight": 900}

# watchdog 多久檢查一次「上一張 frame 是多久以前」；超過 WATCHDOG_IDLE_SECONDS 沒有
# 新 frame、且至少有一個檢視者連著，就重啟一次 screencast。真人停在一個不會自行重繪的
# 畫面（等輸入的 MFA 欄位、還沒點的產品選擇頁）跟「這東西壞了」對操作者來說看起來
# 一模一樣，所以用自我修復處理，不能要求操作者自己發現並重新整理。
WATCHDOG_IDLE_SECONDS = 2.0
WATCHDOG_POLL_SECONDS = 0.5

# 判定成立之後串流刻意再活多久才真的關閉——見模組頂端 docstring 與 design.md §C4：
# 這是操作者看到完成訊息的時間窗，也是 rms/index 白畫面競態的觀察窗，也是一個安全
# 參數（決定已登入狀態的 104 後台畫面認證完成之後還會繼續串流多久）。三個角色要求的是
# 同一個值，所以只能縮短、不能隨手放大。
POST_SUCCESS_SETTLE_SECONDS = 3.0

# 「至少要處理」的非可印字元——windowsVirtualKeyCode 是 Win32 VK_* 常數；CDP 用這個
# 欄位（不是 DOM 的 keyCode）決定非可印鍵的預設行為（例如 Backspace 刪字、Enter 送出
# 表單）。
_NAMED_KEY_CODES = {
    "Backspace": 8,
    "Tab": 9,
    "Enter": 13,
}

# -1 = 沒有按鍵；0/1/2 沿用 DOM MouseEvent.button 的編號。沒有對應到已知代碼時退回
# "none" 而不是 "left"（見模組頂端約束 4）。
_MOUSE_BUTTONS = {-1: "none", 0: "left", 1: "middle", 2: "right"}

_POINTER_EVENT_KINDS = ("mousePressed", "mouseReleased", "mouseMoved")


def cdp_modifiers(shift: bool, ctrl: bool, alt: bool, meta: bool) -> int:
    """CDP Input 事件的 modifiers 是位元旗標：Alt=1、Ctrl=2、Meta/Command=4、
    Shift=8——CDP Input domain 自己的定義，不是這個模組發明的。"""
    modifiers = 0
    if alt:
        modifiers |= 1
    if ctrl:
        modifiers |= 2
    if meta:
        modifiers |= 4
    if shift:
        modifiers |= 8
    return modifiers


def key_events_for(event: dict) -> list[dict]:
    """把檢視頁送來的一則 `keydown`/`keyup` 事件轉成對應方向的
    `Input.dispatchKeyEvent` 參數序列。可印字元的 `keydown` 產生
    `[rawKeyDown（不帶 text）, char（帶 text）]`；具名鍵（見 `_NAMED_KEY_CODES`）的
    `keydown`/`keyup` 都帶 `windowsVirtualKeyCode`；其餘鍵（方向鍵、功能鍵……）不在
    已驗證的最小集合裡，兩個方向都回傳空序列，不去猜一個沒驗證過的代碼。

    絕不讀取本函式回傳值以外的任何東西並記錄——呼叫端同樣禁止記錄 `event` 本身。"""
    key = event.get("key", "")
    kind = event.get("type")
    modifiers = cdp_modifiers(
        bool(event.get("shiftKey", False)),
        bool(event.get("ctrlKey", False)),
        bool(event.get("altKey", False)),
        bool(event.get("metaKey", False)),
    )
    vk = _NAMED_KEY_CODES.get(key)
    is_printable = vk is None and len(key) == 1
    if vk is None and not is_printable:
        return []

    if kind == "keydown":
        if vk is not None:
            return [{
                "type": "rawKeyDown",
                "windowsVirtualKeyCode": vk,
                "key": key,
                "code": key,
                "modifiers": modifiers,
            }]
        return [
            {"type": "rawKeyDown", "modifiers": modifiers},
            {"type": "char", "text": key, "modifiers": modifiers},
        ]

    if kind == "keyup":
        payload: dict = {"type": "keyUp", "modifiers": modifiers}
        if vk is not None:
            payload["windowsVirtualKeyCode"] = vk
            payload["key"] = key
            payload["code"] = key
        return [payload]

    return []


def mouse_event_for(event: dict) -> dict | None:
    """把檢視頁送來的一則指標事件（`mousePressed`/`mouseReleased`/`mouseMoved`）轉成
    `Input.dispatchMouseEvent` 的 `type`/`button`/`clickCount`——不含 `x`/`y`，座標由
    `page_coords()` 另外算，兩者由呼叫端合併。不認識的事件型別（含 `mouseWheel`，那個
    交給 `wheel_event_for()`）回傳 `None`。"""
    kind = event.get("event")
    if kind not in _POINTER_EVENT_KINDS:
        return None
    button = _MOUSE_BUTTONS.get(event.get("button", -1), "none")
    return {
        "type": kind,
        "button": button,
        "clickCount": event.get("clickCount", 0) or 0,
    }


def wheel_event_for(event: dict) -> dict | None:
    """把檢視頁送來的 `mouseWheel` 事件轉成 `Input.dispatchMouseEvent` 的
    `type`/`deltaX`/`deltaY`——座標契約與其他滑鼠事件相同，由呼叫端另外併入
    `page_coords()` 算出的 `x`/`y`。非 `mouseWheel` 事件回傳 `None`。"""
    if event.get("event") != "mouseWheel":
        return None
    return {
        "type": "mouseWheel",
        "button": "none",
        "clickCount": 0,
        "deltaX": event.get("deltaX", 0) or 0,
        "deltaY": event.get("deltaY", 0) or 0,
    }


def _is_finite_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def page_coords(
    offset_x: float | None,
    offset_y: float | None,
    rect_w: float | None,
    rect_h: float | None,
    device_width: float | None,
    device_height: float | None,
) -> tuple[float, float] | None:
    """把「使用者在 `<img>` 上點的原始偏移量」換算成「目標頁面的座標系」（screencast
    每一幀 `metadata.deviceWidth`/`deviceHeight` 所在的 CSS pixel 座標系），也就是
    `Input.dispatchMouseEvent` 的 `x`/`y` 要用的值。

    `rect_w`/`rect_h`/`device_width`/`device_height` 任一缺漏或非正值都回傳 `None`
    ——絕不退回 1:1 猜測。`offset_x`/`offset_y` 只要求存在且為有限數，`0` 是合法值
    （畫面最上緣／最左緣），不因為是 0 就被當成缺漏拒絕。"""
    for size in (rect_w, rect_h, device_width, device_height):
        if not _is_finite_number(size) or size <= 0:
            return None
    for offset in (offset_x, offset_y):
        if not _is_finite_number(offset):
            return None

    scale_x = device_width / rect_w
    scale_y = device_height / rect_h
    return (float(offset_x) * scale_x, float(offset_y) * scale_y)


async def _safe_send_str(sink: Any, payload: str) -> None:
    """畫面幀／狀態訊息廣播用——單一檢視者斷線或送出失敗不該讓整個串流炸掉，也不該讓
    另一個還連著的檢視者收不到後續訊息。依定義吞掉送出失敗，不重試、不回報。"""
    try:
        if not getattr(sink, "closed", False):
            await sink.send_str(payload)
    except Exception:
        pass


class CdpLoginStream:
    """一顆受控頁面的 CDP screencast 串流：把畫面幀扇出給所有已註冊的檢視者，把
    檢視者送來的輸入事件轉發到頁面上，並在頁面靜止時仍然維持一個可更新的畫面。

    這個物件只知道自己這三個屬性值（見 `state`），不知道 `PendingLoginResources` 那一層
    的內部生命週期狀態；也不得反向讀取那一層——資訊是由呼叫者（`browser/session.py`
    的 watcher）推進來的，不是由這一層拉過去的。
    """

    def __init__(self, page: "Page") -> None:
        self._page = page
        self._cdp: Any = None
        self._viewers: set[Any] = set()
        self._pending_tasks: set[asyncio.Task] = set()
        self._watchdog_task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._last_frame_at: float = 0.0
        self._state: str = "waiting"

    @property
    def state(self) -> str:
        """唯讀。三個值：`"waiting"` / `"completed"` / `"closed"`——其中只有前兩個會
        出現在線上（`"closed"` 是這個物件自己的屬性值，由 `stop()` 寫入、從不推送）。"""
        return self._state

    def add_viewer(self, sink: Any) -> None:
        """同步：把 `sink` 加進扇出名單，不碰 CDP。呼叫端在這之後應緊接著
        `await refresh_for_new_viewer()`，讓這個檢視者立刻拿到一張新畫面——那對 CDP
        呼叫不在這裡，因為 `await` 進不了同步函式。"""
        self._viewers.add(sink)

    def remove_viewer(self, sink: Any) -> None:
        """同步：把 `sink` 移出扇出名單，不碰 CDP。"""
        self._viewers.discard(sink)

    def _track(self, coro: Any) -> None:
        # 保有強參考直到完成——無參考的 task 可能在執行前就被回收，那會讓 ack 漏送，
        # 而漏送的 ack 會讓串流卡在一幀，看起來跟「頁面本來就靜止」一模一樣。
        task = asyncio.create_task(coro)
        self._pending_tasks.add(task)
        task.add_done_callback(self._pending_tasks.discard)

    def _on_screencast_frame(self, params: dict) -> None:
        assert self._loop is not None
        self._last_frame_at = self._loop.time()

        # 幀只存活到轉發出去為止——這裡是區域變數，不寫檔、不累積進任何清單、不為新
        # 連上的檢視者快取重播。
        frame_payload = json.dumps({
            "type": "frame",
            "data": params.get("data", ""),
            "metadata": params.get("metadata", {}),
        })
        # 每送出一幀之前先讀 state，在同一次送出裡一併重申讀到的那個值——這是線上
        # state 訊息的第二個、也是唯一的第二個發出者（第一個是 announce_completed()）。
        # 只重播已經寫下的值，不在這裡寫 state。
        state_payload = json.dumps({"type": "state", "value": self._state})

        for sink in list(self._viewers):
            self._track(_safe_send_str(sink, frame_payload))
            self._track(_safe_send_str(sink, state_payload))
        del frame_payload, state_payload

        session_id = params.get("sessionId")
        if session_id is not None:
            self._track(self._cdp.send("Page.screencastFrameAck", {"sessionId": session_id}))

    async def _restart_screencast(self) -> None:
        # Page.startScreencast 只在「啟動當下」跟「頁面重繪時」發 frame；停了再重啟
        # 會讓 CDP 立刻重新發一張啟動 frame，這是唯一能在頁面本身沒有重繪的情況下逼出
        # 新畫面的方法。兩個呼叫點（refresh_for_new_viewer、watchdog）都呼叫這支函式，
        # 共用同一份 _SCREENCAST_PARAMS，不會各自維護一份而彼此漂移。
        await self._cdp.send("Page.stopScreencast")
        await self._cdp.send("Page.startScreencast", _SCREENCAST_PARAMS)

    async def _run_watchdog(self) -> None:
        assert self._loop is not None
        while True:
            await asyncio.sleep(WATCHDOG_POLL_SECONDS)
            idle_for = self._loop.time() - self._last_frame_at
            if self._viewers and idle_for > WATCHDOG_IDLE_SECONDS:
                try:
                    await self._restart_screencast()
                except Exception:
                    pass
                # 不論 restart 是否真的成功送出，都往後推一次時間戳——避免緊接著
                # 下一輪又立刻判定「還是逾時」而連續重啟；如果這次真的沒有生效，下一個
                # WATCHDOG_IDLE_SECONDS 視窗會再試一次，不會卡死在密集重試迴圈裡。
                self._last_frame_at = self._loop.time()

    async def start(self) -> None:
        self._loop = asyncio.get_event_loop()
        self._last_frame_at = self._loop.time()
        self._cdp = await self._page.context.new_cdp_session(self._page)
        await self._cdp.send("Page.enable")
        self._cdp.on("Page.screencastFrame", self._on_screencast_frame)
        await self._cdp.send("Page.startScreencast", _SCREENCAST_PARAMS)
        self._watchdog_task = asyncio.create_task(self._run_watchdog())

    async def stop(self) -> None:
        """冪等。第一步是取消 watchdog（收尾順序是背壓約束的一部分——見模組頂端
        docstring 約束 1）：watchdog 隨時可能正停在「判定太久沒有新幀」與「送出那對
        CDP 呼叫」之間，若排空還在飛的 ack 排在取消之前，那對呼叫就可能在 `stop()`
        已經回傳之後才送達一顆已完成認證的瀏覽器上。取消排在最前面，這個競態就不存在，
        而不是變得罕見。"""
        if self._state == "closed":
            return
        self._state = "closed"

        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except asyncio.CancelledError:
                pass
            self._watchdog_task = None

        if self._pending_tasks:
            await asyncio.gather(*self._pending_tasks, return_exceptions=True)

        if self._cdp is not None:
            try:
                await self._cdp.send("Page.stopScreencast")
            except Exception:
                pass

    async def refresh_for_new_viewer(self) -> None:
        """檢視者連上時重啟一次串流（一對 `stopScreencast`/`startScreencast`），讓那位
        檢視者立刻拿到一張新畫面——受控頁面若在第一個檢視者連上之前就已載入完成、之後
        不再重繪，唯一的啟動幀早就發生在沒有接收者的時候，畫面會永遠停在「還沒收到第一
        張」。`stop()` 之後呼叫是安全的：`state` 已是 `"closed"`，直接返回。"""
        if self._state == "closed" or self._cdp is None:
            return
        try:
            await self._restart_screencast()
        except Exception:
            pass

    def mark_completed(self) -> None:
        """同步（沒有 `await`）——這個物件唯一的狀態寫入點：把 `state` 由
        `"waiting"` 轉為 `"completed"`，同時就是關上 `dispatch_input` 的輸入閘門。
        不推送任何東西。冪等：`state` 已是 `"completed"` 時什麼都不做；`"closed"` 時
        同樣什麼都不做——終端狀態不倒走，沒有這道守衛，一個已經停掉的物件會被改寫回報
        「已完成」。"""
        if self._state in ("completed", "closed"):
            return
        self._state = "completed"

    async def announce_completed(self) -> None:
        """向每一個已註冊的接收者推送 `{"type": "state", "value": "completed"}`。
        不寫 `state`，也完全不讀 `state`——無條件推送。重複呼叫就是重複推送一次相同的
        值，這是允許的，不是缺陷：這個介面沒有冪等宣告，因為它沒有狀態可以冪等。"""
        if not self._viewers:
            return
        payload = json.dumps({"type": "state", "value": "completed"})
        await asyncio.gather(
            *(_safe_send_str(sink, payload) for sink in list(self._viewers)),
            return_exceptions=True,
        )

    async def _dispatch_mouse(self, event: dict) -> None:
        mouse_kind = event.get("event")
        if mouse_kind == "mouseWheel":
            params = wheel_event_for(event)
        else:
            params = mouse_event_for(event)
        if params is None:
            return

        offset_x = event.get("offset_x")
        offset_y = event.get("offset_y")
        rect_w = event.get("rect_w")
        rect_h = event.get("rect_h")
        device_width = event.get("device_width")
        device_height = event.get("device_height")
        coords = page_coords(offset_x, offset_y, rect_w, rect_h, device_width, device_height)
        if coords is None:
            # 幾何缺漏或非正值：拒絕並記錄——只記幾何，不記座標以外的任何內容（button/
            # clickCount/delta 等都不在這裡出現）。
            log.warning(
                "dropping input event with incomplete/invalid geometry: "
                "offset_x=%r offset_y=%r rect_w=%r rect_h=%r device_width=%r device_height=%r",
                offset_x, offset_y, rect_w, rect_h, device_width, device_height,
            )
            return

        x, y = coords
        await self._cdp.send("Input.dispatchMouseEvent", {**params, "x": x, "y": y})

    async def dispatch_input(self, event: dict) -> None:
        """把一則來自檢視頁的輸入事件施加到頁面。讀自己的 `state`，只有在 `"waiting"`
        時才轉發——`settling`（`state == "completed"`）期間不再轉發真人的輸入事件：
        登入鏈上兩個需要真人點擊的分支都發生在應用 session 憑證出現之前，判定一旦成立，
        輸入沒有剩下任何事情可做。

        隱私規則：這個函式與它呼叫的每一個轉換函式都可能經手帳號持有人的真實密碼字元。
        絕不 print / log / 存檔 `event` 或其中任何一個欄位，包含 `key` 本身，也包含
        例外訊息裡不得夾帶它。"""
        if self._state != "waiting" or self._cdp is None:
            return

        kind = event.get("type")
        if kind == "mouse":
            await self._dispatch_mouse(event)
        elif kind in ("keydown", "keyup"):
            for params in key_events_for(event):
                await self._cdp.send("Input.dispatchKeyEvent", params)
