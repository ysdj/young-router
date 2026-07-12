from __future__ import annotations

from hook_test_utils import *


class HookResponsesWebSearchBridgeTests(HookTestCase):
    def test_provider_citation_suffix_is_removed_from_source_url(self) -> None:
        hooks, _ = load_hook_module()
        self.assertEqual(
            hooks._external_web_search_clean_url(
                "https://example.test/page[[1]](https://example.test/page)"
            ),
            "https://example.test/page",
        )
        self.assertEqual(
            hooks._external_web_search_clean_url(
                "https://example.test/page.[[1]](https://example.test/page)"
            ),
            "https://example.test/page",
        )
        self.assertEqual(
            hooks._external_web_search_clean_url(
                "https://example.test/wiki/(topic)"
            ),
            "https://example.test/wiki/(topic)",
        )
        self.assertEqual(
            hooks._external_web_search_clean_url("https://example.test/page)"),
            "https://example.test/page",
        )

    def test_synthetic_provider_hidden_search_is_exposed_as_standard_lifecycle(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "call_type": "aresponses",
            "model": "synthetic-search-model",
            "input": "Find the synthetic source.",
            "tools": [{"type": "web_search"}],
            "stream": True,
        }
        response = {
            "type": "response.completed",
            "response": {
                "id": "resp_synthetic",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "tco_synthetic-0",
                        "type": "reasoning",
                        "summary": [{"type": "summary_text", "text": "private"}],
                    },
                    {
                        "id": "msg_synthetic",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Answer with a source.",
                                "annotations": [
                                    {
                                        "type": "url_citation",
                                        "url": "https://example.test/source",
                                    }
                                ],
                            }
                        ],
                    },
                ],
            },
        }

        adapter = hooks._adapt_provider_hidden_web_search_stream

        async def upstream():
            yield response

        async def collect():
            return [hooks._jsonable(chunk) async for chunk in adapter(upstream(), request)]

        chunks = asyncio.run(collect())
        types = [chunk.get("type") for chunk in chunks]
        self.assertEqual(types[:4], [
            "response.output_item.added",
            "response.web_search_call.in_progress",
            "response.web_search_call.searching",
            "response.web_search_call.completed",
        ])
        self.assertIn("response.output_item.done", types)
        self.assertEqual(types[-1], "response.completed")
        completed = chunks[-1]["response"]["output"]
        self.assertFalse(any(item.get("id") == "tco_synthetic-0" for item in completed))
        search_items = [item for item in completed if item.get("type") == "web_search_call"]
        self.assertEqual(len(search_items), 1)
        self.assertEqual(search_items[0]["action"]["query"], "Web search")
        self.assertNotIn(request["input"], json.dumps(chunks))
        self.assertEqual(
            search_items[0]["action"]["sources"][0]["url"],
            "https://example.test/source",
        )

    def test_synthetic_hidden_search_only_primary_item_is_converted(self) -> None:
        hooks, _ = load_hook_module()
        request = {"tools": [{"type": "web_search"}], "input": "synthetic query"}
        response = {
            "output": [
                {"id": "tco_synthetic-0", "type": "reasoning"},
                {"id": "tco_synthetic-1", "type": "reasoning"},
            ]
        }
        sanitized = hooks._sanitize_response_stream_payload(response, request)
        self.assertEqual(
            [item.get("type") for item in sanitized["output"]],
            ["web_search_call"],
        )
        self.assertEqual(sanitized["output"][0]["action"]["query"], "Web search")
        self.assertNotIn(request["input"], json.dumps(sanitized))

    def test_regular_reasoning_and_missing_tool_are_not_hidden_search(self) -> None:
        hooks, _ = load_hook_module()
        regular = {"output": [{"id": "rs_synthetic", "type": "reasoning"}]}
        regular_sanitized = hooks._sanitize_response_stream_payload(
            regular,
            {"input": "x"},
        )
        self.assertFalse(
            any(item.get("type") == "web_search_call" for item in regular_sanitized["output"])
        )
        hidden = {"output": [{"id": "tco_synthetic-0", "type": "reasoning"}]}
        hidden_sanitized = hooks._sanitize_response_stream_payload(
            hidden,
            {"input": "x"},
        )
        self.assertFalse(
            any(item.get("type") == "web_search_call" for item in hidden_sanitized["output"])
        )

    async def test_hidden_provider_search_sequence_numbers_follow_upstream_stream(self) -> None:
        hooks, _ = load_hook_module()

        async def upstream():
            yield {
                "type": "response.created",
                "sequence_number": 40,
                "response": {"id": "resp_seq", "object": "response", "status": "in_progress", "output": []},
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "tco_seq-0", "type": "reasoning"},
            }
            yield {
                "type": "response.output_text.delta",
                "output_index": 9,
                "item_id": "msg_seq",
                "sequence_number": 77,
                "delta": "stub",
            }
            yield {
                "type": "response.output_text.annotation.added",
                "output_index": 9,
                "item_id": "msg_seq",
                "sequence_number": 78,
                "annotation": {
                    "type": "url_citation",
                    "url": "https://example.test/source",
                },
            }
            yield {
                "type": "response.completed",
                "sequence_number": 79,
                "response": {
                    "id": "resp_seq",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {"id": "tco_seq-0", "type": "reasoning"},
                        {
                            "id": "msg_seq",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Native answer.",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://example.test/source",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            }

        hook = hooks.LiteLLMMenuHook()
        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream(),
                request_data={
                    "call_type": "aresponses",
                    "model": "synthetic-native-search-model",
                    "input": "Use provider-native search.",
                    "stream": True,
                    "tools": [{"type": "web_search"}],
                },
            )
        ]
        web_events = [
            chunk for chunk in chunks
            if isinstance(chunk, dict)
            and chunk.get("type") in {
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
                "response.web_search_call.completed",
            }
        ]
        self.assertEqual(
            [chunk["type"] for chunk in web_events],
            [
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
                "response.web_search_call.completed",
            ],
        )
        self.assertGreaterEqual(web_events[0]["sequence_number"], 41)
        self.assertEqual(
            web_events[1]["sequence_number"],
            web_events[0]["sequence_number"] + 1,
        )
        self.assertGreater(
            web_events[2]["sequence_number"],
            78,
        )

    async def test_hidden_provider_search_with_answer_never_runs_local_bridge(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action

        async def forbidden_local_search(*_args, **_kwargs):
            raise AssertionError("native provider search must not run the local bridge")

        hooks._external_web_search_run_action = forbidden_local_search
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream():
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {"id": "tco_synthetic-0", "type": "reasoning"},
            }
            yield {
                "type": "response.output_text.annotation.added",
                "annotation": {
                    "type": "url_citation",
                    "url": "https://example.test/source",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_native_synthetic",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {"id": "tco_synthetic-0", "type": "reasoning"},
                        {
                            "id": "msg_native_synthetic",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Native answer.",
                                    "annotations": [
                                        {
                                            "type": "url_citation",
                                            "url": "https://example.test/source",
                                        }
                                    ],
                                }
                            ],
                        },
                    ],
                },
            }

        hook = hooks.LiteLLMMenuHook()
        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream(),
                request_data={
                    "call_type": "aresponses",
                    "model": "synthetic-native-search-model",
                    "input": "Use provider-native search.",
                    "stream": True,
                    "tools": [{"type": "web_search"}],
                    "model_info": {"supports_responses_web_search": True},
                },
            )
        ]
        self.assertEqual(
            [chunk.get("type") for chunk in chunks].count(
                "response.web_search_call.completed"
            ),
            1,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertIn("Native answer.", json.dumps(chunks))

    def test_sanitize_response_maps_openrouter_hosted_search_output_item(self) -> None:
        hooks, _ = load_hook_module()

        response = {
            "id": "resp_hosted",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "or_search_1",
                    "type": "openrouter:web_search",
                    "status": "completed",
                    "query": "latest Python release",
                    "results": [
                        {
                            "type": "search",
                            "url": "https://www.python.org/downloads/",
                            "title": "Python Releases",
                        }
                    ],
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Python 3.14.2 is current.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://www.python.org/downloads/",
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        sanitized = hooks._sanitize_response_stream_payload(response)
        output = sanitized["output"]
        self.assertEqual(output[0]["type"], "web_search_call")
        self.assertEqual(output[0]["query"], "latest Python release")
        self.assertEqual(output[0]["action"]["type"], "search")
        self.assertEqual(output[0]["action"]["query"], "latest Python release")
        self.assertEqual(
            output[0]["action"]["sources"][0]["url"],
            "https://www.python.org/downloads/",
        )

    async def test_post_call_success_maps_openrouter_hosted_search_output_item(self) -> None:
        hooks, _ = load_hook_module()

        response = {
            "id": "resp_hosted",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "or_search_1",
                    "type": "openrouter:web_search",
                    "status": "completed",
                    "query": "latest Python release",
                    "results": [
                        {
                            "type": "search",
                            "url": "https://www.python.org/downloads/",
                        }
                    ],
                },
                {
                    "id": "msg_1",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "Python 3.14.2 is current.",
                            "annotations": [
                                {
                                    "type": "url_citation",
                                    "url": "https://www.python.org/downloads/",
                                }
                            ],
                        }
                    ],
                },
            ],
        }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "latest Python release",
            "tools": [{"type": "web_search"}],
            "responses_api": True,
        }

        sanitized = await hook.async_post_call_success_deployment_hook(
            request_data,
            response,
            "aresponses",
        )
        self.assertEqual(sanitized["output"][0]["type"], "web_search_call")
        self.assertEqual(
            sanitized["output"][0]["action"]["sources"][0]["url"],
            "https://www.python.org/downloads/",
        )

    async def test_chat_only_external_web_search_uses_chat_completion_for_planning(self) -> None:
        hooks, proxy_server = load_hook_module()
        original_calls = []
        chat_calls = []

        class FakeRouter:
            async def acompletion(self, **kwargs):
                chat_calls.append(kwargs)
                if len(chat_calls) == 1:
                    return {
                        "id": "chat_tool",
                        "object": "chat.completion",
                        "model": "legacy-chat",
                        "choices": [
                            {
                                "finish_reason": "tool_calls",
                                "message": {
                                    "role": "assistant",
                                    "content": "",
                                    "tool_calls": [
                                        {
                                            "id": "call_search",
                                            "type": "function",
                                            "function": {
                                                "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                                                "arguments": json.dumps(
                                                    {
                                                        "query": (
                                                            "latest stable Python release python.org"
                                                        )
                                                    }
                                                ),
                                            },
                                        }
                                    ],
                                },
                            }
                        ],
                    }
                return {
                    "id": "chat_final",
                    "object": "chat.completion",
                    "model": "legacy-chat",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": (
                                    "Python 3.14.2 https://www.python.org/downloads/"
                                ),
                            },
                        }
                    ],
                }

        proxy_server.llm_router = FakeRouter()

        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, _page_cache, _page_fetch_tasks):
            return (
                "Web search results for query: latest stable Python release python.org\n"
                "Title: Download Python\n"
                "URL: https://www.python.org/downloads/\n"
                "Snippet: Download the latest stable Python release.",
                ["https://www.python.org/downloads/"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def original_generic_function(**kwargs):
            original_calls.append(kwargs)
            raise AssertionError("chat-only web_search planning should use acompletion")

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="legacy-chat",
            input="Use web_search for the latest stable Python release.",
            stream=False,
            tools=[{"type": "web_search"}],
            tool_choice="auto",
            model_info={
                "id": "chatroute",
                "provider": "provider_chat",
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        )

        self.assertEqual(original_calls, [])
        self.assertEqual(len(chat_calls), 2)
        self.assertNotIn("input", chat_calls[0])
        self.assertNotIn("use_chat_completions_api", chat_calls[0])
        self.assertEqual(chat_calls[0]["stream"], False)
        for chat_call in chat_calls:
            self.assertTrue(
                chat_call["litellm_metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
            self.assertTrue(
                chat_call["metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
        self.assertEqual(
            chat_calls[0]["tools"][0]["function"]["name"],
            hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
        )
        self.assertEqual(chat_calls[1]["stream"], False)
        self.assertEqual(
            response["output_text"],
            "Python 3.14.2 https://www.python.org/downloads/",
        )
        self.assertEqual(response["output"][0]["type"], "web_search_call")

    async def test_generic_response_wrapper_keeps_unknown_web_search_native_with_client_tool_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_raw",
                "object": "response",
                "status": "completed",
                "output_text": "No search needed.",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "No search needed.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="Use tools if needed.",
            tools=[
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
            tool_choice={"type": "web_search"},
            model_info={
                "id": "provider_beta-gpt",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/balanced-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_client_tools": False,
                "supports_responses_function_tools": True,
            },
        )

        self.assertEqual(response["output_text"], "No search needed.")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertNotIn("web_search_options", calls[0])
        self.assertEqual(calls[0]["tool_choice"], {"type": "web_search"})
        self.assertEqual(
            [tool.get("type") for tool in calls[0]["tools"]],
            [
                "web_search",
                "function",
                "function",
                "function",
            ],
        )
        self.assertTrue(calls[0]["tools"][1][hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY])
        self.assertEqual(
            calls[0]["tools"][3][hooks._RESPONSES_BRIDGE_NAMESPACE_KEY],
            "multi_agent_v2",
        )
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY])
        self.assertTrue(
            metadata[hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_PREEMPTIVE_METADATA_KEY]
        )
        self.assertNotIn(hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY, metadata)
        self.assertEqual(
            metadata[hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY],
            "balanced-chat",
        )
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        stats = metadata["responses_function_tool_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_web_search_tools"], 0)
        self.assertEqual(stats["bridged_custom_tools"], 1)
        self.assertEqual(stats["bridged_tool_search_tools"], 1)
        self.assertEqual(stats["bridged_namespace_tools"], 1)
        self.assertEqual(
            stats["kept_tool_names"],
            [
                "apply_patch",
                "tool_search",
                "spawn_agent",
            ],
        )

    async def test_generic_response_wrapper_keeps_explicit_native_web_search_with_client_tool_bridge(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_raw",
                "object": "response",
                "status": "completed",
                "output_text": "No search needed.",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "No search needed.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="Use tools if needed.",
            tools=[
                {"type": "web_search"},
                {"type": "custom", "name": "apply_patch", "description": "Edit files."},
            ],
            tool_choice={"type": "web_search"},
            model_info={
                "id": "native-search-route",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_client_tools": False,
                "supports_responses_function_tools": True,
                "supports_responses_web_search": True,
            },
        )

        self.assertEqual(response["output_text"], "No search needed.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["tool_choice"], {"type": "web_search"})
        self.assertEqual(
            [tool.get("type") for tool in calls[0]["tools"]],
            ["web_search", "function"],
        )
        metadata = calls[0]["litellm_metadata"]
        self.assertNotIn(hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY, metadata)
        stats = metadata["responses_function_tool_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_web_search_tools"], 0)

    async def test_generic_response_wrapper_tries_pure_web_search_natively_when_support_unknown(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_raw",
                "object": "response",
                "status": "completed",
                "output_text": "No search needed.",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "No search needed.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="Use web_search if needed.",
            stream=True,
            tools=[{"type": "web_search"}],
            tool_choice={"type": "web_search"},
            model_info={
                "id": "provider_beta-gpt",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/balanced-chat / key=default",
                "upstream_url_surface": "openai/responses",
            },
        )

        self.assertEqual(response["output_text"], "No search needed.")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual(calls[0]["tool_choice"], {"type": "web_search"})
        self.assertEqual(
            [tool.get("type") for tool in calls[0]["tools"]],
            ["web_search"],
        )
        metadata = calls[0].get("litellm_metadata", {})
        self.assertNotIn(hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY, metadata)
        self.assertNotIn(
            hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
            metadata,
        )
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)

    async def test_generic_response_wrapper_keeps_unknown_generic_chat_route_native_until_error(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_raw",
                "object": "response",
                "status": "completed",
                "output_text": "No search needed.",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "No search needed.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="legacy-chat",
            input="Use web_search if needed.",
            stream=True,
            tools=[{"type": "web_search"}],
            tool_choice={"type": "web_search"},
            model_info={
                "id": "provider_alpha-generic-chat",
                "provider": "provider_alpha",
                "route_key": "provider_alpha / openai/vendor-chat / key=default / order=1",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
        )

        self.assertEqual(response["output_text"], "No search needed.")
        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [tool.get("type") for tool in calls[0]["tools"]],
            ["web_search"],
        )
        metadata = calls[0].get("litellm_metadata", {})
        self.assertNotIn(hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY, metadata)

    async def test_generic_response_wrapper_prefers_external_bridge_for_explicit_false_web_search(self) -> None:
        hooks, _ = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            return {
                "id": "resp_raw",
                "object": "response",
                "status": "completed",
                "output_text": "No search needed.",
                "output": [
                    {
                        "id": "msg_1",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "No search needed.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        response = await request_kwargs["original_generic_function"](
            call_type="aresponses",
            model="balanced-chat",
            input="Use web_search if needed.",
            stream=True,
            tools=[{"type": "web_search"}],
            tool_choice={"type": "web_search"},
            model_info={
                "id": "provider_beta-generic-chat-no-web-search",
                "provider": "provider_beta",
                "route_key": "provider_beta / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/responses",
                "supports_responses_web_search": False,
            },
        )

        self.assertTrue(hooks._response_is_async_iterable(response))
        chunks = [jsonable_stream_chunk(chunk) async for chunk in response]
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["output_text"], "No search needed.")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertIn(
            calls[0]["tool_choice"],
            (
                hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                {"type": "function", "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME},
            ),
        )
        self.assertEqual(
            [tool.get("name") for tool in calls[0]["tools"]],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY])
        self.assertNotIn(
            hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY,
            metadata,
        )
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        stats = metadata["responses_external_web_search_tool_sanitized"]
        self.assertEqual(stats["bridged_web_search_tools"], 1)
        self.assertEqual(stats["kept_tool_names"], [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME])

    async def test_plain_responses_404_is_not_deployment_failover_error(self) -> None:
        hooks, _ = load_hook_module()

        class ResponsesNotFound(Exception):
            status_code = 404

        error = ResponsesNotFound('OpenAIException - {"detail":"Not Found"}')

        self.assertFalse(hooks._is_priority_deployment_failover_error(error))

    async def test_generic_response_wrapper_suppresses_tool_search_after_deferred_tools_loaded(self) -> None:
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
            input=[
                {"role": "user", "content": "开个subagent我看看"},
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "multi_agent_v2",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "spawn_agent",
                                    "description": "Spawn a sub-agent.",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"message": {"type": "string"}},
                                    },
                                },
                                {
                                    "type": "function",
                                    "name": "wait_agent",
                                    "parameters": {"type": "object"},
                                },
                            ],
                        }
                    ],
                },
            ],
            tools=[
                {"type": "tool_search"},
                {
                    "type": "namespace",
                    "name": "multi_agent_v2",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "description": "Spawn a sub-agent.",
                            "parameters": {
                                "type": "object",
                                "properties": {"message": {"type": "string"}},
                            },
                        },
                        {
                            "type": "function",
                            "name": "wait_agent",
                            "parameters": {"type": "object"},
                        },
                    ],
                },
            ],
            tool_choice="auto",
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["spawn_agent", "wait_agent"],
        )
        self.assertNotIn("tool_search", [tool["name"] for tool in calls[1]["tools"]])
        self.assertIn(
            "call it directly instead of calling tool_search again",
            calls[1]["instructions"],
        )
        stats = calls[1]["litellm_metadata"]["responses_chat_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_namespace_tools"], 2)
        self.assertEqual(stats["bridged_tool_search_tools"], 0)
        self.assertEqual(stats["suppressed_tool_search_tools"], 1)
        self.assertEqual(
            stats["tool_search_output_tool_names"],
            ["multi_agent_v2", "spawn_agent", "wait_agent"],
        )
        self.assertEqual(stats["kept_tool_names"], ["spawn_agent", "wait_agent"])

    async def test_generic_response_wrapper_derives_tools_from_tool_search_output(self) -> None:
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
            input=[
                {"role": "user", "content": "试开一个 subagent"},
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "tools": [
                        {
                            "type": "namespace",
                            "name": "multi_agent_v2",
                            "tools": [
                                {
                                    "type": "function",
                                    "name": "spawn_agent",
                                    "description": "Spawn a sub-agent.",
                                    "parameters": {
                                        "type": "object",
                                        "properties": {"message": {"type": "string"}},
                                    },
                                },
                                {
                                    "type": "function",
                                    "name": "wait_agent",
                                    "parameters": {"type": "object"},
                                },
                            ],
                        }
                    ],
                },
            ],
            tools=[{"type": "tool_search"}],
            tool_choice="auto",
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertEqual(
            [tool["name"] for tool in calls[1]["tools"]],
            ["spawn_agent", "wait_agent"],
        )
        self.assertIn(
            "call it directly instead of calling tool_search again",
            calls[1]["instructions"],
        )
        stats = calls[1]["litellm_metadata"]["responses_chat_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_namespace_tools"], 0)
        self.assertEqual(stats["bridged_tool_search_tools"], 0)
        self.assertEqual(stats["bridged_tool_search_output_tools"], 2)
        self.assertEqual(stats["suppressed_tool_search_tools"], 1)
        self.assertEqual(stats["kept_tool_names"], ["spawn_agent", "wait_agent"])

    async def test_generic_response_wrapper_derives_tools_when_tools_field_is_missing(self) -> None:
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
            input=[
                {
                    "type": "tool_search_output",
                    "call_id": "call_search",
                    "tools": [
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
                        }
                    ],
                },
            ],
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertTrue(calls[1]["use_chat_completions_api"])
        self.assertEqual([tool["name"] for tool in calls[1]["tools"]], ["spawn_agent"])
        stats = calls[1]["litellm_metadata"]["responses_chat_bridge_tool_sanitized"]
        self.assertEqual(stats["bridged_tool_search_output_tools"], 1)
