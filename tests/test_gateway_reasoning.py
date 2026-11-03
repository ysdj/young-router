from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from young_router.proxy import reasoning_capability as gateway


class ReasoningCapabilityRegistrationTests(unittest.TestCase):
    """LiteLLM refuses `reasoning.effort` for a model its cost map omits."""

    def test_every_configured_route_is_registered_as_reasoning_capable(self) -> None:
        cost: dict[str, object] = {"gpt-4o": {"supports_reasoning": True}}
        document = (
            "model_list:\n"
            "  - model_name: deepseek-v4.1-flash\n"
            "    litellm_params:\n"
            "      model: openai/deepseek-v4.1-flash\n"
            "      api_base: https://relay.test/v1\n"
            "      api_key: sk-test\n"
            "  - model_name: known-model\n"
            "    litellm_params:\n"
            "      model: openai/gpt-4o\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(document, encoding="utf-8")
            with mock.patch("litellm.model_cost", cost):
                registered = gateway.register(str(path))

        self.assertEqual(1, registered)
        self.assertIs(True, cost["deepseek-v4.1-flash"]["supports_reasoning"])
        # A model LiteLLM already knew is left as it was.
        self.assertEqual({"supports_reasoning": True}, cost["gpt-4o"])

    def test_a_missing_or_unreadable_config_registers_nothing(self) -> None:
        with mock.patch("litellm.model_cost", {}):
            self.assertEqual(0, gateway.register("/nonexistent.yaml"))
            self.assertEqual(0, gateway.register(""))

    def test_the_runtime_config_path_comes_from_the_launcher_environment(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"WORKER_CONFIG": '{"config": "/private/runtime/config.yaml"}'},
            clear=False,
        ):
            self.assertEqual("/private/runtime/config.yaml", gateway.runtime_config_path())

    def test_the_runtime_config_path_falls_back_to_the_litellm_config_env(self) -> None:
        with mock.patch.dict(
            "os.environ",
            {"LITELLM_CONFIG_FILE": "/opt/yr/config.yaml"},
            clear=True,
        ):
            self.assertEqual("/opt/yr/config.yaml", gateway.runtime_config_path())

    def test_an_unknown_route_is_registered_with_native_streaming(self) -> None:
        cost: dict[str, object] = {}
        document = (
            "model_list:\n"
            "  - model_name: deepseek-v4.1-flash\n"
            "    litellm_params:\n"
            "      model: openai/deepseek-v4.1-flash\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(document, encoding="utf-8")
            with mock.patch("litellm.model_cost", cost):
                registered = gateway.register(str(path))

        self.assertEqual(1, registered)
        self.assertIs(True, cost["deepseek-v4.1-flash"]["supports_reasoning"])
        self.assertIs(True, cost["deepseek-v4.1-flash"]["supports_native_streaming"])
        self.assertEqual("openai", cost["deepseek-v4.1-flash"]["litellm_provider"])

    def test_a_known_entry_bound_to_another_provider_is_realigned(self) -> None:
        cost: dict[str, object] = {
            "deepseek-flash": {
                "supports_reasoning": True,
                "supports_native_streaming": True,
                "litellm_provider": "deepseek",
            }
        }
        document = (
            "model_list:\n"
            "  - model_name: deepseek-v4.1-flash\n"
            "    litellm_params:\n"
            "      model: openai/deepseek-flash\n"
        )
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.yaml"
            path.write_text(document, encoding="utf-8")
            with mock.patch("litellm.model_cost", cost):
                registered = gateway.register(str(path))

        self.assertEqual(1, registered)
        self.assertEqual("openai", cost["deepseek-flash"]["litellm_provider"])
        self.assertIs(True, cost["deepseek-flash"]["supports_native_streaming"])

    def test_a_registered_relay_is_not_faked_without_a_provider(self) -> None:
        import litellm
        from litellm.llms.openai.responses.transformation import (
            OpenAIResponsesAPIConfig,
        )

        from young_router.proxy import patches

        patches._install_openai_responses_fake_stream_patch()
        config = OpenAIResponsesAPIConfig()
        cost = {
            "deepseek-v4.1-flash": {"supports_native_streaming": True},
            "non-streaming-model": {"supports_native_streaming": False},
        }
        with mock.patch("litellm.model_cost", cost):
            self.assertFalse(
                config.should_fake_stream(
                    model="deepseek-v4.1-flash",
                    stream=True,
                    custom_llm_provider=None,
                )
            )
            self.assertTrue(
                config.should_fake_stream(
                    model="non-streaming-model",
                    stream=True,
                    custom_llm_provider=None,
                )
            )
            self.assertTrue(
                config.should_fake_stream(
                    model="not-registered",
                    stream=True,
                    custom_llm_provider=None,
                )
            )
