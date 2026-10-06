"""The staged download path keeps urllib's semantics on a reused connection.

Every artifact build fetches a release index and a tarball per staged
integration, and the pooled connection is what removes the second TLS
handshake.  The saving is only real if a request the pool cannot serve still
reaches the same result through urllib, so these tests hold both halves: the
reuse happens, and every failure mode falls back instead of degrading.

The fake connections below replace ``update_common._open_connection`` rather
than ``http.client.HTTPSConnection``: the stdlib resolves its own class names
while connecting, so patching that module attribute would redirect CPython
instead of this module.
"""

from __future__ import annotations

import http.client
import http.server
import importlib
import socketserver
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

update_common = importlib.import_module("update_common")

UGENT = "young-router-staging-test"


class FakeResponse:
    def __init__(self, status: int = 200, body: bytes = b"payload") -> None:
        self.status = status
        self._body = body

    def read(self) -> bytes:
        return self._body


class RecordingConnection:
    """A stand-in for the pooled HTTPS connection that never opens a socket."""

    opened: list["RecordingConnection"] = []

    def __init__(self, origin: str, *, status: int = 200, body: bytes = b"payload") -> None:
        self.origin = origin
        self.status = status
        self.body = body
        self.requests: list[dict[str, object]] = []
        self.closed = 0
        RecordingConnection.opened.append(self)

    def request(self, method, url, body=None, headers=None, **kwargs) -> None:
        self.requests.append({"method": method, "url": url, "headers": dict(headers or {})})

    def getresponse(self) -> FakeResponse:
        return FakeResponse(self.status, self.body)

    def close(self) -> None:
        self.closed += 1


class DownloadBytesTests(unittest.TestCase):
    def setUp(self) -> None:
        RecordingConnection.opened = []
        # The pool is per thread on the module, so one test's fake connection
        # can never be handed to the next one.
        self.addCleanup(
            lambda: update_common._DOWNLOAD_CONNECTIONS.__dict__.pop("connections", None)
        )

    def open_with(self, **kwargs):
        return mock.patch.object(
            update_common,
            "_open_connection",
            side_effect=lambda origin, timeout: RecordingConnection(origin, **kwargs),
        )

    def test_two_downloads_share_one_connection_and_send_no_keep_alive_header(self) -> None:
        with self.open_with():
            first = update_common.download_bytes(
                "https://example.invalid/dist/index.json", timeout=5, user_agent=UGENT
            )
            second = update_common.download_bytes(
                "https://example.invalid/dist/node.tar.gz?x=1", timeout=5, user_agent=UGENT
            )

        self.assertEqual(first, b"payload")
        self.assertEqual(second, b"payload")
        # One connection for both requests is the whole point of the change.
        self.assertEqual(len(RecordingConnection.opened), 1)
        requests = RecordingConnection.opened[0].requests
        self.assertEqual(
            [r["url"] for r in requests], ["/dist/index.json", "/dist/node.tar.gz?x=1"]
        )
        self.assertEqual(requests[0]["method"], "GET")
        self.assertEqual(requests[0]["headers"]["User-Agent"], UGENT)
        # HTTP/1.1 is persistent by default, so no Connection header is needed
        # for the reuse this test just proved.
        for request in requests:
            self.assertNotIn("Connection", request["headers"])

    def test_a_non_https_url_falls_back_to_urllib(self) -> None:
        with mock.patch.object(
            update_common, "request_bytes", return_value=b"from-urllib"
        ) as fallback:
            payload = update_common.download_bytes(
                "http://example.invalid/pkg.tgz", timeout=5, user_agent=UGENT
            )

        self.assertEqual(payload, b"from-urllib")
        fallback.assert_called_once()

    def test_a_non_200_response_falls_back_to_urllib(self) -> None:
        with self.open_with(status=302, body=b"moved"):
            with mock.patch.object(
                update_common, "request_bytes", return_value=b"followed"
            ) as fallback:
                payload = update_common.download_bytes(
                    "https://example.invalid/pkg.tgz", timeout=5, user_agent=UGENT
                )

        self.assertEqual(payload, b"followed")
        fallback.assert_called_once()

    def test_a_retired_connection_is_replaced_through_urllib(self) -> None:
        def refuse(origin: str, timeout: int):
            raise http.client.CannotSendRequest("connection was closed")

        with mock.patch.object(update_common, "_open_connection", side_effect=refuse):
            with mock.patch.object(
                update_common, "request_bytes", return_value=b"reconnected"
            ) as fallback:
                payload = update_common.download_bytes(
                    "https://example.invalid/pkg.tgz", timeout=5, user_agent=UGENT
                )

        self.assertEqual(payload, b"reconnected")
        fallback.assert_called_once()

    def test_each_origin_gets_its_own_connection(self) -> None:
        with self.open_with():
            update_common.download_bytes("https://a.invalid/x", timeout=5, user_agent=UGENT)
            update_common.download_bytes("https://b.invalid/y", timeout=5, user_agent=UGENT)

        self.assertEqual([c.origin for c in RecordingConnection.opened], ["a.invalid", "b.invalid"])

    def test_each_thread_gets_its_own_connection(self) -> None:
        with self.open_with():
            threads = [
                threading.Thread(
                    target=update_common.download_bytes,
                    args=("https://registry.invalid/pkg",),
                    kwargs={"timeout": 5, "user_agent": UGENT},
                )
                for _ in range(3)
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join()

        # The parallel staging jobs must never share one socket: one job's read
        # would otherwise consume another job's response.
        self.assertEqual(len(RecordingConnection.opened), 3)


class LocalServerConnection:
    """Talks plain HTTP to a local test server, so the pool can be exercised."""

    def __init__(self, port: int) -> None:
        self._connection = http.client.HTTPConnection("127.0.0.1", port, timeout=5)

    def request(self, method, url, body=None, headers=None, **kwargs) -> None:
        self._connection.request(method, url, body=body, headers=headers or {})

    def getresponse(self):
        return self._connection.getresponse()

    def close(self) -> None:
        self._connection.close()


class PooledDownloadIntegrationTests(unittest.TestCase):
    """The pooled path against a real server, not a fake connection."""

    def setUp(self) -> None:
        self.addCleanup(
            lambda: update_common._DOWNLOAD_CONNECTIONS.__dict__.pop("connections", None)
        )

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802 - required by the stdlib
                body = self.path.encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args) -> None:
                pass

        self.server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        self.addCleanup(self.server.server_close)
        self.server_thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.server_thread.start()
        self.addCleanup(self.server.shutdown)
        self.opened = 0

    def test_one_connection_serves_two_downloads(self) -> None:
        port = self.server.server_address[1]

        def open_local(origin: str, timeout: int):
            self.opened += 1
            return LocalServerConnection(port)

        with mock.patch.object(update_common, "_open_connection", side_effect=open_local):
            first = update_common.download_bytes(
                "https://local.invalid/one", timeout=5, user_agent=UGENT
            )
            second = update_common.download_bytes(
                "https://local.invalid/two", timeout=5, user_agent=UGENT
            )

        self.assertEqual(first, b"/one")
        self.assertEqual(second, b"/two")
        self.assertEqual(self.opened, 1, "one connection should serve both downloads")


if __name__ == "__main__":
    unittest.main()
