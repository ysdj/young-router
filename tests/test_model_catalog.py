from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from litellm_menu.core.model_catalog import (
    catalog_is_current,
    catalog_model_names,
    catalog_names_from_editor,
    catalog_payload,
    selected_model_names,
    write_catalog,
)
from litellm_menu.core.model_contexts import (
    DEFAULT_MODEL_CONTEXT_REFRESH_HOURS,
    MODEL_CONTEXT_SOURCES,
    ModelContextRegistry,
)
from litellm_menu.core.runtime_settings_schema import runtime_settings_metadata


class ModelCatalogTests(unittest.TestCase):
    def test_model_context_refresh_default_is_six_hours(self) -> None:
        self.assertEqual(6, DEFAULT_MODEL_CONTEXT_REFRESH_HOURS)
        setting = next(
            item
            for item in runtime_settings_metadata()
            if item.get("key") == "LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS"
        )
        self.assertEqual("6", setting["default"])

    def test_selected_names_keep_explicit_models_in_order_and_dedupe(self) -> None:
        config = {
            "model": "deepseek-v4-flash",
            "review_model": "deepseek-v4-flash",
        }
        self.assertEqual(["deepseek-v4-flash"], selected_model_names(config))
        self.assertEqual(
            ["deepseek-v4-flash", "review-only"],
            selected_model_names({"model": " deepseek-v4-flash ", "review_model": "review-only"}),
        )
        self.assertEqual([], selected_model_names({"model": "", "review_model": None}))

    def test_catalog_names_include_all_litellm_exposed_models_after_selected_models(self) -> None:
        payload = {
            "structured": {
                "model": "active-model",
                "review_model": "review-model",
            },
            "models": [{"model": "configured-only"}],
            "exposed_models": [
                "other-model",
                "active-model",
                "other-model",
                "image-model",
                "anthropic-model",
            ],
        }

        self.assertEqual(
            [
                "active-model",
                "other-model",
                "image-model",
                "anthropic-model",
            ],
            catalog_names_from_editor(payload),
        )

    def test_catalog_names_include_every_exposed_route(self) -> None:
        self.assertEqual(
            ["active-model", "disabled-model"],
            catalog_names_from_editor(
                {
                    "structured": {"model": "active-model"},
                    "models": [{"model": "disabled-model", "model_enabled": False}],
                    "exposed_models": ["disabled-model", "active-model"],
                }
            ),
        )

    def test_catalog_names_are_empty_when_litellm_model_list_is_unavailable(self) -> None:
        self.assertEqual(
            [],
            catalog_names_from_editor({
                "structured": {"model": "active-model", "review_model": "review-model"},
                "models": None,
                "exposed_models": None,
            }),
        )

    def test_catalog_names_do_not_fallback_to_configured_or_selected_models(self) -> None:
        self.assertEqual(
            [],
            catalog_names_from_editor({
                "structured": {"model": "active-model", "review_model": "review-model"},
                "models": [{"model": "configured-model"}],
            }),
        )

    def test_catalog_round_trip_and_invalid_file_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            self.assertIsNone(catalog_model_names(path))

    def test_sol_catalog_uses_codex_native_reasoning_levels(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Native low"},
                {"effort": "medium", "description": "Native medium"},
                {"effort": "high", "description": "Native high"},
                {"effort": "xhigh", "description": "Native xhigh"},
                {"effort": "max", "description": "Native max"},
                {"effort": "ultra", "description": "Native ultra"},
            ],
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
        }
        with mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[native_profile],
        ):
            model = catalog_payload(["5.6 Sol"])["models"][0]
        self.assertEqual("low", model["default_reasoning_level"])
        self.assertEqual(["low", "medium", "high", "xhigh", "max", "ultra"], [item["effort"] for item in model["supported_reasoning_levels"]])

    def test_catalog_transfers_the_current_native_profile_without_version_branches(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "display_name": "GPT-5.6-Sol",
            "description": "Native description",
            "base_instructions": "Native process words and delegation instructions",
            "model_messages": {"instructions_template": "Native process words and delegation instructions"},
            "supported_reasoning_levels": [{"effort": "v5-reasoning", "description": "Native"}],
            "multi_agent_version": "v5",
            "tool_mode": "code_mode_only",
            "future_native_policy": {"delegation": "native"},
            "context_window": 111,
            "max_context_window": 222,
        }

        with mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[native_profile],
        ), mock.patch("litellm.model_cost", {}):
            model = catalog_payload(["gpt-5.6-sol"])["models"][0]

        self.assertEqual("gpt-5.6-sol", model["slug"])
        self.assertEqual("GPT-5.6-Sol", model["display_name"])
        self.assertEqual("v5", model["multi_agent_version"])
        self.assertEqual("code_mode_only", model["tool_mode"])
        self.assertEqual({"delegation": "native"}, model["future_native_policy"])
        self.assertEqual("Native process words and delegation instructions", model["base_instructions"])
        self.assertEqual(["v5-reasoning"], [item["effort"] for item in model["supported_reasoning_levels"]])
        self.assertEqual(272_000, model["context_window"])
        self.assertEqual(272_000, model["max_context_window"])
        self.assertEqual(1, model["priority"])

    def test_exact_native_profile_keeps_codex_only_delegation_mode(self) -> None:
        native_profile = {
            "slug": "native-agent",
            "default_reasoning_level": "low",
            "supported_reasoning_levels": [
                {"effort": "low", "description": "Native low"},
                {"effort": "medium", "description": "Native medium"},
                {"effort": "high", "description": "Native high"},
                {"effort": "xhigh", "description": "Native xhigh"},
                {"effort": "max", "description": "Native max"},
                {"effort": "ultra", "description": "Native delegation"},
            ],
            "multi_agent_version": "v2",
            "base_instructions": "Native prompt",
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "native-agent": {
                                "context_window": 272_000,
                                "max_context_window": 272_000,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                                "reasoning": True,
                                "supported_reasoning_levels": [
                                    "low",
                                    "medium",
                                    "high",
                                    "xhigh",
                                    "max",
                                ],
                                "default_reasoning_level": "medium",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            capability = registry.reasoning_for("native-agent")
            self.assertIsNotNone(capability)
            assert capability is not None
            self.assertNotIn("ultra", capability.supported_levels)

            with mock.patch(
                "litellm_menu.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["native-agent"], registry=registry)["models"][0]

        self.assertEqual("low", model["default_reasoning_level"])
        self.assertEqual(
            ["low", "medium", "high", "xhigh", "max", "ultra"],
            [item["effort"] for item in model["supported_reasoning_levels"]],
        )
        self.assertEqual("v2", model["multi_agent_version"])

    def test_catalog_uses_native_agent_profile_for_a_public_alias(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "display_name": "GPT-5.6-Sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "multi_agent_version": "v4",
            "future_native_field": ["kept"],
        }

        with mock.patch(
            "litellm_menu.core.model_catalog.load_native_catalog",
            return_value=[native_profile],
        ):
            model = catalog_payload(["litellm-sol"])["models"][0]

        self.assertEqual("litellm-sol", model["slug"])
        self.assertEqual("litellm-sol", model["display_name"])
        self.assertEqual("v4", model["multi_agent_version"])
        self.assertEqual(["kept"], model["future_native_field"])

    def test_catalog_carries_known_context_window_to_codex(self) -> None:
        with mock.patch(
            "litellm.model_cost",
            {"public-model": {"max_input_tokens": 128_000}},
        ):
            model = catalog_payload(["public-model"])["models"][0]

        self.assertEqual(128_000, model["context_window"])
        self.assertEqual(128_000, model["max_context_window"])
        self.assertEqual(95, model["effective_context_window_percent"])
        self.assertNotIn("auto_compact_token_limit", model)

    def test_catalog_unknown_model_defaults_to_codex_258k_effective_window(self) -> None:
        with mock.patch(
            "litellm.model_cost",
            {},
        ):
            model = catalog_payload(["custom-public-model"])["models"][0]

        self.assertEqual(272_000, model["context_window"])
        self.assertEqual(272_000, model["max_context_window"])
        self.assertEqual(95, model["effective_context_window_percent"])
        self.assertEqual(258_400, model["context_window"] * model["effective_context_window_percent"] // 100)

    def test_unknown_model_default_is_runtime_adjustable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "runtime-settings.env"
            settings.write_text(
                "LITELLM_MENU_UNKNOWN_MODEL_CONTEXT_WINDOW=300000\n"
                "LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS=0\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(runtime_settings_path=settings, refresh_enabled=False)
            with mock.patch("litellm.model_cost", {}):
                model = catalog_payload(["unlisted-model"], registry=registry)["models"][0]

        self.assertEqual(300_000, model["context_window"])
        self.assertEqual(300_000, model["max_context_window"])

    def test_glm53_uses_million_token_window_instead_of_unknown_default(self) -> None:
        with mock.patch("litellm.model_cost", {}):
            model = catalog_payload(["glm-5.3"])["models"][0]

        self.assertEqual(1_000_000, model["context_window"])
        self.assertEqual(1_000_000, model["max_context_window"])
        self.assertEqual(95, model["effective_context_window_percent"])
        self.assertEqual(950_000, model["context_window"] * model["effective_context_window_percent"] // 100)

    def test_suffix_lookup_keeps_safest_window_when_provider_copies_differ(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "vendor-a/shared-agent": {
                                "context_window": 1_000_000,
                                "max_context_window": 1_000_000,
                                "effective_context_window_percent": 95,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                            },
                            "vendor-b/x-ai/shared-agent": {
                                "context_window": 1_048_576,
                                "max_context_window": 1_048_576,
                                "effective_context_window_percent": 95,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            with mock.patch("litellm.model_cost", {}):
                model = catalog_payload(["shared-agent"], registry=registry)["models"][0]

        self.assertEqual(1_000_000, model["context_window"])
        self.assertEqual(1_000_000, model["max_context_window"])
        self.assertEqual(95, model["effective_context_window_percent"])

    def test_pi_profile_overrides_lower_priority_bundled_alias(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: kimi-k3\n"
                "    litellm_params:\n"
                "      model: anthropic/kimi-k3\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "moonshotai/kimi-k3": {
                                "context_window": 1_048_576,
                                "max_context_window": 1_048_576,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(
                runtime_config_path=runtime,
                cache_path=cache,
                refresh_enabled=False,
            )
            with mock.patch("litellm.model_cost", {}):
                model = catalog_payload(["kimi-k3"], registry=registry)["models"][0]

        self.assertEqual(1_048_576, model["context_window"])
        self.assertEqual(1_048_576, model["max_context_window"])
        self.assertEqual(95, model["effective_context_window_percent"])

    def test_catalog_resolves_public_alias_from_runtime_route(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: gpt-5.2\n"
                "    litellm_params:\n"
                "      model: openai/kimi-k3\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(runtime_config_path=runtime, refresh_enabled=False)
            model = catalog_payload(["gpt-5.2"], registry=registry)["models"][0]

        self.assertEqual(262_144, model["context_window"])
        self.assertEqual(1_048_576, model["max_context_window"])

    def test_catalog_uses_safest_window_across_routes_in_one_public_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: mixed-agent\n"
                "    litellm_params:\n"
                "      model: anthropic/claude-fable-5\n"
                "  - model_name: mixed-agent\n"
                "    litellm_params:\n"
                "      model: openai/not-known\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(runtime_config_path=runtime, refresh_enabled=False)
            model = catalog_payload(["mixed-agent"], registry=registry)["models"][0]

        self.assertEqual(1_000_000, model["context_window"])
        self.assertEqual(1_000_000, model["max_context_window"])

    def test_unresolved_openai_compatible_alias_does_not_poison_known_window(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: public-gemini\n"
                "    litellm_params:\n"
                "      model: openai/gateway-gemini-3.1-pro-preview\n"
                "  - model_name: public-gemini\n"
                "    litellm_params:\n"
                "      model: openai/gemini-3.1-pro-preview\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(runtime_config_path=runtime, refresh_enabled=False)
            with mock.patch("litellm.model_cost", {}):
                model = catalog_payload(["public-gemini"], registry=registry)["models"][0]

        self.assertEqual(1_048_576, model["context_window"])
        self.assertEqual(1_048_576, model["max_context_window"])

    def test_upstream_refresh_uses_pi_agent_policy_before_hard_limit_catalogs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
            cache = root / "contexts.json"

            def fetch(source: str) -> object:
                if source == MODEL_CONTEXT_SOURCES[0]:
                    return {
                        "openai": {
                            "gpt-5.6-sol": {
                                "id": "gpt-5.6-sol",
                                "contextWindow": 272_000,
                                "maxTokens": 128_000,
                            }
                        },
                        "openrouter": {
                            "openai/gpt-5.6-sol": {
                                "id": "openai/gpt-5.6-sol",
                                "contextWindow": 1_050_000,
                                "maxTokens": 128_000,
                            }
                        },
                    }
                if source == MODEL_CONTEXT_SOURCES[1]:
                    return {
                        "models": [
                            {
                                "slug": "gpt-5.6-sol",
                                "context_window": 300_000,
                                "max_context_window": 300_000,
                            }
                        ]
                    }
                return {
                    "vendor/new-agent": {"limit": {"context": 400_000, "input": 500_000}},
                    "openai/gpt-5.6-sol": {"limit": {"context": 1_050_000, "input": 922_000}},
                    "moonshotai/kimi-k3": {"limit": {"context": 1_048_576, "input": 1_048_576}},
                }

            registry = ModelContextRegistry(
                runtime_settings_path=settings,
                cache_path=cache,
                refresh_enabled=True,
                fetcher=fetch,
                clock=lambda: 1_000_000,
            )
            self.assertTrue(registry.refresh_if_due(force=True))
            self.assertEqual(400_000, registry.record_for("vendor/new-agent").context_window)
            self.assertEqual(300_000, registry.record_for("gpt-5.6-sol").context_window)
            self.assertEqual(272_000, registry.record_for("openai/gpt-5.6-sol").context_window)
            self.assertEqual(1_050_000, registry.record_for("openrouter/openai/gpt-5.6-sol").context_window)
            self.assertEqual(262_144, registry.record_for("openai/kimi-k3").context_window)
            self.assertTrue(cache.exists())

            cached = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            self.assertEqual(400_000, cached.record_for("vendor/new-agent").context_window)

    def test_legacy_context_cache_refreshes_for_pi_profiles_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000_000,
                        "records": {
                            "openai/gpt-5.6-sol": {
                                "context_window": 1_050_000,
                                "max_context_window": 1_050_000,
                                "source": "https://models.dev/models.json",
                                "priority": 20,
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            fetched: list[str] = []

            def fetch(source: str) -> object:
                fetched.append(source)
                if source == MODEL_CONTEXT_SOURCES[0]:
                    return {
                        "openai": {
                            "gpt-5.6-sol": {
                                "id": "gpt-5.6-sol",
                                "contextWindow": 272_000,
                            }
                        }
                    }
                return {}

            registry = ModelContextRegistry(
                runtime_settings_path=settings,
                cache_path=cache,
                refresh_enabled=True,
                fetcher=fetch,
                clock=lambda: 1_000_000,
            )
            self.assertTrue(registry.refresh_if_due())
            self.assertIn(MODEL_CONTEXT_SOURCES[0], fetched)
            self.assertEqual(272_000, registry.record_for("openai/gpt-5.6-sol").context_window)

    def test_pi_thinking_level_map_controls_catalog_levels_and_survives_cache_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: custom-fable\n"
                "    litellm_params:\n"
                "      custom_llm_provider: anthropic\n"
                "      model: claude-fable-5\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"

            def fetch(source: str) -> object:
                if source == MODEL_CONTEXT_SOURCES[0]:
                    return {
                        "anthropic": {
                            "claude-fable-5": {
                                "id": "claude-fable-5",
                                "reasoning": True,
                                "contextWindow": 1_000_000,
                                "thinkingLevelMap": {
                                    "off": None,
                                    "xhigh": "xhigh",
                                    "max": "max",
                                },
                            }
                        }
                    }
                return {}

            registry = ModelContextRegistry(
                runtime_config_path=runtime,
                runtime_settings_path=settings,
                cache_path=cache,
                refresh_enabled=True,
                fetcher=fetch,
                clock=lambda: 1_000_000,
            )
            self.assertTrue(registry.refresh_if_due(force=True))
            capability = registry.reasoning_for("custom-fable")
            self.assertIsNotNone(capability)
            assert capability is not None
            self.assertEqual(
                ("minimal", "low", "medium", "high", "xhigh", "max"),
                capability.supported_levels,
            )
            self.assertEqual("medium", capability.default_level)
            self.assertEqual({"none": None, "xhigh": "xhigh", "max": "max"}, capability.thinking_level_map)

            model = catalog_payload(["custom-fable"], registry=registry)["models"][0]
            self.assertEqual(
                ["minimal", "low", "medium", "high", "xhigh", "max"],
                [item["effort"] for item in model["supported_reasoning_levels"]],
            )
            self.assertEqual("medium", model["default_reasoning_level"])

            cached = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            cached_capability = cached.reasoning_for_model_id("anthropic/claude-fable-5")
            self.assertIsNotNone(cached_capability)
            assert cached_capability is not None
            self.assertEqual(capability.supported_levels, cached_capability.supported_levels)
            self.assertEqual(capability.thinking_level_map, cached_capability.thinking_level_map)

    def test_pi_reasoning_catalog_uses_safe_intersection_across_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: mixed-reasoning\n"
                "    litellm_params:\n"
                "      custom_llm_provider: provider-a\n"
                "      model: agent-a\n"
                "  - model_name: mixed-reasoning\n"
                "    litellm_params:\n"
                "      custom_llm_provider: provider-b\n"
                "      model: agent-b\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "provider-a/agent-a": {
                                "context_window": 1000,
                                "max_context_window": 1000,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                                "reasoning": True,
                                "thinking_level_map": {"off": "none", "xhigh": "xhigh"},
                            },
                            "provider-b/agent-b": {
                                "context_window": 1000,
                                "max_context_window": 1000,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                                "reasoning": True,
                                "thinking_level_map": {"off": "none", "max": "max"},
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(runtime_config_path=runtime, cache_path=cache, refresh_enabled=False)
            capability = registry.reasoning_for("mixed-reasoning")
            self.assertIsNotNone(capability)
            assert capability is not None
            self.assertEqual(("none", "minimal", "low", "medium", "high"), capability.supported_levels)

    def test_catalog_current_check_detects_changed_model_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            write_catalog(path, ["5.6 Sol"])
            self.assertTrue(catalog_is_current(path, ["5.6 Sol"]))
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["models"][0]["supported_reasoning_levels"] = []
            path.write_text(json.dumps(payload), encoding="utf-8")
            self.assertFalse(catalog_is_current(path, ["5.6 Sol"]))
            write_catalog(path, ["deepseek-v4-flash"])
            self.assertEqual(["deepseek-v4-flash"], catalog_model_names(path))
            self.assertIn("supports_parallel_tool_calls", json.loads(path.read_text(encoding="utf-8")).get("models")[0])
            path.write_text('{"models":[{}]}\n', encoding="utf-8")
            self.assertIsNone(catalog_model_names(path))


if __name__ == "__main__":
    unittest.main()
