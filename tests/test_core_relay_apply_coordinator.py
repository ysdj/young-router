from __future__ import annotations

import copy
import json
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.core.domains.relay_accounts import RelayAccountsDomain, RelayAccountsError
from young_router.core.service import CoreError, CoreStore, _relay_binding_projection


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

    def test_a_linked_route_the_relay_cannot_resolve_names_its_own_cause(self) -> None:
        """A binding the relay cannot resolve is not a bare rollback.

        The linked route's upstream model is absent from the key's catalog, so
        materialization refuses the write before any remote mutation.  Core
        reports that cause (``relay_binding_failed``) instead of the generic
        ``apply_failed`` the strip renders as 更改未生效, and the refused draft
        leaves the key unmaterialized.
        """

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
                    "payload": {"type": "newapi", "label": "Relay", "origin": "https://relay.example.test"},
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
                    "payload": {"provider": {"name": "provider-a", "enabled": True, "api_base": "https://relay.example.test/v1", "models": []}},
                },
                expected_revision=core.revision,
            )
            provider_id = providers.snapshot()["providers"][0]["id"]
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.add",
                    "payload": {
                        "provider_id": provider_id,
                        "model": {"name": "public-chat", "upstream_model": "model-not-in-the-catalog"},
                    },
                },
                expected_revision=core.revision,
            )
            model_id = providers.snapshot()["providers"][0]["models"][0]["id"]
            core.dispatch(
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
            config_path = root / "config.yaml"
            self.assertFalse(config_path.exists())

            with self.assertRaises(CoreError) as raised:
                core.apply(domain="providers_models", revision=core.revision)

            self.assertEqual("relay_binding_failed", raised.exception.code)
            # Nothing was written and the refused draft stays staged for repair.
            self.assertFalse(config_path.exists())
            relay_key = next(
                key for key in providers.snapshot()["providers"][0]["key_states"] if key["source"]["kind"] == "relay"
            )
            self.assertFalse(relay_key["configured"])
            model = providers.snapshot()["providers"][0]["models"][0]
            self.assertEqual("relay_linked", model["catalog_mode"])
            # The pane can mark the row that refused the write: the issue the
            # relay reported names its key and the route on it.
            self.assertEqual(
                [{
                    "code": "catalog_model_missing",
                    "provider_key_id": relay_key["id"],
                    "model_id": model["editor_id"],
                    "provider": providers.snapshot()["providers"][0]["editor_id"],
                }],
                providers.snapshot()["binding_issues"],
            )
            # A resolution that worked clears them again.
            providers.record_binding_issues([])
            self.assertEqual([], providers.snapshot()["binding_issues"])

    def test_an_aged_out_station_session_does_not_read_as_a_dead_linked_key(self) -> None:
        """A linked route still applies after its station session ages out.

        Every part of a relay-bound Apply is authenticated by the same dashboard
        session: the resource refresh, the key materialization.  A sub2api
        session lives about two days, so a window that stayed open past that
        used to end with `refresh_failed`/`resource_key_unavailable` and then
        `relay_binding_failed` — the pane reported the linked key as
        unavailable and the route never applied — while the account's remembered
        password could still mint a session.  Core renews that session and
        retries the read instead.
        """

        class Sub2ApiHTTP:
            def __init__(self) -> None:
                self.expired = False
                self.password_logins: list[tuple[str, str, str, str]] = []
                self.calls: list[tuple[str, str]] = []

            def json(self, origin: str, path: str, *, headers: dict[str, str]) -> object:
                del origin
                self.calls.append(("GET", path))
                if self.expired:
                    # The station rejects the stored dashboard session, exactly
                    # as it does once that session ages out.
                    raise RelayAccountsError("Relay login has expired")
                if path == "/api/v1/keys?page=1&page_size=100":
                    return {
                        "code": 0,
                        "data": {
                            "items": [
                                {"id": 51, "name": "GroupFixture", "status": "active", "key": "replace-linked-key", "group_id": 51},
                            ]
                        },
                    }
                if path == "/api/v1/channels/available":
                    return {"code": 0, "data": []}
                if path == "/api/v1/user/profile":
                    return {"code": 0, "data": {"balance": 1.0}}
                if path == "/api/v1/groups/available":
                    return {"code": 0, "data": [{"id": 51, "name": "GroupFixture"}]}
                if path == "/api/v1/groups/rates":
                    return {"code": 0, "data": {"51": 0.15}}
                if path == "/v1/models":
                    return {"object": "list", "data": [{"id": "claude-opus-5-5"}]}
                raise AssertionError(f"unexpected GET {path}")

            def post(self, origin: str, path: str, *, headers: dict[str, str], body: object = None) -> object:
                del origin, headers, body
                self.calls.append(("POST", path))
                raise AssertionError(f"unexpected POST {path}")

            def password_login(self, origin: str, account_type: str, username: str, password: str) -> dict[str, str]:
                self.password_logins.append((origin, account_type, username, password))
                self.expired = False
                return {
                    "username": username,
                    "cookie": "",
                    "access_token": "replace-renewed-token",
                    "refresh_token": "replace-renewed-refresh",
                }

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = Sub2ApiHTTP()
            relay = RelayAccountsDomain(root, http_client=http)
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "relay_accounts",
                    "type": "account.add",
                    "payload": {"type": "sub2api", "label": "Relay", "origin": "https://relay.example.test", "remember_password": True},
                }
            )
            account = relay.snapshot()["accounts"][0]
            core.accept_relay_login(
                account_id=account["id"],
                account_type="sub2api",
                label="Relay",
                origin="https://relay.example.test",
                username="person@example.test",
                access_token="replace-stale-token",
                password="replace-password",
            )
            core.refresh_relay_resources(account["id"], revision=core.revision)
            resource_id = relay.snapshot()["accounts"][0]["resources"][0]["id"]
            self.assertEqual("sub2api-51", resource_id)
            core.import_relay_resources(account["id"], [resource_id], revision=core.revision)

            # The session ages out while the window is still open.
            http.expired = True
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.add",
                    "payload": {
                        "provider_id": "relay-" + relay.snapshot()["accounts"][0]["station_id"],
                        "model": {"model_name": "claude-opus-5-5", "litellm_model": "anthropic/claude-opus-5-5"},
                    },
                },
                expected_revision=core.revision,
            )
            provider = providers.snapshot()["providers"][0]
            model = next(item for item in provider["models"] if item["model_name"] == "claude-opus-5-5")
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.select_relay_resource",
                    "payload": {
                        "provider_id": provider["id"],
                        "model_id": model["id"],
                        "station_id": relay.snapshot()["accounts"][0]["station_id"],
                        "account_id": account["id"],
                        "resource_id": resource_id,
                    },
                },
                expected_revision=core.revision,
            )

            applied = core.apply(domain="providers_models", revision=core.revision)

            self.assertEqual("applied", applied["status"])
            self.assertEqual([], providers.snapshot()["binding_issues"])
            self.assertEqual(
                [("https://relay.example.test", "sub2api", "person@example.test", "replace-password")],
                http.password_logins,
            )
            # The renewed session is what wrote the route, and the linked key
            # carries its materialized credential.
            written = (root / "config.yaml").read_text(encoding="utf-8")
            self.assertIn("claude-opus-5-5", written)
            self.assertIn("replace-linked-key", written)
            self.assertEqual("signed_in", relay.snapshot()["accounts"][0]["login_status"])

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
                "young_router.core.domains.providers_models.isolated_http_opener",
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
                "young_router.core.domains.providers_models.isolated_http_opener",
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


class LocalEditBesideARelayBacklogTests(unittest.TestCase):
    """A local provider/model edit must not inherit the relay's backlog.

    The coordination exists for a draft that actually binds relay material.
    Dragging every providers_models edit into the relay transaction made an
    ordinary ＋ (a new model on an independent provider) report the relay's own
    pending key work as a validation failure of that edit.
    """

    def _providers(self) -> dict[str, object]:
        return {
            "providers": [
                {
                    "name": "independent",
                    "api_base": "https://api.example.test/v1",
                    "api_keys": [{"name": "primary", "value": "secret", "source": {"kind": "independent"}}],
                    "models": [],
                }
            ]
        }

    def test_projection_ignores_a_local_only_edit_and_keeps_relay_bindings(self) -> None:
        before = {
            "providers": [
                {
                    "name": "independent",
                    "api_keys": [{"name": "primary", "value": "secret", "source": {"kind": "independent"}}],
                    "models": [{"model_name": "one", "api_key_name": "primary"}],
                },
                {
                    "name": "relay",
                    "api_keys": [
                        {
                            "id": "slot-1",
                            "name": "station-key",
                            "value": "",
                            "source": {
                                "kind": "relay",
                                "station_id": "station-1",
                                "account_id": "account-1",
                                "resource_id": "resource-1",
                            },
                        }
                    ],
                    "models": [{"model_name": "linked", "api_key_name": "station-key", "provider_key_id": "slot-1"}],
                },
            ]
        }
        local_edit = copy.deepcopy(before)
        local_edit["providers"][0]["models"].append({"model_name": "新建模型", "api_key_name": ""})
        local_edit["providers"][0]["name"] = "renamed"
        self.assertEqual(
            _relay_binding_projection(before),
            _relay_binding_projection(local_edit),
        )

        relay_key_edit = copy.deepcopy(before)
        relay_key_edit["providers"][1]["api_keys"][0]["source"]["resource_id"] = "resource-2"
        self.assertNotEqual(
            _relay_binding_projection(before),
            _relay_binding_projection(relay_key_edit),
        )

        bound_model = copy.deepcopy(before)
        bound_model["providers"][1]["models"].append(
            {"model_name": "another", "api_key_name": "station-key", "provider_key_id": "slot-1"}
        )
        self.assertNotEqual(
            _relay_binding_projection(before),
            _relay_binding_projection(bound_model),
        )

    def test_a_local_model_edit_applies_while_the_relay_is_not_ready(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            http = RelayCoordinatorHTTP()
            relay = RelayAccountsDomain(root, http_client=http)
            providers = ProvidersModelsDomain(root / "config.yaml")
            core = CoreStore(domains=[relay, providers])
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.add",
                    "payload": {
                        "provider": {
                            "name": "independent",
                            "enabled": True,
                            "api_base": "https://api.example.test/v1",
                        }
                    },
                },
                expected_revision=core.revision,
            )
            # The relay cannot finish its own work here; that backlog is not
            # this edit's business.
            with patch.object(RelayAccountsDomain, "prepare_apply", return_value={"ready": False, "issues": []}):
                core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "model.add",
                        "payload": {
                            "provider_id": "independent",
                            "model": {"name": "新建模型", "upstream_model": "新建模型", "enabled": False, "order": 0},
                        },
                    },
                    expected_revision=core.revision,
                )
                result = core.apply(domains=["providers_models"], revision=core.revision)

            self.assertTrue(result["applied"])
            self.assertEqual("applied", result["status"])
            self.assertEqual(["providers_models"], result["domains"])

    def test_a_relay_bound_edit_still_states_the_relay_itself(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            core, relay, providers, _http, account_id, resource_id = RelayApplyCoordinatorIntegrationTests()._linked_core(root)
            self.assertTrue(providers.dependency_summary()["provider_key_count"] > 0)
            with patch.object(RelayAccountsDomain, "prepare_apply", return_value={"ready": False, "issues": []}):
                try:
                    core.apply(domains=["providers_models", "relay_accounts"], revision=core.revision)
                except CoreError as exc:
                    self.assertIn(exc.code, {"relay_not_ready", "relay_preflight_failed"})
                else:
                    self.fail("the relay's own backlog must be reported as a relay failure")
            del account_id, resource_id, relay


if __name__ == "__main__":
    unittest.main()
