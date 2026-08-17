from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from litellm_menu.core.domains.providers_models import DomainError, ProvidersModelsDomain


def relay_source(
    *,
    station_id: str = "station-a",
    account_id: str = "account-a",
    resource_id: str = "resource-a",
    provider_name: str = "relay-station-a",
    models: list[str] | None = None,
    multiplier: float | None = 1.25,
) -> dict[str, object]:
    source: dict[str, object] = {
        "station_id": station_id,
        "account_id": account_id,
        "resource_id": resource_id,
        "provider_name": provider_name,
        "api_base": "https://relay.example.test/v1",
        "enabled": True,
        "name": "Relay Key",
        "models": models or ["upstream-chat"],
    }
    if multiplier is not None:
        source["multiplier"] = multiplier
    return source


class RelayModelBindingTests(unittest.TestCase):
    def test_model_selects_matching_relay_resource_atomically_and_keeps_key_types_exclusive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://relay.example.test/v1",
                        "api_keys": [
                            {
                                "name": "independent",
                                "value": "sk-independent-fixture",
                            }
                        ],
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            added = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "public-chat",
                        "upstream_model": "upstream-chat",
                        "api_key_name": "independent",
                    },
                },
            )
            model_id = added["providers"][0]["models"][0]["id"]

            selected = domain.dispatch(
                "model.select_relay_resource",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "source": relay_source(),
                },
            )
            summary = selected["operation_summary"]
            self.assertEqual("model_relay_key_selected", summary["operation"])
            provider = selected["providers"][0]
            self.assertEqual(
                {"independent", "relay"},
                {key["source"]["kind"] for key in provider["key_states"]},
            )
            relay_key = next(
                key for key in provider["key_states"] if key["source"]["kind"] == "relay"
            )
            self.assertFalse(relay_key["configured"])
            model = provider["models"][0]
            self.assertEqual(relay_key["id"], model["provider_key_id"])
            self.assertEqual(relay_key["name"], model["api_key_name"])
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertNotIn("sk-independent-fixture", json.dumps(selected))

            repeated = domain.dispatch(
                "model.select_relay_resource",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "source": relay_source(),
                },
            )
            self.assertTrue(repeated["operation_summary"]["reused"])
            self.assertEqual(2, len(repeated["providers"][0]["key_states"]))

    def test_model_relay_resource_selection_rejects_a_different_base_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://independent.example.test/v1",
                        "api_keys": [{"name": "independent", "value": "fixture"}],
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            model_id = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "public-chat",
                        "upstream_model": "upstream-chat",
                        "api_key_name": "independent",
                    },
                },
            )["providers"][0]["models"][0]["id"]
            before = domain.snapshot()

            with self.assertRaisesRegex(DomainError, "does not match the provider Base URL"):
                domain.dispatch(
                    "model.select_relay_resource",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "source": relay_source(),
                    },
                )

            self.assertEqual(before, domain.snapshot())

    def test_provider_relay_key_import_is_idempotent_and_secret_free(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_base": "https://independent.example.test/v1",
                        "api_keys": [],
                        "models": [],
                    }
                },
            )["providers"][0]
            source = relay_source()
            imported = domain.dispatch(
                "provider.import_relay_key",
                {
                    "provider_id": provider["id"],
                    "source": source,
                    "api_key_name": "Relay Key",
                },
            )
            summary = imported["operation_summary"]
            self.assertTrue(summary["imported"])
            self.assertFalse(summary["reused"])
            self.assertTrue(summary["slot_id"].startswith("provider-slot-"))
            self.assertNotIn("replace-materialized-credential", json.dumps(imported))
            staged_provider = imported["providers"][0]
            self.assertEqual("https://independent.example.test/v1", staged_provider["api_base"])
            self.assertEqual(1, len(staged_provider["key_states"]))
            key = staged_provider["key_states"][0]
            self.assertFalse(key["configured"])
            self.assertEqual("relay", key["source"]["kind"])
            self.assertEqual("station-a", key["source"]["station_id"])
            with self.assertRaisesRegex(DomainError, "managed by its source"):
                domain.dispatch(
                    "provider.key_patch",
                    {
                        "provider_id": provider["id"],
                        "old_name": key["name"],
                        "name": "renamed-relay-key",
                    },
                )
            with self.assertRaisesRegex(DomainError, "managed by its source"):
                domain.stage_secret(
                    "api_key",
                    f'{provider["id"]}\x1f{key["name"]}',
                    "sk-replace-should-not-stage",
                )

            repeated = domain.dispatch(
                "provider.import_relay_key",
                {
                    "provider_id": provider["id"],
                    "source": source,
                    "api_key_name": "Renamed upstream key must not rename the slot",
                },
            )
            repeated_summary = repeated["operation_summary"]
            self.assertFalse(repeated_summary["imported"])
            self.assertTrue(repeated_summary["reused"])
            self.assertEqual(summary["slot_id"], repeated_summary["slot_id"])
            self.assertEqual(1, len(repeated["providers"][0]["key_states"]))

    def test_model_provider_key_selection_derives_relay_state_and_clears_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            provider = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "provider-a",
                        "enabled": True,
                        "api_keys": [
                            {
                                "name": "independent",
                                "value": "sk-independent-fixture",
                            }
                        ],
                        "models": [],
                    }
                },
            )["providers"][0]
            provider_id = provider["id"]
            added = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "public-chat",
                        "upstream_model": "upstream-chat",
                        "api_key_name": "independent",
                        "order": 7,
                    },
                },
            )
            model_id = added["providers"][0]["models"][0]["id"]
            imported = domain.dispatch(
                "provider.import_relay_key",
                {
                    "provider_id": provider_id,
                    "source": relay_source(),
                    "api_key_name": "Relay Key",
                },
            )
            relay_key_id = imported["operation_summary"]["slot_id"]
            key_states = imported["providers"][0]["key_states"]
            independent_key_id = next(
                item["id"] for item in key_states if item["source"]["kind"] == "independent"
            )

            linked = domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"provider_key_id": relay_key_id},
                },
            )["providers"][0]["models"][0]
            self.assertEqual(relay_key_id, linked["provider_key_id"])
            self.assertEqual("relay_linked", linked["catalog_mode"])
            self.assertEqual("upstream-chat", linked["source_model_id"])
            self.assertEqual("linked", linked["binding_health"]["status"])

            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"order_mode": "relay_multiplier"},
                },
            )
            independent = domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": model_id,
                    "changes": {"provider_key_id": independent_key_id},
                },
            )["providers"][0]["models"][0]
            self.assertEqual("independent", independent["catalog_mode"])
            self.assertEqual("", independent["source_model_id"])
            self.assertEqual("independent", independent["binding_health"]["status"])
            self.assertEqual("manual", independent["order_mode"])
            self.assertEqual(7, independent["effective_order"])
            with self.assertRaises(DomainError):
                domain.dispatch(
                    "model.patch",
                    {
                        "provider_id": provider_id,
                        "model_id": model_id,
                        "changes": {"order_mode": "relay_multiplier"},
                    },
                )

    def test_legacy_model_relay_fields_are_read_but_provider_key_source_wins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(
                "\n".join(
                    [
                        "providers:",
                        "  provider-a:",
                        '    api_base: "https://relay.example.test/v1"',
                        "    api_keys:",
                        "      - name: relay-key",
                        '        value: "sk-legacy-fixture"',
                        "    x-litellm-menu-relay-keys:",
                        "      version: 1",
                        "      slots:",
                        "        - id: provider-slot-legacy",
                        "          api_key_name: relay-key",
                        "          source:",
                        "            kind: relay",
                        "            station_id: station-a",
                        "            account_id: account-a",
                        "            resource_id: resource-a",
                        "model_list:",
                        "  - model_name: public-chat",
                        "    litellm_params:",
                        "      model: openai/upstream-chat",
                        '      api_base: "https://relay.example.test/v1"',
                        '      api_key: "sk-legacy-fixture"',
                        "      order: 3",
                        "    model_info:",
                        '      id: "00000001"',
                        "      provider: provider-a",
                        "      api_key_name: relay-key",
                        "      x-litellm-menu-provider-key-id: provider-slot-legacy",
                        "      x-litellm-menu-relay-catalog-mode: independent",
                        "      x-litellm-menu-relay-source-model: stale-upstream",
                        "      x-litellm-menu-order-mode: manual",
                        "      x-litellm-menu-manual-order: 3",
                        "",
                    ]
                ),
                encoding="utf-8",
            )
            domain = ProvidersModelsDomain(path)
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertEqual("upstream-chat", model["source_model_id"])
            self.assertEqual("linked", model["binding_health"]["status"])

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("x-litellm-menu-relay-catalog-mode", saved)
            self.assertNotIn("x-litellm-menu-relay-source-model", saved)

    def test_linked_import_preflight_materializes_multiplier_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            source = relay_source()
            source["order_mode"] = "relay_multiplier"
            source["manual_order"] = 7

            imported = domain.stage_relay_import([source])
            self.assertEqual("linked", imported["operation_summary"]["import_mode"])
            self.assertFalse(domain.validate()["valid"])
            self.assertTrue(domain.validate_relay_preflight()["valid"])

            before = imported["providers"][0]
            key_state = before["key_states"][0]
            self.assertEqual("relay", key_state["source"]["kind"])
            self.assertTrue(key_state["id"].startswith("provider-slot-"))
            self.assertNotIn("replace-materialized-credential", json.dumps(imported))

            materialized = domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-materialized-credential",
                            "multiplier": 1.5,
                        }
                    ]
                }
            )
            self.assertEqual([], materialized["issues"])
            self.assertEqual(1, materialized["materialized"])
            self.assertNotIn("replace-materialized-credential", json.dumps(materialized))
            self.assertTrue(domain.validate()["valid"])
            domain.apply()

            saved = path.read_text(encoding="utf-8")
            self.assertIn("x-litellm-menu-relay-keys", saved)
            self.assertIn("x-litellm-menu-provider-key-id", saved)
            self.assertIn("x-litellm-menu-order-mode: relay_multiplier", saved)
            self.assertNotIn("x-litellm-menu-relay-catalog-mode", saved)
            self.assertNotIn("x-litellm-menu-relay-source-model", saved)

            reloaded_domain = ProvidersModelsDomain(path)
            reloaded = reloaded_domain.snapshot()["providers"][0]
            model = reloaded["models"][0]
            self.assertEqual("relay_linked", model["catalog_mode"])
            self.assertEqual("upstream-chat", model["source_model_id"])
            self.assertEqual("relay_multiplier", model["order_mode"])
            self.assertEqual(7, int(model["manual_order"]))
            self.assertEqual(1.5, model["effective_order"])
            self.assertEqual(1.5, model["order"])
            self.assertEqual("linked", model["binding_health"]["status"])
            self.assertNotIn("replace-materialized-credential", json.dumps(reloaded))

            rematerialized = reloaded_domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-rematerialized-credential",
                            "multiplier": 1.75,
                        }
                    ]
                }
            )
            self.assertEqual([], rematerialized["issues"])
            self.assertNotIn("replace-rematerialized-credential", json.dumps(rematerialized))

    def test_materialization_reports_catalog_and_multiplier_problems_without_secret(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source(models=["upstream-chat"])
            source["order_mode"] = "relay_multiplier"
            domain.stage_relay_import([source])

            result = domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-materialized-credential",
                            "models": ["different-model"],
                        }
                    ]
                }
            )

            self.assertEqual({"catalog_model_missing"}, {item["code"] for item in result["issues"]})
            self.assertNotIn("replace-materialized-credential", json.dumps(result))

            domain = ProvidersModelsDomain(Path(directory) / "second.yaml")
            domain.stage_relay_import([source])
            result = domain.materialize_relay_bindings(
                {
                    "resources": [
                        {
                            **source,
                            "api_key": "replace-materialized-credential",
                            "multiplier": None,
                        }
                    ]
                }
            )
            self.assertEqual({"multiplier_missing"}, {item["code"] for item in result["issues"]})
            self.assertNotIn("replace-materialized-credential", json.dumps(result))

    def test_dependency_policy_detaches_or_disables_without_reusing_relay_key(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            source = relay_source()
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            dependency = domain.dependency_summary(
                {
                    "station_id": "station-a",
                    "account_id": "account-a",
                    "resource_id": "resource-a",
                }
            )
            self.assertEqual(1, dependency["provider_key_count"])
            self.assertEqual(1, dependency["model_count"])

            detached = domain.apply_relay_dependency_policy(
                [
                    {
                        "station_id": "station-a",
                        "account_id": "account-a",
                        "resource_id": "resource-a",
                    }
                ],
                "detach",
            )
            self.assertEqual([], detached["issues"])
            self.assertEqual(1, detached["detached_models"])
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("independent", model["catalog_mode"])
            self.assertEqual("independent", model["binding_health"]["status"])
            self.assertEqual(0, domain.dependency_summary()["model_count"])

            # A fresh relay binding demonstrates the remote-delete policy:
            # it removes the soon-to-be-revoked credential rather than cloning
            # it into an apparently usable independent deployment.
            domain = ProvidersModelsDomain(Path(directory) / "disabled.yaml")
            domain.stage_relay_import([source])
            domain.materialize_relay_bindings(
                {"resources": [{**source, "api_key": "replace-materialized-credential"}]}
            )
            disabled = domain.apply_relay_dependency_policy([source], "detach_disabled")
            self.assertEqual([], disabled["issues"])
            self.assertEqual(1, disabled["disabled_detached_models"])
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertFalse(model["enabled"])
            self.assertEqual("", model["provider_key_id"])
            self.assertEqual("credential_required", model["binding_health"]["status"])
            self.assertNotIn("replace-materialized-credential", json.dumps(domain.snapshot()))


if __name__ == "__main__":
    unittest.main()
