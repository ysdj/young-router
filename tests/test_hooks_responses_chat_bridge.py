from __future__ import annotations

from hook_test_utils import *


class HookResponsesChatBridgeTests(HookTestCase):
    def test_chat_bridge_stream_payload_maps_responses_max_output_tokens(self) -> None:
        hooks, _ = load_hook_module()

        payload = hooks._chat_bridge_stream_payload(
            {
                "model": "default-chat",
                "input": "Summarize this thread.",
                "stream": True,
                "max_output_tokens": 4096,
                "model_info": {
                    "id": "chat-only",
                    "upstream_url_surface": "openai/chat",
                    "supported_upstream_url_surfaces": ["openai/chat"],
                    "supports_responses_endpoint": False,
                },
            }
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.get("max_completion_tokens"), 4096)
        self.assertNotIn("max_tokens", payload)

    def test_selected_responses_route_ignores_stale_outer_chat_surface(self) -> None:
        hooks, _ = load_hook_module()
        tools = [
            {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
            {"type": "web_search"},
        ]
        stale_outer_chat = {
            "call_type": "aresponses",
            "model": "default-chat",
            "tools": tools,
            "model_info": {
                "id": "backup_provider-chat",
                "provider": "backup_provider",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat", "anthropic"],
                "supports_responses_endpoint": False,
            },
        }
        selected_responses = {
            "call_type": "aresponses",
            "model": "openai/default-chat",
            "stream": True,
            "tools": tools,
            "model_info": {
                "id": "compat_provider-responses",
                "provider": "compat_provider",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        }

        self.assertIsNone(
            hooks._responses_chat_bridge_preemptive_kwargs(
                selected_responses,
                stale_outer_chat,
                include_hosted_web_search_unsupported=True,
                include_client_tool_unsupported=True,
            )
        )

        selected_responses_web_search_bridge = {
            **selected_responses,
            "model_info": {
                **selected_responses["model_info"],
                "supports_responses_web_search": False,
            },
        }
        external_bridge = hooks._with_responses_external_web_search_bridge(
            selected_responses_web_search_bridge,
            stale_outer_chat,
        )

        self.assertIsNotNone(external_bridge)
        assert external_bridge is not None
        self.assertNotIn("use_chat_completions_api", external_bridge)
        self.assertTrue(
            external_bridge["litellm_metadata"][hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY]
        )

    async def test_function_tool_bridge_bad_response_status_stays_on_responses(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class BadResponseStatus(Exception):
            status_code = 400

        error = BadResponseStatus(
            'OpenAIException - {"error":{"message":"openai_error",'
            '"type":"bad_response_status_code","param":"",'
            '"code":"bad_response_status_code"}}'
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(BadResponseStatus):
            await request_kwargs["original_generic_function"](
                call_type="aresponses",
                model="balanced-chat",
                input="hi",
                stream=True,
                tools=[
                    {
                        "type": "namespace",
                        "name": "mcp__computer_use",
                        "tools": [
                            {
                                "type": "function",
                                "name": "click",
                                "parameters": {"type": "object"},
                            }
                        ],
                    },
                    {"type": "tool_search"},
                ],
                model_info={
                    "id": "chatroute",
                    "provider": "provider_chat",
                    "route_key": "provider_chat / openai/vendor-chat / key=default",
                    "upstream_url_surface": "openai/responses",
                    "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])

    async def test_function_tool_bridge_schema_error_retries_with_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class InvalidResponsesSchema(Exception):
            status_code = 400

        error = InvalidResponsesSchema(
            'OpenAIException - {"error":{"code":"invalid_prompt",'
            '"message":"Invalid Responses API request"},'
            '"metadata":{"raw":"[{\\n \\\"code\\\": \\\"invalid_union\\\",'
            '\\n \\\"errors\\\": [[{\\n \\\"expected\\\": \\\"string\\\",'
            '\\n \\\"code\\\": \\\"invalid_type\\\",\\n \\\"path\\\": [],'
            '\\n \\\"message\\\": \\\"Invalid input: expected string, received array\\\"'
            '}]]}]"}}'
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=[
                {"role": "user", "content": "运行个 computer use 看看"},
                {
                    "type": "tool_search_output",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "mcp__computer_use",
                            "description": "Control local apps.",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "click",
                                    "description": "Click a point.",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "app": {"type": "string"},
                                            "x": {"type": "number"},
                                            "y": {"type": "number"},
                                        },
                                        "required": ["app", "x", "y"],
                                    },
                                },
                                {
                                    "type": "function",
                                    "name": "type_text",
                                    "description": "Type text.",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {
                                            "app": {"type": "string"},
                                            "text": {"type": "string"},
                                        },
                                        "required": ["app", "text"],
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
            stream=True,
            tools=[{"type": "tool_search"}],
            tool_choice="auto",
            model_info={
                "id": "provider_beta-generic-chat",
                "provider": "provider_beta",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertNotIn("use_chat_completions_api", calls[0])
        first_metadata = calls[0]["litellm_metadata"]
        self.assertTrue(first_metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY])
        self.assertEqual([tool["name"] for tool in calls[0]["tools"]], ["click", "type_text"])
        self.assertTrue(calls[1]["use_chat_completions_api"])
        second_metadata = calls[1]["litellm_metadata"]
        self.assertTrue(second_metadata[hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY])
        self.assertEqual(
            second_metadata[hooks._RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY],
            "responses_schema_unsupported",
        )
        self.assertEqual(
            second_metadata["responses_chat_bridge_input_sanitized"],
            {"changed": True, "dropped_tool_search_items": 1},
        )
        self.assertEqual(
            [item.get("type") for item in calls[1]["input"] if isinstance(item, dict)],
            [None],
        )
        self.assertEqual([tool["name"] for tool in calls[1]["tools"]], ["click", "type_text"])

    async def test_function_tool_bridge_schema_error_drops_tool_search_history_for_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class InvalidResponsesSchema(Exception):
            status_code = 400

        error = InvalidResponsesSchema(
            'OpenAIException - {"error":{"code":"invalid_prompt",'
            '"message":"Invalid Responses API request"},'
            '"metadata":{"raw":"[{\\n \\"code\\": \\"invalid_union\\",'
            '\\n \\"message\\": \\"Invalid input: expected string, received array\\"'
            '}]"}}'
        )

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=[
                {"role": "user", "content": "运行一次computer use试试"},
                {
                    "type": "tool_search_call",
                    "call_id": "call_search",
                    "status": "completed",
                    "arguments": {"query": "computer use"},
                },
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "status": "completed",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "mcp__computer_use",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "list_apps",
                                    "parameters": {"type": "object", "properties": {}},
                                },
                                {
                                    "type": "function",
                                    "name": "get_app_state",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"app": {"type": "string"}},
                                        "required": ["app"],
                                    },
                                },
                            ],
                        }
                    ],
                },
            ],
            stream=True,
            tools=[{"type": "tool_search"}],
            tool_choice="auto",
            model_info={
                "id": "provider_beta-generic-chat",
                "provider": "provider_beta",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses", "openai/chat"],
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertEqual(
            [item.get("type") for item in calls[1]["input"] if isinstance(item, dict)],
            [None],
        )
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["list_apps", "get_app_state"],
        )
        self.assertEqual(
            calls[1]["litellm_metadata"]["responses_chat_bridge_input_sanitized"],
            {"changed": True, "dropped_tool_search_items": 2},
        )

    async def test_generic_response_wrapper_sanitizes_tools_for_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="hi",
            tools=[
                {"type": "custom", "name": "shell"},
                {
                    "type": "namespace",
                    "name": "multi_agent_v2",
                    "description": "Tools for spawning and managing sub-agents.",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "description": "Spawn a sub-agent.",
                            "parameters": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                                "required": ["message"],
                            },
                        },
                        {
                            "type": "function",
                            "name": "bad.name",
                            "parameters": {},
                        },
                    ],
                },
                {"type": "tool_search"},
                {
                    "type": "function",
                    "function": {
                        "name": "valid_func",
                        "description": "do a valid thing",
                        "parameters": {
                            "type": "object",
                            "properties": {"path": {"type": "string"}},
                        },
                        "strict": True,
                    },
                },
                {
                    "type": "function",
                    "name": "valid_responses",
                    "parameters": {"properties": {}},
                },
                {"type": "function", "function": {"description": "missing name"}},
                {"type": "function", "name": "bad.name", "parameters": {}},
            ],
            tool_choice={"type": "custom", "name": "shell"},
            parallel_tool_calls=True,
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertEqual(
            calls[1]["tools"],
            [
                {
                    "type": "function",
                    "name": "shell",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "input": {
                                "type": "string",
                                "description": "Raw custom tool input.",
                            }
                        },
                        "required": ["input"],
                        "additionalProperties": False,
                    },
                    hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True,
                    "description": "Use this local shell tool to inspect repository files, list paths, search text, and run project commands.",
                },
                {
                    "type": "function",
                    "name": "spawn_agent",
                    "parameters": {
                        "type": "object",
                        "properties": {"message": {"type": "string"}},
                        "required": ["message"],
                    },
                    "description": "Spawn a sub-agent. This tool was originally exposed under the multi_agent_v2 namespace.",
                    hooks._RESPONSES_BRIDGE_NAMESPACE_KEY: "multi_agent_v2",
                },
                {
                    "type": "function",
                    "name": "tool_search",
                    "description": (
                        "Search the client-side deferred tool registry and return "
                        "matching tool definitions, such as Codex sub-agent tools."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Natural-language query for deferred tool discovery.",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum number of matching tools to return.",
                            },
                        },
                        "required": ["query"],
                        "additionalProperties": False,
                    },
                },
                {
                    "type": "function",
                    "name": "valid_func",
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                    },
                    "description": "do a valid thing",
                    "strict": True,
                },
                {
                    "type": "function",
                    "name": "valid_responses",
                    "parameters": {"properties": {}},
                },
            ],
        )
        self.assertEqual(
            calls[1]["tool_choice"],
            {"type": "function", "function": {"name": "shell"}},
        )
        self.assertTrue(calls[1]["parallel_tool_calls"])
        stats = calls[1]["litellm_metadata"]["responses_chat_bridge_tool_sanitized"]
        self.assertEqual(stats["original_count"], 7)
        self.assertEqual(stats["kept_count"], 5)
        self.assertEqual(stats["invalid_function_tools"], 2)
        self.assertEqual(stats["bridged_custom_tools"], 1)
        self.assertEqual(stats["bridged_tool_search_tools"], 1)
        self.assertEqual(stats["bridged_namespace_tools"], 1)
        self.assertEqual(stats["bridged_web_search_tools"], 0)
        self.assertEqual(
            stats["kept_tool_names"],
            ["shell", "spawn_agent", "tool_search", "valid_func", "valid_responses"],
        )
        self.assertEqual(
            stats["dropped_types"],
            [],
        )

    async def test_generic_response_wrapper_bridges_client_tools_without_chat_for_responses_route(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="edit a file",
            stream=True,
            tools=[
                {"type": "custom", "name": "apply_patch", "description": "Edit files."},
                {"type": "tool_search"},
                {
                    "type": "namespace",
                    "name": "multi_agent_v2",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            ],
            tool_choice={"type": "custom", "name": "apply_patch"},
            model_info={
                "id": "provider_beta-generic-chat",
                "provider": "provider_beta",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual(
            [tool.get("name") for tool in calls[0]["tools"]],
            ["apply_patch", "tool_search", "spawn_agent"],
        )
        self.assertEqual(
            calls[0]["tool_choice"],
            {"type": "function", "name": "apply_patch"},
        )
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY])
        stats = metadata["responses_function_tool_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_custom_tools"], 1)
        self.assertEqual(stats["bridged_tool_search_tools"], 1)
        self.assertEqual(stats["bridged_namespace_tools"], 1)

    async def test_pre_call_drops_tool_choice_when_tools_are_empty(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Say hello.",
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "stream": True,
        }

        updated = await hook.async_pre_call_deployment_hook(
            request_kwargs,
            "aresponses",
        )

        self.assertIsNotNone(updated)
        self.assertNotIn("tools", updated)
        self.assertNotIn("tool_choice", updated)
        self.assertNotIn("parallel_tool_calls", updated)
        self.assertEqual(updated["model"], "balanced-chat")
        self.assertIn("tool_choice", request_kwargs)

    async def test_generic_response_wrapper_drops_empty_tool_controls(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="Say hello.",
            tools=[],
            tool_choice="auto",
            parallel_tool_calls=False,
            stream=True,
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("tools", calls[0])
        self.assertNotIn("tool_choice", calls[0])
        self.assertNotIn("parallel_tool_calls", calls[0])

    async def test_preemptive_chat_only_responses_bridge_streams_via_acompletion(self) -> None:
        hooks, proxy_server = load_hook_module()
        chat_calls = []

        async def original_generic_function(**_kwargs):
            raise AssertionError("chat-only stream bridge should bypass aresponses")

        async def chat_stream():
            yield {"choices": [{"delta": {"content": "hel"}}]}
            yield {"choices": [{"delta": {"content": "lo"}}]}
            yield {
                "choices": [{"delta": {}, "finish_reason": "stop"}],
                "usage": {
                    "prompt_tokens": 4,
                    "completion_tokens": 2,
                    "total_tokens": 6,
                },
            }

        class FakeRouter:
            async def acompletion(self, **payload):
                chat_calls.append(payload)
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="chat-only-gemini",
            input="Say hello.",
            stream=True,
            model_info={
                "id": "experimental_provider-gemini",
                "model_group": "chat-only-gemini",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
                "supports_responses_endpoint": False,
            },
        )

        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]

        self.assertEqual(len(chat_calls), 1)
        self.assertEqual(chat_calls[0]["model"], "chat-only-gemini")
        self.assertTrue(chat_calls[0]["stream"])
        self.assertEqual(chat_calls[0]["messages"], [{"role": "user", "content": "Say hello."}])
        self.assertNotIn("use_chat_completions_api", chat_calls[0])
        self.assertEqual(
            [chunk["delta"] for chunk in chunks if chunk.get("type") == "response.output_text.delta"],
            ["hel", "lo"],
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["output_text"], "hello")
        self.assertEqual(chunks[-1]["response"]["usage"]["input_tokens"], 4)
        self.assertEqual(chunks[-1]["response"]["usage"]["output_tokens"], 2)

    async def test_direct_chat_bridge_stream_error_after_text_yields_failed_event(self) -> None:
        hooks, proxy_server = load_hook_module()

        async def original_generic_function(**_kwargs):
            raise AssertionError("chat-only stream bridge should bypass aresponses")

        async def chat_stream():
            yield {"choices": [{"delta": {"content": "partial"}}]}
            raise RuntimeError("chat stream disconnected")

        class FakeRouter:
            async def acompletion(self, **_payload):
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="chat-only-gemini",
            input="Say hello.",
            stream=True,
            model_info={
                "id": "experimental_provider-gemini",
                "model_group": "chat-only-gemini",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
                "supports_responses_endpoint": False,
            },
        )

        hook = hooks.LiteLLMMenuHook()
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data={
                    "call_type": "aresponses",
                    "model": "chat-only-gemini",
                    "input": "Say hello.",
                    "stream": True,
                },
            )
        ]

        self.assertIn("partial", [chunk.get("delta") for chunk in chunks])
        self.assertEqual(chunks[-1]["type"], "response.failed")
        self.assertEqual(chunks[-1]["response"]["status"], "failed")

    async def test_codex_compaction_request_preserves_native_request_shape(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": [
                {"role": "user", "content": "prior work"},
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 2048 tokens. Preserve only unresolved work."
                    ),
                },
            ],
            "stream": True,
            "reasoning": {"effort": "medium"},
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
        }

        updated = await hook.async_pre_call_deployment_hook(
            request_kwargs,
            "aresponses",
        )

        self.assertIsNotNone(updated)
        self.assertEqual(updated["tools"], [])
        self.assertEqual(updated["tool_choice"], "auto")
        self.assertFalse(updated["parallel_tool_calls"])
        self.assertNotIn("max_output_tokens", updated)
        self.assertEqual(updated["reasoning"], {"effort": "medium"})
        self.assertNotIn("use_chat_completions_api", updated)
        metadata = updated.get("litellm_metadata", {})
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY, metadata)
        self.assertNotIn("responses_chat_bridge_preemptive_reason", metadata)
        self.assertNotIn("codex_compaction_optimized", metadata)
        self.assertNotIn("codex_compaction_max_output_tokens", metadata)

    async def test_codex_compaction_request_strips_existing_bridge_metadata(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                },
            ],
            "stream": True,
            "reasoning": {"effort": "medium"},
            "use_chat_completions_api": True,
            "litellm_metadata": {
                hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY: True,
                hooks._RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY: True,
                "responses_chat_bridge_preemptive_reason": "codex_compaction",
                hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY: True,
                hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY: True,
                "responses_function_tool_bridge_preemptive_reason": "client_tools_need_responses_function_bridge",
            },
        }

        updated = await hook.async_pre_call_deployment_hook(
            request_kwargs,
            "aresponses",
        )

        self.assertIsNotNone(updated)
        assert updated is not None
        self.assertNotIn("max_output_tokens", updated)
        self.assertEqual(updated["reasoning"], {"effort": "medium"})
        self.assertNotIn("use_chat_completions_api", updated)
        metadata = updated["litellm_metadata"]
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY, metadata)
        self.assertNotIn("responses_chat_bridge_preemptive_reason", metadata)
        self.assertNotIn(hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY, metadata)
        self.assertNotIn("responses_function_tool_bridge_preemptive_reason", metadata)
        self.assertNotIn("codex_compaction_optimized", metadata)
        self.assertNotIn("codex_compaction_max_output_tokens", metadata)

    async def test_generic_response_wrapper_keeps_compaction_on_responses_surface(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=[
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                },
            ],
            reasoning={"effort": "medium"},
            stream=True,
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertNotIn("max_output_tokens", calls[0])
        self.assertEqual(calls[0]["reasoning"], {"effort": "medium"})
        self.assertNotIn("use_chat_completions_api", calls[0])
        metadata = calls[0].get("litellm_metadata", {})
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn("codex_compaction_optimized", metadata)

    async def test_generic_response_wrapper_does_not_bridge_compaction_with_historical_tools(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {
            "original_generic_function": original_generic_function,
            "tools": [
                {
                    "type": "namespace",
                    "name": "codex_app",
                    "tools": [
                        {"type": "function", "function": {"name": "read_thread"}},
                    ],
                }
            ],
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="default-chat",
            input=[
                {
                    "type": "tool_search_output",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "codex_app",
                            "tools": [
                                {"type": "function", "function": {"name": "read_thread"}},
                            ],
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM that will resume the task."
                    ),
                },
            ],
            reasoning={"effort": "xhigh"},
            tools=[],
            tool_choice="auto",
            parallel_tool_calls=False,
            stream=True,
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tools"], [])
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertFalse(calls[0]["parallel_tool_calls"])
        metadata = calls[0].get("litellm_metadata", {})
        self.assertNotIn(hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY, metadata)
        self.assertNotIn("responses_function_tool_bridge_preemptive_reason", metadata)

    async def test_generic_response_wrapper_chat_bridges_optional_web_search_for_subagent(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise error
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="试开一个 subagent",
            tools=[
                {"type": "web_search"},
                {"type": "tool_search"},
                {
                    "type": "namespace",
                    "name": "multi_agent_v2",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            ],
            model_info={
                "id": "provider_alpha-generic-chat",
                "route_key": "provider_alpha / openai/vendor-chat / key=default",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        metadata = calls[1]["litellm_metadata"]
        self.assertTrue(metadata[hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY])
        self.assertTrue(metadata[hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY])
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, "tool_search", "spawn_agent"],
        )
        self.assertNotIn("web_search_options", calls[1])
        self.assertFalse(hasattr(error, "responses_endpoint_unsupported"))
