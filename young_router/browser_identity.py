"""The one browser identity every request of our own presents.

Young Router never exposes a User-Agent of its own.  A downstream client's
User-Agent is forwarded byte-for-byte, and every request the app makes on its
own behalf — relay dashboard calls, provider probes, usage-log reads, WebDAV,
helper workers, local proxy calls — presents the same browser identity a real
client of that platform would.

One literal per platform, not a rotating pool: a relay that binds a browser
session to its IP and User-Agent fingerprint (sub2api's session binding) keeps
one account on one fingerprint, so the embedded sign-in page, the native proof
requests, and Core's dashboard client must all send exactly this value.  The
native hosts pin the same literals; ``test_browser_identity`` compares them.
"""

from __future__ import annotations

import sys
from typing import Mapping
from urllib.parse import unquote

# Pinned by the macOS relay login webview (``AppKitNativeLeaf``) and reused by
# every macOS-side request of ours.
MACOS_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/605.1.15 (KHTML, like Gecko) "
    "Version/18.6 Safari/605.1.15"
)
# Pinned by the Windows relay login webview (``WindowsRelayLogin``) and reused
# by every Windows-side request of ours.
WINDOWS_BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0"
)
BROWSER_ACCEPT_LANGUAGE = "zh-CN,zh;q=0.9,en;q=0.8"

# Anything that names this app or a helper it ships is our own identity, never
# a client's.  It must not leave the machine.
_SELF_IDENTITY_MARKERS = (
    "young router",
    "young-router",
    "youngrouter",
    "young_router",
    "pi-web-access",
)


def browser_user_agent(platform: str | None = None) -> str:
    """Return the browser User-Agent this platform presents."""

    target = sys.platform if platform is None else platform
    return WINDOWS_BROWSER_USER_AGENT if str(target).startswith("win") else MACOS_BROWSER_USER_AGENT


def browser_request_headers(
    *,
    accept: str = "application/json",
    extra: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Return the header set for one request the app makes on its own behalf.

    A caller may add its own headers; a caller-supplied ``User-Agent`` still
    loses to the shared identity, because no request of ours may identify the
    app.
    """

    headers = {
        "Accept": accept,
        "Accept-Language": BROWSER_ACCEPT_LANGUAGE,
        "User-Agent": browser_user_agent(),
    }
    if extra:
        headers.update({key: value for key, value in extra.items() if key.lower() != "user-agent"})
    return headers


def is_self_identifying_user_agent(value: object) -> bool:
    """Whether one User-Agent names this app instead of a client of it."""

    if not isinstance(value, str) or not value.strip():
        return False
    raw = value.strip()
    candidates = {raw.lower()}
    if "%" in raw:
        # A proxy serialization can hand the header over percent-encoded.
        candidates.add(unquote(raw).lower())
    return any(
        marker in candidate
        for candidate in candidates
        for marker in _SELF_IDENTITY_MARKERS
    )


__all__ = [
    "BROWSER_ACCEPT_LANGUAGE",
    "MACOS_BROWSER_USER_AGENT",
    "WINDOWS_BROWSER_USER_AGENT",
    "browser_request_headers",
    "browser_user_agent",
    "is_self_identifying_user_agent",
]
