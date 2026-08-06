from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from litellm_menu.core.model_catalog import (
    catalog_is_current,
    catalog_model_names,
    catalog_payload,
    selected_model_names,
    write_catalog,
)
from litellm_menu.core.model_contexts import MODEL_CONTEXT_SOURCES, ModelContextRegistry


class ModelCatalogTests(unittest.TestCase):
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

    def test_catalog_round_trip_and_invalid_file_are_distinct(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "catalog.json"
            self.assertIsNone(catalog_model_names(path))

    def test_sol_catalog_uses_codex_native_reasoning_levels(self) -> None:
        model = catalog_payload(["5.6 Sol"])["models"][0]
        self.assertEqual("low", model["default_reasoning_level"])
        self.assertEqual(["low", "medium", "high", "xhigh", "max", "ultra"], [item["effort"] for item in model["supported_reasoning_levels"]])

    def test_catalog_carries_known_context_window_to_codex(self) -> None:
        with mock.patch(
            "litellm_menu.core.model_contexts.litellm.model_cost",
            {"public-model": {"max_input_tokens": 128_000}},
        ):
            model = catalog_payload(["public-model"])["models"][0]

        self.assertEqual(128_000, model["context_window"])
        self.assertEqual(128_000, model["max_context_window"])
        self.assertEqual(95, model["effective_context_window_percent"])
        self.assertNotIn("auto_compact_token_limit", model)

    def test_catalog_unknown_model_defaults_to_codex_258k_effective_window(self) -> None:
        with mock.patch(
            "litellm_menu.core.model_contexts.litellm.model_cost",
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
            with mock.patch("litellm_menu.core.model_contexts.litellm.model_cost", {}):
                model = catalog_payload(["unlisted-model"], registry=registry)["models"][0]

        self.assertEqual(300_000, model["context_window"])
        self.assertEqual(300_000, model["max_context_window"])

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

        self.assertEqual(272_000, model["context_window"])
        self.assertEqual(272_000, model["max_context_window"])

    def test_upstream_refresh_updates_cache_and_codex_policy_has_priority(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
            cache = root / "contexts.json"

            def fetch(source: str) -> object:
                if source == MODEL_CONTEXT_SOURCES[0]:
                    return {
                        "vendor/new-agent": {"limit": {"context": 400_000, "input": 500_000}},
                        "openai/gpt-5.6-sol": {"limit": {"context": 1_050_000, "input": 922_000}},
                        "moonshotai/kimi-k3": {"limit": {"context": 1_048_576, "input": 1_048_576}},
                    }
                return {
                    "models": [
                        {
                            "slug": "gpt-5.6-sol",
                            "context_window": 300_000,
                            "max_context_window": 300_000,
                        }
                    ]
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
            self.assertEqual(300_000, registry.record_for("openai/gpt-5.6-sol").context_window)
            self.assertEqual(262_144, registry.record_for("openai/kimi-k3").context_window)
            self.assertTrue(cache.exists())

            cached = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            self.assertEqual(400_000, cached.record_for("vendor/new-agent").context_window)

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
