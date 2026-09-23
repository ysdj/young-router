from __future__ import annotations

import json
from pathlib import Path

from hook_test_utils import HookTestCase, load_hook_module


class HookReasoningMappingTests(HookTestCase):
    def _write_contexts_cache(self, root: Path, records: dict, *, legacy: bool = False) -> Path:
        payload = json.dumps({"records": records})
        codex_home = root / "codex"
        codex_home.mkdir(exist_ok=True)
        if legacy:
            path = codex_home / "young-router-model-contexts.json"
        else:
            path = root / "model-contexts.json"
        path.write_text(payload, encoding="utf-8")
        self.set_env("CODEX_HOME", str(codex_home))
        self.set_env("LITELLM_RUNTIME_ROOT", str(root))
        self.set_env("LITELLM_CONFIG_FILE", str(root / "config.yaml"))
        return path

    def _configure_pi_cache(self, root: Path, records: dict) -> None:
        self._write_contexts_cache(root, records)

    def test_legacy_codex_home_cache_is_migrated_into_the_runtime_root(self) -> None:
        hooks, _ = load_hook_module()
        root = Path(self.create_temp_dir())
        legacy = self._write_contexts_cache(
            root,
            {"custom/agent": self._record({"high": "high"})},
            legacy=True,
        )

        registry = hooks._reasoning_registry()
        record = registry.record_for("custom/agent")

        self.assertEqual(1000, record.context_window)
        self.assertTrue((root / "model-contexts.json").exists())
        self.assertFalse(legacy.exists())

    @staticmethod
    def _record(thinking_level_map: dict[str, str | None], *, reasoning: bool = True) -> dict:
        return {
            "context_window": 1000,
            "max_context_window": 1000,
            "source": "https://pi.dev/api/models",
            "priority": 40,
            "reasoning": reasoning,
            "thinking_level_map": thinking_level_map,
        }

    def test_request_maps_provider_wire_effort_and_clamps_like_pi(self) -> None:
        hooks, _ = load_hook_module()
        root = Path(self.create_temp_dir())
        self._configure_pi_cache(
            root,
            {
                "baseten/moonshotai/kimi-k2.5": self._record(
                    {
                        "off": "off",
                        "minimal": None,
                        "low": None,
                        "medium": None,
                        "high": "high",
                        "xhigh": None,
                        "max": None,
                    }
                ),
                "custom/agent": self._record(
                    {
                        "off": "disabled",
                        "minimal": "tiny",
                        "low": "small",
                        "medium": "balanced",
                        "high": "deep",
                        "xhigh": "xdeep",
                        "max": "maximum",
                    }
                ),
            },
        )

        request = {
            "model": "kimi",
            "litellm_params": {
                "model": "moonshotai/Kimi-K2.5",
                "custom_llm_provider": "baseten",
            },
            "reasoning": {"effort": "low"},
            "reasoning_effort": "xhigh",
        }
        mapped = hooks._with_model_reasoning_mapping(request)
        self.assertIsNotNone(mapped)
        assert mapped is not None
        self.assertEqual("high", mapped["reasoning"]["effort"])
        self.assertEqual("high", mapped["reasoning_effort"])
        self.assertEqual("low", request["reasoning"]["effort"])

        request = {
            "model": "agent",
            "litellm_params": {"model": "agent", "custom_llm_provider": "custom"},
            "reasoning": {"effort": "none"},
            "extra_body": {"reasoning": {"effort": "max"}},
        }
        mapped = hooks._with_model_reasoning_mapping(request)
        self.assertIsNotNone(mapped)
        assert mapped is not None
        self.assertEqual("disabled", mapped["reasoning"]["effort"])
        self.assertEqual("maximum", mapped["extra_body"]["reasoning"]["effort"])

    def test_request_uses_exact_provider_route_and_leaves_unknown_effort_untouched(self) -> None:
        hooks, _ = load_hook_module()
        root = Path(self.create_temp_dir())
        self._configure_pi_cache(
            root,
            {
                "openai/same-name": self._record({"off": "none", "xhigh": "xhigh", "max": "max"}),
                "other/same-name": self._record({"off": "none", "high": "high"}),
            },
        )
        request = {
            "model": "same-name",
            "litellm_params": {"model": "same-name", "custom_llm_provider": "other"},
            "reasoning_effort": "xhigh",
        }
        mapped = hooks._with_model_reasoning_mapping(request)
        self.assertIsNotNone(mapped)
        assert mapped is not None
        self.assertEqual("high", mapped["reasoning_effort"])

        unknown = {
            "model": "other/same-name",
            "reasoning_effort": "vendor-custom-level",
        }
        self.assertIsNone(hooks._with_model_reasoning_mapping(unknown))

    async def test_pre_call_hook_applies_pi_mapping_after_deployment_selection(self) -> None:
        hooks, _ = load_hook_module()
        root = Path(self.create_temp_dir())
        self._configure_pi_cache(
            root,
            {
                "provider/agent": self._record(
                    {
                        "off": "none",
                        "minimal": None,
                        "low": "low",
                        "medium": "medium",
                        "high": "high",
                        "xhigh": None,
                        "max": "max",
                    }
                )
            },
        )
        request = {
            "model": "public-agent",
            "litellm_params": {
                "model": "agent",
                "custom_llm_provider": "provider",
            },
            "reasoning": {"effort": "xhigh"},
        }

        mapped = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            request,
            call_type="aresponses",
        )

        self.assertIsNotNone(mapped)
        assert mapped is not None
        # Pi searches upward first when an unsupported level is clamped, so
        # xhigh selects the explicitly supported max level here.
        self.assertEqual("max", mapped["reasoning"]["effort"])
        self.assertEqual("xhigh", request["reasoning"]["effort"])

    def create_temp_dir(self) -> str:
        import tempfile

        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        return directory.name

    def _state_file(self) -> Path:
        path = Path(self.create_temp_dir()) / "routing.json"
        self.set_env("YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FILE", str(path))
        return path

    @staticmethod
    def _rejection(message: str) -> Exception:
        class ProviderBadRequest(Exception):
            status_code = 400

        return ProviderBadRequest(message)

    def test_reasoning_configuration_rejection_is_classified_narrowly(self) -> None:
        hooks, _ = load_hook_module()

        self.assertTrue(
            hooks._is_reasoning_configuration_unsupported_error(
                self._rejection(
                    'OpenAIException - model "[次]gemini-3.8-flash" does not have a '
                    "known gemini thinking configuration (request id: 2026abcd)"
                )
            )
        )
        # An ordinary request/format 400 that merely mentions the model must not
        # be turned into a reasoning-parameter change.
        self.assertFalse(
            hooks._is_reasoning_configuration_unsupported_error(
                self._rejection("invalid_request_error: model gemini-3.8-flash is not available")
            )
        )
        self.assertFalse(
            hooks._is_reasoning_configuration_unsupported_error(
                self._rejection("invalid_request_error: messages must not be empty")
            )
        )

    def test_reasoning_parameters_compat_retry_requires_the_rejection(self) -> None:
        hooks, _ = load_hook_module()
        self._state_file()
        request = {
            "model": "[次]gemini-3.8-flash",
            "reasoning_effort": "high",
            "model_info": {"id": "baa28073"},
        }

        rejection = self._rejection(
            'OpenAIException - model "[次]gemini-3.8-flash" does not have a known '
            "gemini thinking configuration"
        )
        candidates = hooks._reasoning_parameters_compat_retry_candidates(
            rejection,
            request,
        )
        # The native Google shape keeps the requested level and is tried first;
        # the strip replay stays as the fallback for a gateway that refuses it.
        self.assertEqual(
            ["google_thinking_level", "strip"],
            [mode for mode, _kwargs in candidates],
        )
        google_kwargs = candidates[0][1]
        self.assertNotIn("reasoning_effort", google_kwargs)
        self.assertEqual(
            "high",
            google_kwargs["extra_body"]["extra_body"]["google"]["thinking_config"][
                "thinking_level"
            ],
        )
        self.assertTrue(
            google_kwargs["litellm_metadata"][
                hooks._REASONING_PARAMETERS_COMPAT_RETRY_METADATA_KEY
            ]
        )
        strip_kwargs = candidates[1][1]
        self.assertNotIn("reasoning_effort", strip_kwargs)
        self.assertNotIn("extra_body", strip_kwargs)
        # The client request object stays intact and the compat retry happens
        # at most once per request.
        self.assertEqual("high", request["reasoning_effort"])
        self.assertEqual(
            [],
            hooks._reasoning_parameters_compat_retry_candidates(
                rejection,
                google_kwargs,
            ),
        )
        # A non-Gemini route only gets the strip replay.
        self.assertEqual(
            ["strip"],
            [
                mode
                for mode, _kwargs in hooks._reasoning_parameters_compat_retry_candidates(
                    rejection,
                    {"model": "plain-model", "reasoning_effort": "high"},
                )
            ],
        )
        # A request without reasoning fields cannot be helped by this retry.
        self.assertEqual(
            [],
            hooks._reasoning_parameters_compat_retry_candidates(
                rejection,
                {"model": "[次]gemini-3.8-flash", "messages": []},
            ),
        )
        self.assertEqual(
            [],
            hooks._reasoning_parameters_compat_retry_candidates(
                self._rejection("invalid_request_error: bad payload"),
                request,
            ),
        )

    def test_google_thinking_level_mapping_keeps_the_client_level(self) -> None:
        hooks, _ = load_hook_module()
        for effort, level in (
            ("none", "minimal"),
            ("minimal", "minimal"),
            ("low", "low"),
            ("medium", "medium"),
            ("high", "high"),
            ("xhigh", "high"),
            ("max", "high"),
        ):
            translated = hooks._with_google_thinking_level(
                {"model": "[次]gemini-3.8-flash", "reasoning_effort": effort}
            )
            assert translated is not None, effort
            self.assertNotIn("reasoning_effort", translated)
            self.assertEqual(
                level,
                translated["extra_body"]["extra_body"]["google"]["thinking_config"][
                    "thinking_level"
                ],
            )
        # A client that already chose a native thinking configuration keeps it.
        self.assertIsNone(
            hooks._with_google_thinking_level(
                {
                    "model": "[次]gemini-3.8-flash",
                    "reasoning_effort": "high",
                    "extra_body": {
                    "extra_body": {
                        "google": {"thinking_config": {"thinking_budget": 512}}
                    }
                },
                }
            )
        )
        # A non-canonical level has no native equivalent.
        self.assertIsNone(
            hooks._with_google_thinking_level(
                {"model": "[次]gemini-3.8-flash", "reasoning_effort": "vendor-custom"}
            )
        )

    async def test_learned_native_reasoning_shape_is_applied_to_later_requests(self) -> None:
        hooks, _ = load_hook_module()
        self._state_file()
        request = {
            "model": "public-gemini",
            "litellm_params": {"model": "[次]gemini-3.8-flash"},
            "model_info": {"id": "baa28073"},
            "reasoning": {"effort": "high"},
            "reasoning_effort": "high",
            "extra_body": {"reasoning_effort": "high"},
        }

        self.assertTrue(
            hooks._record_reasoning_parameters_compat(
                request,
                "google_thinking_level",
            )
        )
        state = hooks._reasoning_parameters_compat_cached(request)
        self.assertIsNotNone(state)
        assert state is not None
        self.assertEqual("google_thinking_level", state["mode"])

        normalized = hooks._with_model_reasoning_parameter_support(request)
        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertNotIn("reasoning", normalized)
        self.assertNotIn("reasoning_effort", normalized)
        self.assertNotIn("reasoning_effort", normalized["extra_body"])
        self.assertEqual(
            "high",
            normalized["extra_body"]["extra_body"]["google"]["thinking_config"][
                "thinking_level"
            ],
        )
        # The caller's own request object is never mutated in place.
        self.assertEqual("high", request["reasoning_effort"])

        # A remembered strip route keeps working without the native shape.
        strip_request = {
            "model": "public-plain",
            "model_info": {"id": "strip-route"},
            "reasoning_effort": "high",
        }
        self.assertTrue(
            hooks._record_reasoning_parameters_compat(strip_request, "strip")
        )
        stripped = hooks._with_model_reasoning_parameter_support(strip_request)
        self.assertIsNotNone(stripped)
        assert stripped is not None
        self.assertNotIn("reasoning_effort", stripped)
        self.assertNotIn("extra_body", stripped)

        # A request without reasoning fields is left alone, and an unrelated
        # deployment is not affected by another route's memory.
        self.assertIsNone(
            hooks._with_model_reasoning_parameter_support(
                {"model": "public-gemini", "model_info": {"id": "baa28073"}}
            )
        )
        self.assertIsNone(
            hooks._with_model_reasoning_parameter_support(
                {
                    "model": "public-gemini",
                    "model_info": {"id": "other-route"},
                    "reasoning_effort": "high",
                }
            )
        )

    async def test_pre_call_hook_applies_learned_native_reasoning_shape(self) -> None:
        hooks, _ = load_hook_module()
        self._state_file()
        root = Path(self.create_temp_dir())
        (root / "codex").mkdir()
        self.set_env("CODEX_HOME", str(root / "codex"))
        self.set_env("LITELLM_RUNTIME_ROOT", str(root))
        self.set_env("LITELLM_CONFIG_FILE", str(root / "config.yaml"))
        hooks._record_reasoning_parameters_compat(
            {
                "model": "public-gemini",
                "litellm_params": {"model": "[次]gemini-3.8-flash"},
                "model_info": {"id": "baa28073"},
                "reasoning_effort": "high",
            },
            "google_thinking_level",
        )
        request = {
            "model": "public-gemini",
            "litellm_params": {"model": "[次]gemini-3.8-flash"},
            "model_info": {"id": "baa28073"},
            "reasoning_effort": "high",
            "messages": [{"role": "user", "content": "hi"}],
        }

        normalized = await hooks.YoungRouterHook().async_pre_call_deployment_hook(
            request,
            call_type="acompletion",
        )

        self.assertIsNotNone(normalized)
        assert normalized is not None
        self.assertNotIn("reasoning_effort", normalized)
        self.assertEqual(
            "high",
            normalized["extra_body"]["extra_body"]["google"]["thinking_config"][
                "thinking_level"
            ],
        )
        self.assertEqual(
            [{"role": "user", "content": "hi"}],
            normalized["messages"],
        )



if __name__ == "__main__":
    import unittest

    unittest.main()
