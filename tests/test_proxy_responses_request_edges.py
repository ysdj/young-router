from __future__ import annotations

from unittest import mock

from hook_test_utils import *


class ProxyResponsesRequestEdgesTests(HookTestCase):
    def test_valid_function_arguments_skip_repair_scan(self) -> None:
        hooks, _ = load_hook_module()
        module = hooks._responses_request_module
        with mock.patch.object(
            module,
            "_codex_quote_unquoted_function_keys",
            side_effect=AssertionError("valid arguments were rescanned"),
        ):
            parsed = module._codex_function_arguments_object('{"path": "a.txt"}')
        self.assertEqual({"path": "a.txt"}, parsed)

    def test_replay_input_normalization_does_not_deep_copy_unchanged_items(self) -> None:
        hooks, _ = load_hook_module()
        module = hooks._responses_request_module
        content = [{"type": "input_text", "text": "hello"}]
        unchanged = {"role": "user", "content": content}
        request = {
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "web_search"},
                        {"type": "function", "name": "keep"},
                    ],
                },
                unchanged,
            ],
            "tools": [],
        }
        modified, _ = module._codex_openrouter_native_search_request(request, [])
        self.assertIs(content, modified["input"][1]["content"])

    def test_capability_probe_reuses_the_replay_input(self) -> None:
        hooks, _ = load_hook_module()
        module = hooks._responses_request_module
        request = {
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [
                        {"type": "web_search"},
                        {"type": "function", "name": "keep"},
                    ],
                }
            ],
            "tools": [],
        }
        probe = module._codex_openrouter_search_capability_probe_request(request)
        self.assertIs(request["input"], probe["input"])

    def test_nested_tool_scan_runs_only_with_top_level_native(self) -> None:
        hooks, _ = load_hook_module()
        module = hooks._responses_request_module
        request = {
            "call_type": "aresponses",
            "model_info": {
                "provider": "openrouter",
                "supports_responses_web_search": True,
            },
            "client_metadata": {"x-codex-turn-metadata": "{}"},
            "input": [
                {
                    "type": "additional_tools",
                    "tools": [{"type": "namespace", "name": "functions"}],
                }
            ],
            "tools": [],
        }
        with mock.patch.object(
            module,
            "_codex_declared_tools",
            wraps=module._codex_declared_tools,
        ) as declared_tools:
            module._with_codex_openrouter_native_web_search_tool(request)
        self.assertEqual(1, declared_tools.call_count)

    def test_truncation_guard_counts_integer_attempts(self) -> None:
        hooks, _ = load_hook_module()
        module = hooks._responses_request_module
        key = module._RESPONSES_CONTEXT_TRUNCATION_FALLBACK_METADATA_KEY
        limit = len(module._context_window_module._RETRY_FRACTIONS)
        request = {"input": "hello", "litellm_metadata": {key: limit}}
        self.assertFalse(
            module._request_can_attempt_responses_context_truncation_fallback(request)
        )
        request["litellm_metadata"] = {key: True}
        self.assertTrue(
            module._request_can_attempt_responses_context_truncation_fallback(request)
        )
