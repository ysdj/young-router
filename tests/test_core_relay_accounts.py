from __future__ import annotations

import json
import os
from pathlib import Path
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from young_router.core.domains import relay_accounts
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import (
    DETECTION_TIMEOUT_SECONDS,
    RelayAccountsDomain,
    RelayAccountsError,
    RelayHTTPClient,
    RelayTransportError,
)
from young_router.core.protocol import validate_method_result
from young_router.core.service import CoreError, CoreStore, RevisionConflict


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
        # Existing resource fixtures intentionally omit it; the ones that align
        # auto-grouping pass their catalog as a normal response instead.
        if path == "/api/user/self/groups" and path not in self.responses:
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
                    "format": "young-router-core-package",
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
                        "format": "young-router-core-package",
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

    def test_a_newapi_key_reports_its_own_groups_models(self) -> None:
        """The bare catalog is a union; a key learns its own group's list.

        New API builds ``/api/user/models`` by collecting every usable group's
        models into one answer, so handing that list to each key made every key
        report the whole station: opening any row in 分组管理 showed the same
        models, and a linked route could offer one the key's group never serves.
        The endpoint takes ``?group=``, which answers that group alone.
        """

        class PerGroupRelay(FakeRelayHTTPClient):
            union = ["claude-opus-5", "gpt-5.5", "gpt-image-2.5"]
            by_group = {
                "claude-kiro": ["claude-opus-5"],
                "gpt-plus": ["gpt-5.5"],
                "gpt-image-2.5": ["gpt-image-2.5"],
            }

            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                self.requests.append((origin, path, dict(headers)))
                if path.startswith("/api/user/models?group="):
                    group = path.split("group=", 1)[1]
                    return {"data": list(self.by_group.get(group, []))}
                if path == "/api/user/models":
                    return {"data": list(self.union)}
                if path == "/api/user/self/groups":
                    return {"data": {group: {"ratio": 1} for group in self.by_group}}
                if path not in self.responses:
                    raise AssertionError(f"unexpected relay path: {path}")
                return self.responses[path]

        with tempfile.TemporaryDirectory() as directory:
            fake = PerGroupRelay(
                {
                    "/api/token/?p=1&size=100": {
                        "data": {
                            "items": [
                                {"id": 1, "name": "claude", "status": 1, "key": "masked-1", "group": "claude-kiro"},
                                {"id": 2, "name": "plus", "status": 1, "key": "masked-2", "group": "gpt-plus"},
                                {"id": 3, "name": "image", "status": 1, "key": "masked-3", "group": "gpt-image-2.5"},
                            ]
                        }
                    },
                    "/api/user/self": {"data": {"quota": 1_000_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "New API", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", access_token="replace-token")

            resources = domain.refresh_resources(account["id"])["resources"]
            by_name = {resource["name"]: resource["models"] for resource in resources}
            self.assertEqual(["claude-opus-5"], by_name["claude"])
            self.assertEqual(["gpt-5.5"], by_name["plus"])
            self.assertEqual(["gpt-image-2.5"], by_name["image"])
            # One read per distinct group, never one per key.
            scoped = [path for _origin, path, _headers in fake.requests if "group=" in path]
            self.assertEqual(3, len(scoped))

    def test_a_newapi_fork_without_a_group_parameter_keeps_the_union_catalog(self) -> None:
        """A station that refuses ``?group=`` must not lose its catalog."""

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["model-a", "model-b"]},
                    "/api/token/?p=1&size=100": {
                        "data": {"items": [{"id": 1, "name": "default", "status": 1, "key": "masked", "group": "default"}]}
                    },
                    "/api/user/self": {"data": {"quota": 1_000_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "New API", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", access_token="replace-token")

            resources = domain.refresh_resources(account["id"])["resources"]
            # The fixture raises on the unexpected scoped path, so this proves
            # the failure was absorbed and the union list survived.
            self.assertEqual(["model-a", "model-b"], resources[0]["models"])

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

    def test_relay_key_read_keeps_the_rest_of_the_station_key_list(self) -> None:
        """One station answer caches every key, so the next key is a cache hit.

        The group manager reveals one key at a time through Core's plaintext
        lease and a sub2api key list arrives whole.  A Core that just started
        holds the account's resources but none of their keys, so the first read
        fetches the list and every other key of that account is served from
        what it kept.
        """

        key_path = "/api/v1/keys?page=1&page_size=100"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeRelayHTTPClient(
                {
                    "/api/v1/user/profile": {"data": {"balance": 8.75}},
                    key_path: {
                        "data": {
                            "items": [
                                {"id": "11", "name": "alpha", "status": "active", "key": "sk-replace-alpha"},
                                {"id": "12", "name": "beta", "status": "active", "key": "sk-replace-beta"},
                            ]
                        }
                    },
                    "/api/v1/channels/available": {
                        "data": [{"platforms": [{"supported_models": ["model-test"]}]}]
                    },
                }
            )
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Sub2API", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="person@example.test",
                access_token="replace-token",
                remember_password=True,
            )
            resources = domain.refresh_resources(account_id)["resources"]
            domain.apply()
            self.assertEqual(["sub2api-11", "sub2api-12"], [resource["id"] for resource in resources])

            # A reloaded Core has the resources but not their keys: the first
            # read fetches the list once, the second key is answered from it.
            reloaded = RelayAccountsDomain(root, http_client=fake)
            list_reads = lambda: [path for _, path, _ in fake.requests if path == key_path]
            before = len(list_reads())
            self.assertEqual(
                "sk-replace-alpha",
                reloaded.trusted_secret_value("api_key", f"{account_id}:sub2api-11"),
            )
            self.assertEqual(before + 1, len(list_reads()))
            self.assertEqual(
                "sk-replace-beta",
                reloaded.trusted_secret_value("api_key", f"{account_id}:sub2api-12"),
            )
            self.assertEqual(before + 1, len(list_reads()))

    def test_a_label_stored_without_a_key_id_is_only_repaired_when_it_is_unique(self) -> None:
        """Two same-named station keys never answer for one another.

        A resource whose stored id came from a label (an older answer without key
        ids) is repaired by that label while it is unique.  Duplicate labels are
        legal on the station, and reading "the first one" would materialize
        another key's credential onto this slot.
        """

        key_path = "/api/v1/keys?page=1&page_size=100"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeRelayHTTPClient(
                {
                    "/api/v1/user/profile": {"data": {"balance": 8.75}},
                    key_path: {
                        "data": {
                            "items": [
                                {"id": "11", "name": "alpha", "status": "active", "key": "sk-replace-alpha-one"},
                                {"id": "12", "name": "alpha", "status": "active", "key": "sk-replace-alpha-two"},
                            ]
                        }
                    },
                    "/api/v1/channels/available": {
                        "data": [{"platforms": [{"supported_models": ["model-test"]}]}]
                    },
                }
            )
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Sub2API", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="person@example.test",
                access_token="replace-token",
                remember_password=True,
            )
            resources = domain.refresh_resources(account_id)["resources"]
            self.assertEqual(["sub2api-11", "sub2api-12"], [resource["id"] for resource in resources])
            domain.apply()

            # An older document named this resource after the station label.
            state_path = domain.storage_path
            state = json.loads(state_path.read_text(encoding="utf-8"))
            account = next(item for item in state["accounts"] if item["id"] == account_id)
            account["resources"][0]["id"] = "sub2api-alpha"
            account["resources"][0]["name"] = "alpha"
            state_path.write_text(json.dumps(state), encoding="utf-8")

            reloaded = RelayAccountsDomain(root, http_client=fake)
            # The resource is known; its key is not, which is a key problem and
            # not a resource problem — and never one of the two "alpha" keys.
            with self.assertRaisesRegex(RelayAccountsError, "API key is unavailable"):
                reloaded.trusted_secret_value("api_key", f"{account_id}:sub2api-alpha")

            # A key the station identifies by its own id still reads normally.
            self.assertEqual(
                "sk-replace-alpha-two",
                reloaded.trusted_secret_value("api_key", f"{account_id}:sub2api-12"),
            )

    def test_a_station_that_names_keys_by_key_id_still_reads_the_stored_slot(self) -> None:
        """The stored resource id is derived the same way on both sides.

        The resource list derives an id from ``id`` or ``key_id``, while the
        read used to compare only ``id``: a station that names its keys with
        ``key_id`` produced ids this lookup could never match.
        """

        key_path = "/api/v1/keys?page=1&page_size=100"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeRelayHTTPClient(
                {
                    "/api/v1/user/profile": {"data": {"balance": 8.75}},
                    key_path: {
                        "data": {
                            "items": [
                                {"key_id": "21", "name": "alpha", "status": "active", "key": "sk-replace-key-id"},
                                {"key_id": "22", "name": "beta", "status": "active", "key": "sk-replace-other"},
                            ]
                        }
                    },
                    "/api/v1/channels/available": {
                        "data": [{"platforms": [{"supported_models": ["model-test"]}]}]
                    },
                }
            )
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Sub2API", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="person@example.test",
                access_token="replace-token",
                remember_password=True,
            )
            resources = domain.refresh_resources(account_id)["resources"]
            self.assertEqual(["sub2api-21", "sub2api-22"], [resource["id"] for resource in resources])

            # The slot's own key answers, and it is not the other key's value.
            self.assertEqual(
                "sk-replace-key-id",
                domain.trusted_secret_value("api_key", f"{account_id}:sub2api-21"),
            )

    def test_a_lost_key_is_refused_instead_of_adopting_the_only_remaining_one(self) -> None:
        """A station with one unrelated key does not answer for a lost slot.

        The last-resort fallback returned the only key in the list whenever the
        stored id and label matched nothing, materializing a credential onto a
        slot that never held it.
        """

        key_path = "/api/v1/keys?page=1&page_size=100"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeRelayHTTPClient(
                {
                    "/api/v1/user/profile": {"data": {"balance": 8.75}},
                    key_path: {
                        "data": {
                            "items": [
                                {"id": "31", "name": "replacement", "status": "active", "key": "sk-replace-unrelated"},
                            ]
                        }
                    },
                    "/api/v1/channels/available": {
                        "data": [{"platforms": [{"supported_models": ["model-test"]}]}]
                    },
                }
            )
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Sub2API", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="person@example.test",
                access_token="replace-token",
                remember_password=True,
            )
            domain.refresh_resources(account_id)
            domain.apply()

            # A slot the station no longer carries, with one unrelated key left.
            state_path = domain.storage_path
            state = json.loads(state_path.read_text(encoding="utf-8"))
            account = next(item for item in state["accounts"] if item["id"] == account_id)
            account["resources"] = [{"id": "sub2api-99", "name": "deleted-key", "group_id": "", "enabled": True}]
            state_path.write_text(json.dumps(state), encoding="utf-8")

            reloaded = RelayAccountsDomain(root, http_client=fake)
            with self.assertRaisesRegex(RelayAccountsError, "API key is unavailable"):
                reloaded.trusted_secret_value("api_key", f"{account_id}:sub2api-99")

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

    def test_core_relay_resource_dispatch_reports_its_status_in_the_action_summary(self) -> None:
        """Refresh and import answer in the dispatch envelope, not their projection.

        The dispatch result is a revision plus an optional action summary.
        Returning the relay projection itself (account_id/resource_status/...)
        adds fields the IPC contract rejects, so every caller of
        `resources.refresh` sees a protocol error instead of a resource status.
        """

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
            core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-dashboard-token",
            )

            refreshed = core.dispatch(
                {"domain": "relay_accounts", "type": "resources.refresh", "payload": {"account_id": account["id"]}},
                expected_revision=core.revision,
            )
            validate_method_result("dispatch", refreshed)
            self.assertEqual(
                {"account_id", "resource_status", "resource_count"},
                set(refreshed["action_summary"]),
            )
            self.assertEqual("ready", refreshed["action_summary"]["resource_status"])
            self.assertEqual(core.revision, refreshed["revision"])

            resources = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]["resources"]
            imported = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "resources.import",
                    "payload": {"account_id": account["id"], "resource_ids": [resources[0]["id"]]},
                },
                expected_revision=core.revision,
            )
            validate_method_result("dispatch", imported)
            self.assertEqual(
                {"imported", "import_mode", "resource_count", "model_count"},
                set(imported["action_summary"]),
            )
            self.assertTrue(imported["action_summary"]["imported"])

    def test_core_relay_resource_transients_check_the_callers_revision(self) -> None:
        """A stale refresh or import conflicts instead of being accepted.

        Both actions run outside the ordinary dispatch path, and hard-coding
        the store's own revision skipped the guard the contract advertises:
        a window holding an old revision could still stage an import against
        a draft it never saw.
        """

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
            core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-dashboard-token",
            )

            core.dispatch(
                {"domain": "relay_accounts", "type": "resources.refresh", "payload": {"account_id": account["id"]}},
                expected_revision=core.revision,
            )
            stale = core.revision - 1
            resources = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]["resources"]

            with self.assertRaises(RevisionConflict):
                core.dispatch(
                    {"domain": "relay_accounts", "type": "resources.refresh", "payload": {"account_id": account["id"]}},
                    expected_revision=stale,
                )
            with self.assertRaises(RevisionConflict):
                core.dispatch(
                    {
                        "domain": "relay_accounts",
                        "type": "resources.import",
                        "payload": {"account_id": account["id"], "resource_ids": [resources[0]["id"]]},
                    },
                    expected_revision=stale,
                )
            # The current revision still passes both, so the guard is a
            # comparison and not a blanket refusal.
            core.dispatch(
                {"domain": "relay_accounts", "type": "resources.refresh", "payload": {"account_id": account["id"]}},
                expected_revision=core.revision,
            )
            imported = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "resources.import",
                    "payload": {"account_id": account["id"], "resource_ids": [resources[0]["id"]]},
                },
                expected_revision=core.revision,
            )
            self.assertTrue(imported["action_summary"]["imported"])

    def test_a_completed_login_reaches_disk_without_an_apply(self) -> None:
        """A sign-in is a fact, not a draft: it survives a Core restart.

        The host calls Core only after the sign-in page is done and the
        post-login question has been answered, so nothing about a completed
        login is left for the user to confirm.  Treating the account shell as a
        staged draft made every later ``_persist`` return early, so the account
        and its session lived only in memory — a station that had just been
        signed in could not be found on disk at all, and a restart lost it.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient({}))
            core = CoreStore(domains=[relay])
            account_id = "login-nimbus-0123456789abcdef0123"

            core.accept_relay_login(
                account_id=account_id,
                account_type="newapi",
                label="nimbus",
                origin="https://relay.example.test",
                username="person@example.test",
                access_token="replace-token",
                password="replace-password",
                user_id="2411",
                station_name="nimbus",
                station_type="newapi",
                station_origin="https://relay.example.test",
                remember_password=True,
                pending_account=True,
            )

            storage = root / ".litellm-runtime" / "relay-accounts.json"
            persisted = json.loads(storage.read_text(encoding="utf-8"))
            self.assertEqual([account_id], [item["id"] for item in persisted["accounts"]])
            self.assertEqual("2411", persisted["accounts"][0]["user_id"])
            self.assertTrue(persisted["accounts"][0]["session"]["access_token"])

            # The proof: a fresh Core reads the account back.
            reloaded = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient({}))
            self.assertEqual(
                ["nimbus"],
                [account["label"] for account in reloaded.snapshot()["accounts"]],
            )

    def test_a_users_own_relay_edit_still_waits_for_apply(self) -> None:
        """The completed-login write must not let an ordinary draft through.

        A login persists because it is finished work; the panes' own relay CRUD
        is the draft that waits for Apply.  Those are different writers, and this
        pins that the second one still holds its edit back — a draft reaching
        disk on a login would be the far worse bug.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient({}))
            relay.dispatch(
                "account.add",
                {"type": "newapi", "label": "manual", "origin": "https://relay.example.test"},
            )
            storage = root / ".litellm-runtime" / "relay-accounts.json"
            self.assertNotEqual({}, relay.draft_state())
            self.assertEqual([], json.loads(storage.read_text(encoding="utf-8"))["accounts"])

            reloaded = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient({}))
            self.assertEqual([], reloaded.snapshot()["accounts"])

    def test_core_pending_login_creates_account_without_reserving_cancelled_slot(self) -> None:
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
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[relay, providers],
            )

            # A cancelled pending flow never reaches Core, so no slot exists.
            self.assertEqual([], relay.snapshot()["accounts"])

            result = core.accept_relay_login(
                account_id="pending0123456789abcdef0123456789ab",
                account_type="newapi",
                label="https://relay.example.test",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-dashboard-token",
                station_name="Relay",
                station_type="newapi",
                remember_password=True,
                pending_account=True,
            )

            self.assertEqual("signed_in", result["login_status"])
            self.assertEqual("sample-user", result["username"])
            snapshot = core.snapshot()
            accounts = snapshot["domains"]["relay_accounts"]["accounts"]
            self.assertEqual(1, len(accounts))
            account = accounts[0]
            self.assertEqual("pending0123456789abcdef0123456789ab", account["id"])
            self.assertEqual("signed_in", account["login_status"])
            self.assertEqual("sample-user", account["username"])
            self.assertTrue(account["remember_password"])
            stations = snapshot["domains"]["relay_accounts"]["stations"]
            self.assertEqual(1, len(stations))
            self.assertEqual(stations[0]["id"], account["station_id"])
            self.assertTrue(relay.secret_present("session", account["id"]))
            # The login is a completed fact: the account, its station, and the
            # session the post-login answer chose to remember are on disk, so a
            # Core restart keeps a station the user just signed in to.
            persisted = json.loads(
                (root / ".litellm-runtime" / "relay-accounts.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                ["pending0123456789abcdef0123456789ab"],
                [item["id"] for item in persisted["accounts"]],
            )
            self.assertEqual(
                "session=replace-cookie",
                persisted["accounts"][0]["session"]["cookie"],
            )
            # A station API key is never written: it stays in Core's
            # process-local cache and crosses the boundary only through the
            # one-shot plaintext lease.
            persisted_json = "\n".join(path.read_text() for path in root.rglob("*.json"))
            self.assertNotIn("replace-relay-key", persisted_json)

    def test_core_login_accept_without_pending_flag_still_rejects_unknown_account(self) -> None:
        fake = FakeRelayHTTPClient({})
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            relay = RelayAccountsDomain(root, http_client=fake)
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[relay, providers],
            )
            with self.assertRaises(CoreError):
                core.accept_relay_login(
                    account_id="unknown0123456789abcdef0123456789ab",
                    account_type="newapi",
                    label="https://relay.example.test",
                    origin="https://relay.example.test",
                    username="sample-user",
                    cookie="session=replace-cookie",
                    pending_account=False,
                )
            self.assertEqual([], core.snapshot()["domains"]["relay_accounts"]["accounts"])

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

    def test_accept_login_result_applies_the_post_login_remember_choice(self) -> None:
        """The host prompt's tri-state decision flows into the stored flags."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root)
            remembered = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "Relay One",
                    "origin": "https://relay.example.test",
                    "remember_password": True,
                },
            )["accounts"][0]
            domain.accept_login_result(
                remembered["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                access_token="replace-access-token",
                password="replace-password",
            )

            # "仅记住登录态": retire the saved password and persisted session.
            session_only = domain.accept_login_result(
                remembered["id"],
                username="sample-user",
                cookie="session=replaced-session-cookie",
                remember_password=False,
            )
            self.assertEqual("signed_in", session_only["login_status"])
            self.assertFalse(session_only["remember_password"])
            self.assertFalse(session_only["password_saved"])

            # "记住密码": adopt saving later, on an account that never saved.
            sessionless = domain.dispatch(
                "account.add",
                {
                    "type": "newapi",
                    "label": "Relay Two",
                    "origin": "https://relay.example.test",
                },
            )["accounts"][0]
            domain.accept_login_result(
                sessionless["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                remember_password=True,
            )
            updated = domain.accept_login_result(
                sessionless["id"],
                username="sample-user",
                cookie="session=replace-cookie",
                password="replace-password",
            )
            self.assertTrue(updated["remember_password"])
            self.assertTrue(updated["password_saved"])

            # Absent decision: an existing preference stands.
            untouched = domain.accept_login_result(
                sessionless["id"],
                username="sample-user",
                cookie="session=replace-cookie",
            )
            self.assertTrue(untouched["remember_password"])
            self.assertTrue(untouched["password_saved"])

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

    def test_removing_a_draft_only_account_cancels_out_to_a_clean_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(Path(directory))
            core = CoreStore(domains=[domain])

            def relay_dirty() -> bool:
                return core.snapshot()["drafts"]["relay_accounts"]["dirty"]

            self.assertFalse(relay_dirty())
            account = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {"type": "newapi", "label": "Relay Draft", "origin": "https://relay.example.test"},
                }
            )
            self.assertTrue(relay_dirty())
            account_id = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]["id"]
            core.dispatch({"domain": "relay_accounts", "type": "account.delete", "payload": {"id": account_id}})
            # The add was undone before Apply, so the draft must be clean again:
            # closing the relay window must not ask to discard changes that
            # never effectively existed.
            self.assertFalse(relay_dirty())
            snapshot = core.snapshot()["domains"]["relay_accounts"]
            self.assertEqual([], snapshot["accounts"])
            self.assertEqual([], snapshot["stations"])

            # A persisted account is still a real deletion and must stage.
            applied = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {"type": "newapi", "label": "Relay Kept", "origin": "https://relay.example.test"},
                }
            )
            kept_id = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]["id"]
            core.apply("relay_accounts", revision=core.revision)
            self.assertFalse(relay_dirty())
            core.dispatch({"domain": "relay_accounts", "type": "account.delete", "payload": {"id": kept_id}})
            self.assertTrue(relay_dirty())
            self.assertEqual([], domain.snapshot()["accounts"])

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

    def test_a_proven_session_is_not_signed_out_by_the_hosts_own_probe(self) -> None:
        """A routine session check must not rewrite a verified login.

        The account pane asks the host to restore a saved session on every
        mount, and the host's restore answers from the same session store Core
        already holds.  When that probe failed (a native round trip that timed
        out, a station that answered a public endpoint differently) the account
        was recorded as signed out — even though Core still held the verified
        session, and the row then reported 未登录 until the next authenticated
        read silently repaired it.

        The two observations are kept apart now: a probe cannot sign out a
        session Core can still use, while a station that actually rejected its
        own session is still recorded, and an account with no session at all is
        still signed out by the probe.
        """

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "status": 1, "key": "masked"}]},
            },
        }
        fake = FakeRelayHTTPClient(responses)
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
            core.accept_relay_login(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="sample-user",
                cookie="session=replace-cookie",
            )
            core.refresh_relay_resources(account["id"], revision=core.revision)

            # The host's probe answers signed_out while Core still holds the
            # verified session.  That is an observation about the probe, not
            # about the login, so the row stays signed in.
            result = core.restore_relay_session(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                login_status="signed_out",
            )

            self.assertEqual("signed_in", result["login_status"])
            account_snapshot = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
            self.assertEqual("signed_in", account_snapshot["login_status"])
            self.assertTrue(relay.secret_present("session", account["id"]))
            # The probe states nothing about the station's keys either.
            self.assertEqual(["newapi-7"], [item["id"] for item in account_snapshot["resources"]])
            self.assertEqual("ready", account_snapshot["resource_status"])

            # A station that actually rejected its own session is still the
            # freshest fact about it, and retires the stored session.
            result = core.restore_relay_session(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                login_status="expired",
            )
            self.assertEqual("expired", result["login_status"])
            self.assertFalse(relay.secret_present("session", account["id"]))

            # Nothing is held for an account that reaches Core without a
            # session: a signed-out probe still reads as signed out.
            bare = relay.dispatch(
                "add",
                {"type": "newapi", "label": "Bare", "origin": "https://bare.example.test"},
            )["accounts"][-1]
            result = core.restore_relay_session(
                account_id=bare["id"],
                account_type="newapi",
                label="Bare",
                origin="https://bare.example.test",
                login_status="signed_out",
            )
            self.assertEqual("signed_out", result["login_status"])

    def test_a_fresh_login_observation_answers_the_panes_own_recheck(self) -> None:
        """One station round trip answers every check inside the window.

        The account pane re-checks its station session on every mount, so
        without a window each entry re-probed the site.  Core keeps the
        observation instead: a session it still holds and observed inside the
        window answers the next check, and a session it cannot vouch for (a
        restart, an expired one, or an account with no session) always gets a
        real probe.  The window is a runtime threshold, and 0 turns the
        short-circuit off.
        """

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "status": 1, "key": "masked"}]},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            domain = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient(responses))
            account_id = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]

            # Nothing observed yet: a check is not reused.
            self.assertFalse(domain.session_check_is_recent(account_id))
            # The pane reads the window from the relay projection, so both sides
            # apply one number.
            self.assertIsInstance(domain.snapshot()["session_recheck_seconds"], float)

            domain.accept_login_result(account_id, username="sample-user", cookie="session=replace-cookie")

            self.assertTrue(domain.session_check_is_recent(account_id))
            account = domain.snapshot()["accounts"][0]
            # The pane ages the observation itself, so it can decide before it
            # asks the host for anything.
            self.assertIsInstance(account["login_observed_seconds_ago"], float)
            self.assertLess(account["login_observed_seconds_ago"], 5.0)

            # A recorded rejection is a fresh observation too, and it retires
            # the session the window was describing.
            domain.set_login_status(account_id, "expired")
            self.assertFalse(domain.session_check_is_recent(account_id))

            # A window of 0 re-checks on every mount by definition.
            domain.accept_login_result(account_id, username="sample-user", cookie="session=replace-cookie")
            settings.write_text(
                "YOUNG_ROUTER_RELAY_SESSION_RECHECK_SECONDS=0\n",
                encoding="utf-8",
            )
            with mock.patch("young_router.core.domains._shared._default_runtime_settings_path", return_value=settings):
                self.assertFalse(domain.session_check_is_recent(account_id))
                settings.write_text(
                    "YOUNG_ROUTER_RELAY_SESSION_RECHECK_SECONDS=600\n",
                    encoding="utf-8",
                )
                self.assertTrue(domain.session_check_is_recent(account_id))

            # A stored observation older than the window is probed again.
            domain.apply()
            stored = root / ".litellm-runtime" / "relay-accounts.json"
            aged = json.loads(stored.read_text(encoding="utf-8"))
            aged["accounts"][0]["login_observed_at"] = "2000-01-01T00:00:00Z"
            stored.write_text(json.dumps(aged), encoding="utf-8")
            domain.reload()
            with mock.patch("young_router.core.domains._shared._default_runtime_settings_path", return_value=settings):
                self.assertFalse(domain.session_check_is_recent(account_id))

    def test_a_session_window_does_not_survive_a_core_restart(self) -> None:
        """A fresh Core never inherits the window: it holds no session then.

        The re-check window is about a login *this* Core verified.  A restart
        re-reads the document, which drops the persisted ``signed_in`` claim to
        ``unknown``, so the first mount after a restart always probes the
        station for real instead of answering from a stamp on disk.
        """

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "status": 1, "key": "masked"}]},
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            original = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient(responses))
            account_id = original.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            original.accept_login_result(
                account_id,
                username="sample-user",
                cookie="session=replace-cookie",
                remember_password=True,
            )
            self.assertTrue(original.session_check_is_recent(account_id))
            original.apply()

            reloaded = RelayAccountsDomain(root, http_client=FakeRelayHTTPClient(responses))
            account = reloaded.snapshot()["accounts"][0]
            self.assertEqual("unknown", account["login_status"])
            self.assertIsNone(account["login_observed_seconds_ago"])
            self.assertFalse(reloaded.session_check_is_recent(account_id))

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

    def test_core_keeps_cached_relay_keys_when_a_session_check_fails(self) -> None:
        """A session check never hides the station's cached API keys.

        The observation it carries is reconciled against the session Core
        holds (see ``settle_login_status``), so the key facts below hold for
        both of that reconciliation's outcomes: whichever login state the
        account ends up with, the verified key list and its ready resource
        state are untouched, and local keys keep loading.
        """

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "status": 1, "key": "masked"}]},
            },
        }
        fake = FakeRelayHTTPClient(responses)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample-user", cookie="session=replace-cookie")
            resources = domain.refresh_resources(account_id)["resources"]
            self.assertTrue(resources)

            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[domain, providers],
            )

            result = core.restore_relay_session(
                account_id=account_id,
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                login_status="signed_out",
            )

            self.assertIn(result["login_status"], {"signed_in", "signed_out"})
            # The probe's own observation, whatever it settled on, is the only
            # thing it may change: it never downgrades or hides the verified key
            # list, so local keys keep loading.
            self.assertTrue(domain.secret_present("session", account_id))
            account = core.snapshot()["domains"]["relay_accounts"]["accounts"][0]
            self.assertEqual(
                [resource["id"] for resource in resources],
                [item["id"] for item in account["resources"]],
            )
            self.assertEqual("ready", account["resource_status"])
            self.assertEqual("none", account["resource_error"])

    def test_a_session_check_without_a_locally_held_session_still_signs_out(self) -> None:
        """A probe is authority over an account Core has no session for.

        Reconciliation holds a verified session against the host's own probe;
        an account Core cannot authenticate with is exactly the case the
        signed-out observation is for, and it must still be recorded.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RelayAccountsDomain(root)
            account = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(
                metadata_path=root / ".litellm-runtime" / "core-state.json",
                domains=[domain, providers],
            )

            result = core.restore_relay_session(
                account_id=account["id"],
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                login_status="signed_out",
            )

            self.assertEqual("signed_out", result["login_status"])
            self.assertEqual(
                "signed_out",
                core.snapshot()["domains"]["relay_accounts"]["accounts"][0]["login_status"],
            )
            self.assertEqual("none", account["resource_error"])

    def test_local_relay_keys_stay_loadable_and_refreshable_while_signed_out(self) -> None:
        """A remembered session is a local credential, not a login gate.

        The account reads signed out after a failed session check, yet its
        cached keys stay listed and readable, and the station can still be
        refreshed without a fresh browser sign-in.
        """

        responses = {
            "/api/user/models": {"success": True, "data": ["chat-a"]},
            "/api/token/?p=1&size=100": {
                "success": True,
                "data": {"items": [{"id": 7, "status": 1, "key": "masked"}]},
            },
            "/api/token/7/key": {"success": True, "data": {"key": "replace-relay-key"}},
        }
        fake = FakeRelayHTTPClient(responses)
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(Path(directory), http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample-user",
                cookie="session=replace-cookie",
                remember_password=True,
            )
            resources = domain.refresh_resources(account_id)["resources"]
            self.assertTrue(resources)
            resource_id = resources[0]["id"]

            domain.set_login_status(account_id, "signed_out")
            account = domain.snapshot()["accounts"][0]
            self.assertEqual("signed_out", account["login_status"])
            self.assertEqual([resource_id], [item["id"] for item in account["resources"]])
            self.assertEqual("ready", account["resource_status"])
            # The native plaintext lease reads the locally known key without a
            # fresh sign-in, and a refresh may still read the station.
            self.assertEqual(
                "sk-replace-relay-key",
                domain.trusted_secret_value("api_key", f"{account_id}:{resource_id}"),
            )
            self.assertEqual("ready", domain.refresh_resources(account_id)["resource_status"])

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

    def test_an_expired_remembered_session_is_renewed_from_the_saved_password(self) -> None:
        """An aged-out station session is re-authenticated, not read as a dead key.

        A sub2api session lives about two days while the app can stay open for
        longer.  Every authenticated read — the group manager's key list, and
        the Apply that materializes a linked route — shares that session, so a
        rejected read used to end as `login_expired` and the Apply then reported
        the linked key as unavailable, discarding the one credential that could
        mint a new session.  The remembered password is that credential.
        """

        class ExpiringSessionClient(FakeRelayHTTPClient):
            def __init__(self) -> None:
                super().__init__(
                    {
                        "/api/v1/keys?page=1&page_size=100": {
                            "code": 0,
                            "data": {"items": [{"id": 4, "name": "Plus", "status": "active", "key": "sk-replace-plus-key", "group_id": 21}]},
                        },
                        "/api/v1/channels/available": {"code": 0, "data": []},
                        "/api/v1/user/profile": {"code": 0, "data": {"balance": 1.0}},
                        "/v1/models": {"object": "list", "data": [{"id": "model-a"}]},
                    },
                    password_login_result={
                        "username": "person@example.test",
                        "cookie": "",
                        "access_token": "replace-renewed-token",
                        "refresh_token": "replace-renewed-refresh",
                    },
                )
                self.expired = False

            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                # Only the authenticated dashboard read fails, and a fresh
                # sign-in is exactly what a station accepts again.
                if self.expired and path == "/api/v1/keys?page=1&page_size=100":
                    self.requests.append((origin, path, dict(headers)))
                    raise RelayAccountsError("Relay login has expired")
                return super().json(origin, path, headers=headers)

            def password_login(self, origin: str, account_type: str, username: str, password: str) -> dict[str, str]:
                result = super().password_login(origin, account_type, username, password)
                self.expired = False
                return result

        with tempfile.TemporaryDirectory() as directory:
            fake = ExpiringSessionClient()
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test", "remember_password": True},
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="person@example.test",
                access_token="replace-stale-token",
                refresh_token="replace-stale-refresh",
                password="replace-password",
            )
            domain.apply()

            # The stored session ages out on the station side.
            fake.expired = True
            result = domain.refresh_resources(account["id"])

            self.assertEqual("ready", result["resource_status"])
            self.assertEqual("none", result["resource_error"])
            self.assertEqual("signed_in", result["login_status"])
            self.assertEqual(["sub2api-4"], [resource["id"] for resource in result["resources"]])
            # The saved password is what minted the new session, on the same
            # site and for the same account — never a guessed replacement.
            self.assertEqual(
                [("https://sub.example.test", "sub2api", "person@example.test", "replace-password")],
                fake.password_logins,
            )
            # The renewed session replaced the stale one on disk.
            persisted = json.loads(
                (Path(directory) / ".litellm-runtime" / "relay-accounts.json").read_text(encoding="utf-8")
            )
            self.assertEqual("replace-renewed-token", persisted["accounts"][0]["session"]["access_token"])
            self.assertNotIn("replace-stale-token", json.dumps(persisted))

    def test_a_renewal_is_attempted_once_and_skipped_without_a_saved_password(self) -> None:
        """A station that rejects the renewed session is the account's real answer.

        The renewal is bounded to one attempt per read, and an account that
        never saved its password has nothing to renew from: the expired
        observation stands and no login is attempted.
        """

        class AlwaysExpiredClient(FakeRelayHTTPClient):
            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                if path == "/api/v1/keys?page=1&page_size=100":
                    self.requests.append((origin, path, dict(headers)))
                    raise RelayAccountsError("Relay login has expired")
                return super().json(origin, path, headers=headers)

        with tempfile.TemporaryDirectory() as directory:
            fake = AlwaysExpiredClient({}, password_login_result=RelayAccountsError("Relay username or password is invalid"))
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test", "remember_password": True},
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="person@example.test",
                access_token="replace-stale-token",
                password="replace-password",
            )
            domain.apply()

            result = domain.refresh_resources(account["id"])

            self.assertEqual("login_expired", result["resource_error"])
            self.assertEqual(1, len(fake.password_logins))
            # A renewal the station refused is the account's real answer: the
            # read is not sent again, so there is no renewal loop.
            self.assertEqual(
                1,
                len([path for _, path, _ in fake.requests if path == "/api/v1/keys?page=1&page_size=100"]),
            )

        with tempfile.TemporaryDirectory() as directory:
            fake = AlwaysExpiredClient({})
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test", "remember_password": False},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person@example.test", access_token="replace-stale-token")

            result = domain.refresh_resources(account["id"])

            self.assertEqual("login_expired", result["resource_error"])
            self.assertEqual([], fake.password_logins)

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
            # Dashboard reads keep the session token; only the key's own catalog
            # probe authenticates with that key.
            self.assertTrue(all(headers == {"Authorization": "Bearer replace-access-token"} for _, path, headers in fake.requests if path != "/v1/models"))
            self.assertEqual(
                [{"Authorization": "Bearer sk-replace-sub-key"}],
                [headers for _, path, headers in fake.requests if path == "/v1/models"],
            )

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

    def test_sub2api_gateway_model_fallback_skips_disabled_keys(self) -> None:
        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "Active", "status": "active", "key": "sk-replace-active-key"},
                            {"id": 5, "name": "Disabled", "status": "disabled", "key": "sk-replace-disabled-key"},
                        ]
                    },
                },
                "/v1/models": {
                    "object": "list",
                    "data": [{"id": "gateway-model"}],
                },
            },
            errors={"/api/v1/channels/available": RelayAccountsError("Channel catalog unavailable")},
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample@example.test",
                access_token="replace-dashboard-token",
            )

            resources = domain.refresh_resources(account_id)["resources"]

            self.assertEqual(2, len(resources))
            self.assertEqual(
                [{"Authorization": "Bearer sk-replace-active-key"}],
                [headers for _, path, headers in fake.requests if path == "/v1/models"],
            )

    def test_sub2api_linked_material_joins_the_keys_own_catalog_over_a_narrower_group_page(self) -> None:
        """A group page must never veto a model the key's own catalog serves.

        The channel page lists what one channel declares for a group, while the
        key's own ``/v1/models`` list answers for what that key can call.  A
        linked route is materialized against both: a page naming six Claude
        models answered a key whose catalog named a seventh, and the route could
        never apply while the page alone decided it.
        """

        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "Plus", "status": "active", "key": "sk-replace-plus-key", "group_id": 21},
                        ]
                    },
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [
                        {
                            "name": "plus",
                            "platforms": [
                                {
                                    "platform": "anthropic",
                                    "groups": [{"id": 21, "name": "Plus分组"}],
                                    "supported_models": [{"name": "model-a"}],
                                }
                            ],
                        }
                    ],
                },
                "/v1/models": {
                    "object": "list",
                    "data": [{"id": "model-a"}, {"id": "model-b"}],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample@example.test", cookie="session=replace-cookie")

            resources = domain.refresh_resources(account_id)["resources"]

            # The group manager keeps the page's list: an ordinary read never
            # fans out one gateway read per key.
            self.assertEqual([["model-a"]], [resource["models"] for resource in resources])
            self.assertEqual([], [path for _, path, _ in fake.requests if path == "/v1/models"])

            account = domain.snapshot()["accounts"][0]
            materials = domain.binding_materials(
                [
                    {
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resources[0]["id"],
                    }
                ]
            )

            self.assertEqual([], materials["issues"])
            self.assertEqual(["model-a", "model-b"], materials["resources"][0]["models"])
            self.assertEqual(["model-a", "model-b"], materials["resources"][0]["source_models"])
            # The gateway read is authenticated by the key it is about.
            self.assertEqual(
                [{"Authorization": "Bearer sk-replace-plus-key"}],
                [headers for _, path, headers in fake.requests if path == "/v1/models"],
            )
            # What the key taught Apply stays inside Apply: the stored catalog
            # the pane renders is unchanged.
            self.assertEqual(
                [["model-a"]],
                [resource["models"] for resource in domain.snapshot()["accounts"][0]["resources"]],
            )

    def test_a_cached_key_still_materializes_when_the_station_session_expires(self) -> None:
        """An apply-time refresh must not throw away the key it already holds.

        Model management is decoupled from relay sign-in: the only thing a
        binding needs from the relay is the key value, and a key this Core
        already read is cached data.  An apply-time refresh clears that cache
        before reading the station, so a session that ages out used to leave the
        index empty and report the key as unavailable — while the same key's own
        gateway catalog (authenticated by the key, not the session) was still
        answering.  The read is now an improvement, not a precondition.
        """

        class ExpiringStation:
            def __init__(self) -> None:
                self.alive = True
                self.calls: list[str] = []

            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                del origin
                self.calls.append(path)
                if path == "/v1/models" and headers.get("Authorization", "").endswith("sk-replace-plus-key"):
                    return {"object": "list", "data": [{"id": "model-a"}, {"id": "model-b"}]}
                if not self.alive:
                    raise RelayAccountsError("Relay login has expired")
                if path == "/api/v1/keys?page=1&page_size=100":
                    return {
                        "code": 0,
                        "data": {"items": [{"id": 4, "name": "Plus", "status": "active", "key": "sk-replace-plus-key", "group_id": 21}]},
                    }
                if path == "/api/v1/channels/available":
                    return {"code": 0, "data": []}
                if path == "/api/v1/user/profile":
                    return {"code": 0, "data": {"balance": 1.0}}
                if path == "/api/v1/groups/available":
                    return {"code": 0, "data": [{"id": 21, "name": "Plus"}]}
                if path == "/api/v1/groups/rates":
                    return {"code": 0, "data": {"21": 1.25}}
                raise AssertionError(f"unexpected GET {path}")

            def post(self, *_: object, **__: object) -> object:
                raise AssertionError("unexpected POST")

            def password_login(self, *_: object, **__: object) -> dict[str, str]:
                raise RelayAccountsError("Relay username or password is invalid")

        with tempfile.TemporaryDirectory() as directory:
            fake = ExpiringStation()
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample@example.test", access_token="replace-token")

            # The ordinary read is what caches the key value.
            resources = domain.refresh_resources(account_id)["resources"]
            self.assertEqual("sk-replace-plus-key", domain.trusted_secret_value("api_key", f"{account_id}:{resources[0]['id']}"))
            source = {
                "station_id": domain.snapshot()["accounts"][0]["station_id"],
                "account_id": account_id,
                "resource_id": resources[0]["id"],
            }

            # The session then expires while the relay itself stays reachable.
            fake.alive = False
            fake.calls.clear()
            materials = domain.binding_materials([source], refresh=True)

            self.assertEqual([], materials["issues"])
            self.assertEqual(1, len(materials["resources"]))
            self.assertEqual("sk-replace-plus-key", materials["resources"][0]["api_key"])
            # The key's own catalog still answered, and the station's dashboard
            # endpoints were only asked, never made into a precondition.
            self.assertEqual(["model-a", "model-b"], materials["resources"][0]["models"])

    def test_a_persisted_slot_value_resolves_a_key_on_a_fresh_core(self) -> None:
        """The durable half of "a key this Core already holds".

        ``_resource_secret_cache`` is process-local, so a Core that just started
        has none of it — while the credential itself is safely on the provider
        key slot, where the last successful materialization wrote it.  A station
        that cannot be read must not turn such a key into an unavailable one,
        and it must not cost the resource its own catalog either: the linked
        model is judged against the models the resource already reports.
        """

        class DeadStation:
            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                del origin, path, headers
                raise RelayAccountsError("Relay login has expired")

            def post(self, *_: object, **__: object) -> object:
                raise RelayAccountsError("Relay login has expired")

            def password_login(self, *_: object, **__: object) -> dict[str, str]:
                raise RelayAccountsError("Relay username or password is invalid")

        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=DeadStation())
            account = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]
            domain._accounts[0]["resources"] = [
                {
                    "id": "sub2api-4",
                    "name": "Plus",
                    "enabled": True,
                    "api_base": "https://sub.example.test/v1",
                    "models": ["model-a", "model-b"],
                }
            ]
            self.assertEqual({}, domain._resource_secret_cache)

            materials = domain.binding_materials(
                [{"station_id": account["station_id"], "account_id": account["id"], "resource_id": "sub2api-4"}],
                refresh=True,
                slot_credentials={(account["id"], "sub2api-4"): "sk-persisted-on-the-slot"},
            )

            self.assertEqual([], materials["issues"])
            self.assertEqual(1, len(materials["resources"]))
            self.assertEqual("sk-persisted-on-the-slot", materials["resources"][0]["api_key"])
            # The resource keeps its own catalog, so a model this group serves
            # is judged against it instead of against an empty list.
            self.assertEqual(["model-a", "model-b"], materials["resources"][0]["models"])

    def test_a_key_with_no_cached_value_is_still_unresolved_when_the_session_expires(self) -> None:
        """The fallback stops where the app's own data does.

        A Core that never read this key has nothing to materialize from, so the
        station's refusal remains the row's real cause instead of being hidden
        by the decoupling.
        """

        class DeadStation:
            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                del origin, path, headers
                raise RelayAccountsError("Relay login has expired")

            def post(self, *_: object, **__: object) -> object:
                raise RelayAccountsError("Relay login has expired")

            def password_login(self, *_: object, **__: object) -> dict[str, str]:
                raise RelayAccountsError("Relay username or password is invalid")

        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=DeadStation())
            account = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]
            domain._accounts[0]["resources"] = [
                {"id": "sub2api-4", "name": "Plus", "enabled": True, "models": ["model-a"]}
            ]

            materials = domain.binding_materials(
                [{"station_id": account["station_id"], "account_id": account["id"], "resource_id": "sub2api-4"}],
                refresh=True,
            )

            self.assertEqual([], materials["resources"])
            self.assertEqual(
                {"resource_key_unavailable", "refresh_failed"},
                {item["code"] for item in materials["issues"]},
            )

    def test_sub2api_linked_material_keeps_the_group_page_when_the_key_catalog_is_unavailable(self) -> None:
        """A rejected key catalog is not a verdict about the group's models.

        Some deployments protect ``/v1/models``; the page's list then remains
        the catalog, exactly as it did before the key's own read joined it.
        """

        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "Plus", "status": "active", "key": "sk-replace-plus-key", "group_id": 21},
                        ]
                    },
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [
                        {
                            "name": "plus",
                            "platforms": [
                                {
                                    "platform": "anthropic",
                                    "groups": [{"id": 21, "name": "Plus分组"}],
                                    "supported_models": [{"name": "model-a"}],
                                }
                            ],
                        }
                    ],
                },
            },
            errors={"/v1/models": RelayAccountsError("Gateway catalog rejected the key")},
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample@example.test", cookie="session=replace-cookie")
            resources = domain.refresh_resources(account_id)["resources"]
            account = domain.snapshot()["accounts"][0]

            materials = domain.binding_materials(
                [
                    {
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resources[0]["id"],
                    }
                ]
            )

            self.assertEqual([], materials["issues"])
            self.assertEqual(["model-a"], materials["resources"][0]["models"])

    def test_sub2api_channel_page_supplies_each_groups_models_without_per_key_reads(self) -> None:
        """One channel read describes every group, so no key is probed.

        The channel page names each group beside the models it serves; opening
        the group manager must not fan out one gateway read per key when that
        page already answered.
        """

        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "Plus", "status": "active", "key": "sk-replace-plus-key", "group_id": 21},
                            {"id": 5, "name": "Pro", "status": "active", "key": "sk-replace-pro-key", "group_id": 22},
                        ]
                    },
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [
                        {
                            "name": "plus",
                            "platforms": [
                                {
                                    "platform": "openai",
                                    "groups": [{"id": 21, "name": "Plus分组"}],
                                    "supported_models": [{"name": "model-a"}, {"name": "model-b"}],
                                }
                            ],
                        },
                        {
                            "name": "pro",
                            "platforms": [
                                {
                                    "platform": "anthropic",
                                    "groups": [{"id": 22, "name": "Pro分组"}],
                                    "supported_models": [{"name": "model-b"}, {"name": "model-c"}],
                                }
                            ],
                        },
                    ],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample@example.test", cookie="session=replace-cookie")

            resources = domain.refresh_resources(account_id)["resources"]

            # Each key reports its own group's models, in the page's order.
            self.assertEqual([["model-a", "model-b"], ["model-b", "model-c"]], [resource["models"] for resource in resources])
            self.assertEqual([], [path for _, path, _ in fake.requests if path == "/v1/models"])
            self.assertEqual(
                1,
                len([path for _, path, _ in fake.requests if path == "/api/v1/keys?page=1&page_size=100"]),
            )

    def test_sub2api_keeps_a_stored_group_list_instead_of_reading_the_key_again(self) -> None:
        """A group the channel page omits is read once, then served from the key.

        Opening the group manager reloads the station facts; the models must not
        cost a request per key on every open.
        """

        fake = FakeRelayHTTPClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {"items": [{"id": 7, "name": "Fallback", "status": "active", "key": "sk-replace-fallback-key", "group_id": 70}]},
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [
                        {
                            "name": "plus",
                            "platforms": [
                                {
                                    "platform": "openai",
                                    "groups": [{"id": 21, "name": "Plus分组"}],
                                    "supported_models": [{"name": "model-plus"}],
                                }
                            ],
                        }
                    ],
                },
                "/v1/models": {"object": "list", "data": [{"id": "model-own"}]},
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(account_id, username="sample@example.test", cookie="session=replace-cookie")

            first = domain.refresh_resources(account_id)["resources"]
            self.assertEqual([["model-own"]], [resource["models"] for resource in first])
            self.assertEqual(1, len([path for _, path, _ in fake.requests if path == "/v1/models"]))

            second = domain.refresh_resources(account_id)["resources"]

            self.assertEqual([["model-own"]], [resource["models"] for resource in second])
            self.assertEqual(1, len([path for _, path, _ in fake.requests if path == "/v1/models"]))

    def test_sub2api_each_key_keeps_its_own_gateway_model_list(self) -> None:
        """A key's group models are its own; the channel catalog is only a fallback.

        The dashboard catalog is one union across the deployment, so reading it
        for every key made every group show the same model list.
        """

        class PerKeyGatewayClient(FakeRelayHTTPClient):
            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                if path == "/v1/models":
                    self.requests.append((origin, path, dict(headers)))
                    key = headers.get("Authorization", "")
                    if key.endswith("sk-replace-silent-key"):
                        raise RelayAccountsError("Gateway catalog unavailable")
                    suffix = key.rsplit("-", 2)[-2]
                    return {"object": "list", "data": [{"id": f"model-{suffix}"}]}
                return super().json(origin, path, headers=headers)

        fake = PerKeyGatewayClient(
            {
                "/api/v1/keys?page=1&page_size=100": {
                    "code": 0,
                    "data": {
                        "items": [
                            {"id": 4, "name": "Alpha", "status": "active", "key": "sk-replace-alpha-key"},
                            {"id": 5, "name": "Beta", "status": "active", "key": "sk-replace-beta-key"},
                            {"id": 6, "name": "Silent", "status": "active", "key": "sk-replace-silent-key"},
                        ]
                    },
                },
                "/api/v1/channels/available": {
                    "code": 0,
                    "data": [{"name": "channel", "platforms": [{"platform": "openai", "supported_models": [{"name": "union-model"}]}]}],
                },
            }
        )
        with tempfile.TemporaryDirectory() as directory:
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "add",
                {"type": "sub2api", "label": "Sub Relay", "origin": "https://sub.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="sample@example.test",
                access_token="replace-dashboard-token",
            )

            resources = domain.refresh_resources(account_id)["resources"]

            self.assertEqual(
                [["model-alpha"], ["model-beta"], ["union-model"]],
                [resource["models"] for resource in resources],
            )
            self.assertEqual(
                [
                    {"Authorization": "Bearer sk-replace-alpha-key"},
                    {"Authorization": "Bearer sk-replace-beta-key"},
                    {"Authorization": "Bearer sk-replace-silent-key"},
                ],
                sorted(
                    [headers for _, path, headers in fake.requests if path == "/v1/models"],
                    key=lambda headers: headers["Authorization"],
                ),
            )

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
            self.assertTrue(all(headers == {"Cookie": "session=replace-cookie"} for _, path, headers in fake.requests if path != "/v1/models"))
            self.assertEqual(
                [{"Authorization": "Bearer sk-replace-sub-key"}],
                [headers for _, path, headers in fake.requests if path == "/v1/models"],
            )

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


class RelayReadPolicyTests(unittest.TestCase):
    """One station read's bound and the one retry a dropped read is allowed.

    A relay commonly fails by dropping the connection instead of answering, and
    the refresh waits for the slowest read of its round, so a read gets a short
    attempt bound (``READ_TIMEOUT_SECONDS``) with one more attempt after a
    transport failure, while a write is never replayed and a documentary read
    answers from a single short attempt.
    """

    @staticmethod
    def _serve(handler: type[BaseHTTPRequestHandler]) -> tuple[ThreadingHTTPServer, threading.Thread]:
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        return server, thread

    def test_a_read_the_station_drops_is_sent_once_more(self) -> None:
        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attempts.append(self.path)
                if len(attempts) == 1:
                    # The station drops the first connection without answering,
                    # which is how a flaky relay fails.
                    self.close_connection = True
                    self.connection.close()
                    return
                body = json.dumps({"data": {"ok": True}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server, thread = self._serve(Handler)
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            payload = RelayHTTPClient().json(origin, "/api/v1/keys?page=1&page_size=100", headers={})

            self.assertEqual({"data": {"ok": True}}, payload)
            self.assertEqual(["/api/v1/keys?page=1&page_size=100"] * 2, attempts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_read_that_stalls_stops_at_the_read_bound_and_retries(self) -> None:
        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attempts.append(self.path)
                # Hold the socket open past the read bound: the client waits for
                # the bound, gives up, and resends the read with the next bound.
                time.sleep(0.6)
                self.close_connection = True
                self.connection.close()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server, thread = self._serve(Handler)
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            with mock.patch.object(relay_accounts, "READ_ATTEMPT_SECONDS", (0.2, 0.2)):
                started = time.monotonic()
                with self.assertRaises(RelayTransportError) as caught:
                    RelayHTTPClient().json(origin, "/api/v1/keys?page=1&page_size=100", headers={})
                elapsed = time.monotonic() - started

            self.assertEqual("Relay is unavailable", str(caught.exception))
            self.assertEqual(2, len(attempts))
            self.assertLess(elapsed, 1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_dropped_read_gets_the_next_longer_bound(self) -> None:
        """A station that is merely slow is not lost with the short first bound."""

        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attempts.append(self.path)
                if len(attempts) == 1:
                    time.sleep(0.5)
                    self.close_connection = True
                    self.connection.close()
                    return
                body = json.dumps({"data": {"ok": True}}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server, thread = self._serve(Handler)
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            with mock.patch.object(relay_accounts, "READ_ATTEMPT_SECONDS", (0.2, 2.0)):
                started = time.monotonic()
                payload = RelayHTTPClient().json(origin, "/api/v1/keys?page=1&page_size=100", headers={})
                elapsed = time.monotonic() - started

            self.assertEqual({"data": {"ok": True}}, payload)
            self.assertEqual(2, len(attempts))
            self.assertGreaterEqual(elapsed, 0.2)
            self.assertLess(elapsed, 1.5)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_write_is_never_sent_twice(self) -> None:
        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                attempts.append(self.path)
                self.close_connection = True
                self.connection.close()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server, thread = self._serve(Handler)
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            with mock.patch.object(relay_accounts, "REQUEST_TIMEOUT_SECONDS", 0.2):
                with self.assertRaises(RelayTransportError):
                    RelayHTTPClient().post(origin, "/api/token/7/key", headers={})

            self.assertEqual(["/api/token/7/key"], attempts)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_documentary_read_gets_one_short_attempt(self) -> None:
        """The balance can never hold the round trip that carries the key list."""

        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attempts.append(self.path)
                time.sleep(0.6)
                self.close_connection = True
                self.connection.close()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server, thread = self._serve(Handler)
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            with mock.patch.object(relay_accounts, "OPTIONAL_READ_TIMEOUT_SECONDS", 0.2), mock.patch.object(
                relay_accounts, "READ_ATTEMPT_SECONDS", (5.0, 5.0, 5.0)
            ):
                started = time.monotonic()
                with self.assertRaises(RelayTransportError):
                    RelayHTTPClient().json(origin, "/api/v1/user/profile", headers={})
                elapsed = time.monotonic() - started

            self.assertEqual(["/api/v1/user/profile"], attempts)
            self.assertLess(elapsed, 1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_key_gateway_catalog_is_documentary(self) -> None:
        """A key's own model catalog never holds the round trip that carries keys."""

        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                attempts.append(self.path)
                time.sleep(0.6)
                self.close_connection = True
                self.connection.close()

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server, thread = self._serve(Handler)
        try:
            origin = f"http://127.0.0.1:{server.server_address[1]}"
            with mock.patch.object(relay_accounts, "OPTIONAL_READ_TIMEOUT_SECONDS", 0.2), mock.patch.object(
                relay_accounts, "READ_ATTEMPT_SECONDS", (5.0, 5.0, 5.0)
            ):
                started = time.monotonic()
                with self.assertRaises(RelayTransportError):
                    RelayHTTPClient().json(origin, "/v1/models", headers={})
                elapsed = time.monotonic() - started

            self.assertEqual(["/v1/models"], attempts)
            self.assertLess(elapsed, 1.0)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_a_transport_error_keeps_the_message_every_caller_classifies(self) -> None:
        self.assertTrue(issubclass(RelayTransportError, RelayAccountsError))
        self.assertEqual("Relay is unavailable", str(RelayTransportError()))
    def test_a_newapi_fork_account_id_travels_as_the_account_header(self) -> None:
        """A fork that requires ``New-Api-User`` must receive the station's own id.

        New API resolves that header with ``strconv.Atoi`` and compares it to the
        session's user, so without it every dashboard read answers
        ``未提供 New-Api-User`` and an account that is genuinely signed in reads as
        unusable — which is what left 供应商与模型 reporting 登录成功 but 无法加载 API
        资源.  The id is the station's own, captured with the session and sent on
        every authenticated read (refresh, group catalog, key list, and a key
        materialization) rather than guessed from a label or a username.
        """

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["gpt-5.6-sol"]},
                    "/api/token/?p=1&size=100": {
                        "data": {
                            "items": [
                                {"id": 4154, "name": "x-cheap", "status": 1, "key": "sk-replace-key"}
                            ]
                        }
                    },
                    "/api/user/self": {"data": {"quota": 1_250_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                    "/api/token/4154/key": {"data": {"key": "sk-replace-key"}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "NIMBUS", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(
                account["id"],
                username="riveryang6@example.test",
                access_token="replace-token",
                user_id="2411",
            )

            domain.refresh_resources(account["id"])
            domain.trusted_secret_value("api_key", f"{account['id']}:newapi-4154")

            authenticated = [
                (path, headers) for _, path, headers in fake.requests if path != "/api/status"
            ]
            self.assertTrue(authenticated)
            for path, headers in authenticated:
                self.assertEqual("2411", headers.get("New-Api-User"), path)
                self.assertIn("Authorization", headers)
            # The unauthenticated type probe still carries no account identity.
            self.assertEqual(
                ["/api/status"],
                [path for _, path, headers in fake.requests if not headers.get("New-Api-User")],
            )

    def test_a_station_id_that_is_not_a_decimal_id_is_never_sent(self) -> None:
        """A value the header cannot carry is dropped, never invented.

        ``New-Api-User`` is parsed as a decimal integer, so a username, a UUID,
        or a label would be rejected as a mismatched account.  An account that
        never reported a usable id must therefore send no header at all, which
        is what a stock New API endpoint accepts.
        """

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["gpt-test"]},
                    "/api/token/?p=1&size=100": {
                        "data": {"items": [{"id": 1, "name": "default", "status": 1, "key": "masked"}]}
                    },
                    "/api/user/self": {"data": {"quota": 500_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "New API", "origin": "https://relay.example.test"},
            )["accounts"][0]
            for rejected in ("2411\r\nX-Evil: 1", "riveryang6", "user-2411", "4711abc", ""):
                domain.accept_login_result(
                    account["id"],
                    username="person",
                    access_token="replace-token",
                    user_id=rejected,
                )
                fake.requests.clear()

                domain.refresh_resources(account["id"])

                self.assertNotIn(
                    "New-Api-User",
                    {key for _, path, headers in fake.requests for key in headers},
                    rejected,
                )

    def test_a_sub2api_station_never_receives_the_newapi_account_header(self) -> None:
        """The header belongs to one station family, not to every dashboard read.

        A sub2api deployment authenticates its dashboard API with the session
        cookie alone, and an invented header on its requests would be sent to a
        service that never asked for one.
        """

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/v1/user/profile": {"data": {"balance": 8.75}},
                    "/api/v1/keys?page=1&page_size=100": {
                        "data": {"items": [{"id": "11", "name": "alpha", "status": "active", "key": "sk-a"}]}
                    },
                    "/api/v1/channels/available": {
                        "data": [{"platforms": [{"supported_models": ["model-test"]}]}]
                    },
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account_id = domain.dispatch(
                "account.add",
                {"type": "sub2api", "label": "Sub2API", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            # Even an id the caller wrongly attaches stays off a sub2api read.
            domain.accept_login_result(
                account_id,
                username="person@example.test",
                access_token="replace-token",
                user_id="2411",
            )
            fake.requests.clear()

            domain.refresh_resources(account_id)

            self.assertTrue(fake.requests)
            self.assertEqual(
                [],
                [
                    path
                    for _, path, headers in fake.requests
                    if "New-Api-User" in headers
                ],
            )

    def test_a_refresh_keeps_the_staged_rename_visible(self) -> None:
        """A read must not overwrite the preview of staged work.

        A station read reports the station's own state, and a staged edit has not
        reached it yet.  Folding the read straight over the account therefore
        replaced 自动分组's just-staged renames with the station's old key names,
        and the 分组管理 sheet showed the un-grouped reading again — the switch
        looked like it had done nothing even though every rename was staged and
        Apply wrote them all correctly.
        """

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["gpt-test"]},
                    "/api/token/?p=1&size=100": {
                        "data": {
                            "items": [
                                {"id": 1, "name": "fast", "status": 1, "key": "sk-1", "group": "gpt-pro"},
                                {"id": 2, "name": "pro", "status": 1, "key": "sk-2", "group": "gpt-pro"},
                            ]
                        }
                    },
                    "/api/user/self": {"data": {"quota": 500_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                    "/api/user/self/groups": {"data": {"gpt-pro": {"ratio": 1.0}}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "NIMBUS", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", access_token="replace-token")
            domain.refresh_resources(account["id"])

            aligned = domain.set_auto_grouping(account["id"], True)
            # 自动分组 owns one key per group: the group's first key is kept and
            # renamed, and the extra key in the same group is staged for
            # deletion.
            self.assertEqual(
                ["gpt-pro", "pro"],
                [resource["name"] for resource in aligned["resources"]],
            )
            self.assertTrue(any(
                operation["kind"] == "api_key_update"
                for operation in domain._pending_operations
            ))
            self.assertTrue(any(
                operation["kind"] == "api_key_delete"
                for operation in domain._pending_operations
            ))

            # The pane's own mount probe (or opening the sheet) reads the
            # station again.  The fixture keeps reporting the original two keys,
            # so a read that simply replaced the account would undo the rename
            # and resurrect the duplicate the layout just retired; the layout
            # must survive it as the one-key-one-group shape it staged.
            refreshed = domain.refresh_resources(account["id"])
            self.assertEqual(
                ["gpt-pro"],
                [resource["name"] for resource in refreshed["resources"]],
            )
            # A second read is equally idempotent.
            again = domain.refresh_resources(account["id"])
            self.assertEqual(
                ["gpt-pro"],
                [resource["name"] for resource in again["resources"]],
            )

    def test_an_apply_time_read_still_reports_the_station_itself(self) -> None:
        """The read that decides "did my write land?" keeps the station's answer.

        ``reconcile_apply`` compares what the station reports against a staged
        change to decide whether to retire that operation or hand it back for
        another attempt.  A read that painted the staged change onto its own
        answer would make an operation whose write never reached the station look
        applied and silently drop it, so the Apply-time read stays the station's
        own report.
        """

        with tempfile.TemporaryDirectory() as directory:
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["gpt-test"]},
                    "/api/token/?p=1&size=100": {
                        "data": {
                            "items": [
                                {"id": 1, "name": "fast", "status": 1, "key": "sk-1", "group": "gpt-pro"},
                            ]
                        }
                    },
                    "/api/user/self": {"data": {"quota": 500_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                    "/api/user/self/groups": {"data": {"gpt-pro": {"ratio": 1.0}}},
                }
            )
            domain = RelayAccountsDomain(directory, http_client=fake)
            account = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "NIMBUS", "origin": "https://relay.example.test"},
            )["accounts"][0]
            domain.accept_login_result(account["id"], username="person", access_token="replace-token")
            domain.refresh_resources(account["id"])
            domain.set_auto_grouping(account["id"], True)

            apply_read = domain.refresh_resources(account["id"], _for_apply=True)
            # The station still names the key ``fast``: the staged rename has not
            # been written, and this read is what proves it.
            self.assertIn("fast", [resource["name"] for resource in apply_read["resources"]])

    def test_a_slim_fork_token_can_still_be_renamed_and_regrouped(self) -> None:
        """A fork that omits token fields must not block the edit it was asked for.

        A New API PUT binds the body into a fresh struct and writes every field
        back, so a field the station did not report is its own zero value rather
        than "leave this alone".  Requiring every field to be present made an
        older or slimmer fork refuse the whole edit, and 自动分组 could then
        never rename or regroup its keys — the switch appeared to do nothing.
        """

        domain = RelayAccountsDomain
        # A fork that serializes only the fields its own UI edits.
        thin = {"id": 1, "name": "fast", "group": "gpt-pro-full", "status": 1}
        payload = domain._newapi_update_payload(thin, {"name": "gpt-pro-full", "group": "gpt-pro-full"})
        self.assertEqual("gpt-pro-full", payload["name"])
        self.assertEqual("gpt-pro-full", payload["group"])
        # The fields it never reported carry their documented binding default,
        # never an invented value.
        self.assertEqual(-1, payload["expired_time"])
        self.assertEqual(0, payload["remain_quota"])
        self.assertFalse(payload["unlimited_quota"])
        self.assertFalse(payload["model_limits_enabled"])
        self.assertEqual("", payload["model_limits"])
        self.assertEqual("", payload["allow_ips"])
        self.assertFalse(payload["cross_group_retry"])

        # A station that states a field with the wrong shape is a broken answer
        # and is still refused: only a genuinely absent field falls back.
        broken = dict(thin, expired_time="soon")
        with self.assertRaises(RelayAccountsError):
            domain._newapi_update_payload(broken, {"name": "x"})
        # ``allow_ips`` is nullable in New API; null means the same as absent.
        for allow_ips in (None, "", "1.2.3.4"):
            nullish = dict(thin, allow_ips=allow_ips)
            self.assertEqual(
                "" if not isinstance(allow_ips, str) else allow_ips,
                domain._newapi_update_payload(nullish, {})["allow_ips"],
            )

    def test_a_remembered_account_id_survives_a_restart_and_a_session_check(self) -> None:
        """The id is durable session material, and it is what the probe sends.

        A Core restart holds no process-local session, so a remembered account
        verifies itself from storage.  That verification read is an ordinary
        authenticated read on a fork that requires the header, and without the
        stored id it would report a healthy session as signed out.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            fake = FakeRelayHTTPClient(
                {
                    "/api/user/models": {"data": ["gpt-test"]},
                    "/api/token/?p=1&size=100": {
                        "data": {"items": [{"id": 1, "name": "default", "status": 1, "key": "masked"}]}
                    },
                    "/api/user/self": {"data": {"quota": 500_000}},
                    "/api/status": {"data": {"quota_per_unit": 500_000}},
                }
            )
            domain = RelayAccountsDomain(root, http_client=fake)
            account_id = domain.dispatch(
                "account.add",
                {"type": "newapi", "label": "New API", "origin": "https://relay.example.test"},
            )["accounts"][0]["id"]
            domain.accept_login_result(
                account_id,
                username="person",
                cookie="session=replace-cookie",
                user_id="2411",
                remember_password=True,
            )
            domain.commit_apply()
            self.assertEqual(
                "2411",
                json.loads(Path(domain.storage_path).read_text(encoding="utf-8"))["accounts"][0]["user_id"],
            )

            reloaded = RelayAccountsDomain(root, http_client=fake)
            fake.requests.clear()
            restored = reloaded.restore_saved_session(account_id)

            self.assertEqual("signed_in", restored["login_status"])
            self.assertEqual(
                ["2411"],
                [
                    headers["New-Api-User"]
                    for _, path, headers in fake.requests
                    if path == "/api/user/self"
                ],
            )


if __name__ == "__main__":
    unittest.main()
