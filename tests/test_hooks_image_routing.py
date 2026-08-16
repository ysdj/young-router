from __future__ import annotations

import ast

from hook_test_utils import *


class HookImageRoutingTests(HookTestCase):
    def test_image_generation_module_contains_only_imggen_behavior(self) -> None:
        tree = ast.parse((ROOT / "litellm_menu" / "image_generation.py").read_text(encoding="utf-8"))
        definitions = [
            node.name
            for node in tree.body
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        ]

        self.assertTrue(definitions)
        self.assertTrue(all("image_generation" in name for name in definitions), definitions)

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
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )

    def test_image_input_budget_separates_encrypted_prefix_from_new_suffix(self) -> None:
        hooks, _ = load_hook_module()

        import base64

        prefix_url = "data:image/png;base64," + base64.b64encode(b"prefix-bytes").decode("ascii")
        suffix_url = "data:image/png;base64," + base64.b64encode(b"suffix-bytes").decode("ascii")
        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": prefix_url}],
                },
                {"type": "compaction", "encrypted_content": "opaque-encrypted-summary"},
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": suffix_url}],
                },
            ]
        }

        budget = hooks._image_input_budget(request_kwargs)

        self.assertIsNotNone(budget)
        assert budget is not None
        self.assertEqual(budget["image_count"], 2)
        self.assertEqual(budget["encrypted_prefix_image_count"], 1)
        self.assertEqual(budget["new_suffix_image_count"], 1)
        self.assertEqual(budget["encrypted_prefix_inline_image_bytes"], len(b"prefix-bytes"))
        self.assertEqual(budget["new_suffix_inline_image_bytes"], len(b"suffix-bytes"))
        self.assertEqual(
            budget["encrypted_prefix_largest_inline_image_bytes"],
            len(b"prefix-bytes"),
        )
        self.assertEqual(
            budget["new_suffix_largest_inline_image_bytes"],
            len(b"suffix-bytes"),
        )
        self.assertTrue(budget["encrypted_boundary_present"])

        summary = hooks._trace_request_summary(request_kwargs)
        serialized = json.dumps(summary, ensure_ascii=False)
        self.assertEqual(summary["image_budget"], budget)
        self.assertNotIn("data:image", serialized)
        self.assertNotIn(prefix_url, serialized)
        self.assertNotIn(suffix_url, serialized)

    def test_with_bounded_image_inputs_preserves_encrypted_prefix_while_bounding_suffix(
        self,
    ) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        oversized_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        prefix_url = "data:image/png;base64," + base64.b64encode(b"signed-prefix").decode("ascii")
        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": prefix_url}],
                },
                {"type": "compaction", "encrypted_content": "opaque-encrypted-summary"},
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": oversized_url}],
                },
            ]
        }

        modified = hooks._with_bounded_image_inputs(request_kwargs)

        self.assertIsNotNone(modified)
        assert modified is not None
        bounded_prefix = modified["input"][0]["content"][0]["image_url"]
        bounded_suffix = modified["input"][2]["content"][0]["image_url"]
        self.assertEqual(bounded_prefix, prefix_url)
        self.assertNotEqual(bounded_suffix, oversized_url)
        self.assertLessEqual(
            hooks._image_data_url_size(bounded_suffix),
            hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES,
        )

    async def test_pre_call_image_budget_trace_contains_only_numeric_image_metadata(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        import base64

        image_url = "data:image/png;base64," + base64.b64encode(b"trace-only-image").decode("ascii")
        request_kwargs = {
            "request_id": "request-under-test",
            "client_metadata": {"session_id": "session-under-test"},
            "input": [
                {
                    "role": "user",
                    "content": [{"type": "input_image", "image_url": image_url}],
                }
            ],
        }
        calls = []
        original_trace = hooks._route_trace
        hooks._route_trace = lambda event, **fields: calls.append((event, fields))
        self.addCleanup(setattr, hooks, "_route_trace", original_trace)

        await hook.async_pre_call_deployment_hook(request_kwargs, call_type="aresponses")

        image_events = [fields for event, fields in calls if event == "image_input_budget"]
        self.assertEqual(len(image_events), 1)
        serialized = json.dumps(image_events[0], ensure_ascii=False)
        self.assertNotIn("data:image", serialized)
        self.assertNotIn(image_url, serialized)
        self.assertEqual(image_events[0]["before"]["image_count"], 1)
        self.assertEqual(image_events[0]["after"]["image_count"], 1)
        self.assertFalse(image_events[0]["changed"])

    def test_with_bounded_image_inputs_enforces_each_multi_image_target(self) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        data_urls = []
        for _ in range(2):
            image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=95)
            data_urls.append(
                "data:image/jpeg;base64,"
                + base64.b64encode(buffer.getvalue()).decode("ascii")
            )
        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": data_url}
                        for data_url in data_urls
                    ],
                }
            ]
        }

        modified = hooks._with_bounded_image_inputs(request_kwargs)

        self.assertIsNotNone(modified)
        assert modified is not None
        bounded_urls = [
            item["image_url"] for item in modified["input"][0]["content"]
        ]
        self.assertTrue(all(url.startswith("data:image/jpeg;base64,") for url in bounded_urls))
        self.assertTrue(
            all(
                hooks._image_data_url_size(url)
                <= hooks._INLINE_IMAGE_MANY_TARGET_BYTES
                for url in bounded_urls
            )
        )
        self.assertLessEqual(
            sum(hooks._image_data_url_size(url) for url in bounded_urls),
            hooks._INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES,
        )

    def test_with_bounded_image_inputs_compresses_one_large_image_among_small_images(self) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        def data_url(size: tuple[int, int], quality: int) -> str:
            image = Image.frombytes("RGB", size, os.urandom(size[0] * size[1] * 3))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=quality)
            return "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")

        large = data_url((1000, 1000), 95)
        small = data_url((120, 120), 90)
        self.assertGreater(hooks._image_data_url_size(large), hooks._INLINE_IMAGE_MANY_TARGET_BYTES)
        self.assertLessEqual(hooks._image_data_url_size(small), hooks._INLINE_IMAGE_MANY_TARGET_BYTES)

        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": large},
                        {"type": "input_image", "image_url": small},
                    ],
                }
            ]
        }

        modified = hooks._with_bounded_image_inputs(request_kwargs)

        self.assertIsNotNone(modified)
        assert modified is not None
        bounded = modified["input"][0]["content"]
        self.assertLessEqual(
            hooks._image_data_url_size(bounded[0]["image_url"]),
            hooks._INLINE_IMAGE_MANY_TARGET_BYTES,
        )
        self.assertEqual(bounded[1]["image_url"], small)

    def test_with_bounded_image_inputs_limits_total_for_many_images(self) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        original_urls = []
        for _ in range(12):
            image = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=95)
            original_urls.append(
                "data:image/jpeg;base64,"
                + base64.b64encode(buffer.getvalue()).decode("ascii")
            )
        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": url}
                        for url in original_urls
                    ],
                }
            ]
        }

        modified = hooks._with_bounded_image_inputs(request_kwargs)

        self.assertIsNotNone(modified)
        assert modified is not None
        bounded_urls = [
            item["image_url"] for item in modified["input"][0]["content"]
        ]
        per_image_target = hooks._INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES // len(
            original_urls
        )
        self.assertTrue(
            all(hooks._image_data_url_size(url) <= per_image_target for url in bounded_urls)
        )
        self.assertLessEqual(
            sum(hooks._image_data_url_size(url) for url in bounded_urls),
            hooks._INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES,
        )

    def test_with_bounded_image_inputs_accounts_for_accumulated_encrypted_images(self) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        image = Image.frombytes("RGB", (900, 900), os.urandom(900 * 900 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        original_url = (
            "data:image/jpeg;base64,"
            + base64.b64encode(buffer.getvalue()).decode("ascii")
        )
        self.assertGreater(
            hooks._image_data_url_size(original_url),
            hooks._INLINE_IMAGE_MANY_TARGET_BYTES,
        )

        history = []
        for batch in range(5):
            request_kwargs = {
                "input": history
                + [
                    {
                        "type": "custom_tool_call_output",
                        "call_id": f"call-{batch}",
                        "output": [
                            {
                                "type": "input_image",
                                "image_url": original_url,
                            }
                            for _ in range(3)
                        ],
                    }
                ]
            }

            modified = hooks._with_bounded_image_inputs(request_kwargs)

            self.assertIsNotNone(modified)
            assert modified is not None
            if history:
                self.assertEqual(modified["input"][:-1], history)
            history = modified["input"] + [
                {
                    "type": "reasoning",
                    "encrypted_content": f"opaque-encrypted-{batch}",
                }
            ]

        budget = hooks._image_input_budget({"input": history})

        self.assertIsNotNone(budget)
        assert budget is not None
        self.assertEqual(budget["image_count"], 15)
        self.assertEqual(budget["new_suffix_image_count"], 0)
        self.assertLessEqual(
            budget["inline_image_bytes"],
            hooks._INLINE_IMAGE_MANY_TOTAL_TARGET_BYTES
            + 6 * hooks._INLINE_IMAGE_HISTORY_MIN_TARGET_BYTES,
        )
        final_batch = history[-2]["output"]
        self.assertTrue(
            all(
                hooks._image_data_url_size(item["image_url"])
                <= hooks._INLINE_IMAGE_HISTORY_MIN_TARGET_BYTES
                for item in final_batch
            )
        )

    def test_with_bounded_image_inputs_reencodes_truncated_oversized_jpeg(self) -> None:
        hooks, _ = load_hook_module()

        import base64
        import io
        import os

        from PIL import Image

        image = Image.frombytes("RGB", (1400, 1400), os.urandom(1400 * 1400 * 3))
        buffer = io.BytesIO()
        image.save(buffer, format="JPEG", quality=95)
        truncated = buffer.getvalue()[:-2]
        self.assertFalse(truncated.endswith(b"\xff\xd9"))
        original_data_url = (
            "data:image/jpeg;base64," + base64.b64encode(truncated).decode("ascii")
        )
        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": original_data_url},
                    ],
                }
            ]
        }

        modified = hooks._with_bounded_image_inputs(request_kwargs)

        self.assertIsNotNone(modified)
        assert modified is not None
        bounded_url = modified["input"][0]["content"][0]["image_url"]
        bounded = base64.b64decode(bounded_url.split(",", 1)[1])
        self.assertTrue(bounded.startswith(b"\xff\xd8"))
        self.assertTrue(bounded.endswith(b"\xff\xd9"))
        self.assertLessEqual(len(bounded), hooks._INLINE_IMAGE_SINGLE_TARGET_BYTES)

    def test_with_bounded_image_inputs_rejects_uncompressible_oversized_data(self) -> None:
        hooks, _ = load_hook_module()
        oversized = "data:image/jpeg;base64," + ("A" * 1_300_000)
        request_kwargs = {
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_image", "image_url": oversized},
                    ],
                }
            ]
        }

        with self.assertRaisesRegex(ValueError, "could not be compressed"):
            hooks._with_bounded_image_inputs(request_kwargs)

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

    async def test_structured_image_tool_with_normal_text_response_forces_probe(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return {
                    "output": [
                        {
                            "type": "image_generation_call",
                            "result": "base64-image",
                        }
                    ]
                }

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

        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_choice"], {"type": "image_generation"})

    async def test_image_policy_refusal_does_not_probe_or_mark_deployment_unsupported(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("policy refusals must not invoke image fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": "make a disallowed image",
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
        }
        refusal = {"output_text": "This request violates our content policy."}

        response = await hook.async_post_call_success_deployment_hook(
            request_data,
            refusal,
            call_type=None,
        )

        self.assertIs(response, refusal)
        policy_error = RuntimeError("This request violates our content policy.")
        policy_error.status_code = 400
        self.assertFalse(
            hooks._is_image_generation_tool_capability_error(policy_error)
        )


if __name__ == "__main__":
    unittest.main()
