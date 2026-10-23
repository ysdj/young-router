from __future__ import annotations

from hook_test_utils import *


class HookResponsesChatBridgeTests(HookTestCase):
    async def test_adapted_chat_stream_reports_restored_reasoning(self) -> None:
        hooks, _ = load_hook_module()

        async def adapted_stream():
            yield {"type": "response.completed", "response": {"status": "completed"}}

        codex_kwargs = {
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "continue"},
                {
                    "type": "reasoning",
                    "summary": [],
                    "encrypted_content": "opaque-reasoning",
                },
            ],
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            # The answer this predicate describes comes from a Chat-shaped
            # upstream re-reading the client's whole context.
            "use_chat_completions_api": True,
        }
        marked = hooks._with_restored_reasoning_marker(
            adapted_stream(),
            codex_kwargs,
        )
        self.assertTrue(hooks._restored_reasoning_included(marked))
        self.assertEqual(
            {"x-reasoning-included": "true"},
            await hooks.YoungRouterHook().async_post_call_response_headers_hook(
                data={},
                user_api_key_dict=None,
                response=marked,
                request_headers={},
            ),
        )

        # The same request without any encrypted reasoning replays nothing, so
        # it must not claim the upstream counted restored reasoning.
        without_reasoning = dict(codex_kwargs)
        without_reasoning["input"] = [{"role": "user", "content": "continue"}]
        plain = hooks._with_restored_reasoning_marker(
            adapted_stream(),
            without_reasoning,
        )
        self.assertFalse(hooks._restored_reasoning_included(plain))

        # A route that serves the history as Responses items replays the same
        # context, so its usage covers the reasoning items too and the header
        # follows the replay rather than the wire shape that carried it.
        native = dict(codex_kwargs)
        native.pop("use_chat_completions_api")
        native["model_info"] = {"upstream_url_surface": "openai/responses"}
        self.assertTrue(
            hooks._restored_reasoning_included(
                hooks._with_restored_reasoning_marker(adapted_stream(), native)
            )
        )

        # A buffered Responses payload is not a stream, so it carries nothing
        # either: the fact only reaches the client through an HTTP header.
        buffered = {"object": "response", "status": "completed", "output": []}
        self.assertIs(
            buffered,
            hooks._with_restored_reasoning_marker(buffered, codex_kwargs),
        )
        self.assertNotIn("headers", buffered)

    async def test_replayed_reasoning_fact_survives_the_surface_rewrite(self) -> None:
        """A Chat-shaped route rewrites ``input`` before the answer exists.

        The evidence that a Codex history carrying encrypted reasoning was
        replayed therefore has to be recorded while ``input`` is still the
        Responses request, and read back from the deployment call's own request.
        """

        hooks, _ = load_hook_module()
        codex_request = {
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "continue"},
                {"type": "reasoning", "summary": [], "encrypted_content": "opaque"},
            ],
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
        }
        self.assertTrue(hooks._chat_bridge_reasoning_included(codex_request))

        # What the surface adapter leaves behind: the replayed fact, and no
        # ``input`` list to re-read it from.
        rewritten = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "continue"}],
            hooks._RESTORED_REASONING_REPLAY_KEY: True,
        }
        self.assertTrue(hooks._restored_reasoning_replayed(rewritten))

        async def adapted_stream():
            yield {"type": "response.completed", "response": {"status": "completed"}}

        marked = hooks._with_restored_reasoning_marker(adapted_stream(), rewritten)
        self.assertTrue(hooks._restored_reasoning_included(marked))

    def test_chat_bridge_removes_empty_nested_enum_from_tool_schema(self) -> None:
        hooks, _ = load_hook_module()
        tools, _, stats = hooks._responses_chat_bridge_sanitize_tools(
            [
                {
                    "type": "function",
                    "name": "update_plan",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "plan": {
                                "type": "array",
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "status": {
                                            "type": "string",
                                            "enum": [],
                                        }
                                    },
                                },
                            }
                        },
                    },
                }
            ]
        )

        self.assertEqual(
            tools[0]["parameters"]["properties"]["plan"]["items"]["properties"]["status"],
            {"type": "string"},
        )
        self.assertTrue(stats["changed"])

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
            "_young_router_upstream_url_surface": "openai/responses",
        }

        self.assertIsNone(
            hooks._responses_chat_bridge_preemptive_kwargs(
                selected_responses,
                stale_outer_chat,
                include_hosted_web_search_unsupported=True,
                include_client_tool_unsupported=True,
            )
        )

        # Even when the selected Responses route is registered as not
        # supporting Hosted search, the caller's declaration is what travels:
        # no local search implementation is substituted for it.
        selected_responses_web_search_bridge = {
            **selected_responses,
            "model_info": {
                **selected_responses["model_info"],
                "supports_responses_web_search": False,
            },
        }
        self.assertIsNone(
            hooks._responses_chat_bridge_preemptive_kwargs(
                selected_responses_web_search_bridge,
                stale_outer_chat,
                include_hosted_web_search_unsupported=True,
                include_client_tool_unsupported=True,
            )
        )
        self.assertEqual(
            selected_responses_web_search_bridge["tools"],
            [{"type": "function", "name": "exec_command", "parameters": {"type": "object"}}, {"type": "web_search"}],
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
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])

    async def test_unknown_client_tools_retry_with_function_bridge_after_native_error(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class UnsupportedClientTool(Exception):
            status_code = 400

        error = UnsupportedClientTool(
            "Invalid tool type 'namespace': supported values are function."
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
            input="use a browser",
            stream=True,
            tools=[
                {"type": "custom", "name": "apply_patch"},
                {
                    "type": "namespace",
                    "name": "browser",
                    "tools": [
                        {
                            "type": "function",
                            "name": "open_page",
                            "parameters": {"type": "object"},
                        }
                    ],
                },
            ],
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [tool["type"] for tool in calls[0]["tools"]],
            ["custom", "namespace"],
        )
        self.assertNotIn(
            hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
            calls[0].get("litellm_metadata", {}),
        )
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["apply_patch", "open_page"],
        )
        self.assertFalse(calls[1]["parallel_tool_calls"])
        metadata = calls[1]["litellm_metadata"]
        self.assertTrue(metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY])
        self.assertEqual(
            metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY],
            "native_client_tools_unsupported",
        )
        self.assertTrue(
            metadata["responses_function_tool_bridge_native_error_fallback"]
        )
        self.assertNotIn(
            hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY,
            metadata,
        )

    async def test_no_compatible_native_tool_endpoint_uses_function_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class UnsupportedClientTool(Exception):
            status_code = 400

        error = UnsupportedClientTool(
            "OpenAIException - "
            '{"error":{"message":"No endpoints found that support the native '
            '`namespace` tool type.","code":400}}. '
            "Received Model Group=default-chat "
            "Available Model Group Fallbacks=None"
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
            model="default-chat",
            input="use a local tool",
            tools=[
                {
                    "type": "namespace",
                    "name": "local_tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "read_item",
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            ],
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["tools"][0]["type"], "namespace")
        self.assertEqual(calls[1]["tools"][0]["type"], "function")
        self.assertEqual(calls[1]["tools"][0]["name"], "read_item")
        self.assertEqual(
            calls[1]["litellm_metadata"][
                hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY
            ],
            "native_client_tools_unsupported",
        )

    async def test_unknown_route_lifts_leading_additional_tools_for_native_attempt(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "description": "Edit files.",
                    },
                    {
                        "type": "namespace",
                        "name": "browser",
                        "description": "Browser tools.",
                        "tools": [
                            {
                                "type": "function",
                                "name": "open_page",
                                "parameters": {"type": "object"},
                            }
                        ],
                    },
                ],
            },
            {"role": "user", "content": "Use a tool."},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=original_input,
            stream=True,
            tools=[{"type": "image_generation"}],
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [tool["type"] for tool in calls[0]["tools"]],
            ["image_generation", "custom", "namespace"],
        )
        self.assertEqual(calls[0]["input"], original_input[1:])
        self.assertEqual(calls[0]["extra_body"]["tools"], calls[0]["tools"])
        self.assertEqual(original_input[0]["type"], "additional_tools")
        metadata = calls[0]["litellm_metadata"]
        self.assertEqual(
            metadata[
                hooks._RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY
            ],
            {
                "tool_count": 3,
                "lifted_additional_tools_items": 1,
                "lifted_tool_count": 2,
            },
        )
        self.assertNotIn(
            hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
            metadata,
        )

    async def test_unknown_route_preserves_controls_while_lifting_only_additional_tools(
        self,
    ) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "custom",
                        "name": "apply_patch",
                        "description": "Edit files.",
                    }
                ],
            },
            {"role": "user", "content": "Use apply_patch exactly once."},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=original_input,
            tools=[],
            tool_choice={"type": "custom", "name": "apply_patch"},
            parallel_tool_calls=False,
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"], original_input[1:])
        self.assertEqual(calls[0]["tools"], [original_input[0]["tools"][0]])
        self.assertEqual(calls[0]["extra_body"]["tools"], calls[0]["tools"])
        self.assertEqual(
            calls[0]["tool_choice"],
            {"type": "custom", "name": "apply_patch"},
        )
        self.assertFalse(calls[0]["parallel_tool_calls"])
        self.assertEqual(original_input[0]["type"], "additional_tools")

    async def test_unknown_route_lifts_leading_additional_tools_for_compaction(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {
                        "type": "custom",
                        "name": "exec",
                        "description": "Run a command.",
                    }
                ],
            },
            {"type": "message", "role": "user", "content": "history"},
            {"type": "compaction_trigger", "id": "compact-now"},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=original_input,
            stream=True,
            tools=[],
            tool_choice="auto",
            parallel_tool_calls=False,
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"], original_input[1:])
        self.assertEqual(calls[0]["input"][-1]["type"], "compaction_trigger")
        self.assertEqual(calls[0]["tools"], [original_input[0]["tools"][0]])
        self.assertEqual(calls[0]["extra_body"]["tools"], calls[0]["tools"])
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertFalse(calls[0]["parallel_tool_calls"])
        self.assertEqual(original_input[0]["type"], "additional_tools")
        metadata = calls[0]["litellm_metadata"]
        self.assertEqual(
            metadata[hooks._RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY],
            {
                "tool_count": 1,
                "lifted_additional_tools_items": 1,
                "lifted_tool_count": 1,
            },
        )
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn("codex_compaction_optimized", metadata)

    async def test_direct_openai_compaction_keeps_additional_tools(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"type": "custom", "name": "exec"}],
            },
            {"type": "message", "role": "user", "content": "history"},
            {"type": "compaction_trigger", "id": "compact-now"},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=original_input,
            stream=True,
            tools=[],
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            model_info={
                "id": "openai-direct",
                "provider": "openai",
                "upstream_url_surface": "openai/responses",
            },
            api_base="https://api.openai.com/v1",
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"], original_input)
        self.assertEqual(calls[0]["input"][0]["type"], "additional_tools")
        metadata = calls[0].get("litellm_metadata", {})
        self.assertNotIn(
            hooks._RESPONSES_NATIVE_CLIENT_TOOL_PASSTHROUGH_METADATA_KEY,
            metadata,
        )

    async def test_openai_compatible_compaction_lifts_additional_tools(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"type": "custom", "name": "exec"}],
            },
            {"type": "message", "role": "user", "content": "history"},
            {"type": "compaction_trigger", "id": "compact-now"},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="openai/public-agent",
            input=original_input,
            stream=True,
            tools=[],
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            model_info={
                "id": "compat-route",
                "provider": "openai",
                "upstream_url_surface": "openai/responses",
            },
            api_base="https://gateway.example.com/v1",
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"][0]["type"], "message")
        self.assertEqual(calls[0]["input"][-1]["type"], "compaction_trigger")
        self.assertEqual(calls[0]["tools"], [original_input[0]["tools"][0]])
        self.assertEqual(original_input[0]["type"], "additional_tools")

    async def test_compaction_lifts_additional_tools_before_api_base_is_bound(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"type": "custom", "name": "exec"}],
            },
            {"type": "message", "role": "user", "content": "history"},
            {"type": "compaction_trigger", "id": "compact-now"},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="openai/public-agent",
            input=original_input,
            stream=True,
            tools=[],
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            model_info={
                "id": "compat-unbound",
                "provider": "openai",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"][0]["type"], "message")
        self.assertEqual(calls[0]["input"][-1]["type"], "compaction_trigger")

    async def test_relay_with_native_client_tools_keeps_additional_tools_on_compaction(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        original_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [{"type": "custom", "name": "exec"}],
            },
            {"type": "message", "role": "user", "content": "history"},
            {"type": "compaction_trigger", "id": "compact-now"},
        ]

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="openai/public-agent",
            input=original_input,
            stream=True,
            tools=[],
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            model_info={
                "id": "compat-native-client-tools",
                "provider": "openai",
                "upstream_url_surface": "openai/responses",
                "supports_responses_client_tools": True,
            },
            api_base="https://gateway.example.com/v1",
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"], original_input)
        self.assertEqual(calls[0]["input"][0]["type"], "additional_tools")

    async def test_lifted_native_tools_retry_with_sanitized_function_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class UnsupportedClientTool(Exception):
            status_code = 400

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise UnsupportedClientTool(
                    "Invalid tool type 'namespace': supported values are function."
                )
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input=[
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {"type": "custom", "name": "apply_patch"},
                        {
                            "type": "namespace",
                            "name": "browser",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "open_page",
                                    "parameters": {"type": "object"},
                                }
                            ],
                        },
                    ],
                },
                {"role": "user", "content": "Use a tool."},
            ],
            stream=True,
            tools=[],
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(
            [tool["type"] for tool in calls[0]["tools"]],
            ["custom", "namespace"],
        )
        self.assertEqual(
            [tool["type"] for tool in calls[1]["tools"]],
            ["function", "function"],
        )
        self.assertEqual(calls[1]["extra_body"]["tools"], calls[1]["tools"])
        self.assertFalse(calls[1]["parallel_tool_calls"])
        self.assertEqual(
            calls[1]["litellm_metadata"][
                hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY
            ],
            "native_client_tools_unsupported",
        )

    async def test_unknown_client_tools_preserve_explicit_parallel_on_native_error(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class UnsupportedClientTool(Exception):
            status_code = 422

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise UnsupportedClientTool(
                    "Unsupported tool type: custom tool."
                )
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="edit",
            tools=[{"type": "custom", "name": "apply_patch"}],
            parallel_tool_calls=True,
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["parallel_tool_calls"])

    async def test_client_tool_schema_union_error_uses_function_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class InvalidClientToolSchema(Exception):
            status_code = 400

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise InvalidClientToolSchema(
                    "Invalid Responses API request: invalid_union at tools.0.type "
                    "for namespace."
                )
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="use tools",
            tools=[
                {
                    "type": "namespace",
                    "name": "browser",
                    "tools": [
                        {
                            "type": "function",
                            "name": "open_page",
                            "parameters": {"type": "object"},
                        }
                    ],
                }
            ],
            model_info={
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["tools"][0]["type"], "function")

    async def test_client_tool_auth_and_generic_errors_do_not_function_bridge(self) -> None:
        hooks, _ = load_hook_module()

        class NativeError(Exception):
            status_code = 400

        for error in (
            NativeError("Authentication failed for namespace tool request"),
            NativeError("Invalid request: malformed input"),
            ConnectionError("Connection refused while sending namespace tool"),
        ):
            calls = []

            async def original_generic_function(**kwargs):
                calls.append(kwargs)
                raise error

            request_kwargs = {"original_generic_function": original_generic_function}
            hooks._with_generic_deployment_failover_wrapper(request_kwargs)

            with self.assertRaises(type(error)):
                await request_kwargs["original_generic_function"](
                    call_type="aresponses",
                    model="balanced-chat",
                    input="use tools",
                    tools=[
                        {
                            "type": "namespace",
                            "name": "browser",
                            "tools": [],
                        }
                    ],
                    model_info={
                        "id": "third-party-responses",
                        "provider": "third-party",
                        "upstream_url_surface": "openai/responses",
                    },
                )

            self.assertEqual(len(calls), 1)

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
                "supports_responses_client_tools": False,
                "supports_responses_function_tools": True,
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
                    "parameters": {
                        "properties": {},
                        "type": "object",
                        "required": [],
                    },
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
                        "required": [],
                    },
                    "description": "do a valid thing",
                    "strict": True,
                },
                {
                    "type": "function",
                    "name": "valid_responses",
                    "parameters": {
                        "properties": {},
                        "type": "object",
                        "required": [],
                    },
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
        self.assertEqual(stats["dropped_web_search_tools"], 0)
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
                "supports_responses_client_tools": False,
                "supports_responses_function_tools": True,
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
        hook = hooks.YoungRouterHook()

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
        message_starts = [
            chunk["item"]
            for chunk in chunks
            if chunk.get("type") == "response.output_item.added"
            and chunk.get("item", {}).get("type") == "message"
        ]
        self.assertNotIn("phase", message_starts[0])
        self.assertEqual(
            {
                chunk["item"].get("type"): chunk.get("output_index")
                for chunk in chunks
                if chunk.get("type") == "response.output_item.added"
            },
            {"message": 0},
        )
        output_message = next(
            item for item in chunks[-1]["response"]["output"]
            if item.get("type") == "message"
        )
        self.assertNotIn("phase", output_message)
        self.assertEqual(chunks[-1]["response"]["usage"]["input_tokens"], 4)
        self.assertEqual(chunks[-1]["response"]["usage"]["output_tokens"], 2)

    async def test_chat_bridge_does_not_publish_other_provider_reasoning_content(
        self,
    ) -> None:
        hooks, proxy_server = load_hook_module()

        async def chat_stream():
            yield {"choices": [{"delta": {"reasoning_content": "Inspect the request."}}]}
            yield {"choices": [{"delta": {"content": "hello"}}]}

        class FakeRouter:
            async def acompletion(self, **_payload):
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {
            "original_generic_function": lambda **_kwargs: None,
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="chat-only-model",
            input="Say hello.",
            stream=True,
            model_info={
                "id": "experimental_provider-chat",
                "model_group": "chat-only-model",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )
        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]

        message = [
            item
            for item in chunks[-1]["response"]["output"]
            if item.get("type") == "message"
        ]
        self.assertEqual(
            [
                item
                for item in chunks[-1]["response"]["output"]
                if item.get("type") == "reasoning"
            ],
            [],
        )
        self.assertEqual(message[0]["content"][0]["text"], "hello")

    async def test_chat_bridge_does_not_publish_reasoning_without_content(
        self,
    ) -> None:
        hooks, proxy_server = load_hook_module()

        async def chat_stream():
            yield {"choices": [{"delta": {"reasoning": "hidden"}}]}
            yield {"choices": [{"delta": {"content": "hello"}}]}

        class FakeRouter:
            async def acompletion(self, **_payload):
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {
            "original_generic_function": lambda **_kwargs: None,
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="gemini-4-argon",
            input="Say hello.",
            stream=True,
            model_info={
                "id": "experimental_provider-gemini",
                "model_group": "gemini-4-argon",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )
        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]

        self.assertEqual(
            [
                item
                for item in chunks[-1]["response"]["output"]
                if item.get("type") == "reasoning"
            ],
            [],
        )

    async def test_chat_bridge_publishes_deepseek_raw_reasoning(
        self,
    ) -> None:
        hooks, proxy_server = load_hook_module()

        async def chat_stream():
            yield {"choices": [{"delta": {"reasoning": "raw thought"}}]}
            yield {"choices": [{"delta": {"content": "hello"}}]}

        class FakeRouter:
            async def acompletion(self, **_payload):
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {
            "original_generic_function": lambda **_kwargs: None,
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="deepseek-v4.1-flash",
            input="Say hello.",
            reasoning={"effort": "high", "summary": "raw"},
            stream=True,
            model_info={
                "id": "experimental_provider-deepseek",
                "model_group": "deepseek-v4.1-flash",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )
        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]

        reasoning = next(
            item
            for item in chunks[-1]["response"]["output"]
            if item.get("type") == "reasoning"
        )
        self.assertEqual(reasoning["summary"][0]["text"], "raw thought")
        self.assertEqual(chunks[-1]["response"]["output_text"], "hello")

    async def test_chat_bridge_streams_deepseek_reasoning_for_auto_summary(
        self,
    ) -> None:
        """A summary request gets the thinking live, before the answer.

        pi asks with ``reasoning: {effort, summary: "auto"}`` and renders a
        thinking block from the ``reasoning_summary_text.delta`` frames of the
        item whose ``output_index`` precedes the message's.  Emitting the
        reasoning only as a finished item after the message left that block
        empty for the whole turn.
        """

        hooks, proxy_server = load_hook_module()

        async def chat_stream():
            yield {"choices": [{"delta": {"reasoning": "raw"}}]}
            yield {"choices": [{"delta": {"reasoning": " thought"}}]}
            yield {"choices": [{"delta": {"content": "hello"}}]}

        class FakeRouter:
            async def acompletion(self, **_payload):
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {
            "original_generic_function": lambda **_kwargs: None,
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="deepseek-v4.1-flash",
            input="Say hello.",
            reasoning={"effort": "high", "summary": "auto"},
            stream=True,
            model_info={
                "id": "experimental_provider-deepseek",
                "model_group": "deepseek-v4.1-flash",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )
        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]

        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.output_item.added",
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary_text.done",
                "response.reasoning_summary_part.done",
                "response.output_item.done",
                "response.output_item.added",
                "response.content_part.added",
                "response.output_text.delta",
                "response.output_text.done",
                "response.content_part.done",
                "response.output_item.done",
                "response.completed",
            ],
        )
        reasoning_item = chunks[1]
        self.assertEqual(reasoning_item["output_index"], 0)
        self.assertEqual(reasoning_item["item"]["type"], "reasoning")
        self.assertEqual(
            [chunk["delta"] for chunk in chunks[3:5]],
            ["raw", " thought"],
        )
        self.assertTrue(
            all(chunk["output_index"] == 0 for chunk in chunks[1:8])
        )
        self.assertEqual(chunks[8]["output_index"], 1)
        self.assertEqual(chunks[8]["item"]["type"], "message")
        self.assertEqual(
            chunks[7]["item"]["summary"],
            [{"type": "summary_text", "text": "raw thought"}],
        )

        completed = chunks[-1]["response"]
        self.assertEqual(completed["output_text"], "hello")
        self.assertEqual(
            [item["type"] for item in completed["output"]],
            ["reasoning", "message"],
        )
        self.assertEqual(
            completed["output"][0]["summary"],
            [{"type": "summary_text", "text": "raw thought"}],
        )

    async def test_chat_bridge_hides_reasoning_no_client_asked_for(
        self,
    ) -> None:
        """A request with no reasoning summary gets no reasoning frames."""

        hooks, proxy_server = load_hook_module()

        async def chat_stream():
            yield {"choices": [{"delta": {"reasoning": "raw thought"}}]}
            yield {"choices": [{"delta": {"content": "hello"}}]}

        class FakeRouter:
            async def acompletion(self, **_payload):
                return chat_stream()

        proxy_server.llm_router = FakeRouter()
        request_kwargs = {
            "original_generic_function": lambda **_kwargs: None,
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="deepseek-v4.1-flash",
            input="Say hello.",
            stream=True,
            model_info={
                "id": "experimental_provider-deepseek",
                "model_group": "deepseek-v4.1-flash",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )
        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]

        self.assertEqual(
            [
                chunk["type"]
                for chunk in chunks
                if chunk["type"].startswith("response.reasoning")
            ],
            [],
        )
        self.assertEqual(chunks[1]["output_index"], 0)
        self.assertEqual(chunks[1]["item"]["type"], "message")
        self.assertEqual(
            [item["type"] for item in chunks[-1]["response"]["output"]],
            ["message"],
        )
        self.assertEqual(chunks[-1]["response"]["output_text"], "hello")

    def test_chat_bridge_stream_payload_sends_the_reasoning_level_as_chat(self) -> None:
        """The text bridge hands its Chat upstream the level, not the object.

        An OpenAI-chat gateway reads ``reasoning_effort`` and ignores a
        Responses ``reasoning`` object whole, so forwarding that object left
        every route served by the bridge answering without thinking.
        """

        hooks, _ = load_hook_module()

        payload = hooks._chat_bridge_stream_payload(
            {
                "model": "default-chat",
                "input": "Summarize this thread.",
                "stream": True,
                "reasoning": {"effort": "high", "summary": "auto"},
                "model_info": {
                    "id": "chat-only",
                    "upstream_url_surface": "openai/chat",
                    "supported_upstream_url_surfaces": ["openai/chat"],
                },
            }
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload.get("reasoning_effort"), "high")
        self.assertNotIn("reasoning", payload)

    async def test_chat_only_bridge_preserves_codex_additional_tools_and_tool_call(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_tool_call",
                "status": "completed",
                "output_text": "我先检查一下。",
                "output": [
                    {
                        "id": "msg_intro",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "我先检查一下。",
                                "annotations": [],
                            }
                        ],
                    },
                    {
                        "id": "call_exec",
                        "call_id": "call_exec",
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": '{"input":"pwd"}',
                        "status": "completed",
                    },
                ],
            }

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="chat-only-model",
            input=[
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "custom",
                            "name": "exec_command",
                            "description": "Run a local command.",
                        }
                    ],
                },
                {"role": "user", "content": "Inspect the workspace."},
            ],
            stream=True,
            model_info={
                "id": "chat-only-route",
                "model_group": "chat-only-model",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["use_chat_completions_api"])
        self.assertTrue(calls[0]["stream"])
        self.assertEqual(
            calls[0]["tools"][0]["name"],
            "exec_command",
        )
        self.assertTrue(
            calls[0]["tools"][0][hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY]
        )
        self.assertNotIn(
            "additional_tools",
            json.dumps(calls[0]["input"], ensure_ascii=False),
        )
        self.assertEqual(
            calls[0]["litellm_metadata"]["responses_chat_bridge_input_sanitized"],
            {
                "changed": True,
                "dropped_tool_search_items": 0,
                "dropped_additional_tools_items": 1,
            },
        )

        dumped = hooks._normalize_response_tool_search_output(
            hooks._jsonable(response),
            custom_tool_names={"exec_command"},
        )
        self.assertEqual(dumped["output_text"], "我先检查一下。")
        tool_call = next(
            item
            for item in dumped["output"]
            if item.get("type") == "custom_tool_call"
        )
        self.assertEqual(tool_call["name"], "exec_command")
        self.assertEqual(tool_call["input"], "pwd")

    async def test_selected_chat_route_sanitizes_responses_tools_when_chat_dispatch_is_set(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        class UnknownToolTypeError(Exception):
            status_code = 400

        tools = [
            {
                "type": "custom",
                "name": "exec_command",
                "description": "Run a local command.",
            },
            {
                "type": "namespace",
                "name": "codex_app",
                "tools": [
                    {
                        "type": "function",
                        "name": "read_thread",
                        "parameters": {"type": "object"},
                    }
                ],
            },
        ]

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if any(tool.get("type") in {"custom", "namespace"} for tool in kwargs["tools"]):
                raise UnknownToolTypeError(
                    "Unknown tool type: custom, currently only function and plugin are supported"
                )
            return {
                "id": "resp_chat_dispatch",
                "status": "completed",
                "output_text": "ok",
                "output": [
                    {
                        "id": "msg_chat_dispatch",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "ok",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_kwargs = {
            "original_generic_function": original_generic_function,
            "call_type": "aresponses",
            "model": "mapped-chat",
            "input": "Reply with ok.",
            "tools": tools,
            "stream": True,
        }
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="openai/chat-only-model",
            input="Reply with ok.",
            tools=tools,
            stream=True,
            use_chat_completions_api=True,
            model_info={
                "id": "chat-only-route",
                "model_group": "mapped-chat",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
            _young_router_upstream_url_surface="openai/chat",
        )

        self.assertEqual(response["output_text"], "ok")
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["use_chat_completions_api"])
        self.assertEqual(
            {tool["name"] for tool in calls[0]["tools"]},
            {"exec_command", "read_thread"},
        )
        self.assertTrue(all(tool["type"] == "function" for tool in calls[0]["tools"]))
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY])
        self.assertEqual(
            metadata["responses_chat_bridge_preemptive_reason"],
            "responses_endpoint_unsupported",
        )
        self.assertNotIn(hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY, metadata)

    def test_selected_chat_route_does_not_retry_on_responses_function_surface(self) -> None:
        hooks, _ = load_hook_module()

        class UnknownToolTypeError(Exception):
            status_code = 400

        error = UnknownToolTypeError(
            "Unknown tool type: custom, currently only function and plugin are supported"
        )
        selected_chat = {
            "call_type": "aresponses",
            "model": "openai/chat-only-model",
            "input": "Reply with ok.",
            "tools": [{"type": "custom", "name": "exec_command"}],
            "stream": True,
            "use_chat_completions_api": True,
            "model_info": {
                "id": "chat-only-route",
                "model_group": "mapped-chat",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
            "_young_router_upstream_url_surface": "openai/chat",
        }
        outer_responses = {
            "call_type": "aresponses",
            "model": "mapped-chat",
            "input": "Reply with ok.",
            "tools": [{"type": "custom", "name": "exec_command"}],
            "stream": True,
        }

        self.assertIsNone(
            hooks._responses_function_tool_bridge_retry_kwargs(
                error,
                selected_chat,
                outer_responses,
            )
        )

    def test_explicit_anthropic_surface_drops_hosted_search_declaration(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "kimi-k3",
            "input": "上海今天的天气",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "tool_choice": "auto",
            "model_info": {
                "provider": "relay-provider",
                "upstream_url_surface": "anthropic",
            },
            "_young_router_upstream_url_surface": "anthropic",
        }

        bridge_kwargs = hooks._responses_chat_bridge_preemptive_kwargs(
            request_kwargs,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
            allow_selected_marker=True,
        )

        self.assertIsNotNone(bridge_kwargs)
        assert bridge_kwargs is not None
        self.assertTrue(bridge_kwargs["use_chat_completions_api"])
        # An Anthropic/Chat surface has no Hosted web_search. The declaration
        # is dropped, exactly once, and no local search functions take its
        # place: the proxy does not run searches itself.
        self.assertEqual(bridge_kwargs.get("tools"), None)
        self.assertNotIn("web_search_options", bridge_kwargs)
        stats = bridge_kwargs["litellm_metadata"][
            "responses_chat_bridge_tool_sanitized"
        ]
        self.assertEqual(stats["dropped_web_search_tools"], 1)
        self.assertIn("web_search", stats["dropped_types"])
        self.assertEqual(
            bridge_kwargs["litellm_metadata"]["responses_chat_bridge_preemptive_reason"],
            "responses_endpoint_unsupported",
        )

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
            },
        )

        hook = hooks.YoungRouterHook()
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
        hook = hooks.YoungRouterHook()

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
                {"type": "compaction_trigger"},
            ],
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
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
        hook = hooks.YoungRouterHook()

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
                {"type": "compaction_trigger"},
            ],
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
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
                {"type": "compaction_trigger"},
            ],
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
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
                {"type": "compaction_trigger"},
            ],
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
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
        # Chat Completions has no Hosted declaration, so the bridge drops it
        # instead of inventing a local search that the proxy would run.
        stats = metadata["responses_chat_bridge_tool_sanitized"]
        self.assertEqual(stats["dropped_web_search_tools"], 1)
        self.assertIn("web_search", stats["dropped_types"])
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["tool_search", "spawn_agent"],
        )
        self.assertNotIn("web_search_options", calls[1])
        self.assertFalse(hasattr(error, "responses_endpoint_unsupported"))

    def test_a_delegation_output_becomes_the_user_turn_it_stands_for(self) -> None:
        """A thread started by ``create_thread`` is given its task as output.

        Replayed as a tool result that task is something the model was told
        rather than something it was asked, so the turn ends on "what would
        you like me to work on" instead of starting the work.
        """

        hooks, _ = load_hook_module()
        bridged, stats = hooks._responses_chat_bridge_input(
            [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "AGENTS.md"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call-created",
                    "name": "create_thread",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-created",
                    "output": (
                        "<codex_delegation>\n"
                        "  <source_thread_id>01a138ab</source_thread_id>\n"
                        "  <input>GOAL: make build-and-restart fast.</input>\n"
                        "</codex_delegation>"
                    ),
                },
            ],
            [{"type": "function", "name": "create_thread"}],
        )

        self.assertEqual(stats.get("lifted_delegation_tasks"), 1)
        user_messages = [
            item
            for item in bridged
            if item.get("type") == "message" and item.get("role") == "user"
        ]
        self.assertEqual(len(user_messages), 2)
        self.assertEqual(
            user_messages[1]["content"][0]["text"],
            "GOAL: make build-and-restart fast.",
        )
        # The lifted turn goes after the call and its result, never between
        # them: a chat surface rejects a tool result that is not adjacent to
        # its call.
        types = [item.get("type") for item in bridged]
        self.assertEqual(
            types,
            ["message", "function_call", "function_call_output", "message"],
        )

    def test_an_ordinary_tool_output_is_never_lifted(self) -> None:
        hooks, _ = load_hook_module()
        for output in (
            "file listing:\nfoo.py",
            "",
            "<codex_delegation><source_thread_id>x</source_thread_id></codex_delegation>",
            "<other><input>not a task</input></other>",
        ):
            with self.subTest(output=output):
                bridged, stats = hooks._responses_chat_bridge_input(
                    [{"type": "function_call_output", "call_id": "c1", "output": output}],
                    [{"type": "function", "name": "create_thread"}],
                )
                self.assertNotIn("lifted_delegation_tasks", stats)
                self.assertFalse(stats.get("changed"))
                self.assertEqual(len(bridged), 1)
