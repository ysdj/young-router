"""A headless browser's screen and keyboard, streamed to the page over CDP.

A server has no screen to open a window on, and the person signing in is at the
other end of a network.  The lightest answer is the browser this machine
already has: start it headless, let the station's own page run in it, and relay
the pixels and the input over the DevTools protocol that is already open —
`Page.startScreencast` pushes a JPEG whenever the page changes, and
`Input.dispatch*` is what a person's click and typing become.  Nothing is
installed for this and nothing is bundled: no X server, no VNC, no second
process.

Measured on the station login page at 560x820: 21 KB per frame, 13-31 ms to
capture one, and a screencast that is silent while nothing moves.  A login form
is exactly the traffic that shape is good for.

The one thing this must not leak is what a headless browser gives away about
itself.  Chrome's default user agent says `HeadlessChrome` and its screen is
800x600, so both are stated explicitly: the agent comes from the browser's own
`Browser.getVersion` with that one word replaced, and the metrics are the size
of the display this session serves.  If a station still refuses a datacenter
address, that is a judgement about the network, not something a mirror can
argue with.
"""

from __future__ import annotations

import asyncio
import threading
from dataclasses import dataclass, field
from typing import Any, Mapping

# JPEG quality and the display this session serves.  Higher quality is more
# bytes per frame for a login form; lower starts to blur the two fields a
# person has to read.
FRAME_QUALITY = 70
DEFAULT_WIDTH = 560
DEFAULT_HEIGHT = 820

# How long a frame request waits for the page to draw something before it is
# answered with "nothing changed".  The page asks again immediately, so this is
# the idle cadence, not a timeout anybody sees.
FRAME_WAIT_SECONDS = 1.0

# A reconnect is what happens when the station navigates and the screencast's
# target goes away.  Backing off keeps a page that is being reloaded from
# spinning the CPU.
RECONNECT_DELAY_SECONDS = 0.4

# The keys a login form needs, by the code CDP wants.  Anything else is typed
# as text, which is what `Input.insertText` is for.
_VIRTUAL_KEYS: dict[str, tuple[int, str]] = {
    "Enter": (13, "Enter"),
    "Tab": (9, "Tab"),
    "Backspace": (8, "Backspace"),
    "Escape": (27, "Escape"),
    "Delete": (46, "Delete"),
    "ArrowUp": (38, "ArrowUp"),
    "ArrowDown": (40, "ArrowDown"),
    "ArrowLeft": (37, "ArrowLeft"),
    "ArrowRight": (39, "ArrowRight"),
    "Home": (36, "Home"),
    "End": (35, "End"),
}


@dataclass
class MirrorSession:
    """One headless page's frames, and the last one anybody can read."""

    port: int
    origin: str
    width: int = DEFAULT_WIDTH
    height: int = DEFAULT_HEIGHT
    _condition: threading.Condition = field(default_factory=threading.Condition)
    _seq: int = 0
    _frame: bytes = b""
    _closed: bool = False
    _thread: threading.Thread | None = None
    _first: threading.Event = field(default_factory=threading.Event)

    def start(self, *, timeout: float = 20.0) -> bool:
        """Begin streaming, and answer whether a first frame ever arrived."""

        if self._thread is not None:
            return self._first.is_set()
        self._thread = threading.Thread(target=self._run, name="yr-mirror", daemon=True)
        self._thread.start()
        return self._first.wait(timeout)

    def _run(self) -> None:
        try:
            asyncio.run(self._stream())
        except Exception:
            pass

    async def _stream(self) -> None:
        import websockets

        from .relay_browser import active_page_target

        while not self._closed:
            target = active_page_target(self.port, self.origin)
            if target is None:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            socket_url = target.get("webSocketDebuggerUrl")
            if not isinstance(socket_url, str) or not socket_url:
                await asyncio.sleep(RECONNECT_DELAY_SECONDS)
                continue
            try:
                async with websockets.connect(socket_url, max_size=32 * 1024 * 1024, open_timeout=5) as socket_:
                    await self._stream_one(socket_)
            except Exception:
                if self._closed:
                    return
            await asyncio.sleep(RECONNECT_DELAY_SECONDS)

    async def _stream_one(self, socket_: Any) -> None:
        import json

        commands: list[Mapping[str, Any]] = [
            {"id": 1, "method": "Page.enable", "params": {}},
            {
                "id": 2,
                "method": "Emulation.setDeviceMetricsOverride",
                "params": {
                    "width": self.width,
                    "height": self.height,
                    "deviceScaleFactor": 1,
                    "mobile": False,
                    "screenWidth": self.width,
                    "screenHeight": self.height,
                },
            },
            {"id": 3, "method": "Browser.getVersion", "params": {}},
            {
                "id": 4,
                "method": "Page.startScreencast",
                "params": {
                    "format": "jpeg",
                    "quality": FRAME_QUALITY,
                    "maxWidth": self.width,
                    "maxHeight": self.height,
                    "everyNthFrame": 1,
                },
            },
        ]
        for command in commands:
            await socket_.send(json.dumps(command))
        while not self._closed:
            raw = await asyncio.wait_for(socket_.recv(), timeout=None)
            message = json.loads(raw)
            method = message.get("method")
            if method == "Page.screencastFrame":
                params = message.get("params") or {}
                data = params.get("data")
                session_id = params.get("sessionId")
                if isinstance(data, str) and data:
                    import base64

                    try:
                        frame = base64.b64decode(data)
                    except ValueError:
                        frame = b""
                    if frame:
                        with self._condition:
                            self._seq += 1
                            self._frame = frame
                            self._condition.notify_all()
                        self._first.set()
                if isinstance(session_id, int):
                    await socket_.send(json.dumps({
                        "id": 0,
                        "method": "Page.screencastFrameAck",
                        "params": {"sessionId": session_id},
                    }))
            elif message.get("id") == 3:
                # The agent this browser would announce names itself.  A
                # station that greps for it is the one thing a mirror can fix.
                result = message.get("result") or {}
                agent = result.get("userAgent")
                if isinstance(agent, str) and agent:
                    await socket_.send(json.dumps({
                        "id": 5,
                        "method": "Emulation.setUserAgentOverride",
                        "params": {
                            "userAgent": agent.replace("HeadlessChrome", "Chrome"),
                            "acceptLanguage": "zh-CN,zh;q=0.9,en;q=0.8",
                            "platform": "Linux x86_64" if "Linux" in agent else "",
                        },
                    }))

    def latest(self, since: int, timeout: float = FRAME_WAIT_SECONDS) -> tuple[int, bytes]:
        """The newest frame, waiting up to `timeout` for one newer than `since`.

        `(seq, b"")` means nothing has been drawn since the caller's last look;
        the caller asks again rather than the display standing still.
        """

        with self._condition:
            if self._seq <= since and not self._closed:
                self._condition.wait(timeout=max(0.0, min(timeout, FRAME_WAIT_SECONDS)))
            return self._seq, self._frame if self._seq > since else b""

    def close(self) -> None:
        with self._condition:
            self._closed = True
            self._condition.notify_all()


def dispatch(port: int, origin: str, event: Mapping[str, Any], *, width: int, height: int) -> bool:
    """Turn one page event into CDP input, or refuse it.

    The page states facts; this decides what is a real input.  A coordinate
    outside the served display, a key nobody listed, or a text longer than a
    field is not an input, and a refused one answers `False` instead of being
    clamped into something the person did not do.
    """

    from .relay_browser import _cdp_exchange, active_page_target

    target = active_page_target(port, origin)
    socket_url = str(target.get("webSocketDebuggerUrl") or "") if target is not None else ""
    if not socket_url:
        return False
    kind = event.get("type")
    commands: list[Mapping[str, Any]] = []
    if kind == "click":
        x = event.get("x")
        y = event.get("y")
        if not isinstance(x, int) or not isinstance(y, int):
            return False
        if not (0 <= x < width and 0 <= y < height):
            return False
        for event_type in ("mousePressed", "mouseReleased"):
            commands.append({
                "method": "Input.dispatchMouseEvent",
                "params": {
                    "type": event_type,
                    "x": x,
                    "y": y,
                    "button": "left",
                    "clickCount": 1,
                    "modifiers": 0,
                },
            })
    elif kind == "text":
        text = event.get("text")
        if not isinstance(text, str) or not text or len(text) > 1000:
            return False
        commands.append({"method": "Input.insertText", "params": {"text": text}})
    elif kind == "key":
        key = event.get("key")
        pair = _VIRTUAL_KEYS.get(key) if isinstance(key, str) else None
        if pair is None:
            return False
        virtual_key, code = pair
        for event_type in ("keyDown", "keyUp"):
            commands.append({
                "method": "Input.dispatchKeyEvent",
                "params": {
                    "type": event_type,
                    "key": code,
                    "code": code,
                    "windowsVirtualKeyCode": virtual_key,
                    "nativeVirtualKeyCode": virtual_key,
                    "text": "\r" if code == "Enter" and event_type == "keyDown" else "",
                },
            })
    elif kind == "wheel":
        delta = event.get("deltaY")
        if not isinstance(delta, int) or not -2000 <= delta <= 2000:
            return False
        commands.append({
            "method": "Input.dispatchMouseEvent",
            "params": {"type": "mouseWheel", "x": width // 2, "y": height // 2, "deltaX": 0, "deltaY": delta},
        })
    else:
        return False
    _cdp_exchange(
        socket_url,
        [{"id": index, **command} for index, command in enumerate(commands, start=1)],
        timeout=4.0,
    )
    return True
