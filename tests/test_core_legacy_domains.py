from __future__ import annotations

import contextlib
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
import time
import unittest
from unittest import mock

from young_router.core.domains import DomainError
from young_router.core.domains.codex import CodexSettingsDomain
from young_router.core.domains.providers_models import ProvidersModelsDomain
from young_router.adapters import traceone
from young_router.core.domains.relay_accounts import RelayAccountsDomain
from young_router.core.domains.runtime import RuntimeSettingsDomain
from young_router.core.domains.webdav import WebDAVSettingsDomain
from young_router.core.model_catalog import catalog_is_current
from young_router.core.service import CoreError, CoreStore
from young_router.core.runtime_settings_io import RuntimeSettingSpec


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


DEEP_TEST_PROMPT = "Using only the current language model, produce 315 separate first-instinct choices of an integer from 1 through 355 inclusive."


def deep_test_config(model_name: str = "gpt-6-astra", surface: str = "openai/responses", *, protocol_mode: str = "") -> str:
    """The provider fixture renamed to a TraceOne route, on a chosen surface."""

    config = textwrap.dedent(PROVIDER_CONFIG).lstrip().replace("default-chat", model_name)
    if surface != "openai/responses":
        config = config.replace("upstream_url_surface: openai/responses", f"upstream_url_surface: {surface}")
    if protocol_mode:
        config = config.replace(
            f"upstream_url_surface: {surface}",
            f"upstream_url_surface: {surface}\n      upstream_protocol_mode: {protocol_mode}",
            1,
        )
    return config


@contextlib.contextmanager
def degradation_engine(*, answer: dict[str, object]):
    """Patch the TraceOne adapter so a test never needs the staged engine."""

    def identify(_text: str, **_kwargs: object) -> dict[str, object]:
        return dict(answer)

    with mock.patch.object(traceone, "engine", return_value={"name": "TraceOne", "source": "test", "revision": "test", "staged_at": "", "available": True}), mock.patch.object(
        traceone, "prompt_text", return_value=DEEP_TEST_PROMPT
    ), mock.patch.object(traceone, "identify", side_effect=identify):
        yield



class ProvidersModelsDomainTests(unittest.TestCase):
    def test_account_login_provider_stages_adapter_and_private_claude_token(self) -> None:
        from young_router.core.provider_auth import ProviderAuthManager

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
            self.assertIn("os.environ/YOUNG_ROUTER_AUTH_", saved)
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
                                "x-young-router-provider-source": {
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
                custom["extra"]["x-young-router-provider-source"],
            )
            self.assertEqual("Custom Provider", custom["name"])
            self.assertEqual("https://custom.example.test/v1", custom["api_base"])
            self.assertEqual("Custom Provider", custom["models"][0]["provider"])
            self.assertEqual("https://custom.example.test/v1", custom["models"][0]["api_base"])

            providers.apply()
            saved = config_path.read_text(encoding="utf-8")
            self.assertIn("x-young-router-provider-source: {kind: custom}", saved)
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

            named = domain.dispatch(
                "provider.add",
                {
                    "provider": {
                        "name": "named",
                        "models": [],
                        "create_default_api_key": True,
                        "initial_api_key_name": "coral",
                    }
                },
            )
            self.assertEqual(["coral"], named["providers"][1]["api_key_names"])
            self.assertNotIn("value", json.dumps(named))

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
                        {
                            "name": "fetched-one",
                            "upstream_model": "fetched-one",
                            "api_key_name": "default",
                            "supports_web_search": True,
                        },
                        {
                            "name": "fetched-two",
                            "upstream_model": "fetched-two",
                            "api_key_name": "default",
                            "supports_responses_web_search": False,
                        },
                    ],
                },
            )

            models = batch["providers"][0]["models"]
            added = [model for model in models if model["name"] in {"manual", "fetched-one", "fetched-two"}]
            self.assertEqual(3, len(added))
            self.assertEqual([0, 0, 0], [model["order"] for model in added])
            self.assertEqual(3, len({model["editor_id"] for model in added}))
            private_models = domain.export(include_sensitive=True)["providers"][0]["models"]
            by_name = {model["model_name"]: model for model in private_models}
            self.assertEqual({"supports_web_search": True}, by_name["fetched-one"]["model_info_extra"])
            self.assertEqual(
                {"supports_responses_web_search": False},
                by_name["fetched-two"]["model_info_extra"],
            )
            domain.apply()
            reloaded = ProvidersModelsDomain(path).export(include_sensitive=True)
            reloaded_models = {
                model["model_name"]: model
                for model in reloaded["providers"][0]["models"]
            }
            self.assertEqual(
                {"supports_web_search": True},
                reloaded_models["fetched-one"]["model_info_extra"],
            )
            self.assertEqual(
                {"supports_responses_web_search": False},
                reloaded_models["fetched-two"]["model_info_extra"],
            )

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

    def test_model_move_provider_prefers_a_same_named_destination_key(self) -> None:
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
                            {"name": "default", "value": "replace-me-default-secret"},
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
            moved = providers["backup"]["models"][0]
            # The destination offers a ProviderKey with the moved model's own
            # key name, so the move keeps the route identity instead of
            # silently re-pointing the model at another credential.
            self.assertEqual("default", moved["api_key_name"])
            destination_slot = next(
                slot
                for slot in providers["backup"]["key_states"]
                if slot["name"] == "default"
            )
            self.assertEqual(destination_slot["id"], moved["provider_key_id"])
            self.assertNotIn("replace-me-default-secret", json.dumps(snapshot))

            domain.apply()
            reloaded = ProvidersModelsDomain(path).snapshot()
            reloaded_providers = {provider["name"]: provider for provider in reloaded["providers"]}
            self.assertEqual("default", reloaded_providers["backup"]["models"][0]["api_key_name"])

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
                    {
                        "object": "list",
                        "data": [
                            {"id": "model-b", "supported_tools": ["web_search"]},
                            {"id": "model-a", "supports_responses_web_search": False},
                            {"id": "model-a"},
                        ],
                    }
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
            self.assertEqual(
                {"supports_web_search": True},
                fetched["model_capabilities"]["model-b"],
            )
            self.assertEqual(
                {"supports_responses_web_search": False},
                fetched["model_capabilities"]["model-a"],
            )
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

    def test_fetch_models_exposes_explicit_responses_compaction_opt_in(self) -> None:
        """Fetched catalog capability records carry the compaction opt-in.

        Only an explicit boolean in the model record counts (canonical key or
        unified alias); absent capability stays unknown and is not included.
        """

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {
                                "id": "model-a",
                                "capabilities": {"supports_responses_compaction": True},
                            },
                            {"id": "model-b", "supports_compaction": False},
                            {"id": "model-c"},
                            {"id": "model-d", "supports_compaction": True},
                        ],
                    }
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
                fetched = domain.dispatch(
                    "providers.fetch_models", {"provider_id": "primary"}
                )["operation_summary"]

            capabilities = fetched["model_capabilities"]
            self.assertEqual(
                {"supports_responses_compaction": True},
                capabilities["model-a"],
            )
            self.assertEqual(
                {"supports_responses_compaction": False},
                capabilities["model-b"],
            )
            # Alias resolves to the canonical field.
            self.assertEqual(
                {"supports_responses_compaction": True},
                capabilities["model-d"],
            )
            # Absent capability is unknown, not unsupported.
            self.assertNotIn("model-c", capabilities)
            self.assertNotIn("replace-me-secret", json.dumps(fetched))
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

    def test_new_model_canonicalizes_responses_compaction_opt_in(self) -> None:
        """Top-level add payloads persist the canonical model_info key.

        The unified alias maps to ``supports_responses_compaction`` and the
        canonical spelling wins over an alias when both are supplied. The key
        survives Apply and a reload round trip so Codex Settings selection can
        read it from the runtime config's ``model_info``.
        """

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            provider_id = domain.snapshot()["providers"][0]["editor_id"]
            domain.dispatch(
                "model.add_many",
                {
                    "provider_id": provider_id,
                    "models": [
                        {
                            "name": "opt-in-canonical",
                            "upstream_model": "opt-in-canonical",
                            "api_key_name": "default",
                            "supports_responses_compaction": True,
                        },
                        {
                            "name": "opt-in-aliased",
                            "upstream_model": "opt-in-aliased",
                            "api_key_name": "default",
                            "supports_compaction": True,
                        },
                        {
                            "name": "opt-in-conflict",
                            "upstream_model": "opt-in-conflict",
                            "api_key_name": "default",
                            "supports_responses_compaction": False,
                            "supports_compaction": True,
                        },
                        {
                            "name": "opt-out",
                            "upstream_model": "opt-out",
                            "api_key_name": "default",
                            "supports_responses_compaction": False,
                        },
                        {
                            "name": "unmarked",
                            "upstream_model": "unmarked",
                            "api_key_name": "default",
                        },
                    ],
                },
            )
            domain.apply()

            reloaded = ProvidersModelsDomain(path).export(include_sensitive=True)
            reloaded_models = {
                model["model_name"]: model
                for model in reloaded["providers"][0]["models"]
            }
            self.assertEqual(
                {"supports_responses_compaction": True},
                reloaded_models["opt-in-canonical"]["model_info_extra"],
            )
            self.assertEqual(
                {"supports_responses_compaction": True},
                reloaded_models["opt-in-aliased"]["model_info_extra"],
            )
            self.assertEqual(
                {"supports_responses_compaction": False},
                reloaded_models["opt-in-conflict"]["model_info_extra"],
            )
            self.assertEqual(
                {"supports_responses_compaction": False},
                reloaded_models["opt-out"]["model_info_extra"],
            )
            self.assertEqual({}, reloaded_models["unmarked"]["model_info_extra"])

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

    def test_fetch_models_accepts_a_model_catalog_larger_than_512_kib(self) -> None:
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                body = json.dumps(
                    {
                        "object": "list",
                        "data": [
                            {"id": "model-a", "description": "x" * (600 * 1024)},
                            {"id": "model-b"},
                        ],
                    }
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
            config = PROVIDER_CONFIG.replace(
                "https://example.test/v1",
                f"http://127.0.0.1:{port}/v1",
            )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(textwrap.dedent(config).lstrip(), encoding="utf-8")
                domain = ProvidersModelsDomain(path)

                fetched = domain.dispatch(
                    "providers.fetch_models",
                    {"provider_id": "primary"},
                )["operation_summary"]

            self.assertTrue(fetched["available"])
            self.assertEqual(["model-a", "model-b"], fetched["models"])
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

    def test_a_changed_probe_input_drops_the_stored_finding(self) -> None:
        """A finding is a claim about one address, model, protocol, and key."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            def probe() -> None:
                with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                    domain.probe({"provider_id": "primary", "model_id": "00000071"})

            def stored_probe() -> object:
                return domain.snapshot()["providers"][0]["models"][0]["probe"]

            probe()
            self.assertIsNotNone(stored_probe())
            # An edit that is not a probe input keeps the finding: the route it
            # describes is still the route on screen.
            domain.dispatch(
                "model.patch",
                {"provider_id": "primary", "model_id": "00000071", "changes": {"manual_order": 7}},
            )
            self.assertIsNotNone(stored_probe())
            for changes in (
                {"upstream_model": "other-chat"},
                {"upstream_protocol_mode": "fixed"},
                {"upstream_url_surface": "anthropic"},
                {"provider_key_id": ""},
            ):
                probe()
                self.assertIsNotNone(stored_probe())
                domain.dispatch(
                    "model.patch",
                    {"provider_id": "primary", "model_id": "00000071", "changes": changes},
                )
                self.assertIsNone(stored_probe(), changes)

    def test_a_moved_address_or_rekeyed_slot_drops_the_stored_finding(self) -> None:
        """The address and the credential are probe inputs too."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            def probe() -> None:
                with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                    domain.probe({"provider_id": "primary", "model_id": "00000071"})

            def stored_probe() -> object:
                return domain.snapshot()["providers"][0]["models"][0]["probe"]

            probe()
            self.assertIsNotNone(stored_probe())
            # A provider that moved is a different route at the same name.
            domain.dispatch(
                "provider.patch",
                {"provider_id": "primary", "changes": {"api_base": "https://moved.example.test/v1"}},
            )
            self.assertIsNone(stored_probe())

            probe()
            self.assertIsNotNone(stored_probe())
            # The credential the probed route answered with is replaced.
            domain.stage_secret("api_key", "primary\x1fdefault", "replace-me-rotated-secret")
            self.assertIsNone(stored_probe())

            probe()
            self.assertIsNotNone(stored_probe())
            domain.dispatch("provider.clear_key", {"provider_id": "primary"})
            self.assertIsNone(stored_probe())

    def test_model_deep_test_sends_the_frozen_prompt_to_a_responses_route(self) -> None:
        deep_requests: list[dict[str, object]] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
                length = int(self.headers.get("Content-Length", "0"))
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if self.path == "/v1/responses":
                    if len(str(payload.get("input", ""))) > 100:
                        deep_requests.append(payload)
                        body = {
                            "id": "resp-deep",
                            "model": payload.get("model"),
                            "output": [{"type": "message", "content": [{"type": "output_text", "text": "[[1]]"}]}],
                        }
                    else:
                        body = {"id": "resp-probe", "output": []}
                elif self.path == "/v1/messages":
                    body = {"content": [{"type": "text", "text": "OK"}]}
                else:
                    body = {"choices": [{"message": {"role": "assistant", "content": "OK"}}]}
                encoded = json.dumps(body).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(encoded)))
                self.end_headers()
                self.wfile.write(encoded)

            def log_message(self, _format: str, *_args: object) -> None:
                return

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            port = server.server_address[1]
            config = deep_test_config(protocol_mode="fixed").replace(
                "https://example.test/v1", f"http://127.0.0.1:{port}/v1"
            )
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory) / "config.yaml"
                path.write_text(config, encoding="utf-8")
                domain = ProvidersModelsDomain(path)
                core = CoreStore(domains=[domain])
                with degradation_engine(answer={"status": "identified", "label": "gpt-6-astra", "numbers": 315}):
                    result = core.probe(
                        {"provider_id": "primary", "model_id": "00000071"},
                        domain="providers_models",
                    )
                model = core.snapshot()["domains"]["providers_models"]["providers"][0]["models"][0]
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(1, len(deep_requests))
        self.assertEqual("gpt-6-astra", deep_requests[0]["model"])
        self.assertEqual(DEEP_TEST_PROMPT, deep_requests[0]["input"])
        self.assertEqual("matched", result["degradation"]["status"])
        self.assertEqual("gpt-6-astra", result["degradation"]["target"])
        self.assertEqual("gpt-6-astra", result["degradation"]["label"])
        self.assertEqual("TraceOne", result["degradation"]["engine"]["name"])
        self.assertTrue(result["degradation"]["engine"]["available"])
        self.assertEqual("matched", model["probe"]["degradation"]["status"])
        self.assertEqual(
            {"includes_degradation": True, "target": "gpt-6-astra", "surface": "openai/responses"},
            model["deep_probe"],
        )
        self.assertTrue(model["model_enabled"])
        self.assertNotIn("replace-me-secret", json.dumps(result))

    def test_model_deep_test_keeps_a_mismatched_model_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(deep_test_config(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            core = CoreStore(domains=[domain])

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            def deep_request(**_kwargs: object) -> tuple[str, str]:
                return "[[1]]", "ok"

            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe), mock.patch.object(
                ProvidersModelsDomain, "_degradation_request", side_effect=deep_request
            ), degradation_engine(answer={"status": "identified", "label": "gpt-5.6-luna", "numbers": 315}):
                result = core.probe(
                    {"provider_id": "primary", "model_id": "00000071"},
                    domain="providers_models",
                )
            snapshot = core.snapshot()
            model = snapshot["domains"]["providers_models"]["providers"][0]["models"][0]

        self.assertEqual("mismatch", result["degradation"]["status"])
        self.assertEqual("gpt-5.6-luna", result["degradation"]["label"])
        self.assertIn("gpt-5.6-luna", result["degradation"]["detail"])
        # A fingerprint mismatch is a finding, never a routing decision: the
        # model keeps its enable checkbox and the draft stays clean.
        self.assertTrue(model["model_enabled"])
        self.assertTrue(model["enabled"])
        self.assertEqual("mismatch", model["probe"]["degradation"]["status"])
        self.assertFalse(snapshot["drafts"]["providers_models"]["dirty"])

    def test_a_degradation_engine_failure_is_a_finding_not_a_python_error(self) -> None:
        """A missing prompt or a failed attribution reports its own cause."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            plan = {"includes_degradation": True, "target": "gpt-6-astra", "surface": "openai/responses"}
            arguments = {
                "plan": plan,
                "api_base": "https://example.test/v1",
                "credential": "replace-me-secret",
                "model_name": "default-chat",
                "surface_status": "ok",
            }
            with mock.patch.object(
                traceone,
                "engine",
                return_value={"name": "TraceOne", "source": "test", "revision": "test", "staged_at": "", "available": True},
            ), mock.patch.object(
                traceone,
                "prompt_text",
                side_effect=traceone.TraceOneUnavailable("TraceOne prompt is missing"),
            ):
                missing = domain._degradation_probe(**arguments)
            with mock.patch.object(
                traceone,
                "engine",
                return_value={"name": "TraceOne", "source": "test", "revision": "test", "staged_at": "", "available": True},
            ), mock.patch.object(
                traceone,
                "prompt_text",
                return_value=DEEP_TEST_PROMPT,
            ), mock.patch.object(
                domain,
                "_degradation_request",
                return_value=("[[1]]", "ok"),
            ), mock.patch.object(
                traceone,
                "identify",
                side_effect=RuntimeError("engine exploded"),
            ):
                broken = domain._degradation_probe(**arguments)

        self.assertEqual("unavailable", missing["status"])
        self.assertIn("TraceOne prompt is missing", missing["detail"])
        self.assertEqual("error", broken["status"])
        self.assertIn("engine exploded", broken["detail"])
        self.assertNotIn("replace-me-secret", json.dumps(missing) + json.dumps(broken))

    def test_model_probe_does_not_retry_a_slow_surface(self) -> None:
        """A read timeout is the verdict; only a dropped connection is retried."""
        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
                attempts.append(self.path)
                time.sleep(1.0)
                body = b'{"id": "late", "output": []}'
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
                with mock.patch.dict(os.environ, {ProvidersModelsDomain._MODEL_PROBE_TIMEOUT_ENV: "0.4"}):
                    result = domain.probe({"provider_id": "primary", "model_id": "00000071"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertEqual(3, len(attempts), attempts)
        self.assertFalse(result["ok"])
        self.assertTrue(result["unreachable"])
        self.assertEqual(["timeout"] * 3, list(result["summary"]["statuses"].values()))

    def test_probe_names_a_rejected_connection_as_rejected(self) -> None:
        """A refusal is reported as a refusal; only silence is a timeout."""

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
                self.close_connection = True
                self.wfile.close()

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
                with mock.patch.object(ProvidersModelsDomain, "_degradation_request", return_value=("", "refused")):
                    result = domain.probe({"provider_id": "primary", "model_id": "00000071"})
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=3)

        self.assertFalse(result["ok"])
        self.assertTrue(result["unreachable"])
        self.assertEqual("refused", result["summary"]["transport"])
        self.assertEqual(
            {"openai/responses", "openai/chat", "anthropic"},
            set(result["summary"]["unreachable_surfaces"]),
        )
        self.assertEqual(["refused"] * 3, list(result["summary"]["statuses"].values()))
        self.assertNotIn("network_error", json.dumps(result["summary"]["statuses"]))

    def test_probe_budget_follows_the_runtime_first_event_setting(self) -> None:
        """Unreachable means what the router means by it, not a private ceiling."""

        self.assertEqual(
            "YOUNG_ROUTER_STREAM_START_TIMEOUT_SECONDS",
            ProvidersModelsDomain._MODEL_PROBE_TIMEOUT_SETTING_KEY,
        )
        self.assertGreater(ProvidersModelsDomain._DEGRADATION_PROBE_TIMEOUT_SECONDS, 200.0)
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "runtime-settings.env"
            settings.write_text("YOUNG_ROUTER_STREAM_START_TIMEOUT_SECONDS=42\n", encoding="utf-8")
            with mock.patch.dict(
                os.environ,
                {"YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": str(settings)},
                clear=False,
            ):
                os.environ.pop(ProvidersModelsDomain._MODEL_PROBE_TIMEOUT_ENV, None)
                self.assertEqual(42.0, ProvidersModelsDomain._model_probe_timeout_seconds())
            # An explicit override still wins for a focused local run.
            with mock.patch.dict(
                os.environ,
                {
                    "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": str(settings),
                    ProvidersModelsDomain._MODEL_PROBE_TIMEOUT_ENV: "7",
                },
                clear=False,
            ):
                self.assertEqual(7.0, ProvidersModelsDomain._model_probe_timeout_seconds())
            os.environ.pop(ProvidersModelsDomain._MODEL_PROBE_TIMEOUT_ENV, None)
        with mock.patch.dict(
            os.environ,
            {"YOUNG_ROUTER_RUNTIME_SETTINGS_FILE": "/nonexistent/runtime-settings.env"},
            clear=False,
        ):
            os.environ.pop(ProvidersModelsDomain._MODEL_PROBE_TIMEOUT_ENV, None)
            self.assertEqual(120.0, ProvidersModelsDomain._model_probe_timeout_seconds())

    def test_model_probe_does_not_recommend_a_protocol_change_after_a_dropped_connection(self) -> None:
        """A reset on the configured surface must not re-point a working route."""

        def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
            return {
                "surface": surface,
                "available": surface == "openai/chat",
                "status": "ok" if surface == "openai/chat" else "network_error",
            }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(deep_test_config(protocol_mode="fixed"), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                result = domain.probe({"provider_id": "primary", "model_id": "00000071"})

        # The route is configured for Responses and that attempt was reset, so
        # the pane reports reachability instead of offering a protocol switch.
        self.assertIsNone(result["recommended_surface"])
        self.assertFalse(result["ok"])
        self.assertEqual(["openai/chat"], result["summary"]["available_surfaces"])
        self.assertIn("no protocol change is recommended", result["detail"])

    def test_model_probe_recommends_the_answering_surface_after_a_real_rejection(self) -> None:
        def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
            return {
                "surface": surface,
                "available": surface == "openai/chat",
                "status": "ok" if surface == "openai/chat" else "unsupported",
            }

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe):
                result = domain.probe({"provider_id": "primary", "model_id": "00000071"})

        self.assertEqual("openai/chat", result["recommended_surface"])
        self.assertTrue(result["ok"])
        self.assertFalse(result["unreachable"])

    def test_model_probe_retries_a_dropped_connection_once(self) -> None:
        """A relay edge that drops the first connection must not read as unavailable."""
        attempts: list[str] = []

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:  # noqa: N802 - stdlib handler hook
                attempts.append(self.path)
                if len(attempts) == 1:
                    # Drop the connection without an HTTP answer, the way a
                    # challenging relay edge does for a fresh diagnostic client.
                    self.close_connection = True
                    self.wfile.close()
                    return
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
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertTrue(result["ok"], result)
        self.assertFalse(result["unreachable"])
        # Three surfaces plus exactly one transport retry, whichever surface
        # happened to lose the first connection.
        self.assertEqual(4, len(attempts))
        self.assertEqual(
            ["/v1/chat/completions", "/v1/messages", "/v1/responses"],
            sorted(set(attempts)),
        )
        self.assertEqual(1, len([path for path in set(attempts) if attempts.count(path) == 2]))

    def test_model_deep_test_skips_the_fingerprint_when_responses_never_answered(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(deep_test_config(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)
            deep_calls: list[object] = []

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": False, "status": "network_error"}

            def deep_request(**_kwargs: object) -> tuple[str, str]:
                deep_calls.append(object())
                return "[[1]]", "ok"

            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe), mock.patch.object(
                ProvidersModelsDomain, "_degradation_request", side_effect=deep_request
            ):
                result = domain.probe({"provider_id": "primary", "model_id": "00000071"})

        self.assertFalse(result["ok"])
        self.assertEqual([], deep_calls)
        # A transport failure is a reachability finding, never a fingerprint or
        # availability verdict.
        self.assertTrue(result["unreachable"])
        self.assertEqual([], result["summary"]["available_surfaces"])
        self.assertEqual(
            ["anthropic", "openai/chat", "openai/responses"],
            sorted(result["summary"]["unreachable_surfaces"]),
        )
        self.assertEqual("unreachable", result["degradation"]["status"])
        # the guard reports the surface's own status, not a generic failure
        self.assertEqual("network_error", result["degradation"]["cause"])
        self.assertIn("did not answer", result["degradation"]["detail"])

    def test_model_deep_test_skips_degradation_for_other_protocols_and_names(self) -> None:
        for label, config in (
            ("fixed chat route", deep_test_config(surface="openai/chat", protocol_mode="fixed")),
            ("unmatched name", deep_test_config(model_name="default-chat")),
        ):
            with self.subTest(label=label):
                with tempfile.TemporaryDirectory() as directory:
                    path = Path(directory) / "config.yaml"
                    path.write_text(config, encoding="utf-8")
                    domain = ProvidersModelsDomain(path)
                    core = CoreStore(domains=[domain])
                    deep_calls: list[object] = []

                    def deep_request(**_kwargs: object) -> tuple[str, str]:
                        deep_calls.append(object())
                        return "[[1]]", "ok"

                    def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                        return {"surface": surface, "available": True, "status": "ok"}

                    with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe), mock.patch.object(
                        ProvidersModelsDomain, "_degradation_request", side_effect=deep_request
                    ):
                        result = core.probe(
                            {"provider_id": "primary", "model_id": "00000071"},
                            domain="providers_models",
                        )
                    model = core.snapshot()["domains"]["providers_models"]["providers"][0]["models"][0]

                self.assertEqual([], deep_calls)
                self.assertEqual("skipped", result["degradation"]["status"])
                self.assertIsNone(result["degradation"]["target"])
                self.assertEqual(
                    {"includes_degradation": False, "target": None, "surface": ""},
                    model["deep_probe"],
                )
                self.assertTrue(model["model_enabled"])

    def test_model_deep_test_reports_an_unstaged_engine(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(deep_test_config(), encoding="utf-8")
            domain = ProvidersModelsDomain(path)

            def surface_probe(*, surface: str, **_kwargs: object) -> dict[str, object]:
                return {"surface": surface, "available": True, "status": "ok"}

            with mock.patch.object(ProvidersModelsDomain, "_surface_probe", side_effect=surface_probe), mock.patch.object(
                traceone, "engine", return_value={"name": "TraceOne", "available": False, "revision": ""}
            ):
                result = domain.probe({"provider_id": "primary", "model_id": "00000071"})

        self.assertTrue(result["ok"])
        self.assertEqual("unavailable", result["degradation"]["status"])
        self.assertEqual("gpt-6-astra", result["degradation"]["target"])

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
    def test_use_local_api_rewrites_the_selected_provider_in_place(self) -> None:
        """The pane's Codex action adopts this app's own proxy as the backend."""

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains._shared import local_proxy_endpoint

            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text(
                'model = "relay-model"\n'
                'model_provider = "relay"\n'
                '\n'
                '[model_providers.relay]\n'
                'name = "relay"\n'
                'base_url = "https://relay.example.test/v1"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "replace-me-secret"}\n', encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)
            self.assertFalse(domain.snapshot()["uses_local_api"])

            # The pane names the route the client leaves behind: the proxy
            # serves public model names, so the direct route id would not
            # resolve there.
            domain.dispatch("use_local_api", {"model": "default-chat"})
            domain.apply()

            base_url, key = local_proxy_endpoint()
            self.assertRegex(base_url, r"^http://127\.0\.0\.1:\d+/v1$")
            structured = domain.snapshot()["structured"]
            # The client keeps the provider ``model_provider`` names; only that
            # row's endpoint changes, so no provider identity is invented and
            # the client keeps its own backend name.
            self.assertEqual("relay", structured["model_provider"])
            relay = next(item for item in structured["providers"] if item["id"] == "relay")
            self.assertEqual(base_url, relay["base_url"])
            self.assertEqual("default-chat", structured["model"])
            documents = domain.export(include_sensitive=True)
            self.assertIn(base_url, documents["config_text"])
            self.assertNotIn("relay.example.test", documents["config_text"])
            self.assertNotIn("[model_providers.custom]", documents["config_text"])
            # The proxy's master key replaces the relay credential.
            self.assertIn(key, documents["config_text"] + documents["auth_text"])
            # The pane's switch reports this state instead of offering a no-op.
            self.assertTrue(domain.snapshot()["uses_local_api"])
            domain.dispatch("use_local_api", {"model": "default-chat"})
            self.assertEqual(documents, domain.export(include_sensitive=True))
            self.assertTrue(domain.snapshot()["uses_local_api"])

    def test_use_local_api_follows_a_built_in_openai_selection(self) -> None:
        """A built-in OpenAI selection takes Codex's own base-url override."""

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains._shared import local_proxy_endpoint

            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text(
                'model = "gpt-5.6-sol"\n'
                'model_provider = "openai"\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "replace-me-secret"}\n', encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            domain.dispatch("use_local_api", {"model": "default-chat"})
            domain.apply()

            base_url, key = local_proxy_endpoint()
            structured = domain.snapshot()["structured"]
            self.assertEqual("openai", structured["model_provider"])
            self.assertEqual(base_url, structured["openai_base_url"])
            documents = domain.export(include_sensitive=True)
            self.assertIn(f'openai_base_url = "{base_url}"', documents["config_text"])
            self.assertNotIn("[model_providers", documents["config_text"])
            self.assertIn(key, documents["auth_text"])
            self.assertTrue(domain.snapshot()["uses_local_api"])

    def test_use_local_api_follows_the_runtime_port_and_master_key(self) -> None:
        """A client on the proxy follows the endpoint the runtime settings own."""

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains import _shared

            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text(
                'model = "default-chat"\n'
                'model_provider = "relay"\n'
                '\n'
                '[model_providers.relay]\n'
                'name = "relay"\n'
                'base_url = "https://relay.example.test/v1"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "retired-key"}\n', encoding="utf-8")
            with mock.patch.object(
                _shared, "local_proxy_endpoint", return_value=("http://127.0.0.1:19999/v1", "sk-first")
            ):
                domain = CodexSettingsDomain(runtime, codex_home=home)
                # A client on one of its own backends is never rewritten: the
                # row names the user's provider, not this app's proxy.
                self.assertFalse(domain.follow_local_api_endpoint())
                domain.dispatch("use_local_api", {"model": "default-chat"})
                domain.apply()
            self.assertIn("http://127.0.0.1:19999/v1", domain.export(include_sensitive=True)["config_text"])

            # The runtime pane moved the proxy to another port and issued a new
            # master key; the client has to follow both, and only the row
            # ``model_provider`` names plus the client's key may change.
            with mock.patch.object(
                _shared, "local_proxy_endpoint", return_value=("http://127.0.0.1:20001/v1", "sk-second")
            ):
                self.assertTrue(domain.follow_local_api_endpoint())
                self.assertFalse(domain.follow_local_api_endpoint())
            documents = domain.export(include_sensitive=True)
            self.assertIn("http://127.0.0.1:20001/v1", documents["config_text"])
            self.assertNotIn("19999", documents["config_text"])
            self.assertEqual(1, documents["config_text"].count("[model_providers.relay]"))
            self.assertIn('model_provider = "relay"', documents["config_text"])
            self.assertNotIn("[model_providers.custom]", documents["config_text"])
            self.assertNotIn("sk-first", documents["auth_text"])
            self.assertIn("sk-second", documents["auth_text"])
            # The model the client runs is not part of the endpoint.
            self.assertEqual("default-chat", domain.snapshot()["structured"]["model"])
            self.assertTrue(domain.snapshot()["uses_local_api"])

    def test_use_local_api_leaves_a_hand_written_provider_alone(self) -> None:
        """A provider row that is not this app's proxy is never rewritten."""

        with tempfile.TemporaryDirectory() as directory:
            from young_router.core.domains import _shared

            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            (home / "config.toml").write_text(
                'model = "default-chat"\n'
                'model_provider = "local-llama"\n'
                '\n'
                '[model_providers.local-llama]\n'
                'name = "local-llama"\n'
                'base_url = "http://127.0.0.1:11434/v1"\n'
                'wire_api = "chat"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "local-key"}\n', encoding="utf-8")
            with mock.patch.object(
                _shared, "local_proxy_endpoint", return_value=("http://127.0.0.1:20002/v1", "sk-second")
            ):
                domain = CodexSettingsDomain(runtime, codex_home=home)
                self.assertFalse(domain.follow_local_api_endpoint())
                documents = domain.export(include_sensitive=True)
                self.assertIn("http://127.0.0.1:11434/v1", documents["config_text"])
                self.assertNotIn("sk-second", documents["auth_text"])

    def test_use_saved_model_restores_the_provider_endpoint_key_and_default_model(self) -> None:
        """The designate action points the client straight at one saved route."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            # The client currently runs a public model name through the local
            # proxy, so every part of the designated route has to be written —
            # the default model, that route's endpoint, and its key — while the
            # provider ``model_provider`` names keeps its identity.
            (home / "config.toml").write_text(
                'model = "stale-model"\n'
                'model_provider = "newapi"\n'
                '\n'
                '[model_providers.newapi]\n'
                'name = "newapi"\n'
                'base_url = "http://127.0.0.1:12390/v1"\n'
                'wire_api = "responses"\n'
                'requires_openai_auth = true\n',
                encoding="utf-8",
            )
            (home / "auth.json").write_text('{"OPENAI_API_KEY": "sk-young-router"}\n', encoding="utf-8")
            domain = CodexSettingsDomain(runtime, codex_home=home)

            domain.dispatch(
                "use_saved_model",
                {"model": "default-chat", "provider": "primary", "deployment_id": "00000071"},
            )
            domain.apply()

            documents = domain.export(include_sensitive=True)
            self.assertIn('model = "default-chat"', documents["config_text"])
            self.assertNotIn('model = "stale-model"', documents["config_text"])
            # The designation rewrites the provider the client already names —
            # its URL, API surface, and key — instead of inventing a provider
            # named after the route.
            self.assertIn('model_provider = "newapi"', documents["config_text"])
            self.assertNotIn('model_provider = "primary"', documents["config_text"])
            self.assertNotIn("[model_providers.primary]", documents["config_text"])
            newapi = next(
                item for item in domain.snapshot()["structured"]["providers"] if item["id"] == "newapi"
            )
            self.assertEqual("https://example.test/v1", newapi["base_url"])
            self.assertEqual("responses", newapi["wire_api"])
            self.assertIn('base_url = "https://example.test/v1"', documents["config_text"])
            # The route's own key replaces the router's master key.
            self.assertIn("replace-me-secret", documents["auth_text"])
            self.assertNotIn("sk-young-router", documents["auth_text"])

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

    @mock.patch("young_router.core.codex_config._local_exposed_models", return_value=(["default-chat"], True))
    def test_live_catalog_refresh_does_not_make_a_noop_editor_stage_dirty(self, _live_models) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            home = root / "codex"
            home.mkdir()
            catalog_path = home / "model-catalog.json"
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

    @mock.patch("young_router.core.codex_config._local_exposed_models", return_value=(['default-chat'], True))
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
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
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
        "young_router.core.codex_config._local_exposed_models",
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
            # The switch answers through the dispatch envelope's own
            # ``action_summary``: the IPC contract has no top-level result
            # field, so the catalog projection is read there.
            self.assertEqual(
                ["default-chat", "second-chat", "third-chat"],
                enabled["action_summary"]["model_catalog"]["public_models"],
            )
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(
                ["default-chat", "second-chat", "third-chat"],
                [model["slug"] for model in catalog["models"]],
            )

    def test_enabled_catalog_keeps_last_verified_models_when_young_router_is_unavailable(self) -> None:
        endpoint = {"result": (["default-chat"], True)}

        def exposed_models(_api_key: str):
            return endpoint["result"]

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
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
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["default-chat"], [model["slug"] for model in catalog["models"]])

    def test_enabled_catalog_updates_when_litellm_reports_an_empty_model_list(self) -> None:
        endpoint = {"result": (["default-chat"], True)}

        def exposed_models(_api_key: str):
            return endpoint["result"]

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
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
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual([], catalog["models"])

    @mock.patch("young_router.core.codex_config._local_exposed_models", return_value=(["default-chat"], True))
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
            catalog_path = home / "model-catalog.json"
            catalog = json.loads(catalog_path.read_text(encoding="utf-8"))
            catalog["models"][0]["description"] = "stale metadata"
            catalog_path.write_text(json.dumps(catalog), encoding="utf-8")

            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]

            self.assertEqual(["default-chat"], snapshot["public_models"])
            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])
            self.assertTrue(catalog_is_current(catalog_path, ["default-chat"], registry=codex._context_registry))

    @mock.patch("young_router.core.codex_config._local_exposed_models", return_value=(["default-chat"], True))
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
            catalog_path = home / "model-catalog.json"
            catalog_path.unlink()

            snapshot = core.snapshot()["domains"]["codex"]["model_catalog"]

            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])
            self.assertTrue(catalog_is_current(catalog_path, ["default-chat"], registry=codex._context_registry))

    def test_catalog_priority_reorder_does_not_request_codex_restart(self) -> None:
        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            return_value=(["model-a", "model-b"], True),
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                textwrap.dedent(
                    """
                    providers:
                      primary:
                        api_base: "https://example.test/v1"
                        api_keys:
                          - name: default
                            value: "replace-me-secret"
                    model_list:
                      - model_name: model-a
                        litellm_params:
                          model: openai/model-a
                          api_base: "https://example.test/v1"
                          api_key: "replace-me-secret"
                      - model_name: model-b
                        litellm_params:
                          model: openai/model-b
                          api_base: "https://example.test/v1"
                          api_key: "replace-me-secret"
                    litellm_settings:
                      public_model_groups: [model-a, model-b]
                    """
                ).lstrip(),
                encoding="utf-8",
            )
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
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))

            self.assertEqual(["model-b", "model-a"], snapshot["public_models"])
            self.assertEqual(["model-b", "model-a"], [model["slug"] for model in catalog["models"]])
            self.assertFalse(snapshot["restart_required"])
            self.assertIsNone(snapshot["change_reason"])

    def test_provider_apply_updates_enabled_catalog_and_requests_restart(self) -> None:
        live_models = {"names": ["default-chat"]}

        def exposed_models(_api_key: str) -> tuple[list[str], bool]:
            return list(live_models["names"]), True

        with tempfile.TemporaryDirectory() as directory, mock.patch(
            "young_router.core.codex_config._local_exposed_models",
            side_effect=exposed_models,
        ), mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
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
            # The reloaded proxy exposes the renamed public model.
            live_models["names"] = ["deepseek-v4-flash"]
            providers_applied = core.apply("providers_models", revision=changed["revision"])

            # The first post-apply observation is not stable yet: the catalog
            # still carries the acknowledged name.
            codex._catalog_source_checked_at = 0.0
            first = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertFalse(first["restart_required"])
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["default-chat"], [model["slug"] for model in catalog["models"]])

            # A second fresh observation completes the repair and queues the
            # restart prompt for the renamed model set.
            codex._catalog_source_checked_at = 0.0
            repaired = core.snapshot()["domains"]["codex"]["model_catalog"]
            self.assertTrue(repaired["restart_required"])
            self.assertEqual("catalog_repaired", repaired["change_reason"])
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["deepseek-v4-flash"], [model["slug"] for model in catalog["models"]])

            core.dispatch(
                {"domain": "codex", "type": "acknowledge_model_catalog_restart", "payload": {}},
                expected_revision=core.revision,
            )
            selected = core.dispatch(
                {
                    "domain": "codex",
                    "type": "patch",
                    "payload": {"model": "missing-model"},
                },
                expected_revision=core.revision,
            )
            core.apply("codex", revision=selected["revision"])

            snapshot = core.snapshot()
            catalog_state = snapshot["domains"]["codex"]["model_catalog"]
            # A Codex selection alone cannot add a model that LiteLLM's live
            # /v1/models surface does not expose.
            self.assertFalse(catalog_state["restart_required"])
            self.assertIsNone(catalog_state["change_reason"])
            catalog = json.loads((home / "model-catalog.json").read_text(encoding="utf-8"))
            self.assertEqual(["deepseek-v4-flash"], [model["slug"] for model in catalog["models"]])

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
            key = "YOUNG_ROUTER_DSH_VISION_ROUTER_TIMEOUT_SECONDS"
            projected = next(item for item in domain.snapshot()["settings"] if item["key"] == key)
            self.assertEqual("integer", projected["kind"])
            self.assertEqual("optional_int", projected["storage_kind"])
            # The quick control projects the advanced vision-router JSON,
            # whose default fallback timeout is 45 seconds.
            self.assertEqual("45", projected["value"])

            def projection() -> str:
                item = next(entry for entry in domain.snapshot()["settings"] if entry["key"] == key)
                return str(item["value"])

            domain.dispatch("set_setting", {"key": key, "value": "27"})
            self.assertEqual("27", projection())
            # Quick fields are a JSON projection, so their storage marker stays
            # empty instead of duplicating the override.
            self.assertEqual("", domain.draft_state()[key])
            domain.dispatch("set_setting", {"key": key, "value": ""})
            self.assertEqual("45", projection())
            self.assertEqual("", domain.draft_state()[key])
            with self.assertRaisesRegex(DomainError, "invalid"):
                domain.dispatch("set_setting", {"key": key, "value": "0"})

    def test_runtime_schema_loads_from_an_isolated_bundled_core_without_service_shells(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "young_router" / "core"
            package.mkdir(parents=True)
            (root / "young_router" / "__init__.py").write_text("", encoding="utf-8")
            (package / "__init__.py").write_text("", encoding="utf-8")
            source_root = Path(__file__).resolve().parents[1]
            (package / "runtime_settings_schema.py").write_bytes(
                (source_root / "young_router/core/runtime_settings_schema.py").read_bytes()
            )
            module_path = package / "young_router.core.runtime_settings_io.py"
            module_path.write_bytes(
                (source_root / "young_router/core/runtime_settings_io.py").read_bytes()
            )
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
            self.assertEqual("12390", loaded["LITELLM_PORT"].default)
            self.assertEqual("0", loaded["YOUNG_ROUTER_MCP_AUTO_APPROVE"].default)

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
                settings["YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED"]["value"],
            )
            self.assertEqual(
                "0",
                settings["YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED"]["value"],
            )
            self.assertEqual(
                "toggle",
                settings["YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED"]["kind"],
            )
            self.assertEqual(
                "toggle",
                settings["YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED"]["kind"],
            )

            domain.dispatch(
                "set_setting",
                {
                    "key": "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED",
                    "value": True,
                },
            )
            domain.apply()

            self.assertIn(
                "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED=1",
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

    def test_retired_shell_service_settings_load_once_and_are_removed_on_apply(self) -> None:
        from young_router.core.runtime_settings_io import RETIRED_PERSISTED_SETTINGS

        retired = (
            "LITELLM_MAX_REQUESTS_BEFORE_RESTART",
            "LITELLM_STATE_TTL_SECONDS",
            "LITELLM_RUNTIME_VERIFY_WAIT_SECONDS",
            "LITELLM_SERVICE_LIFECYCLE_LOCK_WAIT_SECONDS",
            "YOUNG_ROUTER_RELAY_AUTO_GROUP_INTERVAL_MINUTES",
        )
        for key in retired:
            self.assertIn(key, RETIRED_PERSISTED_SETTINGS)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(
                "LITELLM_PORT=4100\n" + "".join(f"{key}=10\n" for key in retired),
                encoding="utf-8",
            )

            domain = RuntimeSettingsDomain(path)

            settings = {item["key"]: item for item in domain.snapshot()["settings"]}
            self.assertEqual("4100", settings["LITELLM_PORT"]["value"])
            for key in retired:
                self.assertNotIn(key, settings)
            domain.apply()
            saved = path.read_text(encoding="utf-8")
            self.assertIn("LITELLM_PORT=4100", saved)
            for key in retired:
                self.assertNotIn(key, saved)

    def test_runtime_schema_exposes_keepalive_affinity_and_log_backup_controls(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            domain = RuntimeSettingsDomain(path)
            settings = {item["key"]: item for item in domain.snapshot()["settings"]}

            keepalive = settings["YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS"]
            self.assertEqual("float", keepalive["storage_kind"])
            self.assertEqual("number", keepalive["kind"])
            self.assertEqual("15", keepalive["value"])
            self.assertEqual(0, keepalive["minimum"])
            self.assertEqual(3600, keepalive["maximum"])

            affinity = settings["YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY"]
            self.assertEqual("bool", affinity["storage_kind"])
            self.assertEqual("toggle", affinity["kind"])
            self.assertEqual("1", affinity["value"])

            backups = settings["YOUNG_ROUTER_LOG_BACKUP_SEGMENTS"]
            self.assertEqual("int", backups["storage_kind"])
            self.assertEqual("integer", backups["kind"])
            self.assertEqual("2", backups["value"])
            self.assertEqual(1, backups["minimum"])
            self.assertEqual(8, backups["maximum"])

            domain.dispatch(
                "set_setting",
                {"key": "YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS", "value": "30"},
            )
            domain.dispatch(
                "set_setting",
                {"key": "YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY", "value": False},
            )
            domain.dispatch(
                "set_setting",
                {"key": "YOUNG_ROUTER_LOG_BACKUP_SEGMENTS", "value": "4"},
            )
            domain.apply()

            saved = path.read_text(encoding="utf-8")
            self.assertIn("YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS=30", saved)
            self.assertIn("YOUNG_ROUTER_SESSION_DEPLOYMENT_AFFINITY=0", saved)
            self.assertIn("YOUNG_ROUTER_LOG_BACKUP_SEGMENTS=4", saved)

    def test_retired_vision_bridge_settings_are_removed_on_apply(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runtime-settings.env"
            path.write_text(
                "YOUNG_ROUTER_VISION_BRIDGE_API_KEY=replace-me-secret\n",
                encoding="utf-8",
            )
            domain = RuntimeSettingsDomain(path)
            self.assertNotIn("YOUNG_ROUTER_VISION_BRIDGE_API_KEY", {item["key"] for item in domain.snapshot()["settings"]})
            domain.apply()
            self.assertNotIn("YOUNG_ROUTER_VISION_BRIDGE_API_KEY", path.read_text(encoding="utf-8"))

    def test_pi_web_access_json_default_is_not_reported_as_configured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            domain = RuntimeSettingsDomain(Path(directory) / "runtime-settings.env")
            key = "YOUNG_ROUTER_PI_WEB_ACCESS_CONFIG_JSON"
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
            key = "YOUNG_ROUTER_PI_WEB_ACCESS_CONFIG_JSON"
            value = '{\n  "provider": "duckduckgo"\n}'

            result = core.stage_secret("runtime", "setting", key, value, revision=core.revision)
            self.assertTrue(result["present"])
            self.assertEqual('{"provider":"duckduckgo"}', core.trusted_secret_value("runtime", "setting", key, revision=core.revision))
            self.assertNotIn(value, json.dumps(core.snapshot()))

            with self.assertRaisesRegex(CoreError, "unavailable"):
                core.trusted_secret_descriptor("runtime", "setting", "YOUNG_ROUTER_VISION_BRIDGE_API_KEY")


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
            with mock.patch("young_router.webdav.core.WebDAVClient.head", return_value=(200, {})), mock.patch(
                "young_router.webdav.core.WebDAVClient.try_mkcol"
            ):
                result = domain.probe()

            self.assertEqual({"ok": True, "protocols": ["webdav"], "detail": "WebDAV probe succeeded"}, result)
            recorded = json.loads(status.read_text(encoding="utf-8"))
            self.assertEqual("probe", recorded["action"])
            self.assertTrue(recorded["ok"])
            self.assertEqual(0o600, stat.S_IMODE(status.stat().st_mode))


if __name__ == "__main__":
    unittest.main()
