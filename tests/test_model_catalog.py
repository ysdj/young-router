from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from young_router.core.model_catalog import (
    catalog_carries_native_profile,
    catalog_is_current,
    catalog_model_names,
    catalog_names_from_editor,
    catalog_payload,
    route_serves_only_images,
    select_inherited_profile,
    write_catalog,
)
from young_router.core import model_catalog as catalog_module
from young_router.core import model_contexts as model_contexts_module
from young_router.core.model_contexts import (
    DEFAULT_MODEL_CONTEXT_REFRESH_HOURS,
    MODEL_CONTEXT_SOURCES,
    ModelContextRegistry,
    published_reasoning_levels,
)
from young_router.core.runtime_settings_schema import runtime_settings_metadata


def _number_text_for_schema(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


class ModelCatalogTests(unittest.TestCase):
    def test_a_custom_route_asks_for_a_visible_thinking_summary(self) -> None:
        """A route's thinking is summary frames the proxy only forwards on request.

        The client reads an absent or ``none`` summary as "hide the thinking",
        and the proxy then drops every ``response.reasoning_summary_*`` frame.
        A custom route inherits the base profile's ``none``, so without this the
        block stayed empty until the user edited the client's own TOML.
        """

        native_models = [
            {
                "slug": "gpt-base",
                "priority": 1,
                "visibility": "list",
                "default_reasoning_summary": "none",
                "model_messages": {"instructions_template": "Base agent"},
            }
        ]
        with mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=native_models,
        ):
            route = catalog_payload(["kimi-k3.1-pro"])["models"][0]
            native = catalog_payload(["gpt-base"])["models"][0]
        self.assertEqual("auto", route["default_reasoning_summary"])
        # The client's own model keeps what it declares: its thinking does not
        # travel through this proxy.
        self.assertEqual("none", native["default_reasoning_summary"])

    def test_a_catalog_written_before_the_summary_default_is_rebuilt(self) -> None:
        """An existing catalog that hides the thinking must not stay current."""

        names = ["kimi-k3.1-pro"]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-catalog.json"
            write_catalog(path, names)
            self.assertTrue(catalog_is_current(path, names))
            stale = json.loads(path.read_text(encoding="utf-8"))
            for model in stale["models"]:
                model["default_reasoning_summary"] = "none"
            path.write_text(json.dumps(stale), encoding="utf-8")
            self.assertFalse(catalog_is_current(path, names))

    def test_fallback_catalog_repairs_the_native_base_profile(self) -> None:
        native_models = [
            {
                "slug": "first-native",
                "priority": 1,
                "visibility": "list",
                "model_messages": {"instructions_template": "First agent"},
            },
            {
                "slug": "second-native",
                "priority": 2,
                "visibility": "list",
                "model_messages": {"instructions_template": "Second agent"},
            },
        ]
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-catalog.json"
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                write_catalog(path, ["public-model"])
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=native_models,
            ):
                self.assertFalse(
                    catalog_is_current(path, ["public-model"], base_slug="second-native")
                )
                write_catalog(
                    path, ["public-model"], base_slug="second-native"
                )
                self.assertTrue(
                    catalog_is_current(path, ["public-model"], base_slug="second-native")
                )

            model = json.loads(path.read_text(encoding="utf-8"))["models"][0]
            self.assertEqual(
                "Second agent",
                model["model_messages"]["instructions_template"],
            )

    def test_fallback_catalog_is_replaced_when_native_instructions_are_readable(self) -> None:
        native_profile = {
            "slug": "gpt-6-astra",
            "model_messages": {"instructions_template": "Native agent instructions"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-catalog.json"
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                write_catalog(path, ["public-model"])
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                self.assertFalse(catalog_is_current(path, ["public-model"]))
                write_catalog(path, ["public-model"])
                self.assertTrue(catalog_is_current(path, ["public-model"]))

            model = json.loads(path.read_text(encoding="utf-8"))["models"][0]
            self.assertEqual(
                "Native agent instructions",
                model["model_messages"]["instructions_template"],
            )
            fallback_model = {
                "slug": model["slug"],
                "base_instructions": "Fallback instructions",
            }
            (path).write_text(
                json.dumps({"models": [fallback_model]}),
                encoding="utf-8",
            )
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                self.assertFalse(catalog_is_current(path, ["public-model"]))

    def test_model_context_refresh_default_is_six_hours(self) -> None:
        self.assertEqual(6, DEFAULT_MODEL_CONTEXT_REFRESH_HOURS)
        setting = next(
            item
            for item in runtime_settings_metadata()
            if item.get("key") == "YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS"
        )
        self.assertEqual("6", setting["default"])

    def test_runtime_schema_defaults_match_proxy_defaults(self) -> None:
        from young_router.proxy import base as base_module
        from young_router.log_rotation import DEFAULT_LOG_BACKUP_SEGMENTS

        defaults_by_key = {
            str(item.get("key")): str(item.get("default"))
            for item in runtime_settings_metadata()
        }
        self.assertEqual(
            _number_text_for_schema(base_module._STREAM_KEEPALIVE_DEFAULT_INTERVAL_SECONDS),
            defaults_by_key["YOUNG_ROUTER_STREAM_KEEPALIVE_INTERVAL_SECONDS"],
        )
        self.assertEqual(
            str(DEFAULT_LOG_BACKUP_SEGMENTS),
            defaults_by_key["YOUNG_ROUTER_LOG_BACKUP_SEGMENTS"],
        )
        self.assertEqual(
            str(int(base_module._WEBSOCKET_MAX_FRAME_DEFAULT_BYTES)),
            defaults_by_key["YOUNG_ROUTER_WEBSOCKET_MAX_FRAME_BYTES"],
        )
        self.assertEqual(
            _number_text_for_schema(
                base_module._HEAVY_REPLAY_STREAM_START_TIMEOUT_DEFAULT_SECONDS
            ),
            defaults_by_key["YOUNG_ROUTER_HEAVY_REPLAY_STREAM_START_TIMEOUT_SECONDS"],
        )
        self.assertEqual(
            _number_text_for_schema(
                base_module._HEAVY_REPLAY_STREAM_START_DEFAULT_THRESHOLD_BYTES
            ),
            defaults_by_key["YOUNG_ROUTER_HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES"],
        )
        from young_router.proxy import image_context as image_context_module

        # The image-context pass reads its own settings through ``os.environ``,
        # so its defaults are the module's constants rather than ``base``'s.
        self.assertEqual(
            image_context_module.IMAGE_CONTEXT_MODE_DEFAULT,
            defaults_by_key["YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MODE"],
        )
        self.assertEqual(
            str(image_context_module.IMAGE_CONTEXT_KEEP_RECENT_DEFAULT),
            defaults_by_key["YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_KEEP_RECENT"],
        )
        self.assertEqual(
            str(image_context_module.IMAGE_CONTEXT_MIN_BYTES_DEFAULT),
            defaults_by_key["YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_MIN_BYTES"],
        )

    def test_catalog_names_are_the_exposed_list_following_the_pickers_order(self) -> None:
        # Every picker this app writes is read top-down and picked by number, so
        # they share one order, and the one probe both clients read owns it.
        # This step filters that order; it never re-ranks it, and a selected model
        # holds no special place.
        payload = {
            "structured": {
                "model": "active-model",
                "review_model": "review-model",
            },
            "models": [
                {"model": "configured-only"},
                {"model": "active-model"},
                {"model": "other-model"},
                {"model": "image-model"},
                {"model": "anthropic-model"},
            ],
            "exposed_models": [
                "active-model",
                "anthropic-model",
                "image-model",
                "other-model",
                "active-model",
            ],
        }

        # The configured model list is an allowlist over the live exposure:
        # unconfigured routes are never catalog entries, and
        # configured-but-unexposed names are not fallbacks.
        self.assertEqual(
            [
                "active-model",
                "anthropic-model",
                "image-model",
                "other-model",
            ],
            catalog_names_from_editor(payload),
        )

    def test_the_catalog_numbers_priorities_in_the_order_it_is_given(self) -> None:
        """Codex sorts its picker by ``priority`` and discards the array order.

        Verified against the client's models-manager: the catalog's own ``models``
        array is not what the list renders.  The priority is therefore the row's
        place in the picker, and it is numbered from the order the names arrive
        in -- the one order the probe every client reads already provides.
        """

        with mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[],
        ):
            models = catalog_payload(["alpha", "mike", "zulu"])["models"]

        self.assertEqual(["alpha", "mike", "zulu"], [model["slug"] for model in models])
        self.assertEqual([1, 2, 3], [model["priority"] for model in models])

    def test_catalog_names_include_every_exposed_configured_route(self) -> None:
        self.assertEqual(
            ["disabled-model"],
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

    def test_inherited_profile_follows_the_clients_declared_hierarchy(self) -> None:
        """The base for alias models ignores catalog order and hidden profiles."""

        def profile(slug: str, *, priority: object, visibility: object, prompt: str) -> dict[str, object]:
            model: dict[str, object] = {
                "slug": slug,
                "base_instructions": prompt,
                "model_messages": {"instructions_template": prompt},
            }
            if priority is not None:
                model["priority"] = priority
            if visibility is not None:
                model["visibility"] = visibility
            return model

        hidden_flagship = profile("cyber-flagship", priority=0, visibility="hide", prompt="Hidden")
        listed_mid = profile("gpt-mid", priority=10, visibility="list", prompt="Mid")
        listed_flagship = profile("gpt-flagship", priority=1, visibility="list", prompt="Flagship")

        selected = select_inherited_profile([hidden_flagship, listed_mid, listed_flagship])
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("gpt-flagship", selected["slug"])

        # A catalog without any listed profile still yields instructions.
        selected = select_inherited_profile([hidden_flagship])
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual("cyber-flagship", selected["slug"])
        self.assertIsNone(select_inherited_profile([]))

    def test_pinned_base_profile_survives_a_newer_generation(self) -> None:
        """A new client flagship must not re-map every alias automatically."""

        astra = {
            "slug": "gpt-6-astra",
            "priority": 1,
            "visibility": "list",
            "base_instructions": "Astra instructions",
            "model_messages": {"instructions_template": "Astra instructions"},
        }
        next_generation = {
            "slug": "gpt-7-orbit",
            "priority": 0,
            "visibility": "list",
            "base_instructions": "GPT-7 instructions",
            "model_messages": {"instructions_template": "GPT-7 instructions"},
        }
        native = [next_generation, astra]

        pinned = catalog_payload(["gemini-9-pro"], native_models=native, base_slug="gpt-6-astra")["models"][0]
        self.assertEqual("Astra instructions", pinned["base_instructions"])
        # Without the pin the client's declared flagship wins the mapping.
        unpinned = catalog_payload(["gemini-9-pro"], native_models=native)["models"][0]
        self.assertEqual("GPT-7 instructions", unpinned["base_instructions"])

    def test_unreadable_client_catalog_never_downgrades_native_instructions(self) -> None:
        """A Codex update must not replace the client's prompts with ours."""

        native_profile = {
            "slug": "gpt-6-astra",
            "base_instructions": "Native GPT-6 process words",
            "model_messages": {"instructions_template": "Native GPT-6 process words"},
        }
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "model-catalog.json"
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                write_catalog(path, ["gpt-6-astra"])
            self.assertTrue(catalog_carries_native_profile(path))

            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                # The client is installed but its bundled catalog cannot be
                # read right now: the file keeps the client's own prompts.
                self.assertTrue(catalog_is_current(path, ["gpt-6-astra"]))

            # A fallback-only file (no client was installed when it was
            # written) still refreshes as soon as a client is readable again.
            fallback = Path(directory) / "fallback.json"
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[],
            ):
                write_catalog(fallback, ["gpt-6-astra"])
            self.assertFalse(catalog_carries_native_profile(fallback))
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                self.assertFalse(catalog_is_current(fallback, ["gpt-6-astra"]))

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
            "young_router.core.model_catalog.load_native_catalog",
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
            "young_router.core.model_catalog.load_native_catalog",
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
                "young_router.core.model_catalog.load_native_catalog",
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
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[native_profile],
        ):
            model = catalog_payload(["litellm-sol"])["models"][0]

        self.assertEqual("litellm-sol", model["slug"])
        self.assertEqual("litellm-sol", model["display_name"])
        self.assertEqual("v4", model["multi_agent_version"])
        self.assertEqual(["kept"], model["future_native_field"])

    def test_inherited_code_mode_profile_gets_the_direct_tool_surface(self) -> None:
        native_profile = {
            "slug": "gpt-6-astra",
            "display_name": "GPT-6-Astra",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "shell_type": "unified_exec",
            "tool_mode": "code_mode_only",
            "apply_patch_tool_type": "freeform",
            "experimental_supported_tools": ["send_user_message_async", "clock"],
        }

        with mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[native_profile],
        ):
            model = catalog_payload(["kimi-k3"])["models"][0]

        self.assertEqual("shell_command", model["shell_type"])
        self.assertEqual("direct", model["tool_mode"])
        self.assertEqual("freeform", model["apply_patch_tool_type"])
        self.assertEqual(
            ["send_user_message_async", "clock"],
            model["experimental_supported_tools"],
        )

    def test_exact_native_profile_keeps_its_own_tool_surface(self) -> None:
        native_profile = {
            "slug": "kimi-k3",
            "display_name": "Kimi K3",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "shell_type": "unified_exec",
            "tool_mode": "code_mode_only",
            "include_plugin_usage_instructions": False,
            "include_skills_usage_instructions": False,
        }

        with mock.patch(
            "young_router.core.model_catalog.load_native_catalog",
            return_value=[native_profile],
        ):
            model = catalog_payload(["kimi-k3"])["models"][0]

        self.assertEqual("unified_exec", model["shell_type"])
        self.assertEqual("code_mode_only", model["tool_mode"])
        self.assertFalse(model["include_plugin_usage_instructions"])
        self.assertFalse(model["include_skills_usage_instructions"])

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
                "YOUNG_ROUTER_UNKNOWN_MODEL_CONTEXT_WINDOW=300000\n"
                "YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS=0\n",
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

    def test_declared_policy_answers_for_the_prefixed_deployment_spelling(self) -> None:
        """A declared name must reach the ids a deployment actually states.

        ``glm-5.3-flash`` lives behind relays, so every deployment of the group
        spells its model with an upstream prefix (``openai/glm-5.3-flash``).
        A provider-qualified id is never suffix-matched on purpose -- picking
        whichever catalogue entry ends in the same name is how a relay route was
        handed a third party's reasoning ladder -- but a record this app
        declares for the model itself is not that guess.  Without this rule the
        listing answered through the bare name while the request path answered
        nothing at all, so the published ladder was decoration.
        """

        with mock.patch("litellm.model_cost", {}):
            prefixed = ModelContextRegistry(
                refresh_enabled=False
            ).reasoning_for_model_id("sisct2/openai/glm-5.3-flash")
            openai_prefixed = ModelContextRegistry(
                refresh_enabled=False
            ).reasoning_for_model_id("openai/glm-5.3-flash")

        self.assertIsNotNone(prefixed)
        assert prefixed is not None
        self.assertEqual(("low", "high", "max"), prefixed.supported_levels)
        # The relay only thinks at the top of its dial, and the declaration
        # states that instead of forwarding a tier that produces nothing.
        self.assertIsNotNone(prefixed.thinking_level_map)
        assert prefixed.thinking_level_map is not None
        self.assertEqual("max", prefixed.thinking_level_map["high"])
        self.assertEqual("low", prefixed.thinking_level_map["low"])
        self.assertIsNotNone(openai_prefixed)
        assert openai_prefixed is not None
        self.assertEqual(prefixed.thinking_level_map, openai_prefixed.thinking_level_map)

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

    def test_search_tool_advertisement_mirrors_gpt_family_default(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "supports_search_tool": True,
            "web_search_tool_type": "text_and_image",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: gpt-5-test\n"
                "    litellm_params:\n"
                "      model: openai/gpt-5-test\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "gpt-5-test": {
                                "context_window": 272_000,
                                "max_context_window": 272_000,
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
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["gpt-5-test"], registry=registry)["models"][0]

        self.assertTrue(model["supports_search_tool"])
        self.assertEqual("text_and_image", model["web_search_tool_type"])

    def test_non_gpt_route_advertises_the_gateway_search_bridge(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "supports_search_tool": True,
            "web_search_tool_type": "text_and_image",
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: kimi-k3\n"
                "    litellm_params:\n"
                "      model: anthropic/kimi-k3\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(runtime_config_path=runtime, refresh_enabled=False)
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["kimi-k3"], registry=registry)["models"][0]

        self.assertTrue(model["supports_search_tool"])
        self.assertEqual("text_and_image", model["web_search_tool_type"])

    def test_explicit_false_native_capability_keeps_the_gateway_search_bridge(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "supports_search_tool": True,
            "web_search_tool_type": "text_and_image",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: gpt-5-test\n"
                "    litellm_params:\n"
                "      model: openai/gpt-5-test\n"
                "    model_info:\n"
                "      supports_web_search: false\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "gpt-5-test": {
                                "context_window": 272_000,
                                "max_context_window": 272_000,
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
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["gpt-5-test"], registry=registry)["models"][0]

        self.assertTrue(model["supports_search_tool"])
        self.assertEqual("text_and_image", model["web_search_tool_type"])

    def test_route_without_function_tools_does_not_advertise_search(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "supports_search_tool": True,
            "web_search_tool_type": "text_and_image",
        }
        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: no-search\n"
                "    litellm_params:\n"
                "      model: anthropic/no-search\n"
                "    model_info:\n"
                "      supports_responses_function_tools: false\n",
                encoding="utf-8",
            )
            registry = ModelContextRegistry(
                runtime_config_path=runtime,
                refresh_enabled=False,
            )
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["no-search"], registry=registry)["models"][0]

        self.assertFalse(model["supports_search_tool"])
        self.assertNotIn("web_search_tool_type", model)

    def test_official_openai_route_keeps_search_tool_advertisement(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "supports_search_tool": True,
            "web_search_tool_type": "text_and_image",
        }
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: third-party-alias\n"
                "    litellm_params:\n"
                "      custom_llm_provider: openai\n"
                "      model: gpt-5-test\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "openai/gpt-5-test": {
                                "context_window": 272_000,
                                "max_context_window": 272_000,
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
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["third-party-alias"], registry=registry)["models"][0]

        self.assertTrue(model["supports_search_tool"])
        self.assertEqual("text_and_image", model["web_search_tool_type"])

    def test_search_tool_advertisement_keeps_native_profile_without_route_config(self) -> None:
        native_profile = {
            "slug": "gpt-5.6-sol",
            "base_instructions": "Native prompt",
            "model_messages": {"instructions_template": "Native prompt"},
            "supports_search_tool": True,
            "web_search_tool_type": "text_and_image",
        }
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "unlisted-alias": {
                                "context_window": 272_000,
                                "max_context_window": 272_000,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ):
                model = catalog_payload(["unlisted-alias"], registry=registry)["models"][0]

        self.assertTrue(model["supports_search_tool"])
        self.assertEqual("text_and_image", model["web_search_tool_type"])

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

    def test_public_catalogs_supply_image_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
            cache = root / "contexts.json"

            def fetch(source: str) -> object:
                if source == MODEL_CONTEXT_SOURCES[0]:
                    return {
                        "openai": {
                            "gpt-image-public": {
                                "id": "gpt-image-public",
                                "type": "image",
                                "modalities": {"input": ["text"], "output": ["image"]},
                            }
                        }
                    }
                if source == MODEL_CONTEXT_SOURCES[2]:
                    return {
                        "openai/gpt-image-public": {
                            "id": "openai/gpt-image-public",
                            "type": "image",
                            "modalities": {"input": ["text", "image"], "output": ["image"]},
                            "supported_parameters": ["size", "quality"],
                            "default_parameters": {"quality": "auto"},
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
            self.assertTrue(registry.refresh_if_due(force=True))
            metadata = registry.image_metadata_for_model_id("openai/gpt-image-public")
            self.assertEqual("image", metadata["type"])
            self.assertEqual(["size", "quality"], metadata["supported_parameters"])
            self.assertEqual({"quality": "auto"}, metadata["default_parameters"])

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
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

    def test_provider_fallback_answer_is_kept_for_the_next_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            record = {
                "context_window": 200_000,
                "max_context_window": 200_000,
                "effective_context_window_percent": 100,
                "source": "litellm",
                "priority": 15,
            }
            fallback = mock.Mock(return_value=dict(record))
            with mock.patch(
                "young_router.core.model_contexts._litellm_record", fallback
            ), mock.patch(
                "young_router.core.model_contexts._installed_litellm_version",
                return_value="test-version",
            ):
                first = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
                self.assertEqual(200_000, first.default_record_for("openai/gpt-image-2.5").context_window)

            stored = json.loads(cache.read_text(encoding="utf-8"))["provider_lookups"]
            self.assertEqual("test-version", stored["litellm_version"])
            # LiteLLM does not know this model, and that answer is kept too: the
            # next start must not import the runtime to ask again.
            self.assertIn("openai/gpt-image-2.5", stored["records"])

            with mock.patch(
                "young_router.core.model_contexts._litellm_record",
                mock.Mock(side_effect=AssertionError("the runtime must not be imported again")),
            ), mock.patch(
                "young_router.core.model_contexts._installed_litellm_version",
                return_value="test-version",
            ):
                second = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
                self.assertEqual(200_000, second.default_record_for("openai/gpt-image-2.5").context_window)

    def test_provider_fallback_answers_are_dropped_when_litellm_changes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "fetched_at": 1_000_000,
                        "records": {},
                        "provider_lookups": {
                            "litellm_version": "previous-version",
                            "records": {"openai/gpt-image-2.5": None},
                        },
                    }
                ),
                encoding="utf-8",
            )
            asked = mock.Mock(return_value=None)
            with mock.patch(
                "young_router.core.model_contexts._litellm_record", asked
            ), mock.patch(
                "young_router.core.model_contexts._installed_litellm_version",
                return_value="test-version",
            ):
                registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
                registry.default_record_for("openai/gpt-image-2.5")

            # A new LiteLLM may know a model the old one did not.
            asked.assert_called_once_with(["openai/gpt-image-2.5"])

    def test_legacy_context_cache_refreshes_for_pi_profiles_immediately(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "runtime-settings.env"
            settings.write_text("YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
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
            settings.write_text("YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS=24\n", encoding="utf-8")
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
                ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
                [item["effort"] for item in model["supported_reasoning_levels"]],
            )
            self.assertEqual("medium", model["default_reasoning_level"])

            cached = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            cached_capability = cached.reasoning_for_model_id("anthropic/claude-fable-5")
            self.assertIsNotNone(cached_capability)
            assert cached_capability is not None
            self.assertEqual(capability.supported_levels, cached_capability.supported_levels)
            self.assertEqual(capability.thinking_level_map, cached_capability.thinking_level_map)

    def test_every_reasoning_route_publishes_max_and_ultra_even_without_one(self) -> None:
        """The client's top tiers are published for a route that maps neither.

        Codex's own resolver does not require ``max``: ``reasoning_effort_for_
        request`` in ``codex-rs/core/src/client.rs`` takes the model's
        ``multi_agent_reasoning_effort`` when it is supported, else its ``max``,
        else its highest supported non-``ultra`` level, and the client's
        ``ultra_reasoning_uses_highest_non_ultra`` test pins that fallback with
        ``ultra`` listed on a model that has no ``max``.  So the catalog states
        the top of the dial for every route that can think at all, and the
        request path folds the pick onto the hardest level the route maps
        (``proxy.reasoning._clamp_reasoning_level``).
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: route-with-max\n"
                "    litellm_params:\n"
                "      custom_llm_provider: relay\n"
                "      model: agent-max\n"
                "  - model_name: route-through-high\n"
                "    litellm_params:\n"
                "      custom_llm_provider: relay\n"
                "      model: agent-high\n",
                encoding="utf-8",
            )
            cache = root / "contexts.json"
            cache.write_text(
                json.dumps(
                    {
                        "records": {
                            "relay/agent-max": {
                                "context_window": 1000,
                                "max_context_window": 1000,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                                "reasoning": True,
                                "thinking_level_map": {
                                    "off": None,
                                    "xhigh": "xhigh",
                                    "max": "max",
                                },
                            },
                            "relay/agent-high": {
                                "context_window": 1000,
                                "max_context_window": 1000,
                                "source": MODEL_CONTEXT_SOURCES[0],
                                "priority": 40,
                                "reasoning": True,
                                "thinking_level_map": {
                                    "off": None,
                                    "xhigh": None,
                                    "max": None,
                                },
                            },
                        }
                    }
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(
                runtime_config_path=runtime, cache_path=cache, refresh_enabled=False
            )
            with_max = catalog_payload(["route-with-max"], registry=registry)["models"][0]
            through_high = catalog_payload(["route-through-high"], registry=registry)[
                "models"
            ][0]
            # The capability itself stays provider-wire only: the proxy maps a
            # client's ``ultra`` down rather than sending the word upstream.
            capability = registry.reasoning_for("route-with-max")
            through_high_capability = registry.reasoning_for("route-through-high")

        self.assertEqual(
            ["minimal", "low", "medium", "high", "xhigh", "max", "ultra"],
            [item["effort"] for item in with_max["supported_reasoning_levels"]],
        )
        self.assertEqual(
            "Maximum reasoning with automatic task delegation",
            with_max["supported_reasoning_levels"][-1]["description"],
        )
        # ``route-through-high`` maps nothing above ``high``, and still reads
        # with the client's top tiers: the pick is folded onto ``high``.
        self.assertEqual(
            ["minimal", "low", "medium", "high", "max", "ultra"],
            [item["effort"] for item in through_high["supported_reasoning_levels"]],
        )
        self.assertIsNotNone(capability)
        assert capability is not None
        # The capability itself stays provider-wire only: the published top
        # tiers never enter it, so nothing unmapped can reach an upstream.
        self.assertNotIn("ultra", capability.supported_levels)
        self.assertIsNotNone(through_high_capability)
        assert through_high_capability is not None
        self.assertEqual(
            ("minimal", "low", "medium", "high"),
            through_high_capability.supported_levels,
        )

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


class PublicModelLimitTests(unittest.TestCase):
    """A public model's explicitly configured context window overrides metadata."""

    @staticmethod
    def _registry(root: Path, body: str, *, cache_records: dict | None = None) -> ModelContextRegistry:
        runtime = root / "config.yaml"
        runtime.write_text(f"model_list:\n{body}", encoding="utf-8")
        cache = root / "contexts.json"
        if cache_records is not None:
            cache.write_text(json.dumps({"records": cache_records}), encoding="utf-8")
        return ModelContextRegistry(
            runtime_config_path=runtime,
            cache_path=cache,
            refresh_enabled=False,
        )

    def test_public_model_context_wins_over_registry_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(
                root,
                "  - model_name: gpt-5.6-sol\n"
                "    litellm_params:\n"
                "      model: openai/gpt-5.6-sol\n"
                "    model_info:\n"
                "      max_input_tokens: 372000\n",
                cache_records={
                    "openai/gpt-5.6-sol": {
                        "context_window": 1_000_000,
                        "max_context_window": 1_000_000,
                        "source": MODEL_CONTEXT_SOURCES[0],
                        "priority": 40,
                    }
                },
            )
            effective = registry.record_for("gpt-5.6-sol")
            default = registry.default_record_for("gpt-5.6-sol")
            catalog = catalog_payload(["gpt-5.6-sol"], registry=registry)["models"][0]

        self.assertEqual(372_000, effective.context_window)
        self.assertEqual(372_000, effective.max_context_window)
        # The registry default stays visible for the pane's fallback hint.
        self.assertEqual(1_000_000, default.context_window)
        # The managed catalog carries the user's policy, not the registry's.
        self.assertEqual(372_000, catalog["context_window"])
        self.assertEqual(372_000, catalog["max_context_window"])

    def test_the_smallest_declared_context_wins_across_a_groups_routes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(
                root,
                "  - model_name: gpt-5.6-sol\n"
                "    litellm_params:\n"
                "      model: openai/gpt-5.6-sol\n"
                "    model_info:\n"
                "      max_input_tokens: 372000\n"
                "  - model_name: gpt-5.6-sol\n"
                "    litellm_params:\n"
                "      model: openai/gpt-5.6-sol\n"
                "    model_info:\n"
                "      max_input_tokens: 262144\n",
            )
            record = registry.record_for("gpt-5.6-sol")

        self.assertEqual(262_144, record.context_window)

    def test_a_route_without_a_context_keeps_the_resolved_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            registry = self._registry(
                root,
                "  - model_name: gpt-5.6-sol\n"
                "    litellm_params:\n"
                "      model: openai/gpt-5.6-sol\n",
            )
            record = registry.record_for("gpt-5.6-sol")

        self.assertEqual(272_000, record.context_window)


class SharedContextCacheTests(unittest.TestCase):
    """One cache file, read by more than one registry instance.

    A pane snapshot resolves through a registry that never refreshes, while the
    Codex domain's registry owns the refresh and rewrites the same file.  A
    copy read once would keep answering from the file as it stood before that
    write, which is how a public model added after a refresh was drawn with the
    unknown-model window while pi.dev named the profile behind it.
    """

    @staticmethod
    def _cache(records: dict) -> dict:
        return {"fetched_at": 1_000_000.0, "records": records}

    @staticmethod
    def _profile(window: int) -> dict:
        return {
            "context_window": window,
            "max_context_window": window,
            "effective_context_window_percent": 95,
            "source": MODEL_CONTEXT_SOURCES[0],
            "priority": 40,
        }

    def test_a_registry_reads_a_cache_another_writer_replaced(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            reader = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            self.assertEqual(272_000, reader.record_for("gemini-4-argon").context_window)

            # The refreshing registry publishes the profile pi.dev states, and
            # the model only exists after that: the window a public model added
            # now must not be the unknown-model default the first read answered
            # with.
            cache.write_text(
                json.dumps(self._cache({"opencode/gemini-4-argon": self._profile(1_048_576)})),
                encoding="utf-8",
            )

            self.assertEqual(1_048_576, reader.record_for("gemini-4-argon").context_window)
            self.assertEqual(1_048_576, reader.default_record_for("gemini-4-argon").context_window)

    def test_a_declared_public_window_still_wins_over_the_reloaded_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: gemini-4-argon\n"
                "    litellm_params:\n"
                "      model: openai/gemini-4-argon\n"
                "    model_info:\n"
                "      max_input_tokens: 200000\n",
                encoding="utf-8",
            )
            cache = root / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            registry = ModelContextRegistry(
                runtime_config_path=runtime,
                cache_path=cache,
                refresh_enabled=False,
            )
            self.assertEqual(200_000, registry.record_for("gemini-4-argon").context_window)

            cache.write_text(
                json.dumps(self._cache({"opencode/gemini-4-argon": self._profile(1_048_576)})),
                encoding="utf-8",
            )

            effective = registry.record_for("gemini-4-argon")
            default = registry.default_record_for("gemini-4-argon")

        self.assertEqual(200_000, effective.context_window)
        self.assertEqual(1_048_576, default.context_window)

    def test_a_relay_model_publishes_the_ladder_its_copies_agree_on(self) -> None:
        """One provider copy may not decide a relay model's thinking for the route.

        ``deepseek-v4.1-flash`` is DeepSeek's own model, and this installation
        reaches it through relays whose deployment spells it
        ``openai/deepseek-v4.1-flash`` -- a wire prefix no public catalog owns,
        so the registry's suffix match finds every provider that serves the
        model.  Reading the first copy in the cache made *that* provider's wire
        the route's ladder: the copy Baseten publishes maps ``low`` to ``null``,
        and it came first, so the catalog offered ``none/high/max`` for a model
        whose own vendor entry (pi.dev's ``deepseek`` provider, the same
        "DeepSeek V4.1 Flash") serves ``low``, ``high`` and ``max``.  A cache
        refresh that reordered the copies published a different ladder for the
        same route.

        ``medium`` is deliberately absent from the result: DeepSeek's own entry
        states ``medium: null``, and offering a tier the provider cannot take is
        the same defect in the other direction.
        """

        # The copies the public catalogs publish for this model, as of the
        # observation.  Only the ones spelled ``.../deepseek-v4.1-flash``
        # participate in the route's suffix match; the vendor's own entry is
        # named ``deepseek-flash`` and is the reference the merge must reproduce.
        copies: dict[str, dict[str, str | None] | None] = {
            "baseten/deepseek-ai/deepseek-v4.1-flash": {
                "off": "none", "minimal": None, "low": None, "medium": None,
                "high": "high", "xhigh": None, "max": "max",
            },
            "huggingface/deepseek-ai/deepseek-v4.1-flash": {
                "off": "none", "minimal": None, "low": "low", "medium": None,
                "high": "high", "xhigh": "xhigh", "max": "max",
            },
            "nvidia/deepseek-ai/deepseek-v4.1-flash": {
                "minimal": None, "low": None, "medium": None,
                "high": "high", "max": "max",
            },
            "opencode-go/deepseek-v4.1-flash": {
                "off": None, "minimal": None, "low": "low", "medium": None,
                "high": "high", "xhigh": None, "max": "max",
            },
            "opencode/deepseek-v4.1-flash": {
                "off": None, "minimal": None, "low": "low", "medium": None,
                "high": "high", "xhigh": None, "max": "max",
            },
            "openrouter/deepseek/deepseek-v4.1-flash": {
                "off": "none", "minimal": None, "low": "low", "medium": None,
                "high": "high", "xhigh": None, "max": "max",
            },
            "qwen-token-plan/deepseek-v4.1-flash": {
                "off": None, "minimal": None, "low": "low", "medium": None,
                "high": "high", "xhigh": None, "max": "max",
            },
            "radius/deepseek-v4.1-flash": {
                "off": "none", "minimal": None, "low": "low", "medium": None,
                "high": "high", "xhigh": None, "max": "max",
            },
            # Together states no thinking above ``high``.
            "together/deepseek-ai/deepseek-v4.1-flash": {
                "minimal": None, "low": None, "medium": None,
            },
            # A copy that states no level map at all states nothing about the
            # ladder, so it takes no part in the merge.
            "vercel-ai-gateway/deepseek/deepseek-v4.1-flash": None,
            # The vendor's own ladder, under the id its own provider uses.
            "deepseek/deepseek-flash": {
                "minimal": None, "low": "low", "medium": None,
                "high": "high", "max": "max",
            },
        }

        ladders: list[list[str]] = []
        defaults: list[str] = []
        for order in (list(copies), list(reversed(copies))):
            with tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                runtime = root / "config.yaml"
                runtime.write_text(
                    "model_list:\n"
                    "  - model_name: deepseek-v4.1-flash\n"
                    "    litellm_params:\n"
                    "      model: openai/deepseek-v4.1-flash\n",
                    encoding="utf-8",
                )
                records = {}
                for key in order:
                    record = self._profile(1_000_000)
                    record["reasoning"] = True
                    if copies[key] is not None:
                        record["thinking_level_map"] = dict(copies[key])
                    records[key] = record
                cache = root / "model-contexts.json"
                cache.write_text(json.dumps(self._cache(records)), encoding="utf-8")
                registry = ModelContextRegistry(
                    runtime_config_path=runtime,
                    cache_path=cache,
                    refresh_enabled=False,
                )
                with mock.patch(
                    "young_router.core.model_catalog.load_native_catalog",
                    return_value=[
                        {
                            "slug": "native-agent",
                            "base_instructions": "Native prompt",
                            "model_messages": {"instructions_template": "Native prompt"},
                            "default_reasoning_level": "medium",
                            "supported_reasoning_levels": [
                                {"effort": level, "description": f"Native {level}"}
                                for level in ("minimal", "low", "medium", "high")
                            ],
                        }
                    ],
                ), mock.patch("litellm.model_cost", {}):
                    model = catalog_payload(
                        ["deepseek-v4.1-flash"], registry=registry
                    )["models"][0]
                ladders.append(
                    [item["effort"] for item in model["supported_reasoning_levels"]]
                )
                defaults.append(model["default_reasoning_level"])
                vendor = registry.reasoning_for_model_id("deepseek/deepseek-flash")
                self.assertIsNotNone(vendor)
                assert vendor is not None
                self.assertEqual(
                    published_reasoning_levels(vendor.supported_levels),
                    ladders[-1],
                )

        # DeepSeek's own ladder, not the first copy's: low is offered, medium is
        # not, and the hardest tier keeps its published step above it.
        self.assertEqual(["none", "low", "high", "max", "ultra"], ladders[0])
        # The cache's record order is an accident of the last refresh; it may
        # never be what decides a route's ladder.
        self.assertEqual(ladders[0], ladders[1])
        self.assertEqual(["low", "low"], defaults)

    def test_a_route_ladder_is_the_one_the_request_path_maps(self) -> None:
        """The catalog and the Pi-facing listing must state one ladder.

        ``reasoning_for`` answers ``None`` for a relay alias whose deployments
        resolve nothing by their provider-qualified id, and the catalog then
        kept the inherited native profile's ``low``..``ultra``.  The same route
        reported ``low``/``high``/``max`` on ``/v1/models``, so one route had two
        ladders and a default it does not have.  Observed live 2026-11-02 with
        ``deepseek-v4.1-flash``: catalog ``low``..``ultra``, listing
        ``off,high,max,ultra``.
        """

        with tempfile.TemporaryDirectory() as directory:
            runtime = Path(directory) / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: deepseek-v4.1-flash\n"
                "    litellm_params:\n"
                "      model: openai/deepseek-v4.1-flash\n"
                "  - model_name: deepseek-v4.1-flash\n"
                "    litellm_params:\n"
                "      model: openai/deepseek-flash\n",
                encoding="utf-8",
            )
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    self._cache(
                        {
                            "openai/deepseek-v4.1-flash": {
                                **self._profile(1_000_000),
                                "reasoning": True,
                                "supported_reasoning_levels": ["none", "high", "max"],
                                "default_reasoning_level": "high",
                            }
                        }
                    )
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(
                runtime_config_path=runtime,
                cache_path=cache,
                refresh_enabled=False,
            )
            # The group's own route lookup refuses -- one deployment resolves
            # nothing -- while the public name still answers.
            self.assertIsNone(registry.reasoning_for("deepseek-v4.1-flash"))
            self.assertIsNotNone(
                registry.reasoning_for_model_id("deepseek-v4.1-flash")
            )

            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[
                    {
                        "slug": "native-agent",
                        "base_instructions": "Native prompt",
                        "model_messages": {"instructions_template": "Native prompt"},
                        "default_reasoning_level": "low",
                        "supported_reasoning_levels": [
                            {"effort": level, "description": f"Native {level}"}
                            for level in ("low", "medium", "high", "xhigh", "max", "ultra")
                        ],
                    }
                ],
            ), mock.patch("litellm.model_cost", {}):
                model = catalog_payload(
                    ["deepseek-v4.1-flash"],
                    registry=registry,
                )["models"][0]

        self.assertEqual(
            ["none", "high", "max", "ultra"],
            [item["effort"] for item in model["supported_reasoning_levels"]],
        )
        self.assertEqual("high", model["default_reasoning_level"])

    def test_a_custom_route_keeps_ultra_at_its_own_top(self) -> None:
        """A relay route's ``ultra`` must not inherit a weaker native pin.

        Codex sends the model's ``multi_agent_reasoning_effort`` when a person
        picks ``ultra``, falling back to ``max`` and then to the model's highest
        supported non-``ultra`` level.  ``gpt-6.1-sol`` pins ``xhigh`` on
        purpose (measured on the installed client: picking Ultra sent
        ``reasoning.effort: xhigh``), so a route that inherited the pin sent
        ``xhigh`` for Ultra while the ``max`` published directly below it sent
        ``max`` -- the top tier thinking less than the one under it.  A custom
        route therefore keeps the client's fallback, and its own model is
        untouched.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            runtime = root / "config.yaml"
            runtime.write_text(
                "model_list:\n"
                "  - model_name: relay-agent\n"
                "    litellm_params:\n"
                "      model: openai/agent-x\n"
                "  - model_name: gpt-6.1-sol\n"
                "    litellm_params:\n"
                "      model: openai/gpt-6.1-sol\n",
                encoding="utf-8",
            )
            cache = root / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            registry = ModelContextRegistry(
                runtime_config_path=runtime, cache_path=cache, refresh_enabled=False
            )
            native = {
                "slug": "native-agent",
                "base_instructions": "Native prompt",
                "model_messages": {"instructions_template": "Native prompt"},
                "multi_agent_version": "v2",
                "multi_agent_reasoning_effort": "xhigh",
                "default_reasoning_level": "medium",
                "supported_reasoning_levels": [
                    {"effort": level, "description": f"Native {level}"}
                    for level in ("low", "medium", "high", "xhigh", "max", "ultra")
                ],
            }
            own_model = {**native, "slug": "gpt-6.1-sol", "priority": 1}
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[own_model, native],
            ), mock.patch("litellm.model_cost", {}):
                custom = catalog_payload(["relay-agent"], registry=registry)["models"][0]
                exact = catalog_payload(["gpt-6.1-sol"], registry=registry)["models"][0]

        self.assertNotIn("multi_agent_reasoning_effort", custom)
        # Delegation itself stays: ``ultra`` still turns on proactive agents.
        self.assertEqual("v2", custom["multi_agent_version"])
        # The client's own model keeps the pin it declares.
        self.assertEqual("xhigh", exact["multi_agent_reasoning_effort"])

    def test_an_image_route_publishes_only_its_disabled_step(self) -> None:
        """A reasoning slider on an image route is a control that does nothing.

        The entry inherits a text profile's ladder, so an image route advertised
        six thinking levels it cannot honour while its listing stated ``off``.
        The modality comes from the public catalog, not from the name.
        """

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    self._cache(
                        {
                            "openai/gpt-image-public": {
                                **self._profile(272_000),
                                "image_model_metadata": {
                                    "type": "image",
                                    "input_modalities": ["text", "image"],
                                    "output_modalities": ["image"],
                                },
                            },
                            "openai/text-route": {
                                **self._profile(272_000),
                                "reasoning": True,
                                "supported_reasoning_levels": ["low", "high"],
                                "default_reasoning_level": "high",
                            },
                        }
                    )
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            native_profile = {
                "slug": "native-agent",
                "base_instructions": "Native prompt",
                "model_messages": {"instructions_template": "Native prompt"},
                "default_reasoning_level": "low",
                "supported_reasoning_levels": [
                    {"effort": level, "description": f"Native {level}"}
                    for level in ("low", "medium", "high", "xhigh", "max", "ultra")
                ],
            }
            with mock.patch(
                "young_router.core.model_catalog.load_native_catalog",
                return_value=[native_profile],
            ), mock.patch("litellm.model_cost", {}):
                image_model = catalog_payload(
                    ["openai/gpt-image-public"],
                    registry=registry,
                )["models"][0]
                text_model = catalog_payload(
                    ["openai/text-route"],
                    registry=registry,
                )["models"][0]

        self.assertEqual(
            ["none"],
            [item["effort"] for item in image_model["supported_reasoning_levels"]],
        )
        self.assertEqual("none", image_model["default_reasoning_level"])
        # The same cache, read as a text route: it keeps its own ladder and
        # gains the client's top tiers, exactly as every reasoning route does.
        self.assertEqual(
            ["low", "high", "max", "ultra"],
            [item["effort"] for item in text_model["supported_reasoning_levels"]],
        )

    def test_the_picker_filter_keeps_a_generator_out_of_the_client_lists(self) -> None:
        """The catalog's own answer is what both clients' pickers consult.

        ``/v1/models`` keeps serving every route, so a filter placed on the
        listing would hide the generator from Pi too.  The pickers are built
        from ``codex_config.local_exposed_models``, which asks this, so the
        answer is the one the catalog already resolves per route: only a route
        that serves images and states no thinking step above ``off``.
        """

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "contexts.json"
            cache.write_text(
                json.dumps(
                    self._cache(
                        {
                            "openai/gpt-image-public": {
                                **self._profile(272_000),
                                "image_model_metadata": {
                                    "type": "image",
                                    "input_modalities": ["text", "image"],
                                    "output_modalities": ["image"],
                                },
                            },
                            # A group that also serves a reasoning text model keeps
                            # its place in the picker: that route is a conversation.
                            "openai/omni-route": {
                                **self._profile(272_000),
                                "reasoning": True,
                                "supported_reasoning_levels": ["low", "high"],
                                "default_reasoning_level": "high",
                                "image_model_metadata": {
                                    "type": "image",
                                    "input_modalities": ["text", "image"],
                                    "output_modalities": ["image", "text"],
                                },
                            },
                            "openai/text-route": {
                                **self._profile(272_000),
                                "reasoning": True,
                                "supported_reasoning_levels": ["low", "high"],
                                "default_reasoning_level": "high",
                            },
                        }
                    )
                ),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)

            self.assertTrue(
                route_serves_only_images("openai/gpt-image-public", registry=registry)
            )
            self.assertFalse(
                route_serves_only_images("openai/omni-route", registry=registry)
            )
            self.assertFalse(
                route_serves_only_images("openai/text-route", registry=registry)
            )
            self.assertFalse(route_serves_only_images("", registry=registry))
            self.assertFalse(route_serves_only_images(None, registry=registry))  # type: ignore[arg-type]

    def test_an_unreadable_catalog_does_not_hide_every_route(self) -> None:
        """An exception in the filter must answer "chat", never "image"."""

        with mock.patch(
            "young_router.core.model_catalog._image_filter_registry",
            side_effect=RuntimeError("cache unreadable"),
        ):
            self.assertFalse(route_serves_only_images("some-chat-model"))

    def test_the_picker_filter_builds_its_registry_from_this_installation(self) -> None:
        """The lazy registry must really resolve, not silently answer "chat".

        ``route_serves_only_images`` fails open, so a builder that raised would
        still return ``False`` for every route and the filter would become a
        no-op -- image models back in both pickers, with nothing failing.  This
        drives the real builder over a real cache under a temporary runtime
        root, which is the only shape that distinguishes "this is a chat model"
        from "the lookup never ran".
        """

        import young_router.core.model_catalog as catalog_module

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / "model-contexts.json"
            cache.write_text(
                json.dumps(
                    self._cache(
                        {
                            "openai/gpt-image-public": {
                                **self._profile(272_000),
                                "image_model_metadata": {
                                    "type": "image",
                                    "input_modalities": ["text", "image"],
                                    "output_modalities": ["image"],
                                },
                            },
                            "openai/text-route": {
                                **self._profile(272_000),
                                "reasoning": True,
                                "supported_reasoning_levels": ["low", "high"],
                                "default_reasoning_level": "high",
                            },
                        }
                    )
                ),
                encoding="utf-8",
            )
            with mock.patch.object(
                catalog_module, "_IMAGE_FILTER_REGISTRY", None
            ), mock.patch(
                "young_router.config_paths.runtime_root", return_value=root
            ):
                self.assertTrue(route_serves_only_images("openai/gpt-image-public"))
                self.assertFalse(route_serves_only_images("openai/text-route"))

    def test_a_generator_whose_catalog_states_no_window_has_no_window(self) -> None:
        """A generator holds no conversation, so 272,000 is not its window.

        pi.dev and models.dev state ``limit.context: 0`` for
        ``gpt-image-2.5-flare`` and its siblings.  That zero is the whole
        statement, and the record must keep it: folding it into the
        unknown-model placeholder published 272,000 as if the catalog had said
        so, on the pane's 公开上下文 and in the catalog Codex compacts against.
        """

        record = model_contexts_module._record_from_payload(
            {
                "id": "openai/gpt-image-2.5-flare",
                "type": "image",
                "modalities": {"input": ["text", "image"], "output": ["image"]},
                "limit": {"context": 0, "output": 0},
            },
            source="https://models.dev/models.json",
            priority=1,
        )
        self.assertIsNotNone(record)
        self.assertEqual(0, record["context_window"])
        self.assertEqual(0, record["max_context_window"])

        # A text record that states nothing still falls back to the placeholder:
        # the rule is about a stated ``0`` beside an image modality, not about
        # every incomplete record.
        self.assertIsNone(
            model_contexts_module._record_from_payload(
                {"id": "openai/unstated-chat"}, source="pi.dev", priority=1
            )
        )

    def test_a_generator_no_profile_describes_states_no_window(self) -> None:
        """The placeholder is a guess about a conversation this route has none of.

        ``gpt-image-2.5-flare`` is stated as ``image_generation`` by the cost
        map bundled with this install, while the registry holds no profile for
        it.  The pane and the managed catalog both read ``record_for``, so both
        must answer "no window" instead of 272,000.
        """

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)

            self.assertEqual(0, registry.record_for("gpt-image-2.5-flare").context_window)
            self.assertEqual(0, registry.default_record_for("gpt-image-2.5-flare").context_window)
            # A text route the registry cannot resolve keeps the placeholder:
            # the rule is the modality, not the absence of a profile.
            self.assertEqual(
                272_000, registry.record_for("totally-unknown-text-route").context_window
            )

    def test_a_probe_result_reaches_the_window_and_survives_a_restart(self) -> None:
        """The probe is the only surface that asks the Images question.

        A provider that ships a model ahead of the bundled LiteLLM cost map
        leaves the modality unreadable from any table, so a probe is what
        settles it.  The pane and the managed catalog are written by domains
        that never probe, so the finding has to travel through the cache they
        both open -- exactly as a stated window does.
        """

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            first = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            with mock.patch.object(
                model_contexts_module, "_litellm_states_image_generation", return_value=False
            ):
                # No table knows it: the placeholder would be published.
                self.assertEqual(
                    272_000, first.default_record_for("vendor/new-generator").context_window
                )
                first.remember_image_generator(["vendor/new-generator"])
                self.assertEqual(
                    0, first.default_record_for("vendor/new-generator").context_window
                )

            # A restart reads the same finding from the shared cache; no probe
            # runs in this process at all.
            second = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            self.assertEqual(
                0, second.default_record_for("vendor/new-generator").context_window
            )
            # The negative is recorded too, so a chat route is never re-asked
            # and never loses its window to a stale positive.
            self.assertFalse(second.is_remembered_image_generator(["vendor/new-chat"]))

    def test_a_recorded_negative_keeps_a_chat_route_a_chat_route(self) -> None:
        """``generator=False`` is evidence, not an absence of evidence."""

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            with mock.patch.object(
                model_contexts_module, "_litellm_states_image_generation", return_value=True
            ):
                registry.remember_image_generator(["openai/relay-chat"], generator=False)
            self.assertFalse(registry.is_remembered_image_generator(["openai/relay-chat"]))
            # The finding is remembered under the route's own spellings, and
            # only those: a probe is a statement about one model.
            self.assertFalse(registry.is_remembered_image_generator(["relay-chat"]))

    def test_a_generator_publishes_no_reasoning_ladder_in_the_catalog(self) -> None:
        """A generator is not a conversation, and its entry states no window.

        LiteLLM's cost map is where a name no public catalog has listed yet
        gets its modality, and that map is what keeps the Codex catalog from
        carrying a 272,000-token window for a model that never reads a prompt.
        """

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-contexts.json"
            cache.write_text(json.dumps(self._cache({})), encoding="utf-8")
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            with mock.patch.object(
                catalog_module, "load_native_catalog", return_value=[]
            ):
                entry = catalog_payload(["gpt-image-2.5-flare"], registry=registry)["models"][0]

        self.assertNotIn("context_window", entry)
        self.assertNotIn("max_context_window", entry)
        self.assertEqual([{"effort": "none", "description": mock.ANY}], [
            {"effort": level["effort"], "description": level.get("description", mock.ANY)}
            for level in entry["supported_reasoning_levels"]
        ])

    def test_a_cache_that_disappears_keeps_the_records_already_in_hand(self) -> None:
        """Losing the file is not evidence a resolved window became unknown."""

        with tempfile.TemporaryDirectory() as directory:
            cache = Path(directory) / "model-contexts.json"
            cache.write_text(
                json.dumps(self._cache({"opencode/gemini-4-argon": self._profile(1_048_576)})),
                encoding="utf-8",
            )
            registry = ModelContextRegistry(cache_path=cache, refresh_enabled=False)
            self.assertEqual(1_048_576, registry.record_for("gemini-4-argon").context_window)

            cache.unlink()

            self.assertEqual(1_048_576, registry.record_for("gemini-4-argon").context_window)


if __name__ == "__main__":
    unittest.main()
