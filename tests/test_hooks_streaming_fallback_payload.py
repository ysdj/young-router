from __future__ import annotations

import copy

from hook_test_utils import *


class HookStreamingErrorFallbackPayloadTests(HookTestCase):
    """Route-recovery fallback payloads keep the selected deployment route."""

    def test_streaming_fallback_payload_prefers_selected_route_over_outer_alias(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "legacy-chat",
                "input": "Original request",
                "stream": True,
                "litellm_metadata": {
                    hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "legacy-chat",
                },
                "model_info": {
                    "id": "79f0dc70",
                    "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
                },
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["model"], "openai/vendor-chat")
        self.assertNotEqual(payload["model"], "legacy-chat")
        self.assertEqual(
            payload["model_info"]["route_key"],
            "provider_chat / openai/vendor-chat / key=default / order=1",
        )

    def test_streaming_fallback_payload_reads_new_route_key_upstream(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "legacy-chat",
                "input": "Original request",
                "stream": True,
                "model_info": {
                    "id": "79f0dc70",
                    "route_key": "model=llmwebsearch / provider=provider_chat / upstream=openai/vendor-chat / host=chat-provider.example / key=default / order=1",
                },
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["model"], "openai/vendor-chat")
        self.assertNotEqual(payload["model"], "llmwebsearch")

    def test_streaming_fallback_payload_keeps_explicit_route_model_group(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "logical-chat",
                "input": "Original request",
                "stream": True,
                "model_info": {
                    "id": "failed-primary",
                    "route_key": "model=logical-chat / provider=primary / upstream=openai/primary-chat / order=1",
                },
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["model"], "logical-chat")

    def test_streaming_fallback_payload_does_not_infer_group_from_upstream_only(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "logical-chat",
                "input": "Original request",
                "stream": True,
                "litellm_params": {"model": "openai/primary-chat"},
                "model_info": {"id": "failed-primary"},
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["model"], "openai/primary-chat")

    def test_streaming_fallback_payload_preserves_selected_runtime_credentials(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "openai/vendor-chat",
                "input": "Original request",
                "stream": True,
                "api_base": "https://chat-provider.example/v1",
                "api_key": "sk-test-route",
                "custom_llm_provider": "openai",
                "model_info": {
                    "id": "79f0dc70",
                    "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
                },
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["api_base"], "https://chat-provider.example/v1")
        self.assertEqual(payload["api_key"], "sk-test-route")
        self.assertEqual(payload["custom_llm_provider"], "openai")

    def test_streaming_fallback_payload_uses_selected_route_without_original_metadata(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "legacy-chat",
                "input": "Original request",
                "stream": True,
                "model_info": {
                    "id": "79f0dc70",
                    "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
                },
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["model"], "openai/vendor-chat")
        self.assertNotEqual(payload["model"], "legacy-chat")
        self.assertEqual(
            payload["model_info"]["route_key"],
            "provider_chat / openai/vendor-chat / key=default / order=1",
        )

    def test_streaming_fallback_payload_keeps_plain_provider_wrapped_model_group(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "default-chat",
                "input": "Original request",
                "stream": True,
                "model_info": {
                    "id": "route-large",
                    "route_key": "compat_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
            method_name="aresponses",
            allow_repeated_attempt=True,
        )

        self.assertIsNotNone(payload)
        self.assertEqual(payload["model"], "default-chat")
        self.assertNotEqual(payload["model"], "openai/default-chat")
