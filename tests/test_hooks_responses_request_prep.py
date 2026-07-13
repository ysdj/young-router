from __future__ import annotations

from hook_test_utils import *


class HookResponsesRequestPrepTests(HookTestCase):
    def test_codex_stale_wait_output_gets_fresh_exec_recovery_hint(self) -> None:
        hooks, _ = load_hook_module()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {"type": "custom", "name": "exec"},
                        {"type": "function", "name": "wait"},
                        {"type": "function", "name": "request_user_input"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-wait",
                    "name": "wait",
                    "arguments": '{"cell_id":"expired-cell"}',
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-wait",
                    "output": "Script error: exec cell expired-cell not found",
                },
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }

        modified = hooks._with_codex_tool_runtime_recovery_hints(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        output = modified["input"][-1]["output"]
        self.assertIn("Start a fresh exec call", output)
        self.assertIn("exec tool itself remains available", output)
        self.assertEqual(
            [tool["name"] for tool in modified["input"][0]["tools"]],
            ["exec", "wait", "request_user_input"],
        )
        self.assertEqual(
            original["input"][-1]["output"],
            "Script error: exec cell expired-cell not found",
        )
        stats = modified["litellm_metadata"][
            hooks._CODEX_TOOL_RUNTIME_RECOVERY_METADATA_KEY
        ]
        self.assertEqual(stats["stale_wait_outputs_hinted"], 1)
        self.assertEqual(stats["unavailable_request_user_input_outputs_hinted"], 0)

    async def test_pre_call_removes_proven_unavailable_request_user_input_only(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {"type": "custom", "name": "exec"},
                        {"type": "function", "name": "wait"},
                        {"type": "function", "name": "request_user_input"},
                    ],
                },
                {
                    "type": "function_call",
                    "call_id": "call-question",
                    "name": "request_user_input",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-question",
                    "output": "request_user_input is unavailable in Default mode",
                },
            ],
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(
            [tool["name"] for tool in modified["tools"]],
            ["exec", "wait"],
        )
        self.assertNotIn("additional_tools", json.dumps(modified["input"]))
        output = modified["input"][-1]["output"]
        self.assertIn("only to request_user_input", output)
        self.assertIn("custom exec tool remains available", output)
        self.assertEqual(modified["tool_choice"], "auto")
        self.assertFalse(modified["parallel_tool_calls"])
        stats = modified["litellm_metadata"][
            hooks._CODEX_TOOL_RUNTIME_RECOVERY_METADATA_KEY
        ]
        self.assertEqual(stats["unavailable_request_user_input_outputs_hinted"], 1)
        self.assertEqual(stats["removed_request_user_input_tools"], 1)
        self.assertEqual(
            [tool["name"] for tool in original["input"][0]["tools"]],
            ["exec", "wait", "request_user_input"],
        )

    async def test_pre_call_deployment_hook_adds_compat_provider_browser_headers(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://headers.example/v1",
            "extra_headers": {"X-Trace": "keep-me"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        self.assertNotEqual(modified, original)
        assert modified is not None
        headers = modified["extra_headers"]
        self.assertEqual(headers["X-Trace"], "keep-me")
        self.assertIn("Mozilla/5.0", headers["User-Agent"])
        self.assertEqual(headers["Accept"], "application/json, text/plain, */*")
        self.assertIn("Accept-Language", headers)
        self.assertEqual(original["extra_headers"], {"X-Trace": "keep-me"})

    async def test_pre_call_deployment_hook_forwards_codex_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://headers.example/v1",
            "proxy_server_request": {
                "headers": {
                    "user-agent": "codex-local/1.2.3",
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["User-Agent"], "codex-local/1.2.3")

    async def test_pre_call_deployment_hook_forwards_litellm_params_proxy_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://example.com/v1",
            "extra_headers": {"X-Trace": "keep-me"},
            "litellm_params": {
                "proxy_server_request": {
                    "headers": {
                        "User-Agent": "LiteLLM%20Menu/1 CFNetwork/3860.600.21 Darwin/25.5.0",
                    },
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["X-Trace"], "keep-me")
        self.assertEqual(
            modified["extra_headers"]["User-Agent"],
            "LiteLLM%20Menu/1 CFNetwork/3860.600.21 Darwin/25.5.0",
        )
        self.assertNotIn("Accept", modified["extra_headers"])
        self.assertEqual(original["extra_headers"], {"X-Trace": "keep-me"})

    async def test_pre_call_deployment_hook_codex_user_agent_overrides_old_extra_header(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://headers.example/v1",
            "extra_headers": {"user-agent": "Mozilla/5.0 stale"},
            "proxy_server_request": {
                "headers": {
                    "User-Agent": "codex-local/9.9.9",
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["user-agent"], "codex-local/9.9.9")
        self.assertNotIn("User-Agent", modified["extra_headers"])

    async def test_pre_call_deployment_hook_preserves_existing_user_agent(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "metadata": {"api_base": "https://api.headers.example/v1"},
            "extra_headers": {"user-agent": "custom-client"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["user-agent"], "custom-client")
        self.assertNotIn("User-Agent", modified["extra_headers"])
        self.assertEqual(modified["extra_headers"]["Accept"], "application/json, text/plain, */*")
        self.assertNotIn("metadata", modified)
        self.assertEqual(modified["litellm_metadata"]["api_base"], "https://api.headers.example/v1")

    async def test_pre_call_deployment_hook_reads_compat_provider_api_base_from_litellm_params(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "litellm_params": {
                "api_base": "https://headers.example/v1",
                "proxy_server_request": {
                    "headers": {
                        "user-agent": "codex-local/4.5.6",
                    },
                },
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_headers"]["User-Agent"], "codex-local/4.5.6")
        self.assertEqual(modified["extra_headers"]["Accept"], "application/json, text/plain, */*")

    async def test_pre_call_deployment_hook_moves_metadata_internal_by_default(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "metadata": {"trace_id": "client-trace", "api_base": "https://example.com/v1"},
            "litellm_metadata": {"model_group": "default-chat"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertNotIn("metadata", modified)
        self.assertEqual(modified["litellm_metadata"]["trace_id"], "client-trace")
        self.assertEqual(modified["litellm_metadata"]["api_base"], "https://example.com/v1")
        self.assertEqual(modified["litellm_metadata"]["model_group"], "default-chat")
        self.assertEqual(original["litellm_metadata"], {"model_group": "default-chat"})

    async def test_pre_call_deployment_hook_preserves_responses_client_metadata_upstream(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        client_metadata = {
            "thread_id": "thread-test-0001",
            "x-codex-turn-metadata": '{"request_kind":"compaction"}',
        }
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": "Create a compact handoff summary for resuming this Codex session.",
                }
            ],
            "client_metadata": client_metadata,
            "prompt_cache_key": "thread-test-0001",
            "extra_body": {"keep": True},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["extra_body"]["client_metadata"], client_metadata)
        self.assertTrue(modified["extra_body"]["keep"])
        self.assertEqual(
            modified["prompt_cache_key"],
            "thread-test-0001",
        )
        self.assertEqual(original["extra_body"], {"keep": True})
        self.assertEqual(original.get("client_metadata"), client_metadata)

    async def test_pre_call_deployment_hook_preserves_codex_compaction_headers_upstream(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        turn_metadata = (
            '{"session_id":"thread-test-0001",'
            '"thread_id":"thread-test-0001",'
            '"request_kind":"compaction"}'
        )
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": "Create a compact handoff summary for resuming this Codex session.",
                }
            ],
            "stream": True,
            "client_metadata": {
                "session_id": "thread-test-0001",
                "thread_id": "thread-test-0001",
                "x-codex-turn-metadata": turn_metadata,
                "x-codex-window-id": "thread-test-0001:7",
            },
            "proxy_server_request": {
                "headers": {
                    "accept": "text/event-stream",
                    "originator": "Codex Desktop",
                    "session-id": "thread-test-0001",
                    "thread-id": "thread-test-0001",
                    "user-agent": "Codex Desktop/0.142.3",
                    "x-client-request-id": "thread-test-0001",
                    "x-codex-beta-features": "remote_compaction_v2",
                    "x-codex-turn-metadata": turn_metadata,
                    "x-codex-window-id": "thread-test-0001:7",
                }
            },
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        self.assertIsNotNone(modified)
        assert modified is not None
        headers = {key.lower(): value for key, value in modified["extra_headers"].items()}
        self.assertEqual(headers["accept"], "text/event-stream")
        self.assertEqual(headers["originator"], "Codex Desktop")
        self.assertEqual(headers["session-id"], "thread-test-0001")
        self.assertEqual(headers["thread-id"], "thread-test-0001")
        self.assertEqual(headers["user-agent"], "Codex Desktop/0.142.3")
        self.assertEqual(headers["x-client-request-id"], "thread-test-0001")
        self.assertEqual(headers["x-codex-beta-features"], "remote_compaction_v2")
        self.assertEqual(headers["x-codex-turn-metadata"], turn_metadata)
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(
            headers["x-codex-window-id"],
            "thread-test-0001:7",
        )

    async def test_pre_call_deployment_hook_does_not_add_responses_client_metadata_to_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "hi",
            "use_chat_completions_api": True,
            "client_metadata": {"thread_id": "thread"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        if modified is not None:
            self.assertNotIn("extra_body", modified)

    def test_structured_codex_compaction_is_bounded_without_changing_protocol_shape(self) -> None:
        hooks, _ = load_hook_module()
        first_output = "alpha-" + ("a" * 125000) + "-omega"
        second_output = "start-" + ("b" * 125000) + "-finish"
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "prompt_cache_key": "thread-test-0002",
            "client_metadata": {
                "thread_id": "thread-test-0002",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "function_call",
                    "call_id": "call_keep",
                    "name": "exec_command",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_keep",
                    "output": first_output,
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_second",
                    "output": second_output,
                },
                {"type": "compaction_trigger", "id": "compact_keep"},
            ],
            "tools": [{"type": "function", "name": "exec_command"}],
            "tool_choice": "auto",
            "parallel_tool_calls": True,
            "reasoning": {"effort": "xhigh"},
        }

        modified = hooks._with_codex_compaction_input_bounded(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIsNot(modified, original)
        self.assertEqual(len(modified["input"]), len(original["input"]))
        self.assertEqual(modified["input"][1], original["input"][1])
        self.assertEqual(modified["input"][2]["call_id"], "call_keep")
        self.assertEqual(modified["input"][3]["call_id"], "call_second")
        self.assertEqual(
            modified["input"][4],
            {"type": "compaction_trigger", "id": "compact_keep"},
        )
        self.assertEqual(modified["tools"], original["tools"])
        self.assertEqual(modified["tool_choice"], "auto")
        self.assertTrue(modified["parallel_tool_calls"])
        self.assertEqual(modified["reasoning"], {"effort": "xhigh"})
        self.assertEqual(
            len(modified["input"][2]["output"]),
            hooks._CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS,
        )
        self.assertIn("original_chars=", modified["input"][2]["output"])
        self.assertTrue(modified["input"][2]["output"].startswith("alpha-"))
        self.assertTrue(modified["input"][2]["output"].endswith("-omega"))
        self.assertEqual(original["input"][2]["output"], first_output)

    def test_structured_codex_compaction_bounds_custom_tool_output_content_only(self) -> None:
        hooks, _ = load_hook_module()
        first_output = [
            {"type": "input_text", "text": "Script completed\n"},
            {
                "type": "input_text",
                "text": "alpha-" + ("a" * 125000) + "-omega",
            },
        ]
        second_output = [
            {"type": "input_text", "text": "Script completed\n"},
            {
                "type": "input_text",
                "text": "start-" + ("b" * 125000) + "-finish",
            },
        ]
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "stream": True,
            "client_metadata": {
                "thread_id": "thread-test-custom",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "custom_tool_call",
                    "id": "ctc_keep",
                    "call_id": "call_keep",
                    "name": "exec",
                    "input": "const result = await tools.exec_command({});",
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_keep",
                    "output": first_output,
                },
                {
                    "type": "custom_tool_call_output",
                    "call_id": "call_second",
                    "output": second_output,
                },
                {"type": "compaction_trigger", "id": "compact_custom"},
            ],
        }

        modified = hooks._with_codex_compaction_input_bounded(original)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["input"][1], original["input"][1])
        self.assertEqual(modified["input"][2]["type"], "custom_tool_call_output")
        self.assertEqual(modified["input"][2]["call_id"], "call_keep")
        self.assertIsInstance(modified["input"][2]["output"], list)
        self.assertEqual(
            [part["type"] for part in modified["input"][2]["output"]],
            ["input_text", "input_text"],
        )
        compacted_text = "".join(
            part["text"] for part in modified["input"][2]["output"]
        )
        self.assertLessEqual(
            len(compacted_text),
            hooks._CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS,
        )
        self.assertIn("original_chars=", compacted_text)
        self.assertTrue(compacted_text.startswith("Script completed\nalpha-"))
        self.assertTrue(compacted_text.endswith("-omega"))
        self.assertEqual(original["input"][2]["output"], first_output)
        self.assertEqual(modified["input"][-1], original["input"][-1])

    def test_structured_codex_compaction_leaves_small_request_untouched(self) -> None:
        hooks, _ = load_hook_module()
        original = {
            "call_type": "aresponses",
            "client_metadata": {
                "thread_id": "thread-test-0002",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "function_call_output",
                    "call_id": "call_small",
                    "output": "x" * (hooks._CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS + 200),
                },
                {"type": "compaction_trigger"},
            ],
        }

        self.assertIsNone(hooks._with_codex_compaction_input_bounded(original))

    def test_codex_compaction_bounding_ignores_non_codex_request(self) -> None:
        hooks, _ = load_hook_module()
        original = {
            "call_type": "aresponses",
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "function_call_output",
                    "call_id": "call_large",
                    "output": "x" * (hooks._CODEX_TOOL_OUTPUT_COMPACT_TOTAL_CHARS + 50_000),
                },
            ],
        }

        self.assertIsNone(hooks._with_codex_compaction_input_bounded(original))

    async def test_pre_call_deployment_hook_keeps_ordinary_codex_turn_byte_for_byte(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "thread_id": "thread-test-0002",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {
                    "type": "function_call_output",
                    "call_id": "call_a",
                    "output": "a" * 125000,
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_b",
                    "output": "b" * 125000,
                },
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type="aresponses")

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIn("extra_body", modified)
        self.assertEqual(
            modified["extra_body"]["client_metadata"],
            original["client_metadata"],
        )
        self.assertEqual(
            modified["input"][1]["output"],
            original["input"][1]["output"],
        )
        self.assertEqual(modified["input"][1]["call_id"], "call_a")
        self.assertEqual(modified["input"][2]["call_id"], "call_b")

    async def test_pre_call_deployment_hook_bounds_structured_compaction_before_upstream(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "call_type": "aresponses",
            "model": "default-chat",
            "client_metadata": {
                "thread_id": "thread-preflight-compaction",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "input": [
                {
                    "type": "message",
                    "id": "msg_keep",
                    "role": "developer",
                    "content": "d" * 40_000,
                },
                {
                    "type": "custom_tool_call_output",
                    "id": "out_keep",
                    "call_id": "call_keep",
                    "output": "x" * 600_000,
                },
                {"type": "compaction_trigger", "id": "trigger_keep"},
            ],
        }

        modified = await hook.async_pre_call_deployment_hook(
            original,
            call_type="aresponses",
        )

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual([item["type"] for item in modified["input"]], [
            "message",
            "custom_tool_call_output",
            "compaction_trigger",
        ])
        self.assertEqual(modified["input"][0]["id"], "msg_keep")
        self.assertEqual(modified["input"][1]["id"], "out_keep")
        self.assertEqual(modified["input"][1]["call_id"], "call_keep")
        self.assertEqual(modified["input"][2], original["input"][2])
        self.assertLessEqual(
            hooks._compaction_text_length(modified["input"]),
            hooks._CODEX_COMPACTION_HISTORY_TEXT_CHARS,
        )
        self.assertEqual(original["input"][1]["output"], "x" * 600_000)

    async def test_pre_call_deployment_hook_ignores_other_api_bases(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {"api_base": "https://example.com/v1"}

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNone(modified)

    async def test_pre_call_deployment_hook_uses_browser_header_retry_marker(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "api_base": "https://api.image.example/v1",
            "litellm_metadata": {
                hooks._BROWSER_COMPATIBLE_HEADERS_RETRY_METADATA_KEY: True,
            },
            "extra_headers": {"User-Agent": "codex-local/1.2.3"},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertIn("Mozilla/5.0", modified["extra_headers"]["User-Agent"])
        self.assertEqual(
            modified["extra_headers"]["Accept"],
            "application/json, text/plain, */*",
        )
        self.assertEqual(modified["extra_headers"]["Accept-Language"], "en-US,en;q=0.9")

    async def test_responses_api_does_not_prefer_browser_compatible_deployments(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://api.primary.example/v1"},
                "model_info": {"id": "primary_provider", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
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
            request_kwargs={"original_generic_function": aresponses},
        )

        self.assertEqual(filtered, deployments)

    async def test_generic_response_wrapper_marks_balance_error_without_403_for_failover(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamBalanceError(Exception):
            status_code = 400

        error = UpstreamBalanceError('{"code":"INSUFFICIENT_BALANCE"}')

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(UpstreamBalanceError):
            await request_kwargs["original_generic_function"](
                model="default-chat",
                litellm_metadata={"model_info": {"id": "empty-account"}},
            )

        self.assertEqual(error.failed_deployment_id, "empty-account")
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_marks_temporary_500_for_failover(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamServerError(Exception):
            status_code = 500

        error = UpstreamServerError("temporary upstream outage")

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(UpstreamServerError):
            await request_kwargs["original_generic_function"](
                model="default-chat",
                model_info={"id": "temporary-failure"},
            )

        self.assertEqual(error.failed_deployment_id, "temporary-failure")
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["temporary-failure"])
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_marks_capacity_error_for_same_deployment_retry(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamCapacityError(Exception):
            pass

        error = UpstreamCapacityError(
            "Selected model is at capacity. Please try a different model."
        )

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(UpstreamCapacityError):
            await request_kwargs["original_generic_function"](
                model="default-chat",
                model_info={"id": "capacity-full-deployment"},
        )

        self.assertEqual(error.failed_deployment_id, "capacity-full-deployment")
        self.assertNotIn("_excluded_deployment_ids", request_kwargs)
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(error))
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_retries_responses_404_via_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"output_text": "ok"}

        request_kwargs = {
            "original_generic_function": original_generic_function,
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "hi",
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="hi",
            model_info={"id": "chat-only-route"},
            litellm_metadata={"model_group": "balanced-chat"},
        )

        self.assertEqual(response, {"output_text": "ok"})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertTrue(
            calls[1]["litellm_metadata"][hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY]
        )
        self.assertEqual(calls[1]["model_info"], {"id": "chat-only-route"})
        self.assertFalse(hasattr(error, "failed_deployment_id"))
