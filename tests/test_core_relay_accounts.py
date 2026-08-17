from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.domains.relay_accounts import (
    DETECTION_TIMEOUT_SECONDS,
    RelayAccountsDomain,
    RelayAccountsError,
    RelayHTTPClient,
)
from litellm_menu.core.service import CoreError, CoreStore


class FakeRelayHTTPClient:
    def __init__(
        self,
        responses: dict[str, object],
        *,
        probes: dict[str, object] | None = None,
        errors: dict[str, Exception] | None = None,
        password_login_result: dict[str, str] | Exception | None = None,
    ):
        self.responses = responses
        self.probe_responses = dict(probes or {})
        self.errors = dict(errors or {})
        self.password_login_result = password_login_result
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.post_requests: list[tuple[str, str, dict[str, str]]] = []
        self.probes: list[tuple[str, str]] = []
        self.password_logins: list[tuple[str, str, str, str]] = []

    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        self.requests.append((origin, path, dict(headers)))
        if path in self.errors:
            raise self.errors[path]
        # Group selection is optional metadata on older relay deployments.
        # Existing resource fixtures intentionally omit it.
        if path == "/api/user/self/groups":
            return {"data": {}}
        if path == "/api/v1/groups/available":
            return {"data": []}
        if path == "/api/v1/groups/rates":
            return {"data": {}}
        if path not in self.responses:
            raise AssertionError(f"unexpected relay path: {path}")
        return self.responses[path]

    def post(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        self.post_requests.append((origin, path, dict(headers)))
        return self.json(origin, path, headers=headers)

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

    def password_login(self, origin: str, account_type: str, username: str, password: str) -> dict[str, str]:
        self.password_logins.append((origin, account_type, username, password))
        if isinstance(self.password_login_result, Exception):
            raise self.password_login_result
        if self.password_login_result is None:
            raise AssertionError("unexpected relay password login")
        return dict(self.password_login_result)


class RelayAccountsDomainTests(unittest.TestCase):
    @staticmethod
    def _remembered_relay_package() -> dict[str, object]:
        return {
            "domain": "relay_accounts",
            "storage": {
                "version": 3,
                "stations": [
                    {
                        "id": "station-transfer",
                        "name": "Transfer Station",
                        "origin": "https://relay.example.test",
                        "type": "sub2api",
                    }
                ],
                "accounts": [
                    {
                        "id": "account-transfer",
                        "station_id": "station-transfer",
                        "type": "sub2api",
                        "label": "Transfer Account",
                        "origin": "https://relay.example.test",
                        "username": "person@example.test",
                        "login_status": "signed_in",
                        "remember_password": True,
                        "password": "replace-package-password",
                        "session": {
                            "cookie": "session=replace-package-cookie",
                            "access_token": "replace-package-token",
                            "refresh_token": "",
                        },
                        "balance": None,
                        "last_updated_at": "",
                        "resource_status": "idle",
                        "resource_error": "none",
                        "resources": [],
                        "groups": [],
                    }
                ],
                "pending_credential_cleanups": [],
            },
        }

    def test_trusted_package_export_stages_private_relay_state_until_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            relay = RelayAccountsDomain(directory)
            storage = relay.storage_path
            before = storage.read_bytes()
            core = CoreStore(domains=[relay])

            imported = core.import_package(
                package={
                    "format": "litellm-menu-core-package",
                    "version": 1,
                    "sections": {"relay_accounts": self._remembered_relay_package()},
                },
                sections=["relay_accounts"],
                revision=core.revision,
            )

            self.assertEqual(["relay_accounts"], imported["draft_domains"])
            self.assertEqual(before, storage.read_bytes())
            safe_snapshot = json.dumps(core.snapshot())
            self.assertNotIn("replace-package-password", safe_snapshot)
            self.assertNotIn("replace-package-cookie", safe_snapshot)
            trusted = relay.export(include_sensitive=True)
            self.assertIn("replace-package-password", json.dumps(trusted))

            core.apply("relay_accounts", revision=core.revision)
            persisted = storage.read_text(encoding="utf-8")
            self.assertIn("replace-package-password", persisted)
            self.assertIn("replace-package-cookie", persisted)
            self.assertFalse(core.snapshot()["drafts"]["relay_accounts"]["dirty"])

    def test_failed_multisection_import_restores_relay_transaction_without_writing(self) -> None:
        class FailingImportDomain:
            name = "runtime"

            def __init__(self) -> None:
                self.revision = 0

            def draft_state(self) -> object:
                return {"value": "saved"}

            def snapshot(self) -> dict[str, object]:
                return {"domain": self.name, "revision": self.revision}

            def dispatch(self, _action: str, _payload: object = None) -> dict[str, object]:
                return self.snapshot()

            def import_package(self, _payload: object) -> None:
                raise ValueError("synthetic failure")

        with tempfile.TemporaryDirectory() as directory:
            relay = RelayAccountsDomain(directory)
            before_file = relay.storage_path.read_bytes()
            before_checkpoint = relay.transaction_checkpoint()
            core = CoreStore(domains=[relay, FailingImportDomain()])

            with self.assertRaises(CoreError):
                core.import_package(
                    package={
                        "format": "litellm-menu-core-package",
                        "version": 1,
                        "sections": {
                            "relay_accounts": self._remembered_relay_package(),
                            "runtime": {"value": "fails"},
                        },
                    },
                    sections=["relay_accounts", "runtime"],
                    revision=core.revision,
                )

            self.assertEqual(before_file, relay.storage_path.read_bytes())
            self.assertEqual(before_checkpoint, relay.transaction_checkpoint())

    def test_newapi_balance_uses_the_station_quota_unit(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["gpt-test"]},
                    "/api/token/?p=1&size=100": {
                        "data": {"items": [{"id": 1, "name": "default", "status": 1, "key": "masked"}]}
                    },
                    "/api/user/self": {"data": {"quota": 1_250_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "New API",
                    "origin": "https://relay.example.test",
                },
            )["accounts"][0]
            domain.accept_login_result(
                account["id"], username="person", access_token="replace-token"
            )

            refreshed = domain.refresh_resources(account["id"])

            self.assertEqual(2.5, refreshed["balance"])

    def test_sub2api_balance_comes_from_the_user_profile(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/v1/user/profile": {"data": {"balance": 8.75}},
                    "/api/v1/keys?page=1&page_size=100": {
                        "data": {"items": [{"id": "key-1", "name": "default", "status": "active", "key": "sk-test"}]}
                    },
                    "/api/v1/channels/available": {
                        "data": [{"platforms": [{"supported_models": ["model-test"]}]}]
                    },
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "sub2api",
                    "label": "Sub2API",
                    "origin": "https://relay.example.test",
                },
            )["accounts"][0]
            domain.accept_login_result(
                account["id"], username="person@example.test", access_token="replace-token"
            )

            refreshed = domain.refresh_resources(account["id"])

            self.assertEqual(8.75, refreshed["balance"])

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

    def test_newapi_key_request_uses_authenticated_post(self) -> None:
        class Response:
            status = 200

            def getcode(self) -> int:
                return self.status

            def read(self, _limit: int) -> bytes:
                return b'{"success":true,"data":{"key":"replace-key"}}'

            def __enter__(self) -> "Response":
                return self

            def __exit__(self, *_args: object) -> None:
                return None

        class Opener:
            def __init__(self) -> None:
                self.request: object | None = None

            def open(self, request: object, timeout: float) -> Response:
                del timeout
                self.request = request
                return Response()

        opener = Opener()
        client = RelayHTTPClient(opener=opener)

        self.assertEqual(
            {"success": True, "data": {"key": "replace-key"}},
            client.post(
                "https://relay.example.test",
                "/api/token/7/key",
                headers={"Authorization": "Bearer replace-dashboard-token"},
            ),
        )
        request = opener.request
        self.assertIsNotNone(request)
        self.assertEqual("POST", request.get_method())  # type: ignore[union-attr]
        self.assertEqual("https://relay.example.test/api/token/7/key", request.full_url)  # type: ignore[union-attr]
        self.assertEqual("Bearer replace-dashboard-token", request.get_header("Authorization"))  # type: ignore[union-attr]

    def test_core_login_transaction_signs_in_without_loading_resources_or_staging_provider_models(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": ["model-a"]},
                "/api/token/?p=1&size=100": {
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
                "/api/token/?p=1&size=100": {
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

    def test_account_snapshot_redacts_remembered_credentials_while_private_file_retains_them(self) -> None:
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
                password="replace-password",
            )
            domain.apply()

            self.assertEqual("signed_in", logged_in["login_status"])
            self.assertTrue(logged_in["remember_password"])
            snapshot_text = json.dumps(domain.snapshot())
            self.assertNotIn("replace-cookie", snapshot_text)
            self.assertNotIn("replace-access-token", snapshot_text)
            self.assertNotIn("replace-password", snapshot_text)
            self.assertNotIn("secrets", snapshot_text)

            storage = root / ".litellm-runtime" / "relay-accounts.json"
            self.assertEqual(0o600, os.stat(storage).st_mode & 0o777)
            private_text = storage.read_text(encoding="utf-8")
            self.assertIn("replace-cookie", private_text)
            self.assertIn("replace-access-token", private_text)
            self.assertIn("replace-password", private_text)
            reloaded = RelayAccountsDomain(root)
            reloaded_account = reloaded.snapshot()["accounts"][0]
            self.assertEqual("unknown", reloaded_account["login_status"])
            self.assertEqual("sample-user", reloaded_account["username"])
            self.assertTrue(reloaded_account["remember_password"])
            self.assertTrue(reloaded_account["password_saved"])

    def test_remembered_session_restores_without_opening_the_browser(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root)
            account = domain.dispatch(
                "account.add",
                {
                    "type": "sub2api",
                    "label": "Relay Session",
                    "origin": "https://relay.example.test",
                    "remember_password": True,
                },
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="person@example.test",
                cookie="session=remembered-cookie",
                access_token="remembered-token",
            )
            domain.apply()

            fake = FakeRelayHTTPClient({"/api/v1/auth/me": {"data": {"email": "person@example.test"}}})
            restored_domain = RelayAccountsDomain(root, http_client=fake)
            restored = restored_domain.restore_saved_session(account["id"])

            self.assertIsNotNone(restored)
            self.assertEqual("signed_in", restored["login_status"])
            self.assertTrue(restored_domain.secret_present("session", account["id"]))
            self.assertEqual(
                [
                    (
                        "https://relay.example.test",
                        "/api/v1/auth/me",
                        {
                            "Cookie": "session=remembered-cookie",
                            "Authorization": "Bearer remembered-token",
                        },
                    )
                ],
                fake.requests,
            )

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
            domain.apply()

            before_delete = (root / ".litellm-runtime" / "relay-accounts.json").read_bytes()
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
            self.assertEqual(before_delete, (root / ".litellm-runtime" / "relay-accounts.json").read_bytes())
            domain.apply()
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

    def test_disabling_password_remember_clears_the_private_file_on_apply(self) -> None:
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
            domain.accept_login_result(
                account["id"],
                username="person@example.test",
                access_token="replace-access-token",
                password="replace-password",
            )
            domain.apply()

            disabled = domain.dispatch(
                "account.update",
                {"id": account["id"], "remember_password": False},
            )
            self.assertFalse(disabled["accounts"][0]["remember_password"])
            self.assertFalse(disabled["accounts"][0]["password_saved"])
            self.assertEqual([], disabled["pending_credential_cleanups"])
            persisted = (root / ".litellm-runtime" / "relay-accounts.json").read_text()
            self.assertIn("replace-password", persisted)
            self.assertIn("replace-access-token", persisted)
            domain.apply()
            persisted = (root / ".litellm-runtime" / "relay-accounts.json").read_text()
            self.assertNotIn("replace-password", persisted)
            self.assertNotIn("replace-access-token", persisted)
            self.assertEqual([], RelayAccountsDomain(root).snapshot()["pending_credential_cleanups"])

    def test_remembered_password_is_private_and_restores_without_a_browser_window(self) -> None:
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
            accepted = domain.accept_login_result(
                account["id"],
                username="person@example.test",
                access_token="replace-access-token",
                password="replace-password",
            )
            domain.apply()
            self.assertTrue(accepted["password_saved"])
            self.assertNotIn("password", accepted)

            storage = root / ".litellm-runtime" / "relay-accounts.json"
            self.assertEqual(0o600, storage.stat().st_mode & 0o777)
            self.assertIn("replace-password", storage.read_text(encoding="utf-8"))

            fake = FakeRelayHTTPClient(
                {},
                password_login_result={
                    "username": "person@example.test",
                    "cookie": "",
                    "access_token": "replace-new-token",
                    "refresh_token": "replace-refresh-token",
                },
            )
            restored_domain = RelayAccountsDomain(root, http_client=fake)
            restored = restored_domain.restore_saved_password(account["id"])
            self.assertEqual("signed_in", restored["login_status"])
            self.assertTrue(restored["password_saved"])
            self.assertEqual(
                [("https://relay.example.test", "sub2api", "person@example.test", "replace-password")],
                fake.password_logins,
            )

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

    def test_relay_origin_add_accepts_host_without_scheme_or_api_base_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory)
            for origin in ("relay.example.test", "relay.example.test/v1", "https://relay.example.test/"):
                with self.subTest(origin=origin):
                    result = domain.dispatch(
                        "account.add",
                        {"type": "newapi", "label": origin, "origin": origin},
                    )
                    self.assertEqual("https://relay.example.test", result["accounts"][-1]["origin"])

    def test_core_restores_a_native_session_without_importing_provider_models(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["model-a"]},
                    "/api/token/?p=1&size=100": {
                        "data": {"items": [{"id": 7, "name": "known-key", "status": 1, "key": "replace-key"}]}
                    },
                    "/api/user/self": {"data": {"quota": 1_000_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                }
            )
            relay = RelayAccountsDomain(root, http_client=fake)
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
            relay.refresh_resources(account["id"])
            relay.apply()
            reloaded = RelayAccountsDomain(root, http_client=fake)
            self.assertEqual("unknown", reloaded.snapshot()["accounts"][0]["login_status"])
            self.assertEqual(["newapi-7"], [item["id"] for item in reloaded.snapshot()["accounts"][0]["resources"]])
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

            restored_snapshot = core.snapshot()
            self.assertEqual(
                ["newapi-7"],
                [item["id"] for item in restored_snapshot["domains"]["relay_accounts"]["accounts"][0]["resources"]],
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

    def test_core_restores_a_remembered_cookie_when_native_memory_is_empty(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root)
            account = relay.dispatch(
                "add",
                {
                    "type": "sub2api",
                    "label": "Relay",
                    "origin": "https://relay.example.test",
                    "remember_password": True,
                },
            )["accounts"][0]
            relay.accept_login_result(
                account["id"],
                username="person@example.test",
                cookie="session=remembered-cookie",
                password="replace-password",
            )
            relay.apply()
            fake = FakeRelayHTTPClient({"/api/v1/auth/me": {"data": {"email": "person@example.test"}}})
            reloaded = RelayAccountsDomain(root, http_client=fake)
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[reloaded, ProvidersModelsDomain(root / "config.yaml")],
            )
            public_account = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
            self.assertIs(public_account["remember_password"], True)
            self.assertIs(public_account["password_saved"], True)

            result = core.restore_relay_session(
                account_id=account["id"],
                account_type="sub2api",
                label="Relay",
                origin="https://relay.example.test",
                login_status="signed_out",
            )

            self.assertEqual("signed_in", result["login_status"])
            self.assertEqual([], fake.password_logins)
            self.assertEqual(
                [
                    (
                        "https://relay.example.test",
                        "/api/v1/auth/me",
                        {"Cookie": "session=remembered-cookie"},
                    )
                ],
                fake.requests,
            )

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
            "/api/token/?p=1&size=100": {
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

            resources = domain.refresh_resources(account_id)["resources"]
            refreshed_account = domain.snapshot()["accounts"][0]
            self.assertEqual("ready", refreshed_account["resource_status"])
            self.assertEqual("none", refreshed_account["resource_error"])
            result = domain.import_resources(account_id, [resource["id"] for resource in resources], providers, mode="independent")

            self.assertTrue(result["imported"])
            self.assertEqual(2, result["model_count"])
            public = providers.snapshot()["providers"][0]
            self.assertTrue(public["api_key_configured"])
            self.assertEqual(["chat-a", "chat-b"], [model["model_name"] for model in public["models"]])
            self.assertNotIn("replace-relay-key", json.dumps(result))
            private = providers.export(include_sensitive=True)["providers"][0]
            self.assertEqual("sk-replace-relay-key", private["api_key"])
            self.assertEqual("openai/chat-a", private["models"][0]["litellm_model"])
            self.assertEqual(["/api/token/7/key"], [path for _, path, _ in fake.post_requests])
            self.assertTrue(all(headers["Authorization"] == "Bearer replace-dashboard-token" for _, _, headers in fake.requests))
            self.assertTrue(all(headers["Cookie"] == "session=replace-cookie" for _, _, headers in fake.requests))

    def test_resource_refresh_exposes_actionable_failure_reason(self) -> None:
        cases = (
            (
                "no_api_keys",
                {
                    "/api/user/models": {"success": True, "data": ["model-a"]},
                    "/api/token/?p=1&size=100": {"success": True, "data": {"items": []}},
                },
                {},
            ),
            (
                "no_models",
                {
                    "/api/user/models": {"success": True, "data": []},
                    "/api/token/?p=1&size=100": {"success": True, "data": {"items": [{"id": 7, "status": 1}]}},
                },
                {},
            ),
            (
                "login_expired",
                {},
                {"/api/user/models": RelayAccountsError("Relay login has expired")},
            ),
        )
        for resource_error, responses, errors in cases:
            with self.subTest(resource_error=resource_error), tempfile.TemporaryDirectory() as directory:
                fake = FakeRelayHTTPClient(responses, errors=errors)
                domain = RelayAccountsDomain(directory, http_client=fake)
                account_id = domain.dispatch(
                    "add",
                    {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
                )["accounts"][0]["id"]
                domain.accept_login_result(account_id, username="sample-user", cookie="session=replace-cookie")

                result = domain.refresh_resources(account_id)

                self.assertEqual("unavailable", result["resource_status"])
                self.assertEqual(resource_error, result["resource_error"])
                self.assertEqual("expired" if resource_error == "login_expired" else "signed_in", result["login_status"])
                if resource_error == "no_models":
                    self.assertEqual(["newapi-7"], [item["id"] for item in result["resources"]])
                else:
                    self.assertEqual([], result["resources"])
                self.assertEqual(resource_error != "login_expired", domain.secret_present("session", account_id))

    def test_failed_refresh_preserves_last_known_resources(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": ["model-a"]},
                "/api/token/?p=1&size=100": {
                    "success": True,
                    "data": {"items": [{"id": 7, "name": "known-key", "status": 1, "key": "replace-key"}]},
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample-user", cookie="session=replace-cookie")
            first = domain.refresh_resources(account_id)
            self.assertEqual(["newapi-7"], [item["id"] for item in first["resources"]])

            fake.errors["/api/user/models"] = RelayAccountsError("Relay is unavailable")
            second = domain.refresh_resources(account_id)

            self.assertEqual("unavailable", second["resource_status"])
            self.assertEqual("unavailable", second["resource_error"])
            self.assertEqual(["newapi-7"], [item["id"] for item in second["resources"]])

    def test_staging_a_new_browser_session_clears_old_resource_selection(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": ["model-a"]},
                "/api/token/?p=1&size=100": {"success": True, "data": {"items": [{"id": 7, "status": 1}]}},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="first-user", cookie="session=first-cookie")
            domain.refresh_resources(account_id)
            self.assertEqual("ready", domain.snapshot()["accounts"][0]["resource_status"])

            domain.stage_secret("session", account_id, json.dumps({"username": "second-user", "cookie": "session=second-cookie"}))

            account = domain.snapshot()["accounts"][0]
            self.assertEqual("second-user", account["username"])
            self.assertEqual("signed_in", account["login_status"])
            self.assertEqual("idle", account["resource_status"])
            self.assertEqual("none", account["resource_error"])
            self.assertEqual([], account["resources"])

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

            resources = domain.refresh_resources(account_id)["resources"]
            result = domain.import_resources(
                account_id,
                [resource["id"] for resource in resources],
                providers,
                mode="independent",
            )

            self.assertEqual(2, result["model_count"])
            self.assertEqual(
                ["model-a", "model-b"],
                [model["model_name"] for model in providers.snapshot()["providers"][0]["models"]],
            )
            self.assertTrue(all(headers == {"Authorization": "Bearer replace-access-token"} for _, _, headers in fake.requests))

    def test_sub2api_import_discovers_models_from_each_gateway_key_when_channels_are_unavailable(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "OpenAI", "status": "active", "key": "sk-replace-openai-key"},
                            {"id": 5, "name": "Other", "status": "active", "key": "sk-replace-other-key"},
                        ]
                    },
                },
                "/v1/models": {
                    "object": "list",
                    "data": [{"id": "gateway-model"}],
                },
            },
            errors={"/api/v1/channels/available": RelayAccountsError("Relay login has expired")},
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
                access_token="replace-dashboard-token",
            )
            providers = ProvidersModelsDomain(root / "config.yaml")

            resources = domain.refresh_resources(account_id)["resources"]
            result = domain.import_resources(account_id, [resource["id"] for resource in resources], providers, mode="independent")

            self.assertEqual(2, result["model_count"])
            self.assertEqual(
                ["gateway-model", "gateway-model"],
                [model["model_name"] for model in providers.snapshot()["providers"][0]["models"]],
            )
            gateway_requests = [
                headers for _, path, headers in fake.requests if path == "/v1/models"
            ]
            self.assertEqual(
                [
                    {"Authorization": "Bearer sk-replace-openai-key"},
                    {"Authorization": "Bearer sk-replace-other-key"},
                ],
                gateway_requests,
            )
            dashboard_requests = [
                headers for _, path, headers in fake.requests if path.startswith("/api/v1/")
            ]
            self.assertTrue(all(headers == {"Authorization": "Bearer replace-dashboard-token"} for headers in dashboard_requests))

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

            resources = domain.refresh_resources(account_id)["resources"]
            result = domain.import_resources(
                account_id,
                [resource["id"] for resource in resources],
                providers,
                mode="independent",
            )

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

            domain.import_resources(account_id, ["sub2api-5"], providers, mode="independent")

            private = providers.export(include_sensitive=True)["providers"][0]
            self.assertEqual("sk-replace-selected-key", private["api_key"])
            self.assertEqual(["Default"], [item["name"] for item in private["api_keys"]])

    def test_selected_resources_are_staged_only_after_explicit_import(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/user/models": {"success": True, "data": ["model-a", "model-b"]},
                "/api/token/?p=1&size=100": {
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
            self.assertEqual("", provider["api_key"])
            self.assertEqual("relay", provider["api_keys"][0]["source"]["kind"])
            self.assertEqual(account["station_id"], provider["api_keys"][0]["source"]["station_id"])
            self.assertEqual(resources[1]["id"], provider["api_keys"][0]["source"]["resource_id"])
            self.assertEqual(["model-a", "model-b"], [item["model_name"] for item in provider["models"]])
            self.assertTrue(core.snapshot()["drafts"]["providers_models"]["dirty"])


if __name__ == "__main__":
    unittest.main()
