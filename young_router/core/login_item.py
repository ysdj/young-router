"""The person's own login item, for a host with no native app process to own it.

The desktop hosts register launch-at-login themselves -- macOS through
``SMAppService.mainApp`` in the app process, Windows through the Run key its
leaf writes -- and Core only records the preference for them.  The browser page
has no such process behind it even though the app it belongs to is installed on
the same machine: the page's Core is that app's Core, so Core is what writes the
login item here.

Two rules keep this from being a second, private notion of autostart:

* The preference stays Core's (``autostart.enabled``).  This module only makes
  the operating system act on it, and answers whether it could -- a working-tree
  Core has no installed app to point at, and says so rather than claiming a
  login item nobody will ever run.
* Nothing here invents an app path.  The bundle is whatever contains this Core
  at its documented place (``Young Router.app/Contents/Resources/Core``, or the
  Windows install's ``YoungRouter\\Core``); a Core outside that shape reports
  that it has no login item to register.

The label is the one older builds and the Homebrew cask's uninstall stanza
already name, so toggling this off leaves no stale entry behind.
"""

from __future__ import annotations

import os
import plistlib
import subprocess
import sys
from pathlib import Path

#: The launchd label / Run value this module owns.
LOGIN_ITEM_ID = "young.router.login"
#: The bundle identifier the login item asks macOS to open.
APP_BUNDLE_ID = "young.router.app"

#: macOS names its login items in the user's own LaunchAgents directory.
_LAUNCH_AGENTS = "Library/LaunchAgents"
_WINDOWS_RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
_WINDOWS_RUN_VALUE = "YoungRouter"


def _core_root() -> Path:
    """This Core's own directory, without importing a runtime helper for it."""

    return Path(__file__).resolve().parents[1]


def app_bundle() -> Path | None:
    """The installed macOS app bundle that contains this Core, if any.

    ``Young Router.app/Contents/Resources/Core`` is the shipped shape, so the
    bundle is three levels up.  A Core run from a working tree -- a checkout, a
    test, a terminal run of the sources -- is not inside an app and answers
    None instead of guessing at one.
    """

    if sys.platform != "darwin":
        return None
    root = _core_root()
    # Core sits at Contents/Resources/Core; the bundle is the directory that
    # carries Contents/Info.plist.
    for parent in list(root.parents)[:4]:
        if (parent / "Contents" / "Info.plist").is_file():
            return parent
    return None


def windows_launcher() -> Path | None:
    """The installed Windows launcher this login item would start.

    The packaged app is launched through the small launcher beside the payload:
    it hands the start to the MSIX registration, which is what puts a window on
    screen.  A Core that is not inside that install has no launcher to name.
    """

    if os.name != "nt":
        return None
    root = _core_root()
    # <install>\YoungRouter\Core  ->  <install>\YoungRouter\YoungRouter.exe, and
    # the launcher form the build writes beside it.
    for parent in [root.parent, *list(root.parents)[:2]]:
        for name in ("YoungRouter.exe", "Young Router.exe"):
            candidate = parent / name
            if candidate.is_file():
                return candidate
    return None


def available() -> bool:
    """Whether this machine has an app this module can put in the login item."""

    if sys.platform == "darwin":
        return app_bundle() is not None
    if os.name == "nt":
        return windows_launcher() is not None
    return False


def _launch_agent_path() -> Path:
    return Path.home() / _LAUNCH_AGENTS / f"{LOGIN_ITEM_ID}.plist"


def _launch_agent_document() -> dict[str, object]:
    """The plist that opens the app at login.

    ``open -b`` asks LaunchServices for the app by identifier, so moving the
    bundle or reinstalling it at another path leaves the login item working --
    and opening an app that is already running activates it instead of starting
    a second copy, which is what makes this safe beside the app's own
    ``SMAppService`` registration.
    """

    return {
        "Label": LOGIN_ITEM_ID,
        "ProgramArguments": ["/usr/bin/open", "-b", APP_BUNDLE_ID],
        "RunAtLoad": True,
        "ProcessType": "Interactive",
    }


def set_login_item(enabled: bool) -> bool:
    """Register or remove the login item; False when this host has none.

    A False answer is not a failure to report as a crash: the caller hands the
    preference back, so the switch never claims a login the system never heard
    of.
    """

    if sys.platform == "darwin":
        return _set_macos_login_item(enabled)
    if os.name == "nt":
        return _set_windows_login_item(enabled)
    return False


def _set_macos_login_item(enabled: bool) -> bool:
    bundle = app_bundle()
    plist = _launch_agent_path()
    domain = f"gui/{os.getuid()}"
    if not enabled:
        _run_launchctl(["bootout", f"{domain}/{LOGIN_ITEM_ID}"])
        try:
            plist.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
        return True
    if bundle is None:
        return False
    try:
        plist.parent.mkdir(parents=True, exist_ok=True)
        plist.write_bytes(plistlib.dumps(_launch_agent_document()))
    except OSError:
        return False
    # Written is what makes it a login item; bootstrapping only makes it live
    # for this session, so a failure there (already loaded, launchd busy) is not
    # a reason to report the preference as unapplied.
    _run_launchctl(["bootout", f"{domain}/{LOGIN_ITEM_ID}"])
    _run_launchctl(["bootstrap", domain, str(plist)])
    return True


def _set_windows_login_item(enabled: bool) -> bool:
    import winreg  # noqa: PLC0415 - only importable on Windows

    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, _WINDOWS_RUN_KEY, 0, winreg.KEY_QUERY_VALUE | winreg.KEY_SET_VALUE) as key:
            if enabled:
                launcher = windows_launcher()
                if launcher is None:
                    return False
                winreg.SetValueEx(key, _WINDOWS_RUN_VALUE, 0, winreg.REG_SZ, f'"{launcher}"')
                return True
            try:
                winreg.DeleteValue(key, _WINDOWS_RUN_VALUE)
            except FileNotFoundError:
                pass
            return True
    except OSError:
        return False


def _run_launchctl(arguments: list[str]) -> None:
    """Best-effort launchctl call: absence of launchd is not an error here."""

    try:
        subprocess.run(["/bin/launchctl", *arguments], check=False, capture_output=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        pass
