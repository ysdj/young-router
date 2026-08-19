from __future__ import annotations

import json
from pathlib import Path

from hook_test_utils import HookTestCase, load_hook_module


class HookReasoningMappingTests(HookTestCase):
    def _configure_pi_cache(self, root: Path, records: dict) -> None:
        codex_home = root / "codex"
        codex_home.mkdir()
        (codex_home / "litellm-menu-model-contexts.json").write_text(
            json.dumps({"records": records}),
            encoding="utf-8",
        )
        self.set_env("CODEX_HOME", str(codex_home))
        self.set_env("LITELLM_RUNTIME_ROOT", str(root))
        self.set_env("LITELLM_CONFIG_FILE", str(root / "config.yaml"))

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

        mapped = await hooks.LiteLLMMenuHook().async_pre_call_deployment_hook(
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


if __name__ == "__main__":
    import unittest

    unittest.main()
