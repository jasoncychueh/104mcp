import asyncio
import pathlib

import aiohttp
from aiohttp import web

NOVNC_DIR = pathlib.Path("/usr/share/novnc")


def create_auth_app(vnc_manager) -> web.Application:
    app = web.Application()
    app["vnc_manager"] = vnc_manager
    app.router.add_get("/auth/{token}", handle_auth_page)
    app.router.add_get("/novnc/{token}/websockify", handle_websocket_proxy)
    app.router.add_get("/novnc/{token}/{path:.*}", handle_novnc_static)
    return app


async def handle_auth_page(request: web.Request) -> web.Response:
    token = request.match_info["token"]
    vnc_mgr = request.app["vnc_manager"]
    session = vnc_mgr.get_session(token)

    if not session:
        return web.Response(text="無效或已過期的登入連結", status=404)

    # noVNC client is served via our own proxy routes — no external ports needed
    novnc_url = f"/novnc/{token}/vnc_lite.html?autoconnect=true&resize=scale&path=novnc/{token}/websockify"

    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
    <meta charset="UTF-8">
    <title>104 人力銀行 - 登入</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{ background: #000; overflow: hidden; }}
        iframe {{ position: fixed; top: 0; left: 0; width: 100vw; height: 100vh; border: none; }}
        .banner {{
            position: fixed; top: 0; left: 0; right: 0; z-index: 10;
            background: rgba(26, 26, 46, 0.9); backdrop-filter: blur(8px);
            padding: 10px 20px; display: flex; align-items: center; justify-content: center; gap: 16px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            transition: opacity 0.3s;
        }}
        .banner:hover {{ opacity: 0.3; }}
        .banner h2 {{ color: #eee; font-family: sans-serif; font-size: 16px; font-weight: 600; }}
        .banner p {{ color: #aaa; font-family: sans-serif; font-size: 13px; }}
    </style>
</head>
<body>
    <div class="banner">
        <h2>請完成 104 人力銀行企業登入</h2>
        <p>登入完成後回到 Agent 繼續操作 · 滑鼠移到此處可隱藏</p>
    </div>
    <iframe src="{novnc_url}"></iframe>
</body>
</html>"""
    return web.Response(text=html, content_type="text/html")


async def handle_novnc_static(request: web.Request) -> web.Response:
    """Serve noVNC static files (HTML, JS, CSS) from /usr/share/novnc/."""
    token = request.match_info["token"]
    vnc_mgr = request.app["vnc_manager"]

    if not vnc_mgr.get_session(token):
        return web.Response(text="無效的 token", status=404)

    path = request.match_info["path"]
    if not path:
        path = "vnc.html"

    file_path = NOVNC_DIR / path

    # Security: ensure resolved path stays within NOVNC_DIR
    try:
        file_path = file_path.resolve()
        if not str(file_path).startswith(str(NOVNC_DIR.resolve())):
            return web.Response(status=403)
    except (ValueError, OSError):
        return web.Response(status=400)

    if not file_path.is_file():
        return web.Response(text="Not found", status=404)

    return web.FileResponse(file_path, headers={"Content-Type": _guess_content_type(file_path)})


async def handle_websocket_proxy(request: web.Request) -> web.WebSocketResponse:
    """Proxy WebSocket traffic between noVNC client and x11vnc (VNC over WebSocket)."""
    token = request.match_info["token"]
    vnc_mgr = request.app["vnc_manager"]
    session = vnc_mgr.get_session(token)

    if not session:
        return web.Response(text="無效的 token", status=404)

    # Accept WebSocket from browser (noVNC client)
    ws_client = web.WebSocketResponse(protocols=["binary"])
    await ws_client.prepare(request)

    # Connect TCP to x11vnc
    try:
        reader, writer = await asyncio.open_connection("127.0.0.1", session.vnc_port)
    except (ConnectionRefusedError, OSError):
        await ws_client.close(message=b"VNC server not available")
        return ws_client

    async def ws_to_vnc():
        """Forward WebSocket messages → VNC TCP."""
        try:
            async for msg in ws_client:
                if msg.type == aiohttp.WSMsgType.BINARY:
                    writer.write(msg.data)
                    await writer.drain()
                elif msg.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break
        finally:
            writer.close()

    async def vnc_to_ws():
        """Forward VNC TCP → WebSocket messages."""
        try:
            while True:
                data = await reader.read(65536)
                if not data:
                    break
                await ws_client.send_bytes(data)
        except (ConnectionResetError, asyncio.CancelledError):
            pass
        finally:
            await ws_client.close()

    # Run both directions concurrently
    await asyncio.gather(ws_to_vnc(), vnc_to_ws(), return_exceptions=True)
    return ws_client


def _guess_content_type(path: pathlib.Path) -> str:
    ext = path.suffix.lower()
    return {
        ".html": "text/html",
        ".js": "application/javascript",
        ".css": "text/css",
        ".png": "image/png",
        ".svg": "image/svg+xml",
        ".ico": "image/x-icon",
        ".woff": "font/woff",
        ".woff2": "font/woff2",
        ".ttf": "font/ttf",
    }.get(ext, "application/octet-stream")
