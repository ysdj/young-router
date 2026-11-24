"""Answer a model listing on this machine without a key.

The listing is not a secret -- it is the same list this app shows in its own
window and writes into a client's catalog -- and reading it locally is what a
person does when they want to know what the router serves:

    curl http://127.0.0.1:12390/codex/models
    curl http://127.0.0.1:12390/v1/models

Nothing else becomes keyless.  Two conditions are required together, and they
are what keeps this from becoming a hole:

* **the peer is loopback** -- a proxy bound to a published address (a gateway
  in a container, a LAN share) answers a remote caller with the ordinary 401,
  because adding the key for them would hand the whole API away;
* **the `Host` is a loopback name** -- a page on any site can make the browser
  connect to `127.0.0.1`; it cannot change the `Host` it sends to a name this
  process answers, so `Host: attacker.example` is refused.  This is the same
  DNS-rebinding guard Core's page gate uses, and it is why the check is on
  `Host` rather than on the peer alone.

Only ``GET``/``HEAD`` on the listing paths is affected.  A request that already
states a credential is passed through untouched, wrong key included: this adds
a key where none was given, it never replaces one or relaxes a check.
"""

from __future__ import annotations

import ipaddress
from typing import Any, Final

#: The paths whose answer is a model listing.
LISTING_PATHS: Final = frozenset({"/v1/models", "/models", "/codex/models"})
#: Names this process answers as "local".  A request must state one of these.
LOOPBACK_HOSTS: Final = frozenset({"127.0.0.1", "localhost", "::1", "[::1]"})
#: Credentials a caller may have stated; any of them means "leave it alone".
CREDENTIAL_HEADERS: Final = ("authorization", "x-api-key")


def is_loopback_address(value: object) -> bool:
    """Whether one host or peer address is this machine's loopback."""

    text = str(value or "").strip().strip("[]").lower()
    if not text:
        return False
    if text == "localhost":
        return True
    try:
        return ipaddress.ip_address(text).is_loopback
    except ValueError:
        return False


def _host_is_local(headers: dict[str, str]) -> bool:
    host = headers.get("host", "")
    if not host:
        return False
    # ``host`` may carry a port; IPv6 literals keep their brackets until the
    # port is split off, which ``rpartition`` cannot do naively.
    if host.startswith("["):
        name = host.split("]", 1)[0] + "]"
    else:
        name = host.rpartition(":")[0] or host
        if name == host and ":" in host:
            name = host.split(":", 1)[0]
    return name.strip().lower() in LOOPBACK_HOSTS or is_loopback_address(name)


def _peer_is_local(scope: dict[str, Any]) -> bool:
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client:
        return False
    return is_loopback_address(client[0])


def local_listing_path(path: str) -> bool:
    return (path.rstrip("/") or "/") in LISTING_PATHS


class LocalListingKeyMiddleware:
    """Give a local, keyless listing read this proxy's own key."""

    def __init__(self, app: Any, *, key_provider: Any = None) -> None:
        self.app = app
        self._key_provider = key_provider

    def _key(self) -> str:
        if callable(self._key_provider):
            try:
                return str(self._key_provider() or "").strip()
            except Exception:
                return ""
        from .search_endpoint import master_key, proxy_general_settings

        return master_key(proxy_general_settings())

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if (
            scope.get("type") != "http"
            or str(scope.get("method") or "GET").upper() not in {"GET", "HEAD"}
            or not local_listing_path(str(scope.get("path") or "/"))
            or not _peer_is_local(scope)
        ):
            await self.app(scope, receive, send)
            return
        headers = [
            (key, value)
            for key, value in scope.get("headers") or []
            if isinstance(key, bytes) and isinstance(value, bytes)
        ]
        stated = {key.decode("latin-1").lower() for key, _ in headers}
        if any(name in stated for name in CREDENTIAL_HEADERS):
            await self.app(scope, receive, send)
            return
        as_text = {
            key.decode("latin-1").lower(): value.decode("latin-1")
            for key, value in headers
        }
        if not _host_is_local(as_text):
            await self.app(scope, receive, send)
            return
        key = self._key()
        if not key:
            await self.app(scope, receive, send)
            return
        scope = dict(scope)
        scope["headers"] = [*headers, (b"authorization", f"Bearer {key}".encode("latin-1"))]
        await self.app(scope, receive, send)


def register(app: Any) -> None:
    """Install the middleware on the proxy app, once."""

    marker = "_young_router_local_listing_key"
    if getattr(app, marker, None):
        return
    setattr(app, marker, True)
    app.add_middleware(LocalListingKeyMiddleware)


__all__ = [
    "LISTING_PATHS",
    "LOOPBACK_HOSTS",
    "LocalListingKeyMiddleware",
    "is_loopback_address",
    "local_listing_path",
    "register",
]
