"""A real browser window for one relay sign-in, driven over CDP.

The two native hosts present the station's own login page in a WebKit view they
own, so every way a station signs a person in (a password form, a verification
code, two-factor, an OAuth redirect) works there.  A browser page has no such
view: it cannot run another origin's login page under this app's origin, cannot
read a cookie another tab minted, and a proxied copy of the page would carry
this app's hostname into widgets that refuse it (a captcha site key is bound to
the station's own domain).

What a page *can* do is ask the Core beside it — which runs on the same machine
— to open a browser at the station's own address.  That browser is a real one:
the person sees the station's login page, with the station's own widgets and
redirects, exactly as they would in any browser.  Core then reads the cookies
that browser holds through the DevTools protocol and hands the session to the
one verified-login boundary every host already uses, closes the window, and the
page continues.

This module is only the process and the wire: finding an installed Chromium
family browser, starting it on a throwaway profile with a debugging port,
reading its cookie jar, and stopping it.  Which cookie is the session, and
whether the station accepts it, belongs to `core.service` and the relay domain —
a harvested cookie is not a login until the station answers with a profile.

Only open protocols are used: the browser is Chromium/Chrome/Edge/Brave, and the
control channel is the Chrome DevTools Protocol over a loopback WebSocket.  No
browser is bundled or downloaded — a machine with none keeps the password form
the page host already has, and `unavailable()` says so.
"""

from __future__ import annotations

import asyncio
import base64
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import urlsplit

# How long a launched browser may take to publish its debugging port.  A cold
# first launch of a Chrome profile on macOS measures about a second; a machine
# under load takes longer, and a wait shorter than this turns a slow start into
# "this host cannot sign in".
START_TIMEOUT_SECONDS = 20.0

# One sign-in is a person typing a password, reading a code from a phone, or
# finishing a redirect.  Ten minutes is generous for that and still ends a
# window somebody opened and walked away from.
SESSION_TIMEOUT_SECONDS = 600.0

# How long a sign-in may go without its page touching it before Core ends it.
# A pane that is open asks for frames about once a second, so silence means the
# page is gone -- closed, crashed, or on a network that dropped -- and nobody is
# left to cancel it.  Ninety seconds is far longer than any real gap between two
# polls and short enough that a forgotten session stops costing a browser.
IDLE_TIMEOUT_SECONDS = 90.0


class BrowserUnavailable(RuntimeError):
    """No Chromium-family browser this host can drive."""


class BrowserLaunchFailed(RuntimeError):
    """A browser was found, and this launch did not reach its debugging port."""


@dataclass(frozen=True)
class BrowserCandidate:
    """One installed browser this module can drive."""

    name: str
    executable: str


def _executable(path: Path) -> str:
    try:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    except OSError:
        pass
    return ""


def _macos_candidates() -> list[BrowserCandidate]:
    """The Chromium family in the places macOS installs it.

    `/Applications` and the per-user `~/Applications` are where a person's own
    browser lives; Homebrew's Caskroom spelling is a symlink farm that some
    installs keep without the app in `/Applications`.
    """

    home = Path.home()
    roots = [Path("/Applications"), home / "Applications", Path("/Applications/Utilities")]
    bundles = (
        ("Google Chrome", "Google Chrome.app/Contents/MacOS/Google Chrome"),
        ("Chromium", "Chromium.app/Contents/MacOS/Chromium"),
        ("Microsoft Edge", "Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
        ("Brave Browser", "Brave Browser.app/Contents/MacOS/Brave Browser"),
        ("Vivaldi", "Vivaldi.app/Contents/MacOS/Vivaldi"),
        ("Google Chrome Canary", "Google Chrome Canary.app/Contents/MacOS/Google Chrome Canary"),
    )
    found: list[BrowserCandidate] = []
    for root in roots:
        for name, relative in bundles:
            executable = _executable(root / relative)
            if executable:
                found.append(BrowserCandidate(name=name, executable=executable))
    return found


def _windows_candidates() -> list[BrowserCandidate]:
    found: list[BrowserCandidate] = []
    bases = [
        os.environ.get("PROGRAMFILES"),
        os.environ.get("PROGRAMFILES(X86)"),
        os.environ.get("LOCALAPPDATA"),
    ]
    relative = (
        ("Google Chrome", r"Google\Chrome\Application\chrome.exe"),
        ("Microsoft Edge", r"Microsoft\Edge\Application\msedge.exe"),
        ("Brave Browser", r"BraveSoftware\Brave-Browser\Application\brave.exe"),
        ("Chromium", r"Chromium\Application\chrome.exe"),
    )
    for base in bases:
        if not base:
            continue
        for name, tail in relative:
            executable = _executable(Path(base) / tail)
            if executable:
                found.append(BrowserCandidate(name=name, executable=executable))
    return found


def _posix_candidates() -> list[BrowserCandidate]:
    names = (
        ("Google Chrome", "google-chrome"),
        ("Google Chrome", "google-chrome-stable"),
        ("Chromium", "chromium"),
        ("Chromium", "chromium-browser"),
        ("Microsoft Edge", "microsoft-edge"),
        ("Microsoft Edge", "microsoft-edge-stable"),
        ("Brave Browser", "brave-browser"),
    )
    found: list[BrowserCandidate] = []
    for name, command in names:
        executable = shutil.which(command)
        if executable:
            found.append(BrowserCandidate(name=name, executable=executable))
    return found


def find_browser() -> BrowserCandidate | None:
    """The first installed browser this module can drive, or None.

    Order is the one a person would pick: Chrome, then Chromium, then Edge,
    then the smaller Chromium forks.  Only used to *offer* a window; nothing
    else in the app depends on a browser existing.

    `YOUNG_ROUTER_BROWSER` states which one instead — by path or by name.  A
    server whose only browser is `chromium` should not have to install Chrome
    to be picked, and a machine with several makes "why is it driving that one"
    a question with an answer.
    """

    if sys.platform == "darwin":
        candidates = _macos_candidates()
    elif os.name == "nt":
        candidates = _windows_candidates()
    else:
        candidates = _posix_candidates()
    stated = os.environ.get("YOUNG_ROUTER_BROWSER", "").strip()
    if stated:
        return _stated_browser(stated, candidates)
    # Stable order by the family list above, not by directory order.
    order = ["Google Chrome", "Chromium", "Microsoft Edge", "Brave Browser", "Vivaldi", "Google Chrome Canary"]
    candidates.sort(key=lambda item: order.index(item.name) if item.name in order else len(order))
    return candidates[0] if candidates else None


def _stated_browser(stated: str, candidates: list[BrowserCandidate]) -> BrowserCandidate | None:
    """The browser `YOUNG_ROUTER_BROWSER` names, if this machine has it."""

    path = Path(stated).expanduser()
    executable = _executable(path)
    if executable:
        return BrowserCandidate(name=path.stem, executable=executable)
    wanted = stated.lower()
    for candidate in candidates:
        if candidate.name.lower() == wanted:
            return candidate
    for candidate in candidates:
        if Path(candidate.executable).name.lower() == wanted:
            return candidate
    found = shutil.which(stated)
    if found:
        return BrowserCandidate(name=Path(found).stem, executable=found)
    return None


def unavailable_reason() -> str:
    """Why a window cannot be offered here, in a word Core can answer with."""

    if find_browser() is None:
        return "no_browser"
    # A screen is not required any more: without one the browser runs headless
    # and its frames are streamed to the page, so the only thing a host can be
    # missing is the browser itself.
    return ""


def needs_virtual_display() -> bool:
    """Whether this machine has no screen of its own to open a window on.

    macOS and Windows always have one.  Everything else asks the environment:
    a desktop session states `DISPLAY` (X11) or `WAYLAND_DISPLAY`, and a server
    states neither.
    """

    if sys.platform in {"darwin", "win32"}:
        return False
    return not os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")


def embedded_display() -> bool:
    """Whether a sign-in here has to be streamed to the page instead of shown.

    True on a machine with no screen of its own: there is no window to open, so
    the browser runs headless and the page draws its frames.
    """

    return needs_virtual_display()


def _free_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def _devtools_version(port: int, timeout: float = 0.4) -> Mapping[str, Any] | None:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/version", timeout=timeout
        ) as response:
            payload = json.loads(response.read(65536).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    return payload if isinstance(payload, Mapping) else None


def _devtools_targets(port: int) -> list[Mapping[str, Any]]:
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/json/list", timeout=1.0
        ) as response:
            payload = json.loads(response.read(1_048_576).decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return []
    if not isinstance(payload, list):
        return []
    return [item for item in payload if isinstance(item, Mapping)]


async def _cdp_messages(ws_url: str, commands: list[Mapping[str, Any]], *, timeout: float) -> list[Mapping[str, Any]]:
    """Send `commands` on one CDP connection and collect their answers.

    Each command carries its own `id`; the answers arrive in whatever order the
    browser finishes them, so they are matched by id rather than by position.
    """

    import websockets

    answers: list[Mapping[str, Any]] = []
    pending = {int(command["id"]) for command in commands}
    async with websockets.connect(ws_url, max_size=64 * 1024 * 1024, open_timeout=timeout) as socket_:
        for command in commands:
            await socket_.send(json.dumps(command))
        deadline = time.monotonic() + timeout
        while pending:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            try:
                raw = await asyncio.wait_for(socket_.recv(), timeout=remaining)
            except asyncio.TimeoutError:
                break
            try:
                message = json.loads(raw)
            except ValueError:
                continue
            if not isinstance(message, Mapping):
                continue
            identifier = message.get("id")
            if not isinstance(identifier, int) or identifier not in pending:
                continue
            pending.discard(identifier)
            answers.append(message)
    return answers


def _run(coroutine: Any) -> Any:
    """Run one CDP exchange from this thread, whatever loop policy it has."""

    try:
        return asyncio.run(coroutine)
    except RuntimeError:
        # A thread that already owns a loop (an embedder's) cannot `asyncio.run`
        # again; a fresh loop in this thread is still the right answer here.
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(coroutine)
        finally:
            loop.close()


def read_cookies(port: int, *, timeout: float = 5.0) -> list[dict[str, Any]]:
    """Every cookie the launched browser holds, flattened for one Cookie header.

    `Storage.getCookies` rather than `Network.getAllCookies`: the first answers
    for the browser's whole jar without attaching to a page target, which is
    what this needs — a sign-in that finished in a popup (an OAuth consent
    window) leaves its cookie in the same jar and nowhere near the window this
    process opened.
    """

    targets = _devtools_targets(port)
    ws_url = ""
    for target in targets:
        candidate = target.get("webSocketDebuggerUrl")
        if isinstance(candidate, str) and candidate:
            ws_url = candidate
            break
    if not ws_url:
        return []
    answers = _run(
        _cdp_messages(
            ws_url,
            [
                {"id": 1, "method": "Storage.getCookies", "params": {}},
                {"id": 2, "method": "Browser.getVersion", "params": {}},
            ],
            timeout=timeout,
        )
    )
    cookies: list[dict[str, Any]] = []
    for answer in answers:
        if answer.get("id") != 1:
            continue
        result = answer.get("result")
        if not isinstance(result, Mapping):
            continue
        for cookie in result.get("cookies") or []:
            if isinstance(cookie, Mapping):
                cookies.append(dict(cookie))
    return cookies


def read_local_storage(port: int, origin: str, *, timeout: float = 5.0) -> dict[str, str]:
    """What the station's own page kept in `localStorage`, as plain strings.

    A New API fork requires its account id as the `New-Api-User` header on every
    dashboard read, and some forks keep the token they mint beside the cookie
    rather than in it.  The page that just signed in holds both, under the same
    origin as the window this process opened — including when the sign-in
    finished in a popup, because storage is per origin and not per window.

    This is a read: it evaluates one expression that serialises the store and
    returns it by value.  A page that is not there yet answers nothing, which is
    the same answer as a station that keeps nothing.
    """

    host = (urlsplit(origin).hostname or "").lower()
    target = None
    for candidate in _devtools_targets(port):
        url = candidate.get("url")
        if isinstance(url, str) and host and (urlsplit(url).hostname or "").lower() == host:
            target = candidate
            break
    if target is None:
        return {}
    ws_url = target.get("webSocketDebuggerUrl")
    if not isinstance(ws_url, str) or not ws_url:
        return {}
    answers = _run(
        _cdp_messages(
            ws_url,
            [
                {
                    "id": 1,
                    "method": "Runtime.evaluate",
                    "params": {
                        "expression": "JSON.stringify(Object.fromEntries(Object.entries(window.localStorage)))",
                        "returnByValue": True,
                        "awaitPromise": False,
                    },
                }
            ],
            timeout=timeout,
        )
    )
    for answer in answers:
        result = answer.get("result")
        if not isinstance(result, Mapping):
            continue
        inner = result.get("result")
        if not isinstance(inner, Mapping):
            continue
        raw = inner.get("value")
        if not isinstance(raw, str):
            continue
        try:
            payload = json.loads(raw)
        except ValueError:
            return {}
        if not isinstance(payload, Mapping):
            return {}
        return {str(key): str(value) for key, value in payload.items() if isinstance(value, str)}
    return {}


def cookie_header(cookies: list[Mapping[str, Any]], origin: str) -> str:
    """The `Cookie` header a browser at `origin` would send.

    Domain matching is the browser's own rule: an exact host, or a parent
    domain the cookie was set for with a leading dot.  Selecting by name would
    guess (a station may call its session anything), so the whole jar's matching
    cookies travel and the station decides whether that is a session.
    """

    host = (urlsplit(origin).hostname or "").lower()
    if not host:
        return ""
    pairs: dict[str, str] = {}
    for cookie in cookies:
        name = cookie.get("name")
        value = cookie.get("value")
        domain = str(cookie.get("domain") or "").lower()
        if not isinstance(name, str) or not name or not isinstance(value, str):
            continue
        bare = domain.lstrip(".")
        if not bare:
            continue
        if not (host == bare or host.endswith("." + bare)):
            continue
        pairs[name] = value
    return "; ".join(f"{name}={value}" for name, value in sorted(pairs.items()))


@dataclass
class LoginBrowser:
    """One launched browser and the window this process opened in it."""

    browser: BrowserCandidate
    origin: str
    port: int
    process: subprocess.Popen[bytes]
    profile: Path
    started_at: float = field(default_factory=time.monotonic)
    # Whenever the page last asked about this sign-in: a poll, a frame, an
    # input.  Idleness is measured from here, not from the launch.
    last_seen_at: float = field(default_factory=time.monotonic)
    closed: bool = False
    # Set when this browser runs headless and Core streams its frames to the
    # page (see `relay_mirror`); `None` means a window on this machine's screen.
    mirror: "Any" = None

    @property
    def expired(self) -> bool:
        return time.monotonic() - self.started_at > SESSION_TIMEOUT_SECONDS

    def idle(self, now: float | None = None) -> bool:
        """Whether its page has stopped asking about it.

        The browser outliving the page is the leak this measures: a headless
        Chrome is a few hundred megabytes, and one left behind by a tab that
        closed without a beacon would sit there until the ten-minute limit — or
        for ever when nothing ever asked about it again.
        """

        moment = time.monotonic() if now is None else now
        return moment - max(self.started_at, self.last_seen_at) > IDLE_TIMEOUT_SECONDS

    def cookies(self) -> list[dict[str, Any]]:
        return read_cookies(self.port)

    def storage(self) -> dict[str, str]:
        return read_local_storage(self.port, self.origin)

    def focus(self) -> bool:
        """Bring this window back to the front, creating it if it is gone.

        The person may have dismissed it, or it may have opened behind their own
        browser; the waiting surface offers one press that puts it back rather
        than asking them to hunt through the Dock.
        """

        if self.closed or self.process.poll() is not None:
            return False
        if self.mirror is not None:
            # A mirrored display is drawn in the page itself: there is no
            # window on a screen to raise, and the pane is already showing it.
            return True
        return surface_window(self.port, self.origin)

    def close(self) -> None:
        """Stop the browser this process started, and nothing else.

        The profile is a throwaway directory this process created for one
        sign-in: the cookies it holds are the credential Core is about to keep,
        so the directory is removed rather than left for the next launch to
        inherit a session nobody asked it to reuse.
        """

        if self.closed:
            return
        self.closed = True
        try:
            targets = _devtools_targets(self.port)
            ws_url = next(
                (
                    str(target.get("webSocketDebuggerUrl"))
                    for target in targets
                    if isinstance(target.get("webSocketDebuggerUrl"), str) and target.get("webSocketDebuggerUrl")
                ),
                "",
            )
            if ws_url:
                _run(_cdp_messages(ws_url, [{"id": 1, "method": "Browser.close", "params": {}}], timeout=2.0))
        except Exception:
            pass
        try:
            self.process.terminate()
            self.process.wait(timeout=5)
        except Exception:
            try:
                self.process.kill()
            except Exception:
                pass
        mirror = self.mirror
        if mirror is not None:
            try:
                mirror.close()
            except Exception:
                pass
        try:
            shutil.rmtree(self.profile, ignore_errors=True)
        except Exception:
            pass


def _browser_websocket(port: int) -> str:
    """The browser-level CDP socket, which can create and raise windows."""

    version = _devtools_version(port)
    if not isinstance(version, Mapping):
        return ""
    socket_url = version.get("webSocketDebuggerUrl")
    return socket_url if isinstance(socket_url, str) else ""


def _cdp_exchange(ws_url: str, commands: list[Mapping[str, Any]], *, timeout: float = 5.0) -> list[Mapping[str, Any]]:
    """Send commands on one socket and return the answers, matched by id."""

    return _run(_cdp_messages(ws_url, commands, timeout=timeout))


def _target_id(target: Mapping[str, Any]) -> str:
    """The id of one `/json/list` entry.

    The list spells it `id`; `Target.createTarget` spells the same value
    `targetId`.  Reading only one of the two makes every later call a no-op —
    the window then is never sized, raised, or tidied.
    """

    for key in ("id", "targetId"):
        value = target.get(key)
        if isinstance(value, str) and value:
            return value
    return ""


def active_page_target(port: int, origin: str) -> Mapping[str, Any] | None:
    """The page whose screen is the one to show: the station, or where it went.

    A sign-in is not one document.  `Continue with GitHub` opens another origin,
    a station that moves to a single-sign-on host leaves the address it started
    on, and a page that reloads into `/login` is the same target with a new URL.
    Matching only the original host froze the mirror on the last frame it saw
    and refused every click that followed — the person was typing into a
    photograph.  So the station wins when it is there, and otherwise the newest
    page that is not a browser-internal one does.
    """

    station = _page_target(port, origin)
    if station is not None:
        return station
    fallback: Mapping[str, Any] | None = None
    for target in _devtools_targets(port):
        if target.get("type") != "page":
            continue
        url = target.get("url")
        if not isinstance(url, str) or not url:
            continue
        if url.startswith(("chrome://", "chrome-extension://", "about:", "devtools://")):
            continue
        fallback = target
    return fallback


def _page_target(port: int, origin: str) -> Mapping[str, Any] | None:
    """The page target showing `origin`, if one exists."""

    host = (urlsplit(origin).hostname or "").lower()
    for target in _devtools_targets(port):
        if target.get("type") != "page":
            continue
        url = target.get("url")
        if isinstance(url, str) and host and (urlsplit(url).hostname or "").lower() == host:
            return target
    return None


def surface_window(port: int, origin: str, *, width: int = 560, height: int = 820) -> bool:
    """Make sure a window showing `origin` exists, is on screen, and is in front.

    Launching Chrome with `--app=` is what draws a chrome-less sign-in window,
    and it is not guaranteed to produce one: measured on a machine where the
    person's own Chrome was already open, the process started, the debugging
    port answered, and the window never appeared — no page target at all, so
    the person saw a waiting surface and nothing to sign in to.

    So the window is created and raised here instead of being assumed: if the
    app window did not take the address, a page target is created for it, the
    window is put back into a normal state and sized onto the visible screen,
    and the target is activated.  `False` means this browser cannot show a
    page at all, which the caller reports instead of waiting.
    """

    target_id = _ensure_page_target(port, origin)
    if not target_id:
        return False
    ws_url = _browser_websocket(port)
    if not ws_url:
        return False
    answers = _cdp_exchange(ws_url, [
        {"id": 1, "method": "Browser.getWindowForTarget", "params": {"targetId": target_id}},
    ])
    window_id: int | None = None
    for answer in answers:
        result = answer.get("result")
        if isinstance(result, Mapping) and isinstance(result.get("windowId"), int):
            window_id = int(result["windowId"])
    if window_id is not None:
        # A window Chrome restored on another Space, or minimised, is a window
        # nobody can see.  `normal` plus an explicit rect puts it on the screen
        # the person is looking at.
        left, top = _visible_origin(width, height)
        _cdp_exchange(ws_url, [
            {
                "id": 1,
                "method": "Browser.setWindowBounds",
                "params": {
                    "windowId": window_id,
                    "bounds": {"windowState": "normal", "left": left, "top": top, "width": width, "height": height},
                },
            },
        ])
    _cdp_exchange(ws_url, [
        {"id": 1, "method": "Target.activateTarget", "params": {"targetId": target_id}},
    ])
    page = _page_target(port, origin)
    page_socket = str(page.get("webSocketDebuggerUrl") or "") if page is not None else ""
    if page_socket:
        _cdp_exchange(page_socket, [{"id": 1, "method": "Page.bringToFront", "params": {}}])
    return True


def _ensure_page_target(port: int, origin: str) -> str:
    """The page target showing `origin`, created if the browser has none.

    A fresh browser may start with a blank tab and never follow the address it
    was given — measured on a machine whose owner already had Chrome open, and
    on a display with no window manager.  Asking for the target and closing the
    blank page afterwards is what makes "the station is on screen" true rather
    than hoped for.
    """

    target = _page_target(port, origin)
    target_id = _target_id(target) if target is not None else ""
    ws_url = _browser_websocket(port)
    if not ws_url:
        return target_id
    if not target_id:
        answers = _cdp_exchange(ws_url, [
            {"id": 1, "method": "Target.createTarget", "params": {"url": origin}},
        ])
        for answer in answers:
            result = answer.get("result")
            if isinstance(result, Mapping) and isinstance(result.get("targetId"), str):
                target_id = str(result["targetId"])
    if not target_id:
        return ""
    # The blank page a fresh browser starts with would sit behind the sign-in
    # window as a second tab.  Closed here, after the target above exists,
    # because the browser may create it a moment later than the request.
    for other in _devtools_targets(port):
        if other.get("type") != "page":
            continue
        other_id = _target_id(other)
        url = other.get("url")
        if not other_id or other_id == target_id or not isinstance(url, str):
            continue
        if url in {"about:blank", "chrome://newtab/", "chrome://new-tab-page/"} or url.startswith("chrome://newtab"):
            _cdp_exchange(ws_url, [{"id": 1, "method": "Target.closeTarget", "params": {"targetId": other_id}}])
    return target_id


def _visible_origin(width: int, height: int) -> tuple[int, int]:
    """A top-left corner that lands the window on the visible screen.

    Chrome's "second instance" windows are placed by the browser, and a window
    reopened onto a display that no longer exists is how one ends up off-screen.
    The main display is read through Quartz on macOS and defaults to a plain
    offset elsewhere; either way the number only has to be sane.
    """

    screen_width, screen_height = 1440, 900
    if sys.platform == "darwin":
        try:
            from Quartz import CGDisplayBounds, CGMainDisplayID  # type: ignore

            bounds = CGDisplayBounds(CGMainDisplayID())
            screen_width = int(bounds.size.width) or screen_width
            screen_height = int(bounds.size.height) or screen_height
        except Exception:
            pass
    left = max(0, (screen_width - width) // 2)
    top = max(0, (screen_height - height) // 2)
    return left, top


def launch(
    origin: str,
    *,
    profile_root: Path,
    account_id: str,
    title: str = "",
    embedded: bool = False,
) -> LoginBrowser:
    """Open one browser window at `origin` with a debugging port.

    The window is *created over CDP* rather than handed to Chrome as `--app=`.
    Measured on a machine whose owner already had Chrome open, `--app=` started
    the process, published the debugging port, and drew nothing at all — no page
    target, no window, so the pane waited in front of a browser that was never
    on screen.  Creating the page target explicitly and raising it is the part
    that makes a window appear; the station is still its own origin, which is
    what keeps a captcha, a token prompt and an OAuth redirect behaving the way
    they do in any browser.
    """

    candidate = find_browser()
    if candidate is None:
        raise BrowserUnavailable(unavailable_reason() or "no_browser")
    if embedded or needs_virtual_display():
        # No screen to put a window on, or a page that is somebody else's: the
        # browser runs headless and its frames go to the page instead.
        return _launch_mirrored(origin, candidate, profile_root=profile_root, account_id=account_id)
    profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = Path(tempfile.mkdtemp(prefix=f"yr-relay-{_slug(account_id)}-", dir=str(profile_root)))
    port = _free_loopback_port()
    arguments = [
        candidate.executable,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--disable-background-networking",
        # A throwaway profile has no Keychain entry; without this macOS asks to
        # unlock one for a browser window that lives ten minutes.
        "--use-mock-keychain",
        "--password-store=basic",
        "--window-size=560,820",
    ]
    if title:
        arguments.insert(1, f"--app-name={title}")
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        raise BrowserLaunchFailed(str(exc)) from None
    browser = LoginBrowser(browser=candidate, origin=origin, port=port, process=process, profile=profile)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _devtools_version(port) is not None:
            # The port answering is not the window: Chrome can start, publish a
            # port, and draw nothing.  Creating or raising the page here is what
            # turns "waiting" into a window somebody can use.
            for attempt in range(4):
                if surface_window(port, origin):
                    return browser
                time.sleep(0.5)
            browser.close()
            raise BrowserLaunchFailed("The browser did not open the station page")
        if process.poll() is not None:
            browser.close()
            raise BrowserLaunchFailed("The browser exited before it opened")
        time.sleep(0.15)
    browser.close()
    raise BrowserLaunchFailed("The browser did not publish a debugging port")


def _launch_mirrored(
    origin: str,
    candidate: BrowserCandidate,
    *,
    profile_root: Path,
    account_id: str,
    width: int = 560,
    height: int = 820,
) -> LoginBrowser:
    """Start the browser headless and stream its screen to the page.

    Everything the person does happens on the station's own page, inside the
    browser this machine already had: its captcha, its redirect, its keyboard.
    Core relays frames and input over the same DevTools port the cookie read
    uses, so nothing is installed and no second port exists.

    A mirror nobody can see is not a mirror: the first frame is waited for
    before this returns, because a browser that started but never painted would
    otherwise leave the pane on a black rectangle with no explanation.
    """

    from . import relay_mirror

    profile_root.mkdir(parents=True, exist_ok=True, mode=0o700)
    profile = Path(tempfile.mkdtemp(prefix=f"yr-relay-{_slug(account_id)}-", dir=str(profile_root)))
    port = _free_loopback_port()
    arguments = [
        candidate.executable,
        f"--user-data-dir={profile}",
        f"--remote-debugging-port={port}",
        "--headless=new",
        "--remote-allow-origins=*",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-features=Translate,MediaRouter",
        "--disable-background-networking",
        "--password-store=basic",
        # A headless window has no frame, and the display this session serves
        # is exactly the size the page draws: no scrollbars to steal pixels.
        "--hide-scrollbars",
        f"--window-size={width},{height}",
    ]
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        # A container usually runs as root, where Chrome's sandbox cannot
        # start.  The isolation there is the container, not this flag.
        arguments.append("--no-sandbox")
    arguments.append(origin)
    try:
        process = subprocess.Popen(
            arguments,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
    except OSError as exc:
        shutil.rmtree(profile, ignore_errors=True)
        raise BrowserLaunchFailed(str(exc)) from None
    browser = LoginBrowser(browser=candidate, origin=origin, port=port, process=process, profile=profile)
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _devtools_version(port) is not None:
            if not _ensure_page_target(port, origin):
                browser.close()
                raise BrowserLaunchFailed("The browser did not open the station page")
            mirror = relay_mirror.MirrorSession(port=port, origin=origin, width=width, height=height)
            if not mirror.start(timeout=START_TIMEOUT_SECONDS):
                mirror.close()
                browser.close()
                raise BrowserLaunchFailed("The station did not paint")
            browser.mirror = mirror
            return browser
        if process.poll() is not None:
            browser.close()
            raise BrowserLaunchFailed("The browser exited before it opened")
        time.sleep(0.15)
    browser.close()
    raise BrowserLaunchFailed("The browser did not publish a debugging port")


def _slug(value: str) -> str:
    safe = "".join(character for character in value if character.isalnum() or character in "-_")
    return (base64.urlsafe_b64encode(safe.encode("utf-8")).decode("ascii").rstrip("=")[:16]) or "account"
