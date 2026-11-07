"""The page and its API on the port a person types.

The router an install publishes is the proxy's port, because that is what every
client's base URL points at.  The browser page therefore rides that same port
instead of a second one nobody would find, and the proxy answers two prefixes
that belong to Core: ``/ui`` is the bundle and ``/ipc`` is the settings API.
Everything else stays LiteLLM's.

These tests pin the three properties that make that safe rather than merely
convenient: the gate and the session stay Core's (one implementation, one
cookie), a Core that is not answering is stated instead of shown as an empty
shell, and the prefixes this app owns cannot grow into the model wire.
"""

from __future__ import annotations

import asyncio
import http.server
import json
import threading
import unittest

from young_router.proxy import web_forward


class PrefixOwnershipTests(unittest.TestCase):
    def test_only_the_page_and_its_api_belong_to_core(self) -> None:
        for owned in ("/ui", "/ui/", "/ui/bundle.js", "/ipc", "/ipc/hello", "/ipc/host/secret/read"):
            self.assertTrue(web_forward.forwards_path(owned), owned)
        # The model wire is LiteLLM's, and so is anything that merely starts
        # with the same letters: a prefix check that matched ``/uix`` or
        # ``/ipcfoo`` would swallow routes this app never defined.
        for foreign in ("/v1", "/v1/chat/completions", "/v1/models", "/health", "/ui2", "/ipcx", "/"):
            self.assertFalse(web_forward.forwards_path(foreign), foreign)

    def test_an_unset_endpoint_means_this_proxy_serves_no_page(self) -> None:
        """No Core still owns the prefixes: it must not become LiteLLM's.

        LiteLLM mounts its own dashboard at ``/ui``.  A proxy launched by hand,
        or the gateway machine that runs the proxy alone, has no Core to answer
        that prefix -- and letting the request fall through made a public port
        serve an admin console behind a key that is also the clients' API key.
        Covering the prefix and covering its absence are the same job.
        """

        import os

        previous = os.environ.pop(web_forward.ENDPOINT_ENV, None)
        try:
            self.assertIsNone(web_forward.core_endpoint())
            installed: list[dict[str, object]] = []

            class App:
                def add_middleware(self, factory: object, **kwargs: object) -> None:
                    installed.append({"factory": factory, **kwargs})

            marker = App()
            # Registered, and honest about there being nothing behind it.
            self.assertFalse(web_forward.register(marker))
            self.assertEqual(
                [{"factory": web_forward.CoreForwardingMiddleware, "endpoint": None}], installed
            )
        finally:
            if previous is not None:
                os.environ[web_forward.ENDPOINT_ENV] = previous

    def test_a_host_with_no_core_answers_the_prefix_itself(self) -> None:
        below = _Unreached()
        middleware = web_forward.CoreForwardingMiddleware(below, endpoint=None)

        # The page: a plain statement that this host has none, and nothing
        # about what else runs here.
        for path in ("/ui", "/ui/", "/ui/bundle.js"):
            below.called = False
            status, headers, body = _call(middleware, "GET", path)
            self.assertEqual(404, status, path)
            self.assertTrue(headers["content-type"].startswith("text/plain"), path)
            self.assertEqual(web_forward.NO_PAGE_BODY, body, path)
            self.assertFalse(below.called, path)

        # The settings API is asked by a program: it reads the same answer a
        # restarting Core gives, because from a client's side they are one fact.
        below.called = False
        status, headers, body = _call(middleware, "POST", "/ipc", b"{}")
        self.assertEqual(503, status)
        self.assertEqual("application/json", headers["content-type"].split(";")[0])
        self.assertEqual("core_unavailable", json.loads(body)["error"]["code"])
        self.assertFalse(below.called)

        # Everything else is still LiteLLM's, on this host too.  A fresh app
        # below, because ``_Unreached`` records the call on its class.
        litellm_side = _Unreached()
        _Unreached.called = False
        status, _headers, body = _call(
            web_forward.CoreForwardingMiddleware(litellm_side, endpoint=None), "GET", "/v1/models"
        )
        self.assertEqual(418, status)
        self.assertEqual(b"litellm", body)
        self.assertTrue(litellm_side.called)

    def test_the_endpoint_is_one_host_and_port(self) -> None:
        import os

        previous = os.environ.get(web_forward.ENDPOINT_ENV)
        try:
            os.environ[web_forward.ENDPOINT_ENV] = "127.0.0.1:45678"
            self.assertEqual(("127.0.0.1", 45678), web_forward.core_endpoint())
            # An IPv6 literal is bracketed on the way in, so the split on the
            # last colon is the address's own colon and not the port's.
            os.environ[web_forward.ENDPOINT_ENV] = "[::1]:45678"
            self.assertEqual(("::1", 45678), web_forward.core_endpoint())
            for broken in ("", "127.0.0.1", "127.0.0.1:", "127.0.0.1:0", "127.0.0.1:70000"):
                os.environ[web_forward.ENDPOINT_ENV] = broken
                self.assertIsNone(web_forward.core_endpoint(), broken)
        finally:
            if previous is None:
                import os as _os

                _os.environ.pop(web_forward.ENDPOINT_ENV, None)
            else:
                os.environ[web_forward.ENDPOINT_ENV] = previous


class _Recorder(http.server.BaseHTTPRequestHandler):
    """A Core stand-in that states what the forwarder sent it."""

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        return

    def do_GET(self) -> None:  # noqa: N802
        if self.path == "/ui/precompressed.js":
            # A precompressed page asset.  Core answers the script with the
            # two headers that name its encoding, and they have to survive
            # this hop: without the first the browser reads brotli bytes as
            # JavaScript.
            payload = b"\x0b\x02\x80precompressed"
            self.send_response(200)
            self.send_header("Content-Type", "text/javascript; charset=utf-8")
            self.send_header("Content-Encoding", "br")
            self.send_header("Vary", "Accept-Encoding")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
            return
        body = json.dumps({
            "path": self.path,
            "host": self.headers.get("Host", ""),
            "page_host": self.headers.get(web_forward.PUBLISHED_HOST_HEADER, ""),
            # What the browser said it reads.  End-to-end, so it is the
            # browser's own list rather than this hop's.
            "accept_encoding": self.headers.get("Accept-Encoding", ""),
            # Every value, not just the first: a header stated twice is how a
            # smuggled copy would arrive.
            "page_host_values": self.headers.get_all(web_forward.PUBLISHED_HOST_HEADER) or [],
        }).encode()
        # Core answers through stdlib's ``BaseHTTPRequestHandler``, whose own
        # ``Server`` header has a trailing space.  h11 refuses to send that
        # value, so the replay must never copy it -- the middleware's answer
        # would otherwise die as an illegal header and the page would see an
        # empty reply instead of Core's.
        self.send_response(200)
        self.send_header("Server", "YoungRouterCore/1 ")
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Core-Answer", "yes")
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", "0") or "0")
        payload = self.rfile.read(length)
        body = json.dumps({"path": self.path, "body": payload.decode("utf-8", "replace")}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def _serve(handler: type[http.server.BaseHTTPRequestHandler]) -> tuple[http.server.ThreadingHTTPServer, str, int]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, "127.0.0.1", server.server_address[1]


def _close(server: http.server.ThreadingHTTPServer) -> None:
    server.shutdown()
    server.server_close()


def _call(app: object, method: str, path: str, body: bytes = b"", headers: list[tuple[bytes, bytes]] | None = None) -> tuple[int, dict[str, str], bytes]:
    """One ASGI exchange, driven the way uvicorn would drive it."""

    async def run() -> tuple[int, dict[str, str], bytes]:
        scope = {
            "type": "http",
            "http_version": "1.1",
            "method": method,
            "path": path,
            "query_string": b"",
            "headers": headers or [],
            "scheme": "http",
        }
        sent: list[dict[str, object]] = []
        pending = body

        async def receive() -> dict[str, object]:
            nonlocal pending
            chunk, pending = pending, b""
            return {"type": "http.request", "body": chunk, "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        await app(scope, receive, send)  # type: ignore[operator]
        head = sent[0]
        return (
            int(head["status"]),  # type: ignore[arg-type]
            {name.decode(): value.decode() for name, value in head["headers"]},  # type: ignore[index]
            sent[-1].get("body", b""),  # type: ignore[return-value]
        )

    return asyncio.run(run())


class ForwardingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.server, self.address, self.port = _serve(_Recorder)
        self.addCleanup(_close, self.server)
        self.middleware = web_forward.CoreForwardingMiddleware(
            _Unreached(), endpoint=(self.address, self.port)
        )

    def test_a_page_request_reaches_core_unchanged(self) -> None:
        status, headers, body = _call(self.middleware, "GET", "/ui/bundle.js")
        self.assertEqual(200, status)
        self.assertEqual("/ui/bundle.js", json.loads(body)["path"])
        # Core answers a loopback `Host` name only, so the forwarder states one
        # instead of passing on whatever the browser typed.
        self.assertEqual(f"{self.address}:{self.port}", json.loads(body)["host"])
        # Core's own answer headers survive, which is what carries the session
        # cookie back to the page.
        self.assertEqual("yes", headers["x-core-answer"])
        # The hop's own server header is not replayed: its value cannot even be
        # serialised by this listener's protocol.
        self.assertNotIn("youngroutercore/1", json.dumps(headers).lower())

    def test_a_precompressed_asset_keeps_its_encoding_across_the_hop(self) -> None:
        """The page's script is carried compressed, and says so on both ends.

        Core answers a built script with `Content-Encoding` and `Vary`, and the
        browser states what it reads in `Accept-Encoding`.  All three are
        end-to-end headers, so none may be dropped by this hop: without the
        first the browser reads brotli bytes as JavaScript, and without the
        second a shared cache replays one browser's encoding to the next.
        """

        status, headers, body = _call(
            self.middleware, "GET", "/ui/precompressed.js", b"", [(b"accept-encoding", b"gzip, br")]
        )
        self.assertEqual(200, status)
        self.assertEqual("br", headers["content-encoding"])
        self.assertEqual("Accept-Encoding", headers["vary"])
        self.assertEqual(len(body), int(headers["content-length"]))
        # And the browser's own list reached Core, which is what picks the
        # sibling to answer with.
        recorded = json.loads(
            _call(self.middleware, "GET", "/ui/", b"", [(b"accept-encoding", b"br, gzip")])[2]
        )
        self.assertEqual("br, gzip", recorded["accept_encoding"])

    def test_the_name_the_browser_used_travels_with_the_request(self) -> None:
        """Core cannot see it otherwise: this hop is loopback either way.

        The page is gated on the name a person reached it by, so the proxy
        states the browser's own `Host` in a header of this app's, next to the
        loopback `Host` Core insists on.
        """

        status, _headers, body = _call(
            self.middleware, "GET", "/ui/", headers=[(b"host", b"router.example.com")]
        )
        self.assertEqual(200, status)
        recorded = json.loads(body)
        self.assertEqual("router.example.com", recorded["page_host"])
        self.assertEqual(f"{self.address}:{self.port}", recorded["host"])

    def test_a_client_cannot_state_the_page_name_itself(self) -> None:
        # The header decides whether Core gates the page, so a copy arriving
        # from the browser is dropped: only this hop says what the page is.
        status, _headers, body = _call(
            self.middleware,
            "GET",
            "/ui/",
            headers=[
                (b"host", b"router.example.com"),
                (web_forward.PUBLISHED_HOST_HEADER.lower().encode(), b"127.0.0.1"),
            ],
        )
        self.assertEqual(200, status)
        recorded = json.loads(body)
        self.assertEqual(["router.example.com"], recorded["page_host_values"])

    def test_a_settings_call_keeps_its_method_and_body(self) -> None:
        payload = json.dumps({"protocol_version": 1, "request_id": "t", "method": "snapshot", "params": {}}).encode()
        status, _headers, body = _call(
            self.middleware, "POST", "/ipc", payload, [(b"content-type", b"application/json")]
        )
        self.assertEqual(200, status)
        self.assertEqual(payload.decode(), json.loads(body)["body"])

    def test_a_stopped_core_is_stated_rather_than_shown_as_an_empty_page(self) -> None:
        # Port 1 on loopback: nothing listens, which is what a Core that is
        # restarting looks like from this side.
        middleware = web_forward.CoreForwardingMiddleware(_Unreached(), endpoint=("127.0.0.1", 1))
        status, _headers, body = _call(middleware, "GET", "/ui/")
        self.assertEqual(503, status)
        self.assertEqual("core_unavailable", json.loads(body)["error"]["code"])

    def test_everything_else_is_left_to_the_app_below(self) -> None:
        below = _Unreached()
        middleware = web_forward.CoreForwardingMiddleware(below, endpoint=(self.address, self.port))
        status, _headers, body = _call(middleware, "POST", "/v1/chat/completions", b"{}")
        self.assertEqual(418, status)
        self.assertEqual(b"litellm", body)
        self.assertTrue(below.called)
        # And the two owned prefixes never fall through to it: LiteLLM has no
        # route for either, so an answer from it would be a 404 the page reads
        # as "the Core is gone".
        for owned in ("/ui/bundle.js", "/ipc"):
            below.called = False
            _call(middleware, "POST" if owned == "/ipc" else "GET", owned, b"" if owned == "/ipc" else b"{}")
            self.assertFalse(below.called, owned)


class _Unreached:
    """The app this middleware sits in front of; it must not see owned paths."""

    called = False

    async def __call__(self, scope: object, receive: object, send: object) -> None:
        type(self).called = True

        async def respond() -> None:
            await send({  # type: ignore[operator]
                "type": "http.response.start",
                "status": 418,
                "headers": [(b"content-length", b"7")],
            })
            await send({"type": "http.response.body", "body": b"litellm"})  # type: ignore[operator]

        await respond()


if __name__ == "__main__":
    unittest.main()
