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
