from __future__ import annotations

from hook_test_utils import *


class ProxyReasoningEdgeTests(HookTestCase):
    def test_strip_removes_the_messages_output_config_effort(self) -> None:
        hooks, _proxy_server = load_hook_module()

        request = {
            "model": "claude-under-test",
            "output_config": {"effort": "high"},
        }

        stripped = hooks._without_reasoning_parameters(request)

        self.assertNotIn("output_config", stripped)
        self.assertTrue(hooks._request_has_reasoning_parameters(request))
        self.assertFalse(hooks._request_has_reasoning_parameters(stripped))
        self.assertEqual({"effort": "high"}, request["output_config"])

    def test_strip_keeps_non_reasoning_output_config_fields(self) -> None:
        hooks, _proxy_server = load_hook_module()

        request = {
            "model": "claude-under-test",
            "output_config": {"effort": "high", "format": {"type": "json"}},
        }

        stripped = hooks._without_reasoning_parameters(request)

        self.assertEqual({"format": {"type": "json"}}, stripped["output_config"])
        self.assertEqual(
            {"effort": "high", "format": {"type": "json"}},
            request["output_config"],
        )

    def test_strip_finds_the_messages_effort_inside_extra_body(self) -> None:
        hooks, _proxy_server = load_hook_module()

        request = {
            "model": "claude-under-test",
            "extra_body": {"output_config": {"effort": "low"}},
        }

        stripped = hooks._without_reasoning_parameters(request)

        self.assertEqual({}, stripped["extra_body"])
        self.assertTrue(hooks._request_has_reasoning_parameters(request))
