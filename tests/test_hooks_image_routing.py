from __future__ import annotations

from hook_test_utils import *


class HookImageRoutingTests(HookTestCase):
    async def test_responses_api_image_generation_tool_does_not_use_static_capability_filter(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-pro", "supports_responses_image_generation_tool": True},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "tools": [{"type": "image_generation"}],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_image_generation_edit_keeps_image_input_candidates_for_runtime_probe(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        deployments = [
            {
                "litellm_params": {"api_base": "https://provider-a.example/v1"},
                "model_info": {
                    "id": "declared-unsafe",
                    "supports_vision": False,
                    "supports_responses_image_input": False,
                },
            },
            {
                "litellm_params": {"api_base": "https://provider-b.example/v1"},
                "model_info": {"id": "declared-safe", "supports_vision": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "call_type": "aresponses",
                "tools": [{"type": "image_generation"}],
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "edit this image"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,abc",
                            },
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_with_image_input_does_not_use_vision_capability_filter(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_vision": True},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_vision": False},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "read this"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/jpeg;base64,abc",
                            },
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_with_image_input_skips_declared_unsafe_deployment(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://provider-a.example/v1"},
                "model_info": {
                    "id": "vision-unsafe",
                    "supports_vision": True,
                    "supports_responses_image_input": False,
                },
            },
            {
                "litellm_params": {"api_base": "https://provider-b.example/v1"},
                "model_info": {"id": "vision-safe", "supports_vision": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "input": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "input_text", "text": "read this"},
                            {
                                "type": "input_image",
                                "image_url": "data:image/png;base64,abc",
                            },
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_chat_completions_image_url_keeps_responses_only_unsafe_deployment(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        deployments = [
            {
                "litellm_params": {"api_base": "https://provider-a.example/v1"},
                "model_info": {
                    "id": "vision-unsafe-for-responses-only",
                    "supports_vision": True,
                    "supports_responses_image_input": False,
                },
            },
            {
                "litellm_params": {"api_base": "https://provider-b.example/v1"},
                "model_info": {"id": "vision-safe", "supports_vision": True},
            },
        ]

        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "read this"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc"}},
                ],
            }
        ]
        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=messages,
            request_kwargs={"messages": messages},
        )

        self.assertEqual(filtered, deployments)

    async def test_chat_completions_image_url_does_not_use_vision_capability_filter(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        deployments = [
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_vision": True},
            },
            {
                "litellm_params": {"api_base": "https://api.backup.example/v1"},
                "model_info": {"id": "backup_provider", "supports_vision": False},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "read this"},
                        {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
                    ],
                }
            ],
            request_kwargs={
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "read this"},
                            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64,abc"}},
                        ],
                    }
                ]
            },
        )

        self.assertEqual(filtered, deployments)

    def test_forced_image_generation_payload_uses_internal_metadata_by_default(self) -> None:
        hooks, _ = load_hook_module()

        payload = hooks._build_forced_image_generation_payload(
            {
                "model": "default-chat",
                "input": "draw",
                "tools": [{"type": "image_generation"}],
                "metadata": {"trace_id": "client-trace"},
                "litellm_metadata": {"model_group": "default-chat"},
            },
            stream=True,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertNotIn("metadata", payload)
        self.assertEqual(payload["litellm_metadata"]["trace_id"], "client-trace")
        self.assertEqual(payload["litellm_metadata"]["model_group"], "default-chat")
        self.assertTrue(payload["litellm_metadata"][hooks._STREAM_FALLBACK_METADATA_KEY])

    def test_forced_image_generation_payload_preserves_metadata_when_opted_in(self) -> None:
        hooks, _ = load_hook_module()

        payload = hooks._build_forced_image_generation_payload(
            {
                "model": "default-chat",
                "input": "draw",
                "tools": [{"type": "image_generation"}],
                "metadata": {"trace_id": "client-trace"},
                "model_info": {"supports_request_metadata": True},
            },
            stream=False,
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["metadata"], {"trace_id": "client-trace"})
        self.assertTrue(payload["litellm_metadata"][hooks._STREAM_FALLBACK_METADATA_KEY])

    def test_with_bounded_image_inputs_downsizes_large_inline_images(self) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_data_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "read this"},
                        {"type": "input_image", "image_url": original_data_url},
                    ],
                }
            ]
        }

        modified = hooks._with_bounded_image_inputs(request_kwargs)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(request_kwargs["input"][0]["content"][1]["image_url"], original_data_url)
        self.assertLess(
            hooks._image_data_url_size(modified["input"][0]["content"][1]["image_url"]),
            hooks._image_data_url_size(original_data_url),
        )
        self.assertLessEqual(
            hooks._image_data_url_size(modified["input"][0]["content"][1]["image_url"]),
            hooks._INLINE_IMAGE_SINGLE_BUDGET_BYTES,
        )

    async def test_plain_text_request_does_not_retry(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("plain text requests must not invoke image fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {"model": "default-chat", "input": "Say pong only."}
        original = {"output_text": "pong"}

        response = await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(response, original)

    async def test_structured_image_tool_with_normal_text_response_does_not_retry(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("normal text response must not invoke forced image fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": "Continue the long-running paper translation and report status.",
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
        }
        original = {"output_text": "现在这轮已经推进到 39 个 block 了，继续等待收口。"}

        response = await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(response, original)


if __name__ == "__main__":
    unittest.main()
