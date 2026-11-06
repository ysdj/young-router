"""The published local port, answering two prefixes that are Core's.

The proxy owns the port a person types (``12390``) because that is where every
client points its base URL.  The browser page lives on that same port, not on a
Core listener nobody knows the number of: one address for the page and the API
it calls is what keeps the page same-origin, so no CORS answer, no preflight and
no second port exist.  The page's own login, its session cookie and its
``/ipc`` calls stay Core's -- this module is a courier, not a second
implementation:

* ``/ui`` is Core's bundle, byte for byte;
* ``/ipc`` is Core's protocol, method and body untouched.

Everything else stays LiteLLM's.  The listener's host check, the password form,
the session cookie and the capability leases are the ones Core already owns, so
a page cannot be signed in here and unsigned-in there.
"""

from __future__ import annotations

import asyncio
import http.client
from typing import Any, Iterable

#: Where Core's own loopback listener answers.  The proxy is Core's child, so
#: Core writes this into the child's environment before launching it; a proxy
#: started by hand on a Core-less machine simply has no page.
ENDPOINT_ENV = "YOUNG_ROUTER_CORE_IPC_ENDPOINT"
#: The prefixes this app owns on the published port.
FORWARDED_PREFIXES = ("/ui", "/ipc")
#: Headers that describe one hop and must not be copied to the next.
#:
#: ``server`` and ``date`` are here because this listener's own server writes
#: them: Core's ``BaseHTTPRequestHandler`` emits ``Server: YoungRouterCore/1 ``
#: with a trailing space, and h11 refuses that value outright -- the whole
#: answer failed as "Illegal header value" until these stopped being replayed.
HOP_BY_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "server", "date",
})
#: A Core answer is a settings document or a page asset.  The long poll is the
#: one slow route: Core bounds it at ``EVENT_POLL_TIMEOUT_SECONDS``, so this
#: only has to outlive that plus the connect/teardown around it.
FORWARD_TIMEOUT_SECONDS = 60.0


def forwards_path(path: str) -> bool:
    """Whether this request belongs to Core rather than to LiteLLM."""

    return any(path == prefix or path.startswith(prefix + "/") for prefix in FORWARDED_PREFIXES)


def core_endpoint() -> tuple[str, int] | None:
    """Core's loopback address and port, as its parent wrote them down."""

    import os

    raw = os.environ.get(ENDPOINT_ENV, "").strip()
    if not raw:
        return None
    address, _separator, port = raw.rpartition(":")
    if not address or not port.isdigit():
        return None
    try:
        number = int(port)
    except ValueError:
        return None
    return (address.strip("[]"), number) if 1 <= number <= 65535 else None


class _Forwarded:
    """One answer Core gave, replayed to the browser."""

    def __init__(self, status: int, headers: list[tuple[str, str]], body: bytes) -> None:
        self.status = status
        self.headers = headers
        self.body = body


def _exchange(
    endpoint: tuple[str, int],
    method: str,
    path: str,
    headers: Iterable[tuple[bytes, bytes]],
    body: bytes,
    timeout: float,
) -> _Forwarded:
    """One request to Core, one answer back.

    A blocking exchange on a worker thread keeps this function readable and
    needs no third HTTP client: the proxy already ships ``http.client`` and the
    bodies involved are a settings document, a page asset, or a bounded upload.
    """

    address, port = endpoint
    connection = http.client.HTTPConnection(address, port, timeout=timeout)
    try:
        outbound = [
            (name.decode("latin-1"), value.decode("latin-1"))
            for name, value in headers
            if name.decode("latin-1").lower() not in HOP_BY_HOP
        ]
        # Core answers a loopback `Host` name only; that check is what keeps a
        # DNS-rebinding page from reaching it, so the name is stated here
        # rather than inherited from whatever the browser typed.
        outbound.append(("Host", f"{address}:{port}"))
        if body:
            outbound.append(("Content-Length", str(len(body))))
        connection.request(method, path, body=body or None, headers=dict(outbound))
        response = connection.getresponse()
        payload = response.read()
        # ``getheaders()`` is already ``str`` here, unlike the request side:
        # stdlib decodes the response with the server's own charset.
        answer = [
            (name, value.strip())
            for name, value in response.getheaders()
            if name.lower() not in HOP_BY_HOP
        ]
        return _Forwarded(response.status, answer, payload)
    finally:
        connection.close()


async def forward(scope: dict[str, Any], receive: Any, send: Any, endpoint: tuple[str, int]) -> None:
    """Answer one ASGI request from Core, or say the Core is not answering."""

    method = str(scope.get("method") or "GET").upper()
    raw_path = str(scope.get("path") or "/")
    query = scope.get("query_string") or b""
    path = raw_path if query else raw_path
    if query:
        path = f"{raw_path}?{query.decode('latin-1')}"
    chunks: list[bytes] = []
    while True:
        message = await receive()
        if message["type"] != "http.request":
            break
        chunks.append(message.get("body") or b"")
        if not message.get("more_body", False):
            break
    body = b"".join(chunks)
    headers = list(scope.get("headers") or [])
    loop = asyncio.get_running_loop()
    try:
        answer = await loop.run_in_executor(
            None, _exchange, endpoint, method, path, headers, body, FORWARD_TIMEOUT_SECONDS
        )
    except (OSError, http.client.HTTPException):
        # Core is stopped, restarting, or too slow to answer.  The page has to
        # say that rather than show a blank shell: this is the one failure the
        # browser would otherwise render as a working app with no data.
        payload = b'{"ok": false, "error": {"code": "core_unavailable", "message": "The local Core is not answering"}}'
        await send({
            "type": "http.response.start",
            "status": 503,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"cache-control", b"no-store"),
                (b"content-length", str(len(payload)).encode("latin-1")),
            ],
        })
        await send({"type": "http.response.body", "body": payload})
        return
    await send({
        "type": "http.response.start",
        "status": answer.status,
        "headers": [
            (name.lower().encode("latin-1"), value.encode("latin-1"))
            for name, value in answer.headers
        ] + [(b"content-length", str(len(answer.body)).encode("latin-1"))],
    })
    await send({"type": "http.response.body", "body": answer.body})


class CoreForwardingMiddleware:
    """Route this app's own two prefixes to Core, everything else to LiteLLM.

    A middleware rather than a route, because ``/ui`` is already a ``Mount`` in
    LiteLLM's own app object and a mount matches before any later route.
    """

    def __init__(self, app: Any, *, endpoint: tuple[str, int]) -> None:
        self.app = app
        self.endpoint = endpoint

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope.get("type") != "http" or not forwards_path(str(scope.get("path") or "/")):
            await self.app(scope, receive, send)
            return
        await forward(scope, receive, send, self.endpoint)


def register(app: Any) -> bool:
    """Take over the page and its API on this port, when this host has one.

    Returns whether the forwarder was installed: a proxy with no Core endpoint
    (a bare LiteLLM launched by hand) keeps serving only its own routes.
    """

    endpoint = core_endpoint()
    if endpoint is None:
        return False
    if getattr(app, _REGISTERED_ATTR, False):
        return True
    # Added last is outermost, which is what makes this see ``/ui`` before
    # LiteLLM's own mount does.
    app.add_middleware(CoreForwardingMiddleware, endpoint=endpoint)
    setattr(app, _REGISTERED_ATTR, True)
    return True


_REGISTERED_ATTR = "_young_router_core_forwarding"

__all__ = [
    "CoreForwardingMiddleware",
    "ENDPOINT_ENV",
    "FORWARDED_PREFIXES",
    "core_endpoint",
    "forward",
    "forwards_path",
    "register",
]
