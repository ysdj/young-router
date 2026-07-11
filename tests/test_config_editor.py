from __future__ import annotations

import re
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import config_editor  # noqa: E402


class ConfigEditorProviderKeyTests(unittest.TestCase):
    def write_config(self, text: str) -> Path:
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        path = Path(temp_dir.name) / "config.yaml"
        path.write_text(textwrap.dedent(text).lstrip(), encoding="utf-8")
        return path

    def test_load_uses_explicit_api_key_label(self) -> None:
        path = self.write_config(
            """
            providers:
              experimental_provider:
                api_base: &experimental_provider_api_base "https://example.com/v1"
                api_keys:
                  - name: renamed
                    value: &experimental_provider_api_key "sk-renamed"
            model_list:
              - model_name: experimental-chat
                litellm_params:
                  model: openai/experimental-chat
                  api_base: *experimental_provider_api_base
                  api_key: *experimental_provider_api_key
                model_info:
                  id: "00000001"
                  provider: experimental_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        provider = config_editor.load_config(path)["providers"][0]

        self.assertEqual(["renamed"], [key["name"] for key in provider["api_keys"]])
        self.assertEqual("renamed", provider["models"][0]["api_key_name"])

    def test_load_accepts_current_disabled_models_companion_file(self) -> None:
        path = self.write_config(
            """
            providers:
              experimental_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list: []
            """
        )
        disabled_path = path.with_name("config.disabled-models.yaml")
        disabled_path.write_text(
            textwrap.dedent(
                """
                disabled_model_list:
                  - model_name: disabled-chat
                    litellm_params:
                      model: openai/disabled-chat
                      api_base: https://example.com/v1
                      api_key: sk-test
                    model_info:
                      id: "0000000d"
                      provider: experimental_provider
                      upstream_url_surface: openai/chat
                      supported_upstream_url_surfaces: [openai/chat]
                """
            ).lstrip(),
            encoding="utf-8",
        )

        provider = config_editor.load_config(path)["providers"][0]

        self.assertEqual(1, len(provider["models"]))
        self.assertFalse(provider["models"][0]["enabled"])
        self.assertEqual(
            ["openai/chat"],
            provider["models"][0]["supported_upstream_url_surfaces"],
        )

    def test_load_rejects_disabled_models_embedded_in_main_config(self) -> None:
        path = self.write_config(
            """
            model_list: []
            disabled_model_list: []
            """
        )

        with self.assertRaisesRegex(ValueError, "config.disabled-models.yaml"):
            config_editor.load_config(path)

    def test_save_round_trip_preserves_renamed_primary_api_key_label(self) -> None:
        path = self.write_config(
            """
            providers:
              experimental_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-renamed"
            model_list:
              - model_name: experimental-chat
                litellm_params:
                  model: openai/experimental-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-renamed"
                model_info:
                  id: "00000002"
                  provider: experimental_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        payload = config_editor.load_config(path)
        provider = payload["providers"][0]
        provider["api_keys"][0]["name"] = "renamed"
        provider["models"][0]["api_key_name"] = "renamed"

        config_editor.save_config(payload["providers"], path)
        reloaded = config_editor.load_config(path)["providers"][0]

        self.assertEqual(["renamed"], [key["name"] for key in reloaded["api_keys"]])
        self.assertEqual("renamed", reloaded["models"][0]["api_key_name"])

    def test_save_round_trip_keeps_multiple_providers_nested(self) -> None:
        path = self.write_config(
            """
            providers:
              primary:
                api_base: "https://primary.example.test/v1"
                api_keys:
                  - name: default
                    value: "sk-primary"
              backup:
                api_base: "https://backup.example.test/v1"
                api_keys:
                  - name: default
                    value: "sk-backup"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://primary.example.test/v1"
                  api_key: "sk-primary"
                model_info:
                  id: "00000021"
                  provider: primary
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://backup.example.test/v1"
                  api_key: "sk-backup"
                model_info:
                  id: "00000022"
                  provider: backup
                  upstream_url_surface: openai/chat
                  supported_upstream_url_surfaces: [openai/chat]
            router_settings: {}
            """
        )
        payload = config_editor.load_config(path)

        result = config_editor.save_config(payload["providers"], path)
        saved = config_editor._load_yaml(path)

        self.assertEqual(2, result["providers"])
        self.assertEqual({"primary", "backup"}, set(saved["providers"]))
        self.assertNotIn("primary", saved)
        self.assertNotIn("backup", saved)
        self.assertEqual({}, saved["router_settings"])
        self.assertEqual(2, len(saved["model_list"]))

    def test_save_drops_legacy_supports_vision_model_info(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000003"
                  provider: provider_alpha
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        payload = config_editor.load_config(path)
        model = payload["providers"][0]["models"][0]
        model["model_info_extra"]["supports_vision"] = True

        config_editor.save_config(payload["providers"], path)
        reloaded_model = config_editor.load_config(path)["providers"][0]["models"][0]
        saved_model_info = config_editor._load_yaml(path)["model_list"][0]["model_info"]

        self.assertNotIn("supports_vision", saved_model_info)
        self.assertNotIn("supports_vision", reloaded_model["model_info_extra"])

    def test_load_rejects_removed_supports_image_generation_flag(self) -> None:
        path = self.write_config(
            """
            providers:
              compat_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000005"
                  provider: compat_provider
                  supports_image_generation: true
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported supports_image_generation"):
            config_editor.load_config(path)

    def test_save_writes_upstream_url_surface_as_first_class_model_info(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: balanced-chat
                litellm_params:
                  model: openai/vendor-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000004"
                  provider: provider_alpha
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        payload = config_editor.load_config(path)
        model = payload["providers"][0]["models"][0]
        model["upstream_url_surface"] = "openai/chat"
        model["supported_upstream_url_surfaces"] = ["openai/chat", "anthropic"]

        config_editor.save_config(payload["providers"], path)
        reloaded_model = config_editor.load_config(path)["providers"][0]["models"][0]

        self.assertEqual("openai/chat", reloaded_model["upstream_url_surface"])
        self.assertEqual(
            ["openai/chat", "anthropic"],
            reloaded_model["supported_upstream_url_surfaces"],
        )
        self.assertNotIn("upstream_api_mode", reloaded_model["model_info_extra"])
        self.assertNotIn("upstream_url_surface", reloaded_model["model_info_extra"])
        self.assertNotIn("supported_upstream_api_modes", reloaded_model["model_info_extra"])
        self.assertNotIn("supported_upstream_url_surfaces", reloaded_model["model_info_extra"])

    def test_save_round_trip_preserves_complete_editor_protocol_order(self) -> None:
        path = self.write_config(
            """
            providers:
              primary:
                api_base: "https://example.test/v1"
                api_keys:
                  - name: default
                    value: "replace-me"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.test/v1"
                  api_key: "replace-me"
                model_info:
                  id: "00000009"
                  provider: primary
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
                  x-litellm-menu-upstream-url-surface-order:
                    - openai/responses
                    - anthropic
                    - openai/chat
            """
        )

        payload = config_editor.load_config(path)
        model = payload["providers"][0]["models"][0]
        self.assertEqual(
            ["openai/responses", "anthropic", "openai/chat"],
            model["model_info_extra"]["x-litellm-menu-upstream-url-surface-order"],
        )

        config_editor.save_config(payload["providers"], path)
        saved_model_info = config_editor._load_yaml(path)["model_list"][0]["model_info"]
        self.assertEqual(
            ["openai/responses", "anthropic", "openai/chat"],
            saved_model_info["x-litellm-menu-upstream-url-surface-order"],
        )
        self.assertEqual(
            ["openai/responses"],
            saved_model_info["supported_upstream_url_surfaces"],
        )

    def test_legacy_disabled_api_key_is_reenabled_and_flag_is_removed(self) -> None:
        path = self.write_config(
            """
            providers:
              primary:
                api_base: "https://example.test/v1"
                api_keys:
                  - name: default
                    value: "replace-me"
                    enabled: false
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.test/v1"
                  api_key: "replace-me"
                model_info:
                  id: "0000000a"
                  provider: primary
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        payload = config_editor.load_config(path)
        provider = payload["providers"][0]
        self.assertTrue(provider["api_keys"][0]["enabled"])
        self.assertTrue(provider["models"][0]["enabled"])

        config_editor.save_config(payload["providers"], path)
        saved = config_editor._load_yaml(path)
        saved_key = saved["providers"]["primary"]["api_keys"][0]
        self.assertNotIn("enabled", saved_key)

    def test_load_rejects_removed_context_metadata(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: balanced-chat
                litellm_params:
                  model: openai/vendor-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000004"
                  provider: provider_alpha
                  max_input_tokens: 262144
                  context_metadata_source: learned-upstream-error
                  context_metadata_model_id: openai/vendor-chat
            """
        )
        with self.assertRaisesRegex(ValueError, "unsupported max_input_tokens"):
            config_editor.load_config(path)

    def test_load_rejects_removed_responses_endpoint_flag(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: balanced-chat
                litellm_params:
                  model: openai/vendor-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000005"
                  provider: provider_alpha
                  supports_responses_endpoint: false
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported supports_responses_endpoint"):
            config_editor.load_config(path)

    def test_supported_url_surfaces_define_primary_without_legacy_flags(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: balanced-chat
                litellm_params:
                  model: openai/vendor-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000006"
                  provider: provider_alpha
                  upstream_url_surface: openai/chat
                  supported_upstream_url_surfaces:
                    - openai/chat
                    - openai/responses
            """
        )

        payload = config_editor.load_config(path)
        model = payload["providers"][0]["models"][0]

        self.assertEqual("openai/chat", model["upstream_url_surface"])
        self.assertEqual(
            ["openai/chat", "openai/responses"],
            model["supported_upstream_url_surfaces"],
        )

        config_editor.save_config(payload["providers"], path)
        saved = config_editor._load_yaml(path)["model_list"][0]["model_info"]
        self.assertEqual("openai/chat", saved["upstream_url_surface"])
        self.assertEqual(
            ["openai/chat", "openai/responses"],
            saved["supported_upstream_url_surfaces"],
        )
        self.assertNotIn("supports_responses_endpoint", saved)

    def test_public_model_mapping_keeps_exact_namespaced_upstream_model(self) -> None:
        path = self.write_config(
            """
            providers:
              compat_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: gpt-compatible
                litellm_params:
                  model: openai/vendor/glm-compatible
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000026"
                  provider: compat_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses, openai/chat]
            litellm_settings:
              public_model_groups: [gpt-compatible]
            """
        )

        payload = config_editor.load_config(path)
        model = payload["providers"][0]["models"][0]
        self.assertEqual("gpt-compatible", model["model_name"])
        self.assertEqual("openai/vendor/glm-compatible", model["litellm_model"])

        config_editor.save_config(payload["providers"], path)
        saved = config_editor._load_yaml(path)
        entry = saved["model_list"][0]

        self.assertEqual("gpt-compatible", entry["model_name"])
        self.assertEqual("openai/vendor/glm-compatible", entry["litellm_params"]["model"])
        self.assertEqual(["gpt-compatible"], saved["litellm_settings"]["public_model_groups"])
        self.assertIn("model=gpt-compatible", entry["model_info"]["route_key"])
        self.assertIn("upstream=openai/vendor/glm-compatible", entry["model_info"]["route_key"])

    def test_load_rejects_primary_surface_that_differs_from_ordered_list(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: balanced-chat
                litellm_params:
                  model: openai/vendor-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000016"
                  provider: provider_alpha
                  upstream_url_surface: openai/chat
                  supported_upstream_url_surfaces:
                    - openai/responses
                    - anthropic
                    - openai/chat
            """
        )

        with self.assertRaisesRegex(ValueError, "must equal the first"):
            config_editor.load_config(path)

    def test_save_generates_random_deployment_token_and_explicit_route_key(self) -> None:
        path = self.write_config(
            """
            providers:
              compat_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: r-plus
                    value: "sk-test"
            model_list: []
            """
        )
        payload = config_editor.load_config(path)
        provider = payload["providers"][0]
        provider["models"].append({
            "enabled": True,
            "model_enabled": True,
            "provider": "compat_provider",
            "model_name": "balanced-chat",
            "litellm_model": "openai/default-chat",
            "api_base": "https://example.com/v1",
            "api_key": "sk-test",
            "api_key_name": "r-plus",
            "order": "2",
            "ssl_verify": "",
            "ssl_verify_present": False,
            "deployment_id": "",
            "supports_responses_image_generation_tool": False,
            "supports_responses_image_generation_tool_present": False,
            "upstream_url_surface": "openai/responses",
            "supported_upstream_url_surfaces": ["openai/responses"],
            "entry_extra": {},
            "litellm_extra": {},
            "model_info_extra": {},
        })

        config_editor.save_config(payload["providers"], path)
        reloaded_model = config_editor.load_config(path)["providers"][0]["models"][0]

        self.assertRegex(reloaded_model["deployment_id"], r"^[0-9a-f]{8}$")
        saved = config_editor._load_yaml(path)["model_list"][0]
        self.assertEqual(
            "model=balanced-chat / provider=compat_provider / upstream=openai/default-chat / host=example.com / key=r-plus / order=2",
            saved["model_info"]["route_key"],
        )
        self.assertEqual("r-plus", saved["model_info"]["api_key_name"])
        self.assertNotIn("openai-default-chat-compat_provider", reloaded_model["deployment_id"])

    def test_save_allows_duplicate_route_key_for_distinct_deployments(self) -> None:
        path = self.write_config(
            """
            providers:
              compat_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: r-plus
                    value: "sk-test"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                  order: 2
                model_info:
                  id: "00000007"
                  provider: compat_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                  order: 2
                model_info:
                  id: "00000008"
                  provider: compat_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        payload = config_editor.load_config(path)

        config_editor.save_config(payload["providers"], path)

        saved = config_editor._load_yaml(path)["model_list"]
        self.assertEqual(
            ["00000007", "00000008"],
            [entry["model_info"]["id"] for entry in saved],
        )
        self.assertEqual(
            [
                "model=default-chat / provider=compat_provider / upstream=openai/default-chat / host=example.com / key=r-plus / order=2",
                "model=default-chat / provider=compat_provider / upstream=openai/default-chat / host=example.com / key=r-plus / order=2",
            ],
            [entry["model_info"]["route_key"] for entry in saved],
        )

    def test_save_route_key_includes_public_model_name(self) -> None:
        path = self.write_config(
            """
            providers:
              openrouter:
                api_base: "https://openrouter.ai/api/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: vendor-chat
                litellm_params:
                  model: openai/vendor/vendor-chat
                  api_base: "https://openrouter.ai/api/v1"
                  api_key: "sk-test"
                  order: 1
                model_info:
                  id: "00000018"
                  provider: openrouter
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: llmwebsearch
                litellm_params:
                  model: openai/vendor/vendor-chat
                  api_base: "https://openrouter.ai/api/v1"
                  api_key: "sk-test"
                  order: 1
                model_info:
                  id: "00000019"
                  provider: openrouter
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        payload = config_editor.load_config(path)

        config_editor.save_config(payload["providers"], path)

        saved = config_editor._load_yaml(path)["model_list"]
        route_keys = [entry["model_info"]["route_key"] for entry in saved]
        self.assertEqual(
            [
                "model=vendor-chat / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
                "model=llmwebsearch / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
            ],
            route_keys,
        )
        self.assertNotEqual(route_keys[0], route_keys[1])

    def test_save_makes_generated_deployment_tokens_unique(self) -> None:
        path = self.write_config(
            """
            providers:
              backup_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list: []
            """
        )
        payload = config_editor.load_config(path)
        provider = payload["providers"][0]
        base_model = {
            "enabled": True,
            "model_enabled": True,
            "provider": "backup_provider",
            "model_name": "default-chat",
            "litellm_model": "openai/default-chat",
            "api_base": "https://example.com/v1",
            "api_key": "sk-test",
            "api_key_name": "default",
            "order": "",
            "ssl_verify": "",
            "ssl_verify_present": False,
            "deployment_id": "",
            "supports_responses_image_generation_tool": False,
            "supports_responses_image_generation_tool_present": False,
            "upstream_url_surface": "openai/responses",
            "supported_upstream_url_surfaces": ["openai/responses"],
            "entry_extra": {},
            "litellm_extra": {},
            "model_info_extra": {},
        }
        second_model = dict(base_model)
        second_model["order"] = "2"
        provider["models"].extend([dict(base_model), second_model])

        config_editor.save_config(payload["providers"], path)
        models = config_editor.load_config(path)["providers"][0]["models"]

        deployment_ids = [model["deployment_id"] for model in models]
        self.assertEqual(2, len(set(deployment_ids)))
        self.assertTrue(all(re.fullmatch(r"[0-9a-f]{8}", value) for value in deployment_ids))

    def test_missing_or_blank_order_defaults_to_one(self) -> None:
        path = self.write_config(
            """
            providers:
              compat_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
                  - name: backup
                    value: "sk-test-2"
            model_list:
              - model_name: gpt-image-2
                litellm_params:
                  model: openai/gpt-image-2
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000009"
                  provider: compat_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
              - model_name: gpt-image-2
                litellm_params:
                  model: openai/gpt-image-2
                  api_base: "https://example.com/v1"
                  api_key: "sk-test-2"
                  order: ""
                model_info:
                  id: "0000000a"
                  provider: compat_provider
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )

        payload = config_editor.load_config(path)
        models = payload["providers"][0]["models"]

        self.assertEqual(["1", "1"], [model["order"] for model in models])

        config_editor.save_config(payload["providers"], path)
        saved = config_editor._load_yaml(path)["model_list"]
        self.assertEqual([1, 1], [entry["litellm_params"]["order"] for entry in saved])
        self.assertEqual(
            [
                "model=gpt-image-2 / provider=compat_provider / upstream=openai/gpt-image-2 / host=example.com / key=default / order=1",
                "model=gpt-image-2 / provider=compat_provider / upstream=openai/gpt-image-2 / host=example.com / key=backup / order=1",
            ],
            [entry["model_info"]["route_key"] for entry in saved],
        )

    def test_load_rejects_unsupported_semantic_deployment_id(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: openai-default-chat-provider_alpha-team-o1
                  provider: provider_alpha
            """
        )

        with self.assertRaisesRegex(ValueError, "model_info.id"):
            config_editor.load_config(path)

    def test_load_rejects_provider_scalar_api_key(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_key: "sk-test"
            model_list: []
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported scalar api_key"):
            config_editor.load_config(path)

    def test_load_rejects_unsupported_upstream_api_mode(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: default-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000007"
                  provider: provider_alpha
                  upstream_api_mode: openai/chat
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported upstream_api_mode"):
            config_editor.load_config(path)

    def test_load_rejects_unsupported_callbacks(self) -> None:
        path = self.write_config(
            """
            providers:
              provider_alpha:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list: []
            litellm_settings:
              callbacks:
                - litellm_menu.callbacks.image_generation_routing_hook
                - example.unsupported_callback
            """
        )

        with self.assertRaisesRegex(ValueError, "unsupported callback"):
            config_editor.load_config(path)

    def test_example_config_uses_current_schema(self) -> None:
        example = ROOT / "config.example.yaml"
        text = example.read_text(encoding="utf-8")

        self.assertNotRegex(text, r"(?m)^  [^:\n]+:\n(?:    .*\n)*    api_key:")
        self.assertNotIn("disabled_api_keys", text)
        self.assertNotIn("upstream_api_mode", text)
        self.assertNotIn("supported_upstream_api_modes", text)
        self.assertGreater(len(config_editor.load_config(example)["providers"]), 0)

    def test_example_config_prefers_image_provider_for_image_model(self) -> None:
        example = ROOT / "config.example.yaml"
        payload = config_editor.load_config(example)
        routes = [
            model
            for provider in payload["providers"]
            for model in provider["models"]
            if model["model_name"] == "image-model"
        ]

        ordered = sorted(routes, key=lambda model: int(model["order"] or 9999))

        self.assertGreaterEqual(len(ordered), 2)
        self.assertEqual("image", ordered[0]["provider"])
        self.assertEqual("default", ordered[0]["api_key_name"])
        self.assertEqual("1", ordered[0]["order"])
        self.assertFalse(ordered[0]["supports_responses_image_generation_tool"])
        self.assertFalse(ordered[0]["supports_responses_image_generation_tool_present"])
        self.assertNotIn(
            ("backup_provider", "1"),
            {(route["provider"], route["order"]) for route in routes},
        )

    def test_save_rejects_stale_editor_revision(self) -> None:
        path = self.write_config(
            """
            providers:
              compat_provider:
                api_base: "https://example.com/v1"
                api_keys:
                  - name: default
                    value: "sk-test"
            model_list:
              - model_name: balanced-chat
                litellm_params:
                  model: openai/default-chat
                  api_base: "https://example.com/v1"
                  api_key: "sk-test"
                model_info:
                  id: "00000008"
                  provider: compat_provider
                  alias_target: default-chat
                  upstream_url_surface: openai/responses
                  supported_upstream_url_surfaces: [openai/responses]
            """
        )
        payload = config_editor.load_config(path)
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  compat_provider:
                    api_base: "https://example.com/v1"
                    api_keys:
                      - name: default
                        value: "sk-test"
                model_list: []
                litellm_settings:
                  public_model_groups: []
                """
            ).lstrip(),
            encoding="utf-8",
        )

        with self.assertRaisesRegex(ValueError, "changed on disk"):
            config_editor.save_config(payload["providers"], path, payload["revision"])


if __name__ == "__main__":
    unittest.main()
