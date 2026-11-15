from __future__ import annotations

from pathlib import Path
import tempfile
import threading
import types
import unittest

from young_router.core import (
    CoreIPCClient,
    CoreIPCServer,
    CoreStore,
    IpcEndpoint,
    MemoryDomain,
    RequestEnvelope,
)


class ImportPreviewFileTests(unittest.TestCase):
    def test_a_refused_preview_discards_the_uploaded_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            core = CoreStore(domains=[MemoryDomain("language")])
            server = CoreIPCServer(core)
            session = server._mint_web_session()
            path = Path(directory) / "package.json"
            path.write_text("{}", encoding="utf-8")
            server._browser_files["upload"] = path
            server._browser_file_sessions["upload"] = session
            request = RequestEnvelope.from_mapping(
                {
                    "protocol_version": 1,
                    "request_id": "preview",
                    "method": "import_preview",
                    "params": {"source_token": "upload", "revision": core.revision + 1},
                }
            )
            response = server.handle_request(request, session_token=session)
            self.assertFalse(response.ok)
            self.assertFalse(path.exists())
            self.assertNotIn("upload", server._browser_files)


class SubscriptionGonePollTests(unittest.TestCase):
    def test_a_gone_subscription_is_replaced_instead_of_repolled(self) -> None:
        client = CoreIPCClient(IpcEndpoint(kind="loopback", address="127.0.0.1", port=1), "bootstrap-token")
        subscriptions = iter(["gone", "fresh"])
        client.call = lambda method, params=None: {"subscription_id": next(subscriptions)}  # type: ignore[method-assign]
        seen: list[str] = []
        replaced = threading.Event()

        def http(path: str, *, payload: bytes | None = None, token: str) -> tuple[int, bytes, dict[str, str]]:
            subscription = path.split("subscription_id=", 1)[1].split("&", 1)[0]
            seen.append(subscription)
            if subscription == "gone":
                return 410, b"{}", {}
            replaced.set()
            return 200, b'{"event": null}', {}

        client._http = http  # type: ignore[method-assign]
        unsubscribe = client.subscribe(lambda _event: None)
        self.addCleanup(unsubscribe)
        self.assertTrue(replaced.wait(2.0), "the poll kept re-asking with the gone subscription")
        unsubscribe()
        self.assertEqual(["gone", "fresh"], seen[:2])


class IPv6EndpointUrlTests(unittest.TestCase):
    def test_ipv6_endpoint_urls_bracket_the_host(self) -> None:
        endpoint = IpcEndpoint(kind="loopback", address="::1", port=1234)
        self.assertEqual("http://[::1]:1234/ipc", endpoint.url)
        client = CoreIPCClient(endpoint, "bootstrap-token")
        self.assertEqual("http://[::1]:1234/ipc", client._url("/ipc"))

    def test_ipv6_web_page_urls_bracket_the_host(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            server = CoreIPCServer(CoreStore(domains=[MemoryDomain("language")]), address="::1")
            server._web_root = Path(directory)
            server._web_password = "password"
            server._web_launch_token = "launch"
            server._server = types.SimpleNamespace(server_address=("::1", 1234))
            self.assertEqual("http://[::1]:1234/ui/", server.web_host_url)
            self.assertEqual("http://[::1]:1234/ui/#launch=launch", server.web_launch_url)
