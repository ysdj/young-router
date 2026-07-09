from __future__ import annotations

from hook_test_utils import *


class HookResponsesToolBridgeTests(HookTestCase):
    def test_external_web_search_bridge_tool_exposes_url_read_without_pseudo_actions(self) -> None:
        hooks, _ = load_hook_module()

        tool = hooks._responses_bridge_web_search_tool({"type": "web_search"})

        self.assertIsNotNone(tool)
        assert tool is not None
        self.assertEqual(tool["name"], hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME)
        dumped = json.dumps(tool)
        self.assertIn('"query"', dumped)
        self.assertIn('"url"', dumped)
        self.assertIn('"pattern"', dumped)
        self.assertNotIn("openPage", dumped)
        self.assertNotIn("findInPage", dumped)
        self.assertEqual(tool["parameters"].get("required"), [])

    def test_responses_tool_bridge_describes_codex_local_file_workflow(self) -> None:
        hooks, _ = load_hook_module()

        exec_tool = hooks._responses_bridge_function_tool(
            {
                "type": "function",
                "name": "exec_command",
                "description": "Run a command.",
                "parameters": {
                    "type": "object",
                    "properties": {"cmd": {"type": "string"}},
                    "required": ["cmd"],
                },
            }
        )
        patch_tool = hooks._responses_bridge_custom_tool(
            {
                "type": "custom",
                "name": "apply_patch",
                "description": "Apply a patch.",
            }
        )

        self.assertIsNotNone(exec_tool)
        self.assertIsNotNone(patch_tool)
        assert exec_tool is not None
        assert patch_tool is not None
        self.assertIn("inspect repository files", exec_tool["description"])
        self.assertIn("run project commands", exec_tool["description"])
        self.assertIn("Use this for file edits", patch_tool["description"])

    def test_trace_tool_summary_expands_namespace_children(self) -> None:
        hooks, _ = load_hook_module()

        tools = [
            {"type": "function", "name": "exec_command"},
            {
                "type": "namespace",
                "name": "codex_app",
                "tools": [
                    {
                        "type": "function",
                        "name": "read_thread",
                        "parameters": {"type": "object"},
                    },
                    {
                        "type": "function",
                        "function": {"name": "set_thread_title"},
                    },
                ],
            },
        ]

        self.assertEqual(
            hooks._trace_tool_names(tools),
            ["exec_command", "codex_app", "read_thread", "set_thread_title"],
        )
        exposed = hooks._trace_tools_summary({"tools": tools})["exposed"]
        self.assertEqual(exposed[1], {"type": "namespace", "name": "codex_app"})
        self.assertEqual(
            exposed[2],
            {"type": "function", "name": "read_thread", "namespace": "codex_app"},
        )
        self.assertEqual(
            exposed[3],
            {"type": "function", "name": "set_thread_title", "namespace": "codex_app"},
        )

    def test_selected_deployment_metadata_is_remembered_for_stream_timeouts(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {"model": "default-chat", "litellm_metadata": {"headers": {"session-id": "s1"}}}
        deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "order": 3,
                "api_base": "https://headers.example/v1",
            },
            "model_info": {
                "id": "pro",
                "provider": "compat_provider",
                "api_key_name": "x-pro",
                "route_key": "compat_provider / openai/default-chat / key=x-pro / order=3",
            },
        }

        hooks._remember_selected_deployment_for_request(request_kwargs, deployment)

        metadata = request_kwargs["litellm_metadata"]
        self.assertEqual(metadata["headers"], {"session-id": "s1"})
        self.assertEqual(metadata["api_base"], "https://headers.example/v1")
        self.assertEqual(metadata["model_info"]["id"], "pro")
        self.assertEqual(metadata["model_info"]["order"], 3)
        self.assertEqual(metadata["model_info"]["model"], "openai/default-chat")

    def test_third_party_responses_route_bridges_client_tools_to_responses_functions(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "简单运行一下websearch，搜Sample City weather",
            "stream": True,
            "reasoning": {"effort": "xhigh"},
            "tools": [
                {"type": "web_search"},
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
            "model_info": {
                "id": "provider_beta-generic-chat",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_web_search": False,
                "supported_upstream_url_surfaces": [
                    "openai/chat",
                    "openai/responses",
                    "anthropic",
                ],
            },
        }

        bridge_kwargs = hooks._with_responses_external_web_search_bridge(request_kwargs)

        self.assertIsNotNone(bridge_kwargs)
        assert bridge_kwargs is not None
        self.assertNotIn("use_chat_completions_api", bridge_kwargs)
        self.assertTrue(bridge_kwargs["stream"])
        self.assertEqual(bridge_kwargs["reasoning"]["effort"], "xhigh")
        self.assertEqual(
            [tool.get("type") for tool in bridge_kwargs["tools"]],
            ["function", "custom", "tool_search", "namespace"],
        )
        self.assertEqual(
            bridge_kwargs["tools"][0]["name"],
            hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
        )
        self.assertEqual(bridge_kwargs["tools"][1]["name"], "apply_patch")
        metadata = bridge_kwargs["litellm_metadata"]
        self.assertTrue(metadata[hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY])
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        stats = metadata["responses_external_web_search_tool_sanitized"]
        self.assertEqual(stats["bridged_web_search_tools"], 1)
        self.assertEqual(
            stats["kept_tool_types"],
            ["function", "custom", "tool_search", "namespace"],
        )

        responses_bridge_kwargs = (
            hooks._responses_function_tool_bridge_preemptive_kwargs(request_kwargs)
        )
        self.assertIsNotNone(responses_bridge_kwargs)
        assert responses_bridge_kwargs is not None
        self.assertNotIn("use_chat_completions_api", responses_bridge_kwargs)
        metadata = responses_bridge_kwargs["litellm_metadata"]
        self.assertTrue(metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY])
        self.assertEqual(
            metadata["responses_function_tool_bridge_preemptive_reason"],
            "client_tools_need_responses_function_bridge",
        )
        self.assertEqual(
            [tool.get("name") for tool in responses_bridge_kwargs["tools"]],
            [
                hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                "apply_patch",
                "tool_search",
                "spawn_agent",
            ],
        )
        self.assertEqual(
            responses_bridge_kwargs["tools"][1][hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY],
            True,
        )
        stats = metadata["responses_function_tool_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_custom_tools"], 1)
        self.assertEqual(stats["bridged_tool_search_tools"], 1)
        self.assertEqual(stats["bridged_namespace_tools"], 1)
        self.assertEqual(stats["bridged_web_search_tools"], 1)

        preemptive_chat_kwargs = hooks._responses_chat_bridge_preemptive_kwargs(
            request_kwargs,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
        )
        self.assertIsNone(preemptive_chat_kwargs)

    def test_explicit_client_tool_support_does_not_preemptively_chat_bridge(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "use tools",
            "tools": [
                {"type": "custom", "name": "apply_patch", "description": "Edit files."},
                {"type": "tool_search"},
            ],
            "model_info": {
                "id": "third-party-native-client-tools",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
                "supports_responses_client_tools": True,
            },
        }

        bridge_kwargs = hooks._responses_chat_bridge_preemptive_kwargs(
            request_kwargs,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
        )

        self.assertIsNone(bridge_kwargs)

    def test_native_openai_route_does_not_preemptively_bridge_codex_tools(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "use tools",
            "tools": [
                {"type": "web_search"},
                {"type": "tool_search"},
                {
                    "type": "namespace",
                    "name": "multi_agent_v2",
                    "tools": [{"type": "function", "name": "spawn_agent"}],
                },
            ],
            "model_info": {
                "id": "openai-native",
                "provider": "openai",
                "supports_responses_hosted_tools": True,
                "supports_responses_client_tools": True,
            },
        }

        bridge_kwargs = hooks._responses_chat_bridge_preemptive_kwargs(
            request_kwargs,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
        )

        self.assertIsNone(bridge_kwargs)

    async def test_preemptive_chat_bridge_retries_xhigh_only_after_explicit_error(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ProviderBadRequest(Exception):
            status_code = 400

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise ProviderBadRequest(
                    "OpenAIException - level \"xhigh\" not supported, "
                    "valid levels: low, medium, high"
                )
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="试一下computeruse",
            reasoning={"effort": "xhigh"},
            tools=[
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
                "id": "provider_beta-generic-chat",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": [
                    "openai/chat",
                    "anthropic",
                ],
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[0]["use_chat_completions_api"])
        self.assertEqual(calls[0]["reasoning"]["effort"], "xhigh")
        self.assertEqual(calls[1]["reasoning"]["effort"], "high")
        self.assertTrue(
            calls[1]["litellm_metadata"][
                hooks._XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY
            ]
        )
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["tool_search", "spawn_agent"],
        )

    async def test_generic_response_wrapper_does_not_retry_non_responses_404(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class PlainNotFound(Exception):
            status_code = 404

        error = PlainNotFound('{"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(PlainNotFound):
            await request_kwargs["original_generic_function"](
                model="balanced-chat",
                messages=[{"role": "user", "content": "hi"}],
            )

        self.assertEqual(len(calls), 1)

    async def test_generic_response_wrapper_does_not_loop_chat_bridge_retry(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(ResponsesNotFound):
            await request_kwargs["original_generic_function"](
                call_type="aresponses",
                model="balanced-chat",
                input="hi",
                use_chat_completions_api=True,
            )

        self.assertEqual(len(calls), 1)

    def test_xhigh_reasoning_retry_requires_explicit_unsupported_error(self) -> None:
        hooks, _ = load_hook_module()

        class ProviderBadRequest(Exception):
            status_code = 400

        request_kwargs = {
            "model": "default-chat",
            "reasoning": {"effort": "xhigh"},
        }

        generic_error = ProviderBadRequest(
            "invalid_request_error: bad reasoning_effort xhigh"
        )
        self.assertIsNone(
            hooks._xhigh_reasoning_compat_retry_kwargs(generic_error, request_kwargs)
        )

        missing_high_list_error = ProviderBadRequest(
            "invalid_request_error: valid values are low, medium; got xhigh"
        )
        self.assertIsNone(
            hooks._xhigh_reasoning_compat_retry_kwargs(
                missing_high_list_error,
                request_kwargs,
            )
        )

        unsupported_error = ProviderBadRequest(
            "invalid_request_error: xhigh is not supported for reasoning.effort"
        )
        retry_kwargs = hooks._xhigh_reasoning_compat_retry_kwargs(
            unsupported_error,
            request_kwargs,
        )
        self.assertIsNotNone(retry_kwargs)
        assert retry_kwargs is not None
        self.assertEqual(retry_kwargs["reasoning"]["effort"], "high")
        self.assertTrue(
            retry_kwargs["litellm_metadata"][
                hooks._XHIGH_REASONING_COMPAT_RETRY_METADATA_KEY
            ]
        )

        allowed_values_error = ProviderBadRequest(
            "invalid_request_error: reasoning.effort must be one of low, medium, high; got xhigh"
        )
        retry_kwargs = hooks._xhigh_reasoning_compat_retry_kwargs(
            allowed_values_error,
            request_kwargs,
        )
        self.assertIsNotNone(retry_kwargs)
        assert retry_kwargs is not None
        self.assertEqual(retry_kwargs["reasoning"]["effort"], "high")

        pydantic_literal_error = ProviderBadRequest(
            "1 validation error: {'type': 'literal_error', "
            "'loc': ('body', 'reasoning_effort'), "
            "\"msg\": \"Input should be 'none', 'low', 'medium', 'high' or 'max'\", "
            "'input': 'xhigh'}"
        )
        retry_kwargs = hooks._xhigh_reasoning_compat_retry_kwargs(
            pydantic_literal_error,
            {
                "model": "balanced-chat",
                "reasoning": {"effort": "xhigh"},
                "reasoning_effort": "xhigh",
            },
        )
        self.assertIsNotNone(retry_kwargs)
        assert retry_kwargs is not None
        self.assertEqual(retry_kwargs["reasoning"]["effort"], "max")
        self.assertEqual(retry_kwargs["reasoning_effort"], "max")

    def test_tool_search_function_call_rewrites_to_response_tool_search_call(self) -> None:
        hooks, _ = load_hook_module()

        converted = hooks._responses_tool_search_call_from_function_call(
            {
                "type": "function_call",
                "id": "call_search",
                "call_id": "call_search",
                "name": "tool_search",
                "arguments": '{"query":"spawn agent","limit":8}',
                "status": "completed",
            }
        )

        dumped = hooks._jsonable(converted)
        self.assertEqual(dumped["type"], "tool_search_call")
        self.assertEqual(dumped["id"], "call_search")
        self.assertEqual(dumped["call_id"], "call_search")
        self.assertEqual(dumped["execution"], "client")
        self.assertEqual(dumped["status"], "completed")
        self.assertEqual(dumped["arguments"], {"query": "spawn agent", "limit": 8})

    def test_reasoning_summary_message_is_stripped_when_tool_call_present(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "id": "resp_chat",
            "object": "response",
            "status": "completed",
            "output_text": "Let me check the current",
            "output": [
                {
                    "id": "rs_1",
                    "type": "reasoning",
                    "summary": [
                        {"type": "summary_text", "text": "Let me check the current"}
                    ],
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {"type": "output_text", "text": "Let me check the current"}
                    ],
                },
                {
                    "id": "msg_blank",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "\n\n"}],
                },
                {
                    "id": "call_1",
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"pwd"}',
                    "status": "completed",
                },
            ],
        }

        sanitized = hooks._sanitize_response_reasoning_items(response)

        self.assertNotIn("output_text", sanitized)
        self.assertEqual(
            sanitized["output"],
            [
                {
                    "id": "call_1",
                    "type": "function_call",
                    "name": "exec_command",
                    "arguments": '{"cmd":"pwd"}',
                    "status": "completed",
                }
            ],
        )

    async def test_responses_api_proxy_request_path_does_not_change_deployment_order(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://api.primary.example/v1",
                    "order": 1,
                },
                "model_info": {"id": "primary_provider", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://api.backup.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "input": [{"role": "user", "content": "Say pong only."}],
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/responses",
                    "method": "POST",
                },
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_non_string_type_does_not_break_routing(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {"api_base": "https://api.primary.example/v1"},
                "model_info": {"id": "primary_provider", "supports_vision": False},
            },
            {
                "litellm_params": {"api_base": "https://headers.example/v1"},
                "model_info": {"id": "compat_provider-normal", "supports_vision": True},
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
                        "type": ["message"],
                        "content": [
                            {"type": ["input_text"], "text": "Say pong only."},
                        ],
                    }
                ],
            },
        )

        self.assertEqual(filtered, deployments)


if __name__ == "__main__":
    unittest.main()
