"""The app must never expose a User-Agent of its own.

One rule, two halves:

* everything the app sends on its own behalf — relay dashboard calls, session
  proofs, provider probes, usage-log reads, WebDAV, helper workers, local proxy
  calls — presents the shared browser identity;
* a downstream client's own User-Agent is forwarded byte-for-byte, and only an
  identity that names this app is repaired on the way upstream.

These tests are the guard for that rule: they compare the Python and native
literals, scan the shipped request code for self-naming User-Agent values, and
drive the data-plane hook and the relay client.
"""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path
import re
import tempfile
import unittest

from young_router import browser_identity
from young_router.core.domains.relay_accounts import RelayHTTPClient

ROOT = Path(__file__).resolve().parent.parent
IDENTITY_MODULE = ROOT / "young_router" / "browser_identity.py"
MACOS_NATIVE = ROOT / "rn/apps/macos/src/native/macos/AppKitNativeLeaf.swift"
WINDOWS_NATIVE = ROOT / "rn/apps/windows/src/native/windows/WindowsRelayLogin.cpp"

# Shipped request code (the app itself, not build tooling and not tests).
SHIPPED_REQUEST_FILES = (
    "codex_config.py",
    "remote_usage_logs.py",
    "webdav/core.py",
    "young_router/browser_identity.py",
    "young_router/dsh_vision_router.py",
    "young_router/responses_request.py",
    "young_router/core/model_contexts.py",
    "young_router/core/operations.py",
    "young_router/core/domains/providers_models.py",
    "young_router/core/domains/relay_accounts.py",
)

# Loopback-only clients may keep an internal marker: their User-Agent never
# leaves the machine.  Everything else must present the browser identity.
LOOPBACK_ONLY_MARKERS = (
    "rn/apps/windows/src/native/windows/CoreIPCBridge.cpp",
)


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def _cxx_wide_literal(text: str, name: str) -> str:
    """Return the concatenated wide string literal of one C++ constant."""

    # The User-Agent itself contains ';' separators, so read the whole run of
    # concatenated wide literals instead of stopping at the first semicolon.
    match = re.search(rf'{name}\[\]\s*=\s*((?:L"[^"]*"\s*)+);', text)
    if match is None:
        return ""
    return "".join(re.findall(r'L"([^"]*)"', match.group(1)))


def _user_agent_literals(text: str) -> list[str]:
    """Return every literal value assigned to a User-Agent-shaped key."""

    pattern = re.compile(
        r"""(?i)["']user-agent["']\s*:\s*["']([^"']*)["']"""
    )
    return pattern.findall(text)


class BrowserIdentityLiteralTests(unittest.TestCase):
    def test_native_hosts_pin_the_shared_identity(self) -> None:
        macOS = _read(MACOS_NATIVE)
        windows = _read(WINDOWS_NATIVE)
        self.assertIn(browser_identity.MACOS_BROWSER_USER_AGENT, macOS)
        self.assertEqual(
            browser_identity.WINDOWS_BROWSER_USER_AGENT,
            _cxx_wide_literal(windows, "kRelayBrowserUserAgent"),
        )
        # The sign-in webview and the session proof share that one identity:
        # a relay's session binding stores the fingerprint of the page that
        # signed in, so every later request of ours must repeat it exactly.
        self.assertIn("webView.customUserAgent = RelayBrowserIdentity.userAgent", macOS)
        for probe_headers in (
            'request.setValue(RelayBrowserIdentity.userAgent, forHTTPHeaderField: "User-Agent")',
        ):
            self.assertEqual(2, macOS.count(probe_headers))
        self.assertIn("core.Settings().UserAgent(kRelayBrowserUserAgent);", windows)
        self.assertIn("kRelayBrowserUserAgent, WINHTTP_ACCESS_TYPE_AUTOMATIC_PROXY", windows)
        self.assertIn("std::wstring(kRelayBrowserAcceptLanguage)", windows)
        self.assertNotIn("L\"User-Agent: \"", windows)

    def test_shipped_request_code_has_no_self_naming_user_agent(self) -> None:
        offenders: list[str] = []
        for relative in SHIPPED_REQUEST_FILES:
            path = ROOT / relative
            if relative == "young_router/browser_identity.py":
                continue
            for literal in _user_agent_literals(_read(path)):
                lowered = literal.lower()
                if any(marker in lowered for marker in ("young", "router", "pi-web-access")):
                    offenders.append(f"{relative}: {literal}")
        self.assertEqual([], offenders)

    def test_shipped_request_code_uses_the_shared_identity(self) -> None:
        self.assertIn("browser_request_headers()", _read(ROOT / "young_router/core/domains/relay_accounts.py"))
        self.assertIn("browser_request_headers()", _read(ROOT / "remote_usage_logs.py"))
        self.assertIn("browser_request_headers(", _read(ROOT / "webdav/core.py"))
        self.assertIn("browser_request_headers()", _read(ROOT / "codex_config.py"))

    def test_loopback_only_clients_are_the_documented_exception(self) -> None:
        for relative in LOOPBACK_ONLY_MARKERS:
            text = _read(ROOT / relative)
            self.assertIn("WINHTTP_ACCESS_TYPE_NO_PROXY", text)
            self.assertIn("L\"YoungRouterCore/1\"", text)

    def test_helpers_describe_the_identity(self) -> None:
        headers = browser_identity.browser_request_headers(extra={"User-Agent": "Young-Router/x", "X-Trace": "1"})
        self.assertEqual(browser_identity.browser_user_agent(), headers["User-Agent"])
        self.assertEqual("1", headers["X-Trace"])
        self.assertEqual(browser_identity.BROWSER_ACCEPT_LANGUAGE, headers["Accept-Language"])
        self.assertTrue(browser_identity.is_self_identifying_user_agent("Young Router/1 CFNetwork/3860.600.21"))
        self.assertTrue(browser_identity.is_self_identifying_user_agent("pi-web-access"))
        self.assertFalse(browser_identity.is_self_identifying_user_agent("codex-local/1.2.3"))
        self.assertFalse(browser_identity.is_self_identifying_user_agent(browser_identity.browser_user_agent()))
        self.assertEqual(
            browser_identity.WINDOWS_BROWSER_USER_AGENT,
            browser_identity.browser_user_agent("win32"),
        )
        self.assertEqual(
            browser_identity.MACOS_BROWSER_USER_AGENT,
            browser_identity.browser_user_agent("darwin"),
        )


class _StubResponse:
    def __init__(self, status: int, body: object, headers: object | None = None):
        self.status = status
        self._body = json.dumps(body).encode("utf-8")
        self.headers = headers if headers is not None else _StubHeaders()

    def getcode(self) -> int:
        return self.status

    def read(self, _limit: int | None = None) -> bytes:
        return self._body

    def __enter__(self) -> "_StubResponse":
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


class _StubHeaders:
    def get_all(self, _name: str) -> list[str]:
        return ["session=replace-session"]


class _StubOpener:
    def __init__(self, body: object):
        self.requests: list[object] = []
        self._body = body

    def open(self, request: object, timeout: float | None = None) -> _StubResponse:
        self.requests.append(request)
        return _StubResponse(200, self._body)


class RelayClientIdentityTests(unittest.TestCase):
    def _headers_of(self, request: object) -> dict[str, str]:
        return {str(key): str(value) for key, value in getattr(request, "headers").items()}

    def test_dashboard_reads_present_the_browser_identity(self) -> None:
        opener = _StubOpener({"data": {"items": []}})
        client = RelayHTTPClient(opener=opener)
        client.json("https://relay.example.test", "/api/v1/keys", headers={"X-Trace": "1"})
        headers = self._headers_of(opener.requests[0])
        self.assertEqual(browser_identity.browser_user_agent(), headers["User-agent"])
        self.assertEqual(browser_identity.BROWSER_ACCEPT_LANGUAGE, headers["Accept-language"])
        self.assertEqual("1", headers["X-trace"])
        self.assertNotIn("Young", json.dumps(headers))

    def test_password_login_presents_the_browser_identity(self) -> None:
        opener = _StubOpener({"data": {"access_token": "replace-token", "user": {"email": "person@example.test"}}})
        client = RelayHTTPClient(opener=opener)
        client.password_login("https://relay.example.test", "sub2api", "person@example.test", "replace-password")
        headers = self._headers_of(opener.requests[0])
        self.assertEqual(browser_identity.browser_user_agent(), headers["User-agent"])
        self.assertEqual(browser_identity.BROWSER_ACCEPT_LANGUAGE, headers["Accept-language"])

    def test_detection_probe_presents_the_browser_identity(self) -> None:
        opener = _StubOpener({"success": True})
        client = RelayHTTPClient(opener=opener)
        client.probe("https://relay.example.test", "/api/status")
        headers = self._headers_of(opener.requests[0])
        self.assertEqual(browser_identity.browser_user_agent(), headers["User-agent"])


class DataPlaneUserAgentTests(unittest.TestCase):
    """The data plane forwards a client UA and never our own."""

    def setUp(self) -> None:
        from young_router import responses_request

        self.responses_request = responses_request

    def _forwarded(self, original: dict) -> dict:
        """Run the two User-Agent steps in hook order, like the hook chain."""

        modified = self.responses_request._with_incoming_user_agent_header(original)
        current = modified if modified is not None else original
        owned = self.responses_request._with_owned_user_agent_header(current)
        return owned if owned is not None else current

    def test_client_user_agent_is_forwarded_byte_for_byte(self) -> None:
        modified = self._forwarded(
            {
                "api_base": "https://relay.example.test/v1",
                "proxy_server_request": {"headers": {"user-agent": "codex-local/1.2.3"}},
                "extra_headers": {"X-Trace": "1"},
            }
        )
        self.assertEqual("codex-local/1.2.3", modified["extra_headers"]["User-Agent"])
        self.assertEqual("1", modified["extra_headers"]["X-Trace"])

    def test_our_own_user_agent_never_reaches_the_upstream(self) -> None:
        for value in (
            "Young Router/1 CFNetwork/3860.600.21 Darwin/25.5.0",
            "Young%20Router/1 CFNetwork/3860.600.21 Darwin/25.5.0",
            "Young-Router-Core/1",
        ):
            with self.subTest(user_agent=value):
                modified = self._forwarded(
                    {
                        "api_base": "https://relay.example.test/v1",
                        "proxy_server_request": {"headers": {"User-Agent": value}},
                    }
                )
                self.assertEqual(
                    browser_identity.browser_user_agent(),
                    modified["extra_headers"]["User-Agent"],
                )

    def test_a_request_without_a_client_user_agent_presents_the_browser_identity(self) -> None:
        modified = self._forwarded({"api_base": "https://relay.example.test/v1"})
        self.assertEqual(
            browser_identity.browser_user_agent(),
            modified["extra_headers"]["User-Agent"],
        )

    def test_hook_chain_runs_the_owned_user_agent_step(self) -> None:
        hook_source = _read(ROOT / "young_router" / "hook.py")
        self.assertIn(
            "            _responses_request_module._with_incoming_user_agent_header,\n"
            "            _responses_request_module._with_owned_user_agent_header,\n",
            hook_source,
        )


class PiWebAccessStagingTests(unittest.TestCase):
    @staticmethod
    def _staging_module():
        spec = importlib.util.spec_from_file_location(
            "update_pi_web_access", ROOT / "scripts" / "update_pi_web_access.py"
        )
        module = importlib.util.module_from_spec(spec)
        assert spec.loader is not None
        spec.loader.exec_module(module)
        return module

    def test_staged_package_loses_its_self_naming_user_agents(self) -> None:
        module = self._staging_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "dist").mkdir()
            (root / "dist" / "index.js").write_text(
                'const REST_HEADERS = { "User-Agent": "pi-web-access" };\n'
                'const USER_AGENT = "Mozilla/5.0 (compatible; pi-web-access/1.0; +https://example.test)";\n'
                'headers: { "user-agent": "pi-web-access" },\n'
                'other: { "User-Agent": "codex-local/1.2.3" },\n',
                encoding="utf-8",
            )
            rewritten = module._normalize_staged_user_agents(root)
            self.assertEqual(3, rewritten)
            staged = (root / "dist" / "index.js").read_text(encoding="utf-8")
            self.assertNotIn("pi-web-access\"", staged)
            self.assertNotIn("pi-web-access/1.0", staged)
            self.assertEqual(3, staged.count(module._browser_user_agent()))
            self.assertIn('"User-Agent": "codex-local/1.2.3"', staged)

    def test_staging_fails_when_upstream_drops_the_literals(self) -> None:
        module = self._staging_module()
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "index.js").write_text('const UA = "Mozilla/5.0";\n', encoding="utf-8")
            with self.assertRaisesRegex(module.UpdateError, "self-naming User-Agent literals"):
                module._normalize_staged_user_agents(root)


if __name__ == "__main__":
    unittest.main()
