import asyncio
from dataclasses import dataclass


@dataclass
class VncSession:
    display: str          # e.g. ":99"
    display_num: int
    xvfb_proc: asyncio.subprocess.Process
    x11vnc_proc: asyncio.subprocess.Process
    vnc_port: int         # x11vnc TCP port (5900 + display_num)


_next_display = 99


class VncManager:
    def __init__(self):
        self._sessions: dict[str, VncSession] = {}

    async def start(self, token: str) -> VncSession:
        """Start Xvfb + x11vnc for a login session."""
        global _next_display
        display_num = _next_display
        _next_display += 1
        display = f":{display_num}"

        # Start Xvfb
        xvfb = await asyncio.create_subprocess_exec(
            "Xvfb", display, "-screen", "0", "1920x1080x24",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.5)

        # Start x11vnc
        vnc_port = 5900 + display_num
        x11vnc = await asyncio.create_subprocess_exec(
            "x11vnc", "-display", display, "-rfbport", str(vnc_port),
            "-nopw", "-forever", "-shared",
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        await asyncio.sleep(0.3)

        session = VncSession(
            display=display,
            display_num=display_num,
            xvfb_proc=xvfb,
            x11vnc_proc=x11vnc,
            vnc_port=vnc_port,
        )
        self._sessions[token] = session
        return session

    def get_session(self, token: str) -> VncSession | None:
        return self._sessions.get(token)

    async def stop(self, token: str):
        """Stop all processes for a login session."""
        session = self._sessions.pop(token, None)
        if not session:
            return
        for proc in [session.x11vnc_proc, session.xvfb_proc]:
            try:
                proc.terminate()
                await asyncio.wait_for(proc.wait(), timeout=5)
            except (ProcessLookupError, asyncio.TimeoutError):
                proc.kill()

    async def stop_all(self):
        for token in list(self._sessions):
            await self.stop(token)
