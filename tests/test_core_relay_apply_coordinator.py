from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.domains.relay_accounts import RelayAccountsDomain
from litellm_menu.core.service import CoreStore


class RelayCoordinatorHTTP:
    """A small authenticated relay fixture that records remote writes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.deleted = False
        self.resource_name = "Primary"
        self.lose_next_update_response = False

    def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        del origin, headers
        self.calls.append(("GET", path))
        if path == "/api/user/models":
            return {"data": ["model-a"]}
        if path == "/api/user/self/groups":
            return {"data": {"default": {"ratio": 1.25}}}
        if path == "/api/token/?p=1&size=100":
            return {
                "data": {
                    "items": [] if self.deleted else [
                        {
                            "id": 7,
                            "name": self.resource_name,
                            "status": 1,
                            "key": "replace-secret",
                            "group": "default",
                        }
                    ]
                }
            }
        if path == "/api/token/7":
            return {
                "data": {
                    "id": 7,
                    "name": self.resource_name,
                    "status": 1,
                    "key": "replace-secret",
                    "expired_time": -1,
                    "remain_quota": 0,
                    "unlimited_quota": True,
                    "model_limits_enabled": False,
                    "model_limits": "",
                    "allow_ips": "",
                    "group": "default",
                    "cross_group_retry": False,
                }
            }
        raise AssertionError(f"unexpected GET {path}")

    def post(
        self,
        origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        del origin, headers, body
        self.calls.append(("POST", path))
        if path == "/api/token/7/key":
            return {"data": {"key": "replace-materialized-key"}}
        raise AssertionError(f"unexpected POST {path}")

    def put(
        self,
        origin: str,
        path: str,
        *,
        headers: dict[str, str],
        body: dict[str, object] | None = None,
    ) -> object:
        del origin, headers
        self.calls.append(("PUT", path))
        if path == "/api/token/" and isinstance(body, dict) and isinstance(body.get("name"), str):
            self.resource_name = body["name"]
            if self.lose_next_update_response:
                self.lose_next_update_response = False
                raise TimeoutError("simulated response loss")
        return {"success": True}

    def delete(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
        del origin, headers
        self.calls.append(("DELETE", path))
        if path == "/api/token/7":
            self.deleted = True
        return {"success": True}


class RelayApplyCoordinatorIntegrationTests(unittest.TestCase):
    def _linked_core(self, root: Path) -> tuple[CoreStore, RelayAccountsDomain, ProvidersModelsDomain, RelayCoordinatorHTTP, str, str]:
        http = RelayCoordinatorHTTP()
        relay = RelayAccountsDomain(root, http_client=http)
        providers = ProvidersModelsDomain(root / "config.yaml")
        core = CoreStore(domains=[relay, providers])
        account = core.dispatch(
            {
                "domain": "relay_accounts",
                "type": "account.add",
                "payload": {
                    "type": "newapi",
                    "label": "Relay",
                    "origin": "https://relay.example.test",
                },
            }
        )
        account_id = account["revision"] and relay.snapshot()["accounts"][0]["id"]
        core.accept_relay_login(
            account_id=account_id,
            account_type="newapi",
            label="Relay",
            origin="https://relay.example.test",
            username="person",
            cookie="session=fixture",
        )
        refreshed = core.refresh_relay_resources(account_id, revision=core.revision)
        self.assertEqual("ready", refreshed["resource_status"])
        resource_id = relay.snapshot()["accounts"][0]["resources"][0]["id"]
        imported = core.import_relay_resources(account_id, [resource_id], revision=core.revision)
        self.assertEqual("linked", imported["import_mode"])
        self.assertNotIn("replace-materialized-key", json.dumps(core.snapshot()))
        return core, relay, providers, http, account_id, resource_id

    def test_provider_imports_relay_key_without_secret_and_apply_materializes_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = RelayCoordinatorHTTP()
            relay = RelayAccountsDomain(root, http_client=http)
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {
                        "type": "newapi",
                        "label": "Relay",
                        "origin": "https://relay.example.test",
                    },
                }
            )
            account_id = relay.snapshot()["accounts"][0]["id"]
            core.accept_relay_login(
                account_id=account_id,
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="person",
                cookie="session=fixture",
            )
            core.refresh_relay_resources(account_id, revision=core.revision)
            account = relay.snapshot()["accounts"][0]
            resource_id = account["resources"][0]["id"]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.add",
                    "payload": {
                        "provider": {
                            "name": "provider-a",
                            "enabled": True,
                            "api_base": "",
                            "models": [],
                        }
                    },
                },
                expected_revision=core.revision,
            )
            calls_before_import = list(http.calls)
            imported = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.import_relay_key",
                    "payload": {
                        "provider_id": "provider-a",
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resource_id,
                    },
                },
                expected_revision=core.revision,
            )
            self.assertEqual(core.revision, imported["revision"])
            self.assertEqual(calls_before_import, http.calls)
            provider = providers.snapshot()["providers"][0]
            self.assertEqual(1, len(provider["key_states"]))
            slot_id = provider["key_states"][0]["id"]
            self.assertFalse(provider["key_states"][0]["configured"])
            self.assertEqual("relay", provider["key_states"][0]["source"]["kind"])
            self.assertNotIn("replace-materialized-key", json.dumps(core.snapshot()))

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.import_relay_key",
                    "payload": {
                        "provider_id": "provider-a",
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resource_id,
                    },
                },
                expected_revision=core.revision,
            )
            self.assertEqual(
                [slot_id],
                [item["id"] for item in providers.snapshot()["providers"][0]["key_states"]],
            )

            applied = core.apply(domain="providers_models", revision=core.revision)
            self.assertTrue(applied["applied"])
            self.assertEqual("applied", applied["status"])
            self.assertIn("relay_accounts", applied["domains"])
            private = providers.export(include_sensitive=True)["providers"][0]
            self.assertEqual("sk-replace-materialized-key", private["api_keys"][0]["value"])
            self.assertEqual("https://relay.example.test/v1", private["api_base"])
            self.assertNotIn("replace-materialized-key", json.dumps(core.snapshot()))

    def test_model_selects_a_discovered_relay_key_by_matching_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = RelayCoordinatorHTTP()
            relay = RelayAccountsDomain(root, http_client=http)
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {
                        "type": "newapi",
                        "label": "Relay",
                        "origin": "https://relay.example.test",
                    },
                }
            )
            account = relay.snapshot()["accounts"][0]
            account_id = account["id"]
            core.accept_relay_login(
                account_id=account_id,
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="person",
                cookie="session=fixture",
            )
            core.refresh_relay_resources(account_id, revision=core.revision)
            account = relay.snapshot()["accounts"][0]
            resource_id = account["resources"][0]["id"]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.add",
                    "payload": {
                        "provider": {
                            "name": "provider-a",
                            "enabled": True,
                            "api_base": "https://relay.example.test/v1",
                            "models": [],
                        }
                    },
                },
                expected_revision=core.revision,
            )
            provider = providers.snapshot()["providers"][0]
            provider_id = provider["id"]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.add",
                    "payload": {
                        "provider_id": provider_id,
                        "model": {
                            "name": "public-chat",
                            "upstream_model": "model-a",
                        },
                    },
                },
                expected_revision=core.revision,
            )
            model_id = providers.snapshot()["providers"][0]["models"][0]["id"]
            calls_before_selection = list(http.calls)

            selected = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.select_relay_resource",
                    "payload": {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "station_id": account["station_id"],
                        "account_id": account_id,
                        "resource_id": resource_id,
                    },
                },
                expected_revision=core.revision,
            )

            self.assertEqual(calls_before_selection, http.calls)
            self.assertEqual(core.revision, selected["revision"])
            self.assertEqual(
                "model_relay_key_selected",
                core.snapshot()["action_summaries"]["providers_models"]["operation_summary"]["operation"],
            )
            provider = providers.snapshot()["providers"][0]
            relay_key = next(key for key in provider["key_states"] if key["source"]["kind"] == "relay")
            self.assertFalse(relay_key["configured"])
            model = provider["models"][0]
            self.assertEqual(relay_key["id"], model["provider_key_id"])
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertNotIn("replace-secret", json.dumps(core.snapshot()))

    def test_fetch_models_uses_the_same_dynamic_relay_key_without_exposing_it(self) -> None:
        class ModelListResponse:
            status = 200

            def __enter__(self) -> "ModelListResponse":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, _: int) -> bytes:
                return b'{"data":[{"id":"model-a"},{"id":"model-b"}]}'

        class ModelListOpener:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            def open(self, request: Any, *, timeout: float) -> ModelListResponse:
                self.requests.append(request)
                if timeout != 5.0:
                    raise AssertionError(f"unexpected timeout {timeout}")
                return ModelListResponse()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, providers, _http, account_id, resource_id = self._linked_core(root)
            account = relay.snapshot()["accounts"][0]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.add",
                    "payload": {
                        "provider": {
                            "name": "provider-a",
                            "enabled": True,
                            "api_base": "https://relay.example.test/v1",
                            "models": [],
                        }
                    },
                },
                expected_revision=core.revision,
            )
            provider_id = providers.snapshot()["providers"][0]["id"]
            opener = ModelListOpener()
            with patch(
                "litellm_menu.core.domains.providers_models.isolated_http_opener",
                return_value=opener,
            ):
                fetched = core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "provider.fetch_relay_resource_models",
                        "payload": {
                            "provider_id": provider_id,
                            "station_id": account["station_id"],
                            "account_id": account_id,
                            "resource_id": resource_id,
                        },
                    },
                    expected_revision=core.revision,
                )

            summary = core.snapshot()["action_summaries"]["providers_models"]["operation_summary"]
            self.assertEqual("fetch_models", summary["operation"])
            self.assertEqual(["model-a", "model-b"], summary["models"])
            self.assertTrue(summary["slot_id"].startswith("provider-slot-"))
            self.assertEqual(1, len(opener.requests))
            self.assertEqual("https://relay.example.test/v1/models", opener.requests[0].full_url)
            self.assertTrue(opener.requests[0].get_header("Authorization").startswith("Bearer sk-"))
            provider = providers.snapshot()["providers"][0]
            relay_key = next(key for key in provider["key_states"] if key["source"]["kind"] == "relay")
            self.assertEqual(summary["slot_id"], relay_key["id"])
            self.assertFalse(relay_key["configured"])
            self.assertNotIn("replace-secret", json.dumps(fetched))
            self.assertNotIn("replace-secret", json.dumps(core.snapshot()))

    def test_fetch_models_reads_relay_key_after_core_restart(self) -> None:
        """A restarted Core has no in-memory session secrets. Fetching relay
        models must fall back to the persisted session, the same behavior as
        the relay resource refresh path."""

        class RestartHTTP(RelayCoordinatorHTTP):
            def __init__(self) -> None:
                super().__init__()
                self.post_calls: list[tuple[str, str]] = []

            def post(
                self,
                origin: str,
                path: str,
                *,
                headers: dict[str, str],
                body: dict[str, object] | None = None,
            ) -> object:
                self.post_calls.append((origin, path))
                return super().post(origin, path, headers=headers, body=body)

        class ModelListResponse:
            status = 200

            def __enter__(self) -> "ModelListResponse":
                return self

            def __exit__(self, *_: object) -> None:
                return None

            def getcode(self) -> int:
                return self.status

            def read(self, _: int) -> bytes:
                return b'{"data":[{"id":"model-a"},{"id":"model-b"}]}'

        class ModelListOpener:
            def __init__(self) -> None:
                self.requests: list[Any] = []

            def open(self, request: Any, *, timeout: float) -> ModelListResponse:
                self.requests.append(request)
                if timeout != 5.0:
                    raise AssertionError(f"unexpected timeout {timeout}")
                return ModelListResponse()

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = RestartHTTP()
            relay = RelayAccountsDomain(root, http_client=http)
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {
                        "type": "newapi",
                        "label": "Relay",
                        "origin": "https://relay.example.test",
                        "remember_password": True,
                    },
                }
            )
            account_id = relay.snapshot()["accounts"][0]["id"]
            core.accept_relay_login(
                account_id=account_id,
                account_type="newapi",
                label="Relay",
                origin="https://relay.example.test",
                username="person",
                cookie="session=fixture",
            )
            core.refresh_relay_resources(account_id, revision=core.revision)
            # Persist the account as if a completed Apply had committed the
            # staged login. A Core restart only sees this durable state.
            relay._persist(force=True)
            # Simulate a Core restart: fresh domain instances share only the
            # persisted files. The new relay domain has no in-memory session
            # secrets and no cached resource keys.
            restarted_relay = RelayAccountsDomain(root, http_client=http)
            restarted_providers = ProvidersModelsDomain(root / "config.yaml")
            restarted = CoreStore(domains=[restarted_relay, restarted_providers])
            self.assertEqual({}, restarted_relay._session_secrets)
            account = restarted_relay.snapshot()["accounts"][0]
            resource_id = account["resources"][0]["id"]
            restarted.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.add",
                    "payload": {
                        "provider": {
                            "name": "provider-a",
                            "enabled": True,
                            "api_base": "https://relay.example.test/v1",
                            "models": [],
                        }
                    },
                },
                expected_revision=restarted.revision,
            )
            http.post_calls.clear()
            provider_id = restarted_providers.snapshot()["providers"][0]["id"]
            opener = ModelListOpener()
            with patch(
                "litellm_menu.core.domains.providers_models.isolated_http_opener",
                return_value=opener,
            ):
                restarted.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "provider.fetch_relay_resource_models",
                        "payload": {
                            "provider_id": provider_id,
                            "station_id": account["station_id"],
                            "account_id": account_id,
                            "resource_id": resource_id,
                        },
                    },
                    expected_revision=restarted.revision,
                )

            summary = restarted.snapshot()["action_summaries"]["providers_models"]["operation_summary"]
            self.assertEqual("fetch_models", summary["operation"])
            self.assertEqual(["model-a", "model-b"], summary["models"])
            self.assertTrue(summary["available"])
            self.assertEqual(1, len(opener.requests))
            self.assertTrue(opener.requests[0].get_header("Authorization").startswith("Bearer sk-"))
            self.assertEqual([("https://relay.example.test", "/api/token/7/key")], http.post_calls)
            self.assertNotEqual({}, restarted_relay._session_secrets)

    def test_linked_import_materializes_only_during_coordinated_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, providers, http, _account_id, _resource_id = self._linked_core(root)
            provider_state = providers.snapshot()["providers"][0]
            model_state = provider_state["models"][0]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.patch",
                    "payload": {
                        "provider_id": provider_state["id"],
                        "model_id": model_state["id"],
                        "changes": {"order_mode": "relay_multiplier"},
                    },
                },
                expected_revision=core.revision,
            )
            private_before = providers.export(include_sensitive=True)
            self.assertEqual("", private_before["providers"][0]["api_key"])
            self.assertNotIn(("DELETE", "/api/token/7"), http.calls)

            result = core.apply(
                domains=["relay_accounts", "providers_models"],
                revision=core.revision,
            )

            self.assertEqual("applied", result["status"])
            self.assertTrue(result["applied"])
            self.assertEqual(0, result["pending_operations"])
            self.assertTrue((root / "config.yaml").exists())
            self.assertTrue(relay.storage_path.exists())
            private_after = providers.export(include_sensitive=True)
            self.assertEqual("sk-replace-materialized-key", private_after["providers"][0]["api_key"])
            model = private_after["providers"][0]["models"][0]
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertEqual(1.25, model["effective_order"])
            self.assertNotIn("replace-materialized-key", json.dumps(core.snapshot()))

    def test_remote_key_delete_applies_dependency_policy_before_remote_delete(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, _relay, providers, http, account_id, resource_id = self._linked_core(root)
            before = providers.dependency_summary()
            self.assertEqual(1, before["model_count"])

            deleted = core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "api_key.delete",
                    "payload": {
                        "account_id": account_id,
                        "resource_id": resource_id,
                        "dependency_policy": "delete_models",
                    },
                },
                expected_revision=core.revision,
            )
            self.assertEqual(core.revision, deleted["revision"])
            self.assertEqual(0, providers.dependency_summary()["model_count"])
            self.assertNotIn(("DELETE", "/api/token/7"), http.calls)

            applied = core.apply(
                domains=["relay_accounts", "providers_models"],
                revision=core.revision,
            )
            self.assertEqual("applied", applied["status"])
            self.assertIn(("DELETE", "/api/token/7"), http.calls)

    def test_response_loss_is_reconciled_without_replaying_the_remote_update(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, _relay, _providers, http, account_id, resource_id = self._linked_core(root)
            http.lose_next_update_response = True

            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "api_key.update",
                    "payload": {
                        "account_id": account_id,
                        "resource_id": resource_id,
                        "name": "Renamed",
                    },
                },
                expected_revision=core.revision,
            )
            result = core.apply(
                domains=["relay_accounts", "providers_models"],
                revision=core.revision,
            )

            self.assertTrue(result["applied"])
            self.assertEqual("applied", result["status"])
            self.assertEqual(0, result["pending_operations"])
            self.assertEqual(1, http.calls.count(("PUT", "/api/token/")))


if __name__ == "__main__":
    unittest.main()
