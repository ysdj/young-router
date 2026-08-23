from __future__ import annotations

import json
import importlib.util
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import os
from pathlib import Path
import stat
import sys
import tempfile
import textwrap
import threading
import unittest
from unittest import mock

from litellm_menu.core.domains import DomainError
from litellm_menu.core.domains.codex import CodexSettingsDomain
from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.domains.relay_accounts import RelayAccountsDomain
from litellm_menu.core.domains.runtime import RuntimeSettingsDomain
from litellm_menu.core.domains.webdav import WebDAVSettingsDomain
from litellm_menu.core.model_catalog import catalog_is_current
from litellm_menu.core.service import CoreError, CoreStore
from runtime_settings_io import RuntimeSettingSpec


PROVIDER_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
    future_provider_field:
      keep: true
model_list:
  - model_name: default-chat
    litellm_params:
      model: openai/default-chat
      api_base: "https://example.test/v1"
      api_key: "replace-me-secret"
      future_param: keep
    model_info:
      id: "00000071"
      provider: primary
      upstream_url_surface: openai/responses
      supported_upstream_url_surfaces: [openai/responses]
      future_info: keep
litellm_settings:
  public_model_groups: [default-chat]
future_top_level:
  keep: true
"""


class ProvidersModelsDomainTests(unittest.TestCase):
    def test_account_login_provider_stages_adapter_and_private_claude_token(self) -> None:
        from litellm_menu.core.provider_auth import ProviderAuthManager

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / "config.yaml"
            path.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            domain = ProvidersModelsDomain(path, auth_manager=ProviderAuthManager(root))

            snapshot = domain.dispatch(
                "service_provider.add",
                {
                    "provider": {
                        "name": "account-provider",
                        "auth_kind": "openai_login",
                    }
                },
            )
            provider = snapshot["providers"][0]
            self.assertEqual("openai_login", provider["auth_kind"])
            self.assertEqual([], provider["api_key_names"])
            self.assertEqual("signed_out", provider["auth_status"])

            snapshot = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider["id"],
                    "model": {"name": "gpt-5.4", "upstream_model": "gpt-5.4"},
                },
            )
            model = snapshot["providers"][0]["models"][0]
            self.assertEqual("chatgpt/gpt-5.4", model["litellm_model"])
            self.assertEqual("openai/responses", model["upstream_url_surface"])
            self.assertEqual("fixed", model["upstream_protocol_mode"])

            snapshot = domain.dispatch(
                "service_provider.add",
                {
                    "provider": {
                        "name": "account-claude",
                        "auth_kind": "claude_login",
                    }
                },
            )
            provider = next(item for item in snapshot["providers"] if item["name"] == "account-claude")
            self.assertEqual("claude_login", provider["auth_kind"])
            self.assertEqual(["claude-oauth"], provider["api_key_names"])
            self.assertEqual("anthropic/claude-sonnet-4-5", provider["models"][0]["litellm_model"])
            token = "sk-ant-oat" + "synthetic0" * 4
            domain.stage_secret("provider_auth_token", provider["id"], token)
            signed_in = next(
                item
                for item in domain.snapshot()["providers"]
                if item["name"] == "account-claude"
            )
            self.assertEqual("signed_in", signed_in["auth_status"])
            self.assertNotIn(token, json.dumps(signed_in))

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertIn("os.environ/LITELLM_MENU_AUTH_", saved)
            self.assertNotIn(token, saved)

    def test_relay_provider_source_sets_name_and_url_atomically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config_path = root / "config.yaml"
            config_path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            relay = RelayAccountsDomain(root / "relay")
            station = relay.dispatch(
                "station.add",
                {
                    "name": "Relay Station",
                    "origin": "https://relay.example.test",
                    "type": "newapi",
                },
            )["stations"][0]
            providers = ProvidersModelsDomain(config_path)
            core = CoreStore(domains=[relay, providers])
            provider_id = core.snapshot()["domains"]["providers_models"]["providers"][0]["editor_id"]

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.select_relay_station",
                    "payload": {"provider_id": provider_id, "station_id": station["id"]},
                }
            )

            selected = core.snapshot()["domains"]["providers_models"]["providers"][0]
            self.assertEqual("Relay Station", selected["name"])
            self.assertEqual("https://relay.example.test", selected["api_base"])
            self.assertEqual("relay", selected["provider_type"])
            self.assertEqual(station["id"], selected["relay_station_id"])
            self.assertEqual("Relay Station", selected["models"][0]["provider"])
            self.assertEqual("https://relay.example.test", selected["models"][0]["api_base"])

            with self.assertRaisesRegex(CoreError, "set by its station"):
                core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "provider.patch",
                        "payload": {"provider_id": provider_id, "changes": {"name": "mixed"}},
                    }
                )
            with self.assertRaisesRegex(CoreError, "Select a relay station"):
                core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "provider.patch",
                        "payload": {
                            "provider_id": provider_id,
                            "changes": {"provider_type": "relay", "relay_station_id": station["id"]},
                        },
                    }
                )

            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {
                        "provider_id": provider_id,
                        "changes": {"provider_type": "custom", "relay_station_id": ""},
                    },
                }
            )
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {
                        "provider_id": provider_id,
                        "changes": {
                            "provider_type": "custom",
                            "relay_station_id": "",
                            "extra": {
                                "x-litellm-menu-provider-source": {
                                    "kind": "relay",
                                    "station_id": station["id"],
                                }
                            },
                        },
                    },
                }
            )
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {
                        "provider_id": provider_id,
                        "changes": {"name": "Custom Provider", "endpoint": "https://custom.example.test/v1"},
                    },
                }
            )
            custom = core.snapshot()["domains"]["providers_models"]["providers"][0]
            self.assertEqual("custom", custom["provider_type"])
            self.assertEqual("", custom["relay_station_id"])
            self.assertEqual(
                {"kind": "custom"},
                custom["extra"]["x-litellm-menu-provider-source"],
            )
            self.assertEqual("Custom Provider", custom["name"])
            self.assertEqual("https://custom.example.test/v1", custom["api_base"])
            self.assertEqual("Custom Provider", custom["models"][0]["provider"])
            self.assertEqual("https://custom.example.test/v1", custom["models"][0]["api_base"])

            providers.apply()
            saved = config_path.read_text(encoding="utf-8")
            self.assertIn("x-litellm-menu-provider-source: {kind: custom}", saved)
            reloaded = ProvidersModelsDomain(config_path).snapshot()["providers"][0]
            self.assertEqual("custom", reloaded["provider_type"])
            self.assertEqual("", reloaded["relay_station_id"])

    def test_canonical_actions_stage_and_apply_without_exposing_secrets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            snapshot = domain.snapshot()
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn(str(path), json.dumps(snapshot))
            domain.dispatch("provider.patch", {"provider_id": "primary", "changes": {"endpoint": "https://example.com/v1"}})
            domain.dispatch("model.patch", {"provider_id": "primary", "model_id": "00000071", "changes": {"upstream_model": "openai/fast-chat"}})
            domain.dispatch("provider.add", {"provider": {"name": "backup", "enabled": True, "models": []}})
            domain.dispatch("provider.move", {"provider_id": "backup", "direction": "up"})
            self.assertTrue(domain.validate()["valid"])

            result = domain.apply()

            self.assertTrue(result["applied"])
            saved = path.read_text(encoding="utf-8")
            self.assertIn("future_top_level", saved)
            self.assertIn("future_param", saved)
            self.assertIn("future_info", saved)
            self.assertIn("openai/fast-chat", saved)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_core_snapshot_keeps_api_key_labels_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")

            snapshot = CoreStore(domains=[ProvidersModelsDomain(path)]).snapshot()
            provider = snapshot["domains"]["providers_models"]["providers"][0]

            self.assertEqual(["default"], provider["api_key_names"])
            self.assertEqual("default", provider["models"][0]["api_key_name"])
            self.assertEqual(1, len(provider["key_states"]))
            key_state = provider["key_states"][0]
            self.assertEqual("default", key_state["name"])
            self.assertTrue(key_state["configured"])
            self.assertEqual(1, key_state["model_count"])
            self.assertRegex(key_state["id"], r"^provider-slot-[0-9a-f]{32}$")
            self.assertEqual({"kind": "independent"}, key_state["source"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))

    def test_provider_model_contract_has_no_upstream_billing_state_or_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            model = domain.snapshot()["providers"][0]["models"][0]

            self.assertNotIn("billing", model)
            self.assertNotIn("multiplier", model)
            with self.assertRaisesRegex(DomainError, "action is unavailable"):
                domain.dispatch("providers.refresh_multiplier")

    def test_model_api_key_configured_requires_the_selected_named_key_value(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch("provider.key_add", {"provider_id": "primary", "name": "secondary"})
            empty = domain.dispatch(
                "model.patch",
                {"provider_id": "primary", "model_id": "00000071", "changes": {"api_key_name": "secondary", "api_key": ""}},
            )
            empty_model = empty["providers"][0]["models"][0]
            self.assertIs(empty_model["api_key_configured"], False)
            self.assertEqual(False, empty["providers"][0]["key_states"][1]["configured"])

            domain.stage_secret("api_key", "primary\x1fsecondary", "replace-me-secondary-secret")
            configured = domain.snapshot()
            configured_model = configured["providers"][0]["models"][0]
            self.assertIs(configured_model["api_key_configured"], True)
            self.assertEqual(True, configured["providers"][0]["key_states"][1]["configured"])

            domain.dispatch(
                "model.patch",
                {"provider_id": "primary", "model_id": "00000071", "changes": {"api_key_name": "missing", "api_key": ""}},
            )
            missing_model = domain.snapshot()["providers"][0]["models"][0]
            self.assertIs(missing_model["api_key_configured"], False)

    def test_apply_refuses_an_external_disk_change(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            source = textwrap.dedent(PROVIDER_CONFIG).lstrip()
            path.write_text(source, encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch("provider.patch", {"provider_id": "primary", "changes": {"enabled": False}})
            path.write_text(source + "external_change: true\n", encoding="utf-8")

            with self.assertRaisesRegex(DomainError, "changed on disk"):
                domain.apply()

    def test_first_apply_creates_private_config_and_rejects_concurrent_target(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "nested" / "config.yaml"
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.add",
                {"provider": {"name": "primary", "api_base": "https://example.test/v1", "enabled": True, "models": []}},
            )
            domain.apply()

            self.assertTrue(path.is_file())
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
            self.assertEqual(0o700, stat.S_IMODE(path.parent.stat().st_mode))

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            domain = ProvidersModelsDomain(path)
            domain.dispatch("provider.add", {"provider": {"name": "primary", "models": []}})
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")

            with self.assertRaisesRegex(DomainError, "changed on disk"):
                domain.apply()

    def test_new_provider_gets_a_core_owned_blank_named_key_slot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            snapshot = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "primary",
                        "models": [],
                        "create_default_api_key": True,
                    }
                },
            )

            provider = snapshot["providers"][0]
            self.assertEqual(["default"], provider["api_key_names"])
            self.assertFalse(provider["api_key_configured"])
            self.assertNotIn("value", json.dumps(snapshot))
            self.assertFalse(domain.validate()["valid"])

    def test_new_draft_ids_stay_stable_across_move_and_order_remains_numeric(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            domain.dispatch("provider.add", {"provider": {"name": "", "models": []}})
            first = domain.snapshot()["providers"][0]["id"]
            domain.dispatch("provider.add", {"provider": {"name": "backup", "models": []}})
            domain.dispatch("provider.move", {"provider_id": first, "direction": "down"})
            moved = next(item for item in domain.snapshot()["providers"] if not item["name"])
            domain.dispatch("model.add", {"provider_id": first, "model": {"name": "", "order": 2}})
            model = next(item for item in domain.snapshot()["providers"] if not item["name"])["models"][0]

            self.assertEqual(first, moved["id"])
            self.assertTrue(model["editor_id"].startswith("model-"))
            self.assertRegex(model["id"], r"^[0-9a-f]{8}$")
            self.assertEqual(2, model["order"])

    def test_new_models_default_to_zero_order_and_fetch_batches_share_one_add_action(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]

            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {"name": "manual", "upstream_model": "manual", "api_key_name": "default"},
                },
            )
            batch = domain.dispatch(
                "model.add_many",
                {
                    "provider_id": provider_id,
                    "models": [
                        {"name": "fetched-one", "upstream_model": "fetched-one", "api_key_name": "default"},
                        {"name": "fetched-two", "upstream_model": "fetched-two", "api_key_name": "default"},
                    ],
                },
            )

            models = batch["providers"][0]["models"]
            added = [model for model in models if model["name"] in {"manual", "fetched-one", "fetched-two"}]
            self.assertEqual(3, len(added))
            self.assertEqual([0, 0, 0], [model["order"] for model in added])
            self.assertEqual(3, len({model["editor_id"] for model in added}))

    def test_new_model_uses_public_name_as_upstream_when_route_is_blank(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]

            added = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {"name": "draft-chat"},
                },
            )["providers"][0]["models"][-1]
            self.assertEqual("draft-chat", added["name"])
            self.assertEqual("draft-chat", added["upstream_model"])
            self.assertEqual("openai/draft-chat", added["litellm_model"])

            blank_route = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {"name": "empty-route", "litellm_model": ""},
                },
            )["providers"][0]["models"][-1]
            self.assertEqual("empty-route", blank_route["upstream_model"])

    def test_model_name_patch_fills_only_an_empty_upstream_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]
            added = domain.dispatch(
                "model.add",
                {"provider_id": provider_id, "model": {"name": ""}},
            )["providers"][0]["models"][-1]

            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": added["editor_id"],
                    "changes": {"name": "named-route"},
                },
            )
            patched = domain.snapshot()["providers"][0]["models"][-1]
            self.assertEqual("named-route", patched["name"])
            self.assertEqual("named-route", patched["upstream_model"])

            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": patched["editor_id"],
                    "changes": {"upstream_model": "different-route"},
                },
            )
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": provider_id,
                    "model_id": patched["editor_id"],
                    "changes": {"name": "renamed-route"},
                },
            )
            explicit = domain.snapshot()["providers"][0]["models"][-1]
            self.assertEqual("renamed-route", explicit["name"])
            self.assertEqual("different-route", explicit["upstream_model"])

    def test_provider_and_model_editor_ids_survive_in_place_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            before = domain.snapshot()["providers"][0]
            provider_id = before["editor_id"]
            model_id = before["models"][0]["editor_id"]

            domain.dispatch(
                "provider.patch",
                {"provider_id": provider_id, "changes": {"endpoint": "https://changed.example.test/v1"}},
            )
            domain.dispatch(
                "model.patch",
                {"provider_id": provider_id, "model_id": model_id, "changes": {"upstream_model": "new-upstream"}},
            )

            after = domain.snapshot()["providers"][0]
            self.assertEqual(provider_id, after["editor_id"])
            self.assertEqual(model_id, after["models"][0]["editor_id"])
            self.assertEqual("new-upstream", after["models"][0]["upstream_model"])

            domain.apply()

            applied = domain.snapshot()["providers"][0]
            self.assertEqual(provider_id, applied["editor_id"])
            self.assertEqual(model_id, applied["models"][0]["editor_id"])

    def test_new_model_editor_id_survives_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]

            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "alternate-chat",
                        "upstream_model": "alternate-chat",
                        "api_key_name": "default",
                        "order": 2,
                        "enabled": True,
                        "upstream_url_surface": "openai/responses",
                        "supported_upstream_url_surfaces": ["openai/responses"],
                    },
                },
            )
            added_id = domain.snapshot()["providers"][0]["models"][1]["editor_id"]

            domain.apply()

            applied = domain.snapshot()["providers"][0]
            self.assertEqual(provider_id, applied["editor_id"])
            self.assertIn(added_id, [model["editor_id"] for model in applied["models"]])

    def test_new_claude_model_defaults_to_anthropic_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]

            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "claude-sonnet-4-5",
                        "upstream_model": "claude-sonnet-4-5",
                        "api_key_name": "default",
                        "order": 2,
                        "enabled": True,
                    },
                },
            )

            model = domain.snapshot()["providers"][0]["models"][1]
            self.assertEqual("anthropic", model["upstream_url_surface"])
            self.assertEqual("claude-sonnet-4-5", model["upstream_model"])
            self.assertEqual("anthropic/claude-sonnet-4-5", model["litellm_model"])

    def test_new_non_claude_model_defaults_to_responses_surface(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]

            domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "gpt-5",
                        "upstream_model": "gpt-5",
                        "api_key_name": "default",
                        "order": 2,
                        "enabled": True,
                    },
                },
            )

            model = domain.snapshot()["providers"][0]["models"][1]
            self.assertEqual("openai/responses", model["upstream_url_surface"])
            self.assertEqual("openai/gpt-5", model["litellm_model"])

    def test_upstream_model_is_displayed_without_prefix_and_saved_canonically(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "model.patch",
                {"provider_id": "primary", "model_id": "00000071", "changes": {"upstream_model": "plain-name"}},
            )
            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("plain-name", model["upstream_model"])
            self.assertEqual("openai/plain-name", model["litellm_model"])
            domain.apply()
            self.assertIn("openai/plain-name", path.read_text(encoding="utf-8"))

    def test_model_api_key_name_patch_is_safe_and_survives_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.patch",
                {
                    "provider_id": "primary",
                    "changes": {
                        "api_keys": [
                            {"name": "default", "value": "replace-me-secret"},
                            {"name": "secondary", "value": "replace-me-secondary-secret"},
                        ]
                    },
                },
            )
            snapshot = domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {"api_key_name": "secondary"},
                },
            )

            provider = snapshot["providers"][0]
            self.assertEqual(["default", "secondary"], provider["api_key_names"])
            self.assertEqual("secondary", provider["models"][0]["api_key_name"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn("replace-me-secondary-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()["providers"][0]
            self.assertEqual("secondary", reloaded["models"][0]["api_key_name"])

    def test_model_move_provider_uses_destination_key_without_exposing_values(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "backup",
                        "enabled": True,
                        "api_base": "https://backup.example.test/v1",
                        "api_key": "replace-me-backup-secret",
                        "api_keys": [
                            {"name": "backup-first", "value": "replace-me-backup-secret"},
                            {"name": "backup-second", "value": "replace-me-other-secret"},
                        ],
                        "models": [],
                    }
                },
            )

            snapshot = domain.dispatch(
                "model.move_provider",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "destination_provider_id": "backup",
                },
            )
            providers = {provider["name"]: provider for provider in snapshot["providers"]}
            self.assertEqual([], providers["primary"]["models"])
            self.assertEqual(["backup-first", "backup-second"], providers["backup"]["api_key_names"])
            moved = providers["backup"]["models"][0]
            self.assertEqual("backup", moved["provider"])
            self.assertEqual("", moved["api_base"])
            self.assertEqual("backup-first", moved["api_key_name"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn("replace-me-backup-secret", json.dumps(snapshot))
            self.assertNotIn("replace-me-other-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()
            reloaded_providers = {provider["name"]: provider for provider in reloaded["providers"]}
            reloaded_model = reloaded_providers["backup"]["models"][0]
            self.assertEqual("backup", reloaded_model["provider"])
            self.assertEqual("backup-first", reloaded_model["api_key_name"])

    def test_provider_key_actions_stage_values_only_through_named_secret_targets(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            pending = domain.dispatch(
                "provider.key_add",
                {"provider_id": "primary", "name": "secondary"},
            )
            self.assertEqual(["default", "secondary"], pending["providers"][0]["api_key_names"])
            self.assertFalse(domain.secret_present("api_key", "primary\x1fsecondary"))
            self.assertFalse(domain.validate()["valid"])
            self.assertNotIn("value", json.dumps(pending))

            domain.stage_secret("api_key", "primary\x1fsecondary", "replace-me-secondary-secret")
            self.assertTrue(domain.secret_present("api_key", "primary\x1fsecondary"))
            self.assertTrue(domain.secret_present("api_key", "primary"))
            self.assertTrue(domain.validate()["valid"])

            renamed = domain.dispatch(
                "provider.key_patch",
                {"provider_id": "primary", "old_name": "secondary", "name": "fallback"},
            )
            provider = renamed["providers"][0]
            self.assertEqual(["default", "fallback"], provider["api_key_names"])
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {"api_key_name": "fallback"},
                },
            )
            deleted = domain.dispatch(
                "provider.key_delete",
                {"provider_id": "primary", "name": "fallback"},
            )
            provider = deleted["providers"][0]
            self.assertEqual(["default"], provider["api_key_names"])
            self.assertEqual([], provider["models"])
            self.assertNotIn("replace-me-secondary-secret", json.dumps(deleted))

            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertNotIn("name: fallback", saved)
            self.assertNotIn("model_name: default-chat", saved)
            self.assertIn("replace-me-secret", saved)

            emptied = ProvidersModelsDomain(path).dispatch(
                "provider.key_delete",
                {"provider_id": "primary", "name": "default"},
            )
            self.assertEqual([], emptied["providers"][0]["api_key_names"])
            self.assertEqual([], emptied["providers"][0]["models"])

    def test_provider_import_link_is_staged_only_through_native_secret_input(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = ProvidersModelsDomain(Path(directory) / "config.yaml")
            link = (
                "ccswitch://v1/import?resource=provider&app=codex&name=primary"
                "&endpoint=https%3A%2F%2Fexample.test%2Fv1&apiKey=replace-me-secret"
                "&model=default-chat"
            )

            self.assertFalse(domain.secret_present("import_link"))
            domain.stage_secret("import_link", None, link)
            snapshot = domain.snapshot()
            self.assertEqual(1, snapshot["provider_count"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            self.assertNotIn(link, json.dumps(snapshot))

    def test_provider_key_rename_updates_model_references_and_named_secret_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "provider.key_add",
                {"provider_id": "primary", "name": "secondary"},
            )
            domain.stage_secret("api_key", "primary\x1fsecondary", "replace-me-secondary-secret")
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {"api_key_name": "secondary"},
                },
            )

            snapshot = domain.dispatch(
                "provider.key_patch",
                {"provider_id": "primary", "old_name": "secondary", "name": "fallback"},
            )
            provider = snapshot["providers"][0]
            self.assertEqual("fallback", provider["models"][0]["api_key_name"])
            self.assertTrue(domain.secret_present("api_key", "primary\x1ffallback"))
            with self.assertRaisesRegex(DomainError, "unavailable"):
                domain.secret_present("api_key", "primary\x1fsecondary")
            with self.assertRaisesRegex(DomainError, "already in use"):
                domain.dispatch(
                    "provider.key_patch",
                    {"provider_id": "primary", "old_name": "fallback", "name": "default"},
                )
            self.assertNotIn("replace-me-secondary-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()["providers"][0]
            self.assertEqual(["default", "fallback"], reloaded["api_key_names"])
            self.assertEqual("fallback", reloaded["models"][0]["api_key_name"])

    def test_named_provider_key_secret_uses_the_existing_core_capability_path(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            core = CoreStore(domains=[ProvidersModelsDomain(path)])
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.key_add",
                    "payload": {"provider_id": "primary", "name": "secondary"},
                }
            )

            named_target = "primary\x1fsecondary"
            descriptor = core.secret_descriptor("providers_models", "api_key", named_target)
            self.assertEqual(named_target, descriptor["target"])
            self.assertFalse(descriptor["present"])
            result = core.stage_secret(
                "providers_models",
                "api_key",
                named_target,
                "replace-me-secondary-secret",
                revision=core.revision,
            )
            self.assertTrue(result["present"])
            self.assertNotIn("replace-me-secondary-secret", json.dumps(result))

            first_key = core.secret_descriptor("providers_models", "api_key", "primary")
            named_key = core.secret_descriptor("providers_models", "api_key", named_target)
            self.assertTrue(first_key["present"])
            self.assertTrue(named_key["present"])
            self.assertNotIn("replace-me-secondary-secret", json.dumps(core.snapshot()))

    def test_model_duplicate_uses_private_draft_and_rebuilds_deployment_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            snapshot = domain.dispatch(
                "model.duplicate",
                {"provider_id": "primary", "model_id": "00000071"},
            )
            models = snapshot["providers"][0]["models"]
            self.assertEqual(2, len(models))
            self.assertEqual("00000071", models[0]["deployment_id"])
            self.assertRegex(models[1]["deployment_id"], r"^[0-9a-f]{8}$")
            self.assertNotEqual(models[0]["deployment_id"], models[1]["deployment_id"])
            self.assertNotEqual(models[0]["editor_id"], models[1]["editor_id"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))

            private_models = domain.export(include_sensitive=True)["providers"][0]["models"]
            self.assertEqual("keep", private_models[1]["litellm_extra"]["future_param"])
            self.assertEqual("keep", private_models[1]["model_info_extra"]["future_info"])
            self.assertEqual("replace-me-secret", private_models[1]["api_key"])
            domain.apply()

            reloaded = ProvidersModelsDomain(path).snapshot()["providers"][0]["models"]
            self.assertEqual(2, len(reloaded))
            self.assertEqual(2, len({model["deployment_id"] for model in reloaded}))

    def test_fetch_models_and_probe_use_generic_openai_model_endpoint(self) -> None:
        requests: list[tuple[str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append((self.path, self.headers.get("Authorization", "")))
                body = json.dumps(
                    {"object": "list", "data": [{"id": "model-b"}, {"id": "model-a"}, {"id": "model-a"}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            config = PROVIDER_CONFIG.replace("https://example.test/v1", f"http://127.0.0.1:{port}/v1")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(config).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)

                fetched = domain.dispatch("providers.fetch_models", {"provider_id": "primary"})[
                    "operation_summary"
                ]
                probed = domain.probe({"provider_id": "primary"})

            self.assertTrue(fetched["available"])
            self.assertEqual(["model-b", "model-a"], fetched["models"])
            self.assertEqual(["openai-models-v1"], fetched["protocols"])
            self.assertTrue(probed["ok"])
            self.assertEqual(["model-b", "model-a"], probed["models"])
            self.assertEqual(
                [("/v1/models", "Bearer replace-me-secret"), ("/v1/models", "Bearer replace-me-secret")],
                requests,
            )
            self.assertNotIn("replace-me-secret", json.dumps(fetched))
            self.assertNotIn("replace-me-secret", json.dumps(probed))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_fetch_models_uses_only_the_requested_named_api_key(self) -> None:
        requests: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                requests.append(self.headers.get("Authorization", ""))
                body = json.dumps({"object": "list", "data": [{"id": "model-a"}]}).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            config = PROVIDER_CONFIG.replace("https://example.test/v1", f"http://127.0.0.1:{port}/v1")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(config).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)
                domain.dispatch(
                    "provider.patch",
                    {
                        "provider_id": "primary",
                        "changes": {
                            "api_keys": [
                                {"name": "default", "value": "replace-me-default-secret"},
                                {"name": "secondary", "value": "replace-me-secondary-secret"},
                            ]
                        },
                    },
                )

                fetched = domain.dispatch(
                    "providers.fetch_models",
                    {"provider_id": "primary", "api_key_name": "secondary"},
                )["operation_summary"]

                self.assertEqual("secondary", fetched["api_key_name"])
                self.assertEqual(["model-a"], fetched["models"])
                self.assertNotIn("replace-me-secondary-secret", json.dumps(fetched))
                with self.assertRaisesRegex(DomainError, "selected API key is unavailable"):
                    domain.dispatch(
                        "providers.fetch_models",
                        {"provider_id": "primary", "api_key_name": "missing"},
                    )

            self.assertEqual(["Bearer replace-me-secondary-secret"], requests)
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_model_probe_checks_all_protocols_in_one_action(self) -> None:
        requests: list[tuple[str, str, str]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                requests.append(
                    (
                        self.path,
                        self.headers.get("Authorization", self.headers.get("x-api-key", "")),
                        str(payload.get("model", "")),
                    )
                )
                body = json.dumps(
                    {"id": "response-1", "output": []}
                    if self.path == "/v1/responses"
                    else {"content": [{"type": "text", "text": "OK"}]}
                    if self.path == "/v1/messages"
                    else {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
                ).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            config = PROVIDER_CONFIG.replace("https://example.test/v1", f"http://127.0.0.1:{port}/v1")
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(config).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)
                result = domain.probe({"provider_id": "primary", "model_id": "00000071"})
                model = domain.snapshot()["providers"][0]["models"][0]

            self.assertTrue(result["available"])
            self.assertEqual("openai/chat", result["recommended_surface"])
            self.assertEqual(["openai/responses", "openai/chat", "anthropic"], result["protocols"])
            self.assertTrue(model["probe"]["available"])
            self.assertTrue(model["probe"]["surfaces"]["openai/responses"]["available"])
            self.assertTrue(model["model_enabled"])
            self.assertEqual("openai/responses", model["upstream_url_surface"])
            self.assertNotIn("supported_upstream_url_surfaces", model)
            self.assertEqual(
                [
                    ("/v1/chat/completions", "Bearer replace-me-secret", "default-chat"),
                    ("/v1/messages", "replace-me-secret", "default-chat"),
                    ("/v1/responses", "Bearer replace-me-secret", "default-chat"),
                ],
                sorted(requests),
            )
            self.assertNotIn("replace-me-secret", json.dumps(result))
            self.assertNotIn("replace-me-secret", json.dumps(model))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_model_probe_reports_recommendation_without_staging_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            domain.dispatch(
                "model.patch",
                {
                    "provider_id": "primary",
                    "model_id": "00000071",
                    "changes": {
                        "model_enabled": False,
                        "upstream_url_surface": "openai/chat",
                    },
                },
            )
            domain.apply()
            core = CoreStore(domains=[domain])
            saved_before_probe = path.read_text(encoding="utf-8")

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": surface == "openai/responses", "status": "ok" if surface == "openai/responses" else "unsupported"}

            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                result = core.probe(
                    {"provider_id": "primary", "model_id": "00000071"},
                    domain="providers_models",
                )

            model = core.snapshot()["domains"]["providers_models"]["providers"][0]["models"][0]
            self.assertTrue(result["ok"])
            self.assertEqual("openai/responses", result["recommended_surface"])
            self.assertFalse(model["model_enabled"])
            self.assertEqual("openai/chat", model["upstream_url_surface"])
            self.assertNotIn("supported_upstream_url_surfaces", model)
            self.assertFalse(core.snapshot()["drafts"]["providers_models"]["dirty"])
            self.assertEqual(saved_before_probe, path.read_text(encoding="utf-8"))

    def test_model_probes_are_independent_and_do_not_lock_provider_edits(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            first = domain.snapshot()["providers"][0]
            provider_id = first["editor_id"]
            first_model_id = first["models"][0]["editor_id"]
            added = domain.dispatch(
                "model.add",
                {
                    "provider_id": provider_id,
                    "model": {
                        "name": "second-chat",
                        "upstream_model": "second-chat",
                        "order": 2,
                        "upstream_url_surface": "openai/responses",
                    },
                },
            )
            second_model_id = added["providers"][0]["models"][1]["editor_id"]
            core = CoreStore(domains=[domain])
            first_started = threading.Event()
            both_started = threading.Event()
            release = threading.Event()
            started_models: set[str] = set()
            started_lock = threading.Lock()

            def surface_probe(*, model_name: str, surface: str, **_kwargs: object) -> dict[str, object]:
                with started_lock:
                    started_models.add(model_name)
                    if model_name == "default-chat":
                        first_started.set()
                    if {"default-chat", "second-chat"}.issubset(started_models):
                        both_started.set()
                self.assertTrue(release.wait(timeout=3))
                return {"surface": surface, "available": surface == "openai/responses", "status": "ok"}

            results: dict[str, dict[str, object]] = {}

            def run_probe(key: str, model_id: str) -> None:
                results[key] = core.probe(
                    {"provider_id": provider_id, "model_id": model_id},
                    domain="providers_models",
                )

            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                first_thread = threading.Thread(target=run_probe, args=("first", first_model_id), daemon=True)
                second_thread = threading.Thread(target=run_probe, args=("second", second_model_id), daemon=True)
                first_thread.start()
                self.assertTrue(first_started.wait(timeout=3))
                # The probe has released CoreStore's lock before network work,
                # so ordinary input staging remains available immediately.
                core.dispatch(
                    {
                        "domain": "providers_models",
                        "type": "provider.patch",
                        "payload": {"provider_id": provider_id, "changes": {"endpoint": "https://edited.example.test/v1"}},
                    }
                )
                second_thread.start()
                self.assertTrue(both_started.wait(timeout=3))
                release.set()
                first_thread.join(timeout=3)
                second_thread.join(timeout=3)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            self.assertTrue(results["first"]["ok"])
            self.assertTrue(results["second"]["ok"])
            models = core.snapshot()["domains"]["providers_models"]["providers"][0]["models"]
            self.assertTrue(models[0]["probe"]["available"])
            self.assertTrue(models[1]["probe"]["available"])
            self.assertEqual("https://edited.example.test/v1", core.snapshot()["domains"]["providers_models"]["providers"][0]["api_base"])

    def test_claude_model_probe_prefers_anthropic_in_diagnostics(self) -> None:
        config = textwrap.dedent(PROVIDER_CONFIG).lstrip().replace("default-chat", "claude-sonnet")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(config, encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                result = domain.probe({"provider_id": "primary", "model_id": "00000071"})

            model = domain.snapshot()["providers"][0]["models"][0]
            self.assertEqual("anthropic", result["recommended_surface"])
            self.assertEqual("anthropic", model["probe"]["recommended_surface"])
            self.assertEqual("openai/responses", model["upstream_url_surface"])
            self.assertNotIn("supported_upstream_url_surfaces", model)


class CodexSettingsDomainTests(unittest.TestCase):
    def test_staged_edits_preserve_existing_codex_file_presence(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            initial = domain.snapshot()
            self.assertTrue(initial["config_exists"])
            self.assertTrue(initial["auth_file_exists"])

            structured = domain.dispatch("patch", {"model_reasoning_effort": "high"})
            self.assertTrue(structured["config_exists"])
            self.assertTrue(structured["auth_file_exists"])

            raw = domain.dispatch("set_raw", {"document": "config", "text": 'model = "edited"\n'})
            self.assertTrue(raw["config_exists"])
            self.assertTrue(raw["auth_file_exists"])

    @mock.patch("codex_config._local_exposed_models", return_value=(["default-chat"], True))
    def test_live_catalog_refresh_does_not_make_a_noop_editor_stage_dirty(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            catalog_path = home / "litellm-menu-model-catalog.json"
            (home / "config.toml").write_text(
                f'model = "default-chat"\nmodel_catalog_json = "{catalog_path}"\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            text = core.editor_document("codex", "config")["text"]
            core.snapshot()
            core.stage_editor_text(
                "codex",
                "config",
                text,
                revision=core.revision,
                expected_text_digest=CoreStore._editor_text_digest(text),
            )

            self.assertFalse(core.snapshot()["drafts"]["codex"]["dirty"])

    def test_missing_codex_file_remains_missing_until_apply_creates_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            domain = CodexSettingsDomain(runtime, codex_home=home)

            staged = domain.dispatch("set_raw", {"document": "config", "text": 'model = "draft"\n'})
            self.assertFalse(staged["config_exists"])
            self.assertFalse(staged["auth_file_exists"])

            applied = domain.apply()
            self.assertTrue(applied["config_exists"])
            self.assertTrue(applied["auth_file_exists"])

    @mock.patch("codex_config._local_exposed_models", return_value=(['default-chat'], True))
    def test_menu_catalog_toggle_preserves_staged_codex_edits_and_tracks_public_models(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\npersonality = "pragmatic"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            providers = ProvidersModelsDomain(runtime)
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[providers, codex])

            staged = core.dispatch(
                {"domain": "codex", "type": "patch", "payload": {"model_reasoning_effort": "high"}},
                expected_revision=core.revision,
            )
            toggled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=staged["revision"],
            )

            applied_text = (home / "config.toml").read_text(encoding="utf-8")
            self.assertIn("model_catalog_json", applied_text)
            self.assertNotIn("model_reasoning_effort", applied_text)
            snapshot = core.snapshot()
            self.assertEqual(toggled["revision"], snapshot["revision"])
            self.assertTrue(snapshot["drafts"]["codex"]["dirty"])
            self.assertTrue(snapshot["domains"]["codex"]["model_catalog"]["enabled"])
            self.assertTrue(snapshot["domains"]["codex"]["model_catalog"]["restart_required"])
            self.assertEqual("enabled", snapshot["domains"]["codex"]["model_catalog"]["change_reason"])
            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["default-chat"], [model["slug"] for model in catalog["models"]])

            acknowledged = core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=snapshot["revision"],
            )
            disabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": False}},
                expected_revision=acknowledged["revision"],
            )
            disabled_catalog = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertEqual(disabled["revision"], core.snapshot()["revision"])
            self.assertFalse(disabled_catalog["enabled"])
            self.assertEqual([], disabled_catalog["public_models"])
            self.assertTrue(disabled_catalog["restart_required"])
            self.assertEqual("disabled", disabled_catalog["change_reason"])

    @mock.patch(
        "codex_config._local_exposed_models",
        return_value=(['default-chat', 'second-chat', 'third-chat'], True),
    )
    def test_model_catalog_uses_every_litellm_exposed_model(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime_text = textwrap.dedent(PROVIDER_CONFIG).lstrip()
            model_list_suffix = (
                "  - model_name: second-chat\n"
                "    litellm_params:\n"
                "      model: openai/second-chat\n"
                "      api_base: \"https://example.test/v1\"\n"
                "      api_key: \"replace-me-secret\"\n"
                "    model_info:\n"
                "      id: \"00000072\"\n"
                "      provider: primary\n"
                "      upstream_url_surface: openai/responses\n"
                "  - model_name: third-chat\n"
                "    litellm_params:\n"
                "      model: openai/third-chat\n"
                "      api_base: \"https://example.test/v1\"\n"
                "      api_key: \"replace-me-secret\"\n"
                "    model_info:\n"
                "      id: \"00000073\"\n"
                "      provider: primary\n"
                "      upstream_url_surface: openai/responses\n"
            )
            runtime_text = runtime_text.replace(
                "litellm_settings:\n",
                model_list_suffix + "litellm_settings:\n",
                1,
            )
            runtime.write_text(runtime_text, encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            before = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertEqual([], before["public_models"])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            self.assertEqual(
                ["default-chat", "second-chat", "third-chat"],
                enabled["result"]["model_catalog"]["public_models"],
            )
            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["default-chat", "second-chat", "third-chat"],
                [model["slug"] for model in catalog["models"]],
            )

    def test_enabled_catalog_keeps_last_verified_models_when_litellm_menu_is_unavailable(self) -> None:
        endpoint = {"result": (["default-chat"], True)}

        def exposed_models(_api_key: str):
            return endpoint["result"]

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ):
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )

            endpoint["result"] = ([], False)
            codex._catalog_source_checked_at = 0.0
            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]

            self.assertEqual(["default-chat"], snapshot["public_models"])
            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])
            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["default-chat"], [model["slug"] for model in catalog["models"]])

    def test_enabled_catalog_updates_when_litellm_reports_an_empty_model_list(self) -> None:
        endpoint = {"result": (["default-chat"], True)}

        def exposed_models(_api_key: str):
            return endpoint["result"]

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            side_effect=exposed_models,
        ):
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )

            endpoint["result"] = ([], True)
            codex._catalog_source_checked_at = 0.0
            # Endpoint-backed model-set repairs require two consecutive
            # observations so a transient worker view cannot rewrite the
            # managed catalog or prompt for a restart.
            core.snapshot()
            codex._catalog_source_checked_at = 0.0
            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]

            self.assertEqual([], snapshot["public_models"])
            self.assertTrue(snapshot["restart_required"])
            self.assertEqual("catalog_repaired", snapshot["change_reason"])
            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual([], catalog["models"])

    @mock.patch("codex_config._local_exposed_models", return_value=(["default-chat"], True))
    def test_catalog_metadata_repair_does_not_request_codex_restart(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )
            catalog_path = home / "litellm-menu-model-catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["models"][0]["description"] = "stale metadata"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]

            self.assertEqual(["default-chat"], snapshot["public_models"])
            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])
            self.assertTrue(catalog_is_current(catalog_path, ["default-chat"], registry=codex._context_registry))

    @mock.patch("codex_config._local_exposed_models", return_value=(["default-chat"], True))
    def test_missing_catalog_repair_does_not_request_codex_restart(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )
            catalog_path = home / "litellm-menu-model-catalog.json"
            catalog_path.unlink()

            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]

            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])
            self.assertTrue(catalog_is_current(catalog_path, ["default-chat"], registry=codex._context_registry))

    def test_catalog_priority_reorder_does_not_request_codex_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "codex_config._local_exposed_models",
            return_value=(["model-a", "model-b"], True),
        ), mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "model-a"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[codex])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            acknowledged = core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )
            staged = core.dispatch(
                {"domain": "codex", "type": "patch", "payload": {"model": "model-b"}},
                expected_revision=acknowledged["revision"],
            )
            core.apply("codex", revision=staged["revision"])

            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]
            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))

            self.assertEqual(["model-b", "model-a"], snapshot["public_models"])
            self.assertEqual(["model-b", "model-a"], [model["slug"] for model in catalog["models"]])
            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])

    @mock.patch("codex_config._local_exposed_models", return_value=(['default-chat'], True))
    def test_provider_apply_updates_enabled_catalog_and_requests_restart(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text('model = "default-chat"\n', encoding="utf-8")
            (home / "auth.json").write_text("{}\n", encoding="utf-8")
            providers = ProvidersModelsDomain(runtime)
            codex = CodexSettingsDomain(runtime, codex_home=home)
            core = CoreStore(domains=[providers, codex])

            enabled = core.dispatch(
                {"domain": "codex", "type": "codex.model_catalog.set", "payload": {"enabled": True}},
                expected_revision=core.revision,
            )
            acknowledged = core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=enabled["revision"],
            )
            upstream_only = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.patch",
                    "payload": {
                        "provider_id": "primary",
                        "model_id": "00000071",
                        "changes": {"upstream_model": "openai/fast-chat"},
                    },
                },
                expected_revision=acknowledged["revision"],
            )
            upstream_applied = core.apply("providers_models", revision=upstream_only["revision"])
            unchanged_public_name = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertFalse(unchanged_public_name["restart_required"])
            changed = core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "model.patch",
                    "payload": {
                        "provider_id": "primary",
                        "model_id": "00000071",
                        "changes": {"name": "deepseek-v4-flash"},
                    },
                },
                expected_revision=upstream_applied["revision"],
            )
            providers_applied = core.apply("providers_models", revision=changed["revision"])

            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["default-chat"], [model["slug"] for model in catalog["models"]])
            self.assertFalse(core.snapshot()["domains"]["codex"]["model_catalog"]["restart_required"])

            selected = core.dispatch(
                {
                    "domain": "codex",
                    "type": "patch",
                    "payload": {"model": "deepseek-v4-flash"},
                },
                expected_revision=providers_applied["revision"],
            )
            core.apply("codex", revision=selected["revision"])

            snapshot = core.snapshot()
            catalog_state = snapshot["domains"]["codex"]["model_catalog"]
            # A Codex selection alone cannot add a model that LiteLLM's live
            # /v1/models surface does not expose.
            self.assertFalse(catalog_state["restart_required"])
            self.assertIsNone(catalog_state["change_reason"])
            catalog = json.loads((home / "litellm-menu-model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["default-chat"], [model["slug"] for model in catalog["models"]])

    def test_sync_and_apply_preserve_unknown_toml_and_auth_fields(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            config = home / "config.toml"
            auth = home / "auth.json"
            config.write_text('model = "default-chat"\npersonality = "pragmatic"\n\n[future]\nkeep = true\n', encoding="utf-8")
            auth.write_text(json.dumps({"OPENAI_API_KEY": "replace-me-secret", "future": {"keep": True}}) + "\n", encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            self.assertNotIn("replace-me-secret", json.dumps(domain.snapshot()))
            domain.dispatch("patch", {"model_reasoning_effort": "high"})
            domain.apply()

            self.assertIn('[future]', config.read_text(encoding="utf-8"))
            self.assertIn('model_reasoning_effort = "high"', config.read_text(encoding="utf-8"))
            self.assertEqual({"keep": True}, json.loads(auth.read_text(encoding="utf-8"))["future"])
            self.assertEqual(0o600, stat.S_IMODE(config.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(auth.stat().st_mode))

    def test_raw_editor_document_action_updates_only_the_selected_document(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "auth.json").write_text('{"future": true}\n', encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            domain.dispatch("set_raw", {"document": "config", "text": 'personality = "pragmatic"\n'})
            exported = domain.export(include_sensitive=True)

            self.assertEqual('{"future": true}\n', exported["auth_text"])
            self.assertEqual('personality = "pragmatic"\n', exported["config_text"])


class RuntimeSettingsDomainTests(unittest.TestCase):
    def test_optional_runtime_numbers_allow_empty_inherit_and_enforce_bounds(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RuntimeSettingsDomain(Path(directory) / "runtime-settings.env")
            key = "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS"
            projected = next(item for item in domain.snapshot()["settings"] if item["key"] == key)
            self.assertEqual("integer", projected["kind"])
            self.assertEqual("optional_int", projected["storage_kind"])
            self.assertEqual("", projected["value"])

            domain.dispatch("set_setting", {"key": key, "value": "45"})
            self.assertEqual("45", domain.draft_state()[key])
            domain.dispatch("set_setting", {"key": key, "value": ""})
            self.assertEqual("", domain.draft_state()[key])
            with self.assertRaisesRegex(DomainError, "invalid"):
                domain.dispatch("set_setting", {"key": key, "value": "0"})

    def test_runtime_schema_loads_from_an_isolated_bundled_core_without_service_shells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "litellm_menu" / "core"
            package.mkdir(parents=True)
            (root / "litellm_menu" / "__init__.py").write_text("", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            source_root = Path(__file__).resolve().parents[1]
            (package / "runtime_settings_schema.py").write_bytes(
                (source_root / "litellm_menu/core/runtime_settings_schema.py").read_bytes()
            )
            module_path = root / "runtime_settings_io.py"
            module_path.write_bytes((source_root / "runtime_settings_io.py").read_bytes())
            spec = importlib.util.spec_from_file_location("bundled_runtime_settings_io", module_path)
            self.assertIsNotNone(spec)
            self.assertIsNotNone(spec.loader if spec is not None else None)
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            with mock.patch.dict(os.environ, {"PYTHONPATH": str(root)}, clear=False), mock.patch.object(
                sys, "path", [str(root), *list(sys.path)]
            ):
                assert spec is not None and spec.loader is not None
                try:
                    spec.loader.exec_module(module)
                finally:
                    sys.modules.pop(spec.name, None)

            loaded = module.load_specs()
            self.assertGreater(len(loaded), 20)
            self.assertEqual("4000", loaded["LITELLM_PORT"].default)
            self.assertEqual("0", loaded["LITELLM_MENU_MCP_AUTO_APPROVE"].default)

    def test_bool_auto_uses_checkbox_projection_and_auto_off_storage(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            domain = RuntimeSettingsDomain(path)
            spec = RuntimeSettingSpec(
                key="EXAMPLE_AUTO_SETTING",
                kind="bool_auto",
                default="off",
                minimum=None,
                maximum=None,
                options=(),
            )
            domain.specs[spec.key] = spec
            domain._raw_values[spec.key] = "off"
            domain._draft_values[spec.key] = "off"

            setting = next(item for item in domain.snapshot()["settings"] if item["key"] == spec.key)
            self.assertEqual("toggle", setting["kind"])
            self.assertEqual("bool_auto", setting["storage_kind"])

            domain.dispatch("set_setting", {"key": spec.key, "value": True})
            self.assertEqual("auto", domain.export(include_sensitive=True)["values"][spec.key])
            domain.dispatch("set_setting", {"key": spec.key, "value": False})
            self.assertEqual("off", domain.export(include_sensitive=True)["values"][spec.key])

            domain.dispatch("set_setting", {"key": spec.key, "value": True})
            with mock.patch.object(domain, "reload", return_value=domain.snapshot()):
                domain.apply()

            self.assertIn(f"{spec.key}=auto", path.read_text(encoding="utf-8"))

    def test_runtime_schema_actions_write_private_atomic_env(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            domain = RuntimeSettingsDomain(path)
            self.assertGreater(len(domain.snapshot()["settings"]), 20)

            domain.dispatch("set_setting", {"key": "LITELLM_PORT", "value": "4100"})
            self.assertTrue(domain.validate()["valid"])
            domain.apply()

            saved = path.read_text(encoding="utf-8")
            self.assertIn("LITELLM_PORT=4100", saved)
            self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))

    def test_runtime_schema_exposes_split_cooldown_write_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            domain = RuntimeSettingsDomain(path)
            settings = {
                item["key"]: item for item in domain.snapshot()["settings"]
            }

            self.assertEqual(
                "1",
                settings["LITELLM_MENU_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED"]["value"],
            )
            self.assertEqual(
                "0",
                settings["LITELLM_MENU_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED"]["value"],
            )
            self.assertEqual(
                "toggle",
                settings["LITELLM_MENU_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED"]["kind"],
            )
            self.assertEqual(
                "toggle",
                settings["LITELLM_MENU_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED"]["kind"],
            )

            domain.dispatch(
                "set_setting",
                {
                    "key": "LITELLM_MENU_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED",
                    "value": True,
                },
            )
            domain.apply()

            self.assertIn(
                "LITELLM_MENU_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED=1",
                path.read_text(encoding="utf-8"),
            )

    def test_retired_config_watch_values_load_once_and_are_removed_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(
                "LITELLM_PORT=4100\n"
                "LITELLM_CONFIG_WATCH_INTERVAL=5\n"
                "LITELLM_CONFIG_WATCH_SETTLE_INTERVAL=2\n",
                encoding="utf-8",
            )

            domain = RuntimeSettingsDomain(path)

            self.assertEqual(
                "4100",
                next(item for item in domain.snapshot()["settings"] if item["key"] == "LITELLM_PORT")["value"],
            )
            self.assertNotIn(
                "LITELLM_CONFIG_WATCH_INTERVAL",
                {item["key"] for item in domain.snapshot()["settings"]},
            )
            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertIn("LITELLM_PORT=4100", saved)
            self.assertNotIn("LITELLM_CONFIG_WATCH", saved)

    def test_retired_vision_bridge_settings_are_removed_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(
                "LITELLM_MENU_VISION_BRIDGE_API_KEY=replace-me-secret\n",
                encoding="utf-8",
            )
            domain = RuntimeSettingsDomain(path)
            self.assertNotIn("LITELLM_MENU_VISION_BRIDGE_API_KEY", {item["key"] for item in domain.snapshot()["settings"]})
            domain.apply()
            self.assertNotIn("LITELLM_MENU_VISION_BRIDGE_API_KEY", path.read_text(encoding="utf-8"))

    def test_pi_web_access_json_default_is_not_reported_as_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RuntimeSettingsDomain(Path(directory) / "runtime-settings.env")
            key = "LITELLM_MENU_PI_WEB_ACCESS_CONFIG_JSON"
            field = next(item for item in domain.snapshot()["settings"] if item["key"] == key)

            self.assertFalse(field["configured"])
            self.assertFalse(domain.secret_present("setting", key))

            domain.stage_secret("setting", key, '{"provider":"duckduckgo"}')
            configured = next(item for item in domain.snapshot()["settings"] if item["key"] == key)
            self.assertTrue(configured["configured"])
            self.assertTrue(domain.secret_present("setting", key))
            self.assertEqual("configured", configured["value"])

    def test_pi_web_access_json_multiline_value_uses_only_the_native_read_lease(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RuntimeSettingsDomain(Path(directory) / "runtime-settings.env")
            core = CoreStore(domains=[domain])
            key = "LITELLM_MENU_PI_WEB_ACCESS_CONFIG_JSON"
            value = '{\n  "provider": "duckduckgo"\n}'

            result = core.stage_secret("runtime", "setting", key, value, revision=core.revision)
            self.assertTrue(result["present"])
            self.assertEqual('{"provider":"duckduckgo"}', core.trusted_secret_value("runtime", "setting", key, revision=core.revision))
            self.assertNotIn(value, json.dumps(core.snapshot()))

            with self.assertRaisesRegex(CoreError, "unavailable"):
                core.trusted_secret_descriptor("runtime", "setting", "LITELLM_MENU_VISION_BRIDGE_API_KEY")


class WebDAVSettingsDomainTests(unittest.TestCase):
    def test_webdav_patch_apply_and_password_presence_are_safe(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch.dict(os.environ, {}, clear=False):
            root = Path(directory)
            settings = root / "webdav.json"
            enabled = root / "webdav.enabled"
            domain = WebDAVSettingsDomain(settings, enabled_path=enabled)
            domain.dispatch(
                "patch",
                {
                    "url": "https://example.test/webdav/",
                    "username": "example-user",
                    "password": "replace-me-secret",
                    "remote_name": "config.json",
                    "sync_interval": "15",
                    "timeout": "10",
                    "enabled": True,
                },
            )

            snapshot = domain.snapshot()
            self.assertEqual("https://example.test/webdav/", snapshot["url"])
            self.assertTrue(snapshot["password_configured"])
            self.assertNotIn("replace-me-secret", json.dumps(snapshot))
            domain.apply()

            saved = json.loads(settings.read_text(encoding="utf-8"))
            self.assertEqual("replace-me-secret", saved["password"])
            self.assertEqual(15, saved["sync_interval_minutes"])
            self.assertEqual(10, saved["timeout_seconds"])
            self.assertTrue(enabled.exists())
            self.assertEqual(0o600, stat.S_IMODE(settings.stat().st_mode))
            self.assertEqual(0o600, stat.S_IMODE(enabled.stat().st_mode))

    def test_webdav_snapshot_preserves_url_credentials_for_local_editing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = WebDAVSettingsDomain(Path(directory) / "webdav.json")
            url = "https://editor-user:editor-token@example.test/webdav/?folder=menu"
            domain.dispatch("patch", {"url": url})

            self.assertEqual(url, domain.snapshot()["url"])

    def test_probe_uses_existing_webdav_client_without_echoing_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status = root / "webdav-sync-status.json"
            domain = WebDAVSettingsDomain(root / "webdav.json", enabled_path=root / "enabled", status_path=status)
            domain.dispatch("patch", {"url": "https://example.test/webdav/", "remote_name": "config.json"})
            with mock.patch("webdav.core.WebDAVClient.head", return_value=(200, {})), mock.patch(
                "webdav.core.WebDAVClient.try_mkcol"
            ):
                result = domain.probe()

            self.assertEqual({"ok": True, "protocols": ["webdav"], "detail": "WebDAV probe succeeded"}, result)
            recorded = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual("probe", recorded["action"])
            self.assertTrue(recorded["ok"])
            self.assertEqual(0o600, stat.S_IMODE(status.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
