from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core.domains.legacy import ProvidersModelsDomain
from litellm_menu.core.domains.relay_accounts import (
    DETECTION_TIMEOUT_SECONDS,
    RelayAccountsDomain,
    RelayAccountsError,
    RelayHTTPClient,
)
from litellm_menu.core.service import CoreError, CoreStore


class FakeRelayHTTPClient:
    def __init__(self, responses: dict[str, object], *, probes: dict[str, object] | None = None):
        self.responses = responses
        self.probe_responses = dict(probes or {})
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.probes: list[tuple[str, str]] = []

    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        self.requests.append((origin, path, dict(headers)))
        if path not in self.responses:
            raise AssertionError(f"unexpected relay path: {path}")
        return self.responses[path]

    def probe(self, origin: str, path: str) -> tuple[int, object | None]:
        self.probes.append((origin, path))
        result = self.probe_responses.get(path)
        if result is None:
            raise AssertionError(f"unexpected relay probe path: {path}")
        if isinstance(result, Exception):
            raise result
        if not isinstance(result, tuple) or len(result) != 2:
            raise AssertionError("invalid relay probe response")
        return result


class RelayAccountsDomainTests(unittest.TestCase):
    def test_type_detection_classifies_public_station_signatures_without_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {},
                probes={
                    "/api/status": (
                        200,
                        {
                            "success": True,
                            "data": {"private": "replace-private-value"},
                            "message": "ok",
                        },
                    )
                },
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            before_revision = domain.revision

            result = domain.dispatch("account.detect_type", {"origin": "https://relay.example.test"})

            self.assertEqual({"detected_type": "newapi", "confidence": "high"}, result)
            self.assertEqual(before_revision, domain.revision)
            self.assertEqual(
                [("https://relay.example.test", "/api/status")],
                fake.probes,
            )
            self.assertNotIn("replace-private-value", json.dumps(result))

    def test_type_detection_classifies_sub2api_from_unauthenticated_key_envelope(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {},
                probes={
                    "/api/status": (404, {"message": "not found"}),
                    "/api/v1/keys?page=1&page_size=1": (
                        401,
                        {"code": "UNAUTHORIZED", "message": "sign in"},
                    ),
                },
            )
            domain = RelayAccountsDomain(directory, http_client=fake)

            result = domain.dispatch("detect_type", {"origin": "https://relay.example.test"})

            self.assertEqual({"detected_type": "sub2api", "confidence": "high"}, result)
            self.assertEqual(
                [
                    ("https://relay.example.test", "/api/status"),
                    ("https://relay.example.test", "/api/v1/keys?page=1&page_size=1"),
                ],
                fake.probes,
            )

    def test_type_detection_is_secret_free_read_only_core_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {},
                probes={
                    "/api/status": (
                        200,
                        {
                            "success": True,
                            "data": {"cookie": "replace-cookie-value"},
                            "message": "ok",
                        },
                    )
                },
            )
            relay = RelayAccountsDomain(directory, http_client=fake)
            core = CoreStore(domains=[relay])

            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.detect_type",
                    "payload": {"origin": "https://relay.example.test"},
                }
            )

            snapshot = core.snapshot()
            self.assertEqual(
                {"detected_type": "newapi", "confidence": "high"},
                snapshot["action_summaries"]["relay_accounts"],
            )
            self.assertFalse(snapshot["drafts"]["relay_accounts"]["dirty"])
            self.assertNotIn("replace-cookie-value", json.dumps(snapshot))
            self.assertNotIn("relay.example.test", json.dumps(snapshot))

    def test_type_detection_rejects_invalid_or_secret_bearing_inputs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=FakeRelayHTTPClient({}))
            for payload in (
                {"origin": "https://relay.example.test?token=replace-token"},
                {"origin": "https://relay.example.test", "cookie": "replace-cookie"},
                {"origin": "https://relay.example.test", "label": "extra"},
            ):
                with self.subTest(payload=payload), self.assertRaises(RelayAccountsError):
                    domain.dispatch("account.detect_type", payload)

    def test_probe_uses_a_bounded_unauthenticated_same_origin_request(self) -> None:
        class Response:
            status = 200

            def __init__(self, url: str):
                self.url = url
                self.closed = False

            def geturl(self) -> str:
                return self.url

            def read(self, _limit: int) -> bytes:
                return b'{"success":true}'

            def close(self) -> None:
                self.closed = True

        class Opener:
            def __init__(self, response: Response):
                self.response = response
                self.request: object | None = None
                self.timeout: float | None = None

            def open(self, request: object, timeout: float) -> Response:
                self.request = request
                self.timeout = timeout
                return self.response

        response = Response("https://relay.example.test/api/status")
        opener = Opener(response)
        client = RelayHTTPClient(opener=opener)

        status, payload = client.probe("https://relay.example.test", "/api/status")

        self.assertEqual(200, status)
        self.assertEqual({"success": True}, payload)
        self.assertEqual(DETECTION_TIMEOUT_SECONDS, opener.timeout)
        request = opener.request
        self.assertIsNotNone(request)
        self.assertEqual("GET", request.get_method())  # type: ignore[union-attr]
        self.assertEqual("https://relay.example.test/api/status", request.full_url)  # type: ignore[union-attr]
        self.assertIsNone(request.get_header("Cookie"))  # type: ignore[union-attr]
        self.assertIsNone(request.get_header("Authorization"))  # type: ignore[union-attr]
        self.assertTrue(response.closed)

        redirected = Response("https://other.example.test/api/status")
        redirected_client = RelayHTTPClient(opener=Opener(redirected))
        self.assertEqual((0, None), redirected_client.probe("https://relay.example.test", "/api/status"))
        self.assertTrue(redirected.closed)

    def test_core_login_transaction_signs_in_without_loading_resources_or_staging_provider_models(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": ["model-a"]},
                "/api/token/?p=0&page_size=100": {
                    "success": True,
                    "data": {"items": [{"id": 7, "status": 1}]},
                },
                "/api/token/7/key": {"success": True, "data": {"key": "replace-relay-key"}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=fake)
            account = relay.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[relay, providers],
            )

            result = core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-dashboard-token",
            )

            self.assertEqual(
                {"revision": 1, "login_status": "signed_in", "username": "sample-user"},
                result,
            )
            snapshot = core.snapshot()
            relay_account = snapshot["domains"]["relay_accounts"]["accounts"][0]
            self.assertEqual("signed_in", relay_account["login_status"])
            self.assertEqual("sample-user", relay_account["username"])
            self.assertFalse(snapshot["drafts"]["providers_models"]["dirty"])
            self.assertEqual([], snapshot["providers_models"]["providers"])
            resources = relay_account["resources"]
            self.assertEqual([], resources)
            self.assertEqual("idle", relay_account["resource_status"])
            self.assertNotIn("replace-cookie", json.dumps(snapshot))
            self.assertNotIn("replace-dashboard-token", json.dumps(snapshot))
            self.assertNotIn("replace-relay-key", json.dumps(result))

            persisted_json = "\n".join(path.read_text() for path in root.rglob("*.json"))
            for secret in (
                "replace-cookie",
                "replace-dashboard-token",
                "replace-relay-key",
            ):
                self.assertNotIn(secret, persisted_json)

            refreshed = core.refresh_relay_resources(account["id"], revision=core.revision)
            self.assertEqual("ready", refreshed["resource_status"])
            refreshed_snapshot = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
            self.assertEqual(["model-a"], refreshed_snapshot["resources"][0]["models"])

    def test_core_login_transaction_records_available_resources_without_staging_provider_models(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": []},
                "/api/token/?p=0&page_size=100": {
                    "success": True,
                    "data": {"items": [{"id": 7, "status": 1}]},
                },
                "/api/token/7/key": {"success": True, "data": {"key": "replace-relay-key"}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=fake)
            account = relay.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[relay, providers],
            )
            result = core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-dashboard-token",
            )

            self.assertEqual("signed_in", result["login_status"])
            self.assertTrue(relay.secret_present("session", account["id"]))
            self.assertFalse(providers.snapshot()["providers"])

    def test_resource_refresh_is_explicit_and_revision_checked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root)
            account = relay.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
            )
            with self.assertRaises(CoreError) as raised:
                core.refresh_relay_resources(account["id"], revision=core.revision - 1)
            self.assertEqual("revision_conflict", raised.exception.code)

    def test_account_snapshot_and_persistence_never_expose_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "Relay One",
                    "origin": "https://relay.example.test",
                    "username": "draft-user",
                    "remember_password": True,
                },
            )["accounts"][0]
            logged_in = domain.accept_login_result(
                account["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-access-token",
            )

            self.assertEqual("signed_in", logged_in["login_status"])
            self.assertTrue(logged_in["remember_password"])
            snapshot_text = json.dumps(domain.snapshot())
            self.assertNotIn("replace-cookie", snapshot_text)
            self.assertNotIn("replace-access-token", snapshot_text)
            self.assertNotIn("replace-password", snapshot_text)
            self.assertNotIn("secrets", snapshot_text)

            storage = root / ".litellm-runtime" / "relay-accounts.json"
            self.assertEqual(0o600, os.stat(storage).st_mode & 0o777)
            reloaded = RelayAccountsDomain(root)
            reloaded_account = reloaded.snapshot()["accounts"][0]
            self.assertEqual("unknown", reloaded_account["login_status"])
            self.assertEqual("sample-user", reloaded_account["username"])
            self.assertTrue(reloaded_account["remember_password"])

    def test_account_delete_persists_secret_free_credential_cleanup_until_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "Relay One",
                    "origin": "https://relay.example.test",
                },
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-access-token",
            )

            deleted = domain.dispatch("account.delete", {"id": account["id"]})
            self.assertEqual([], deleted["accounts"])
            self.assertEqual(
                [
                    {
                        "account_id": account["id"],
                        "label": "Relay One",
                        "kind": "credentials",
                    }
                ],
                deleted["pending_credential_cleanups"],
            )
            persisted = (root / ".litellm-runtime" / "relay-accounts.json").read_text()
            self.assertNotIn("replace-cookie", persisted)
            self.assertNotIn("replace-access-token", persisted)

            reloaded = RelayAccountsDomain(root)
            self.assertEqual(deleted["pending_credential_cleanups"], reloaded.snapshot()["pending_credential_cleanups"])
            confirmed = reloaded.dispatch(
                "credential_cleanup_confirm",
                {"id": account["id"], "kind": "credentials"},
            )
            self.assertEqual([], confirmed["pending_credential_cleanups"])
            self.assertEqual([], RelayAccountsDomain(root).snapshot()["pending_credential_cleanups"])

    def test_disabling_password_remember_persists_secret_free_cleanup_until_confirmed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "sub2api",
                    "label": "Relay Password",
                    "origin": "https://relay.example.test",
                    "remember_password": True,
                },
            )["accounts"][0]

            disabled = domain.dispatch(
                "account.update",
                {"id": account["id"], "remember_password": False},
            )
            self.assertFalse(disabled["accounts"][0]["remember_password"])
            self.assertEqual(
                [
                    {
                        "account_id": account["id"],
                        "label": "Relay Password",
                        "kind": "password",
                    }
                ],
                disabled["pending_credential_cleanups"],
            )
            persisted = (root / ".litellm-runtime" / "relay-accounts.json").read_text()
            self.assertNotIn("replace-password", persisted)

            reloaded = RelayAccountsDomain(root)
            self.assertEqual(disabled["pending_credential_cleanups"], reloaded.snapshot()["pending_credential_cleanups"])
            confirmed = reloaded.dispatch(
                "credential_cleanup_confirm",
                {"id": account["id"], "kind": "password"},
            )
            self.assertEqual([], confirmed["pending_credential_cleanups"])
            self.assertEqual([], RelayAccountsDomain(root).snapshot()["pending_credential_cleanups"])

    def test_account_delete_supersedes_a_pending_password_cleanup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "Relay One",
                    "origin": "https://relay.example.test",
                    "remember_password": True,
                },
            )["accounts"][0]
            domain.dispatch("account.update", {"id": account["id"], "remember_password": False})

            deleted = domain.dispatch("account.delete", {"id": account["id"]})
            self.assertEqual(
                [
                    {
                        "account_id": account["id"],
                        "label": "Relay One",
                        "kind": "credentials",
                    }
                ],
                deleted["pending_credential_cleanups"],
            )

    def test_public_relay_origins_require_https_but_local_loopback_is_allowed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            for origin in ("http://relay.example.test", "http://192.0.2.8"):
                with self.subTest(origin=origin), self.assertRaisesRegex(RelayAccountsError, "HTTPS"):
                    domain.dispatch("add", {"type": "newapi", "label": "Relay", "origin": origin})
            for origin in ("http://localhost:3000", "http://127.0.0.1:3000", "http://[::1]:3000"):
                with self.subTest(origin=origin):
                    result = domain.dispatch("add", {"type": "newapi", "label": origin, "origin": origin})
                    self.assertEqual(origin, result["accounts"][-1]["origin"])

    def test_core_restores_a_native_session_without_importing_provider_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root)
            account = relay.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            # A fresh Core must not trust the persisted prior sign-in claim.
            relay.accept_login_result(
                account["id"],
                username="previous-user",
                cookie="session=previous-cookie",
                access_token="previous-access-token",
            )
            reloaded = RelayAccountsDomain(root)
            self.assertEqual("unknown", reloaded.snapshot()["accounts"][0]["login_status"])
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[reloaded, providers],
            )

            result = core.restore_relay_session(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                login_status="signed_in",
                username="sample-user",
                cookie="session=restored-cookie",
                access_token="restored-access-token",
            )

            core.refresh_relay_resources(account["id"], revision=core.revision)
            snapshot = core.snapshot()
            self.assertEqual({"revision": 1, "login_status": "signed_in", "username": "sample-user"}, result)
            self.assertEqual("signed_in", snapshot["domains"]["relay_accounts"]["accounts"][0]["login_status"])
            self.assertFalse(snapshot["drafts"]["providers_models"]["dirty"])
            self.assertEqual([], snapshot["providers_models"]["providers"])
            self.assertTrue(reloaded.secret_present("session", account["id"]))
            snapshot_text = json.dumps(snapshot)
            self.assertNotIn("restored-cookie", snapshot_text)
            self.assertNotIn("restored-access-token", snapshot_text)

    def test_core_records_expired_native_session_without_retaining_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root)
            account = relay.dispatch(
                "add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[relay, providers],
            )

            result = core.restore_relay_session(
                account_id=account["id"],
                account_type="sub2api",
                label="Relay",
                origin="https://relay.example.test",
                login_status="expired",
            )

            snapshot = core.snapshot()
            self.assertEqual("expired", result["login_status"])
            self.assertEqual("expired", snapshot["domains"]["relay_accounts"]["accounts"][0]["login_status"])
            self.assertFalse(relay.secret_present("session", account["id"]))
            self.assertFalse(snapshot["drafts"]["providers_models"]["dirty"])

    def test_ordinary_dispatch_rejects_credentials_and_core_has_no_password_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]

            for payload in (
                {"id": account_id, "password": "replace-password"},
                {"id": account_id, "nested": {"cookie": "replace-cookie"}},
                {"id": account_id, "access_token": "replace-token"},
            ):
                with self.assertRaisesRegex(RelayAccountsError, "trusted native"):
                    domain.dispatch("update", payload)

            with self.assertRaisesRegex(RelayAccountsError, "unavailable"):
                domain.stage_secret("password", account_id, "replace-password")
            self.assertFalse(hasattr(domain, "saved_password"))
            self.assertNotIn("replace-password", (Path(directory) / ".litellm-runtime" / "relay-accounts.json").read_text())

    def test_newapi_import_stages_provider_models_and_keeps_key_private(self) -> None:
        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a", "chat-b", "chat-a"]},
            "/api/token/?p=0&page_size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "status": 1, "key": "masked"}]},
            },
            "/api/token/7/key": {"success": True, "data": {"key": "replace-relay-key"}},
        }
        fake = FakeRelayHTTPClient(responses)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-dashboard-token",
            )
            providers = ProvidersModelsDomain(root / "config.yaml")

            result = domain.import_into(account_id, providers)

            self.assertTrue(result["imported"])
            self.assertEqual(2, result["model_count"])
            public = providers.snapshot()["providers"][0]
            self.assertTrue(public["api_key_configured"])
            self.assertEqual(["chat-a", "chat-b"], [model["model_name"] for model in public["models"]])
            self.assertNotIn("replace-relay-key", json.dumps(result))
            private = providers.export(include_sensitive=True)["providers"][0]
            self.assertEqual("sk-replace-relay-key", private["api_key"])
            self.assertEqual("openai/chat-a", private["models"][0]["litellm_model"])
            self.assertTrue(all(headers["Authorization"] == "Bearer replace-dashboard-token" for _, _, headers in fake.requests))
            self.assertTrue(all(headers["Cookie"] == "session=replace-cookie" for _, _, headers in fake.requests))

    def test_sub2api_import_flattens_visible_channel_models(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {"items": [{"id": 4, "status": "active", "key": "sk-replace-sub-key"}]},
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [
                        {
                            "name": "channel",
                            "platforms": [
                                {
                                    "platform": "openai",
                                    "supported_models": [{"name": "model-a"}, {"name": "model-b"}],
                                }
                            ],
                        }
                    ],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample@example.test",
                access_token="replace-access-token",
            )
            providers = ProvidersModelsDomain(root / "config.yaml")

            result = domain.import_into(account_id, providers)

            self.assertEqual(2, result["model_count"])
            self.assertEqual(
                ["model-a", "model-b"],
                [model["model_name"] for model in providers.snapshot()["providers"][0]["models"]],
            )
            self.assertTrue(all(headers == {"Authorization": "Bearer replace-access-token"} for _, _, headers in fake.requests))

    def test_sub2api_cookie_only_session_can_import_models(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {"items": [{"id": 4, "status": "active", "key": "sk-replace-sub-key"}]},
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [{"name": "channel", "platforms": [{"platform": "openai", "supported_models": [{"name": "model-a"}]}]}],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Cookie Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample@example.test",
                cookie="session=replace-cookie",
            )
            providers = ProvidersModelsDomain(root / "config.yaml")

            result = domain.import_into(account_id, providers)

            self.assertEqual(1, result["model_count"])
            self.assertTrue(all(headers == {"Cookie": "session=replace-cookie"} for _, _, headers in fake.requests))

    def test_sub2api_import_uses_the_selected_key_id_when_names_repeat(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "Default", "status": "active", "key": "sk-replace-old-key"},
                            {"id": 5, "name": "Default", "status": "active", "key": "sk-replace-selected-key"},
                        ]
                    },
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [{"name": "channel", "platforms": [{"platform": "openai", "supported_models": [{"name": "model-a"}]}]}],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample@example.test",
                access_token="replace-access-token",
            )
            resources = domain.refresh_resources(account_id)["resources"]
            providers = ProvidersModelsDomain(root / "config.yaml")

            domain.import_resources(account_id, ["sub2api-5"], providers)

            private = providers.export(include_sensitive=True)["providers"][0]
            self.assertEqual("sk-replace-selected-key", private["api_key"])
            self.assertEqual(["Default"], [item["name"] for item in private["api_keys"]])

    def test_selected_resources_are_staged_only_after_explicit_import(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": ["model-a", "model-b"]},
                "/api/token/?p=0&page_size=100": {
                    "success": True,
                    "data": {
                        "items": [
                            {"id": 7, "status": 1, "name": "Primary", "key": "masked-a"},
                            {"id": 8, "status": 1, "name": "Secondary", "key": "masked-b"},
                        ]
                    },
                },
                "/api/token/7/key": {"success": True, "data": {"key": "replace-primary-key"}},
                "/api/token/8/key": {"success": True, "data": {"key": "replace-secondary-key"}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=fake)
            account = relay.dispatch(
                "account.add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])

            core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
            )
            core.refresh_relay_resources(account["id"], revision=core.revision)
            snapshot = core.snapshot()
            resources = snapshot["domains"]["relay_accounts"]["accounts"][0]["resources"]
            self.assertEqual(["Primary", "Secondary"], [item["name"] for item in resources])
            self.assertNotIn("replace-primary-key", json.dumps(snapshot))
            self.assertNotIn("masked-a", json.dumps(snapshot))
            self.assertFalse(snapshot["drafts"]["providers_models"]["dirty"])

            imported = core.import_relay_resources(account["id"], [resources[1]["id"]], revision=core.revision)

            self.assertTrue(imported["imported"])
            provider = providers.export(include_sensitive=True)["providers"][0]
            self.assertEqual(["Secondary"], [item["name"] for item in provider["api_keys"]])
            self.assertEqual("sk-replace-secondary-key", provider["api_key"])
            self.assertEqual(["model-a", "model-b"], [item["model_name"] for item in provider["models"]])
            self.assertTrue(core.snapshot()["drafts"]["providers_models"]["dirty"])


if __name__ == "__main__":
    unittest.main()
