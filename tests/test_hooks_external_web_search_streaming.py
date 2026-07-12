from __future__ import annotations

import copy
import ssl

from hook_test_utils import *


class HookExternalWebSearchStreamingTests(HookTestCase):
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

    def test_streaming_external_web_search_fallback_payload_prefers_selected_route_over_outer_alias(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "legacy-chat",
                "input": "Original request",
                "stream": True,
                "litellm_metadata": {
                    "external_web_search_synthesis": True,
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
        self.assertEqual(payload["model"], "legacy-chat")
        self.assertNotEqual(payload["model"], "openai/vendor-chat")
        self.assertEqual(
            payload["model_info"]["route_key"],
            "provider_chat / openai/vendor-chat / key=default / order=1",
        )

    def test_streaming_external_web_search_fallback_payload_keeps_plain_provider_wrapped_model_group(self) -> None:
        hooks, _ = load_hook_module()
        payload = hooks._build_streaming_error_fallback_payload(
            {
                "call_type": "aresponses",
                "model": "default-chat",
                "input": "Original request",
                "stream": True,
                "litellm_metadata": {
                    "external_web_search_synthesis": True,
                    hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "default-chat",
                },
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

    async def test_streaming_external_web_search_parallel_actions_progress_before_slow_completion(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        release_slow = asyncio.Event()
        fast_done = asyncio.Event()
        executed_actions = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            query = action.get("query", "")
            if query == "slow query":
                await release_slow.wait()
            if query == "fast query":
                fast_done.set()
            return (
                f"Web search results for query: {query}\n"
                f"Title: Source for {query}\n"
                f"URL: https://example.test/{query.split()[0]}\n"
                "Snippet: Useful result.",
                [f"https://example.test/{query.split()[0]}"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def original_function(**kwargs):
            return {
                "id": "resp_parallel_final",
                "object": "response",
                "status": "completed",
                "output_text": "Final answer after both searches.",
                "output": [
                    {
                        "id": "msg_parallel_final",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Final answer after both searches.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_parallel_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps({"query": "slow query"}),
                    "status": "completed",
                },
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps({"query": "fast query"}),
                    "status": "completed",
                },
            ],
        }

        stream = hooks._resolve_litellm_web_search_function_calls_stream_rounds(
            response,
            {
                "model": "openai/vendor-chat",
                "input": "Use web_search for two independent queries.",
                "tools": [{"type": "web_search"}],
                "stream": True,
            },
            original_function=original_function,
        )
        chunks = []
        while len(chunks) < 8:
            chunk = await asyncio.wait_for(stream.__anext__(), timeout=1)
            chunks.append(jsonable_stream_chunk(chunk))

        self.assertTrue(fast_done.is_set())
        self.assertFalse(release_slow.is_set())
        event_types = [chunk.get("type") for chunk in chunks]
        self.assertEqual(event_types[0], "response.created")
        self.assertEqual(event_types.count("response.web_search_call.searching"), 2)
        self.assertEqual(event_types.count("response.web_search_call.completed"), 1)
        completed_action = next(
            chunk.get("action", {})
            for chunk in chunks
            if chunk.get("type") == "response.web_search_call.completed"
        )
        self.assertEqual(completed_action.get("query"), "fast query")

        release_slow.set()
        async for chunk in stream:
            chunks.append(jsonable_stream_chunk(chunk))

        dumped = json.dumps(chunks)
        self.assertEqual(
            executed_actions,
            [
                {"type": "search", "query": "slow query"},
                {"type": "search", "query": "fast query"},
            ],
        )
        self.assertEqual(dumped.count("response.web_search_call.completed"), 2)
        self.assertIn("Final answer after both searches.", dumped)

    async def test_streaming_external_web_search_deep_dive_runs_auto_source_actions(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action
        executed_actions = []
        continuation_calls = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_actions.append(action.copy())
            action_type = action.get("type")
            if action_type == "search":
                return (
                    "Web search results for query: sample subject factor A inhibition\n"
                    "Title: Primary transporter source\n"
                    "URL: https://example.test/sample-subject-factor-a\n"
                    "Snippet: The claim needs source-page inspection.",
                    ["https://example.test/sample-subject-factor-a"],
                    action,
                )
            if action_type == "openPage":
                return (
                    "Source page read: https://example.test/sample-subject-factor-a\n"
                    "Excerpt: The full page discusses sample subject and factor A transporter assays.",
                    [action.get("url", "")],
                    action,
                )
            if action_type == "findInPage":
                return (
                    f"Find in page: {action.get('pattern', '')} in {action.get('url', '')}\n"
                    "Match: sample subject factor A inhibition was not directly established.",
                    [action.get("url", "")],
                    action,
                )
            raise AssertionError(f"unexpected action: {action}")

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def original_function(**kwargs):
            continuation_calls.append(copy.deepcopy(kwargs))
            metadata = kwargs.get("litellm_metadata", {})
            self.assertTrue(metadata.get("external_web_search_synthesis"))
            self.assertNotIn("external_web_search_continuation", metadata)
            self.assertIn("Source page read", kwargs.get("input", ""))
            return {
                "id": "resp_post_source_final",
                "object": "response",
                "status": "completed",
                "output_text": "Final after source inspection. https://example.test/sample-subject-factor-a",
                "output": [
                    {
                        "id": "msg_post_source_final",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Final after source inspection. https://example.test/sample-subject-factor-a",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        response = {
            "id": "resp_source_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps({"query": "sample subject factor A inhibition"}),
                    "status": "completed",
                }
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._resolve_litellm_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "Deep dive determine whether sample subject inhibits factor A; use web_search.",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=original_function,
            )
        ]

        self.assertEqual(len(continuation_calls), 1)
        self.assertEqual(executed_actions[0], {"type": "search", "query": "sample subject factor A inhibition"})
        self.assertEqual(executed_actions[1], {"type": "openPage", "url": "https://example.test/sample-subject-factor-a"})
        self.assertTrue(
            all(action.get("url") == "https://example.test/sample-subject-factor-a" for action in executed_actions[1:])
        )
        dumped = json.dumps(chunks)
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("Final after source inspection", dumped)

    async def test_streaming_external_web_search_function_call_is_consumed_before_client(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"Sample City weather"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"Sample City weather"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        async def original_generic_function(**kwargs):
            self.assertTrue(
                kwargs["litellm_metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
            return {
                "id": "resp_synth",
                "object": "response",
                "status": "completed",
                "output_text": "Sample City is sunny. https://example.test/weather",
                "output": [
                    {
                        "id": "msg_synth",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Sample City is sunny. https://example.test/weather",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertNotIn('"type": "function_call", "id": "call_web"', dumped)
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("Sample City is sunny. https://example.test/weather", dumped)

    async def test_streaming_external_web_search_structured_completed_message_stops_after_search(self) -> None:
        hooks, _ = load_hook_module()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: sample route signal marker direct MEK\n"
                "Title: Source\n"
                "URL: https://example.test/source\n"
                "Snippet: signal marker evidence.",
                ["https://example.test/source"],
                action,
            )

        calls = []

        async def original_function(**kwargs):
            calls.append(kwargs)
            metadata = kwargs.get("litellm_metadata", {})
            if metadata.get("external_web_search_continuation"):
                return {
                    "id": "resp_continuation_preamble",
                    "object": "response",
                    "status": "completed",
                    "output_text": "现在我已经收集了足够的证据。让我再搜索一下确认 sample route 是否能直接降低 signal marker。",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "现在我已经收集了足够的证据。让我再搜索一下确认 sample route 是否能直接降低 signal marker。",
                                }
                            ],
                        }
                    ],
                }
            if metadata.get("external_web_search_synthesis"):
                self.fail("structured completed assistant message should not synthesize")
            self.fail("unexpected original_function call")

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        response = {
            "id": "resp_search",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "function_call",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": json.dumps({"query": "sample route signal marker direct MEK"}),
                    "status": "completed",
                }
            ],
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hooks._resolve_litellm_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "sample route signal marker direct MEK answer.",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=original_function,
            )
        ]

        dumped = json.dumps(chunks, ensure_ascii=False)
        self.assertIn("让我再搜索一下确认", dumped)
        self.assertNotIn("Final streaming answer. https://example.test/source", dumped)
        self.assertIn('"type": "response.completed"', dumped)
        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["litellm_metadata"].get("external_web_search_continuation"))
        self.assertNotIn("external_web_search_synthesis", calls[0]["litellm_metadata"])

    async def test_streaming_external_web_search_initial_structured_message_is_terminal(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "id": "resp_preamble",
            "object": "response",
            "status": "completed",
            "output_text": "我来为你深挖调查 sample compound 与supportive medication的联用问题。先调用 data-source helper 抽取数据源速查，再执行全网检索和跨源核验。",
            "output": [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "我来为你深挖调查 sample compound 与supportive medication的联用问题。先调用 data-source helper 抽取数据源速查，再执行全网检索和跨源核验。",
                        }
                    ],
                }
            ],
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hooks._resolve_litellm_web_search_function_calls_stream_rounds(
                response,
                {
                    "model": "openai/vendor-chat",
                    "input": "深挖调查 sample compound 与哪些supportive medication可以联用。",
                    "tools": [{"type": "web_search"}],
                    "stream": True,
                },
                original_function=None,
            )
        ]

        dumped = json.dumps(chunks, ensure_ascii=False)
        self.assertIn("先调用 data-source helper", dumped)
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_streaming_native_web_search_completed_payload_message_is_terminal(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            raise AssertionError("completed assistant message must not enter route recovery")
            yield

        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        search_item = {
            "type": "web_search_call",
            "id": "ws_native_final",
            "status": "completed",
            "action": {"type": "search", "query": "sample marker reference"},
        }
        message_item = {
            "type": "message",
            "id": "msg_native_final",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "Structured final answer after web_search.",
                    "annotations": [],
                }
            ],
        }

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_native_final",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**search_item, "status": "in_progress"},
            }
            yield {
                "type": "response.web_search_call.completed",
                "item_id": "ws_native_final",
                "output_index": 0,
                "sequence_number": 1,
                "action": copy.deepcopy(search_item["action"]),
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": search_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_native_final",
                    "object": "response",
                    "status": "completed",
                    "output": [search_item, message_item],
                },
            }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data={
                    "call_type": "aresponses",
                    "model": "default-chat",
                    "input": "Use native web_search and answer.",
                    "stream": True,
                    "tools": [{"type": "web_search"}],
                },
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertIn("Structured final answer after web_search.", dumped)
        self.assertEqual(
            [chunk.get("type") for chunk in chunks].count("response.completed"),
            1,
        )
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_native_web_search_closes_after_completed_even_if_upstream_continues(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            raise AssertionError("valid response.completed must terminate the stream")
            yield

        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        search_item = {
            "type": "web_search_call",
            "id": "ws_native_terminal",
            "status": "completed",
            "action": {"type": "search", "query": "sample marker follow-up"},
        }
        message_item = {
            "type": "message",
            "id": "msg_native_terminal",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "text": "Final answer before upstream leakage.",
                    "annotations": [],
                }
            ],
        }

        events = [
            {
                "type": "response.created",
                "response": {
                    "id": "resp_native_terminal",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            },
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**search_item, "status": "in_progress"},
            },
            {
                "type": "response.web_search_call.completed",
                "item_id": "ws_native_terminal",
                "output_index": 0,
                "sequence_number": 1,
                "action": copy.deepcopy(search_item["action"]),
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": search_item,
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_native_terminal",
                    "object": "response",
                    "status": "completed",
                    "output": [search_item, message_item],
                },
            },
            {
                "type": "response.output_text.delta",
                "delta": "Post-completed commentary must not leak.",
            },
            {
                "type": "response.output_item.added",
                "output_index": 2,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_after_terminal",
                    "status": "in_progress",
                    "action": {"type": "search", "query": "second query after terminal"},
                },
            },
        ]

        class ContinuingAfterCompletedStream:
            def __init__(self, chunks):
                self.chunks = chunks
                self.index = 0
                self.closed = False
                self.completed_returned = False
                self.pulled_after_completed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                if self.closed or self.index >= len(self.chunks):
                    raise StopAsyncIteration
                chunk = copy.deepcopy(self.chunks[self.index])
                self.index += 1
                if self.completed_returned:
                    self.pulled_after_completed = True
                if chunk.get("type") == "response.completed":
                    self.completed_returned = True
                return chunk

            async def aclose(self):
                self.closed = True

        upstream = ContinuingAfterCompletedStream(events)

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream,
                request_data={
                    "call_type": "aresponses",
                    "model": "default-chat",
                    "input": "Use native web_search and answer.",
                    "stream": True,
                    "tools": [{"type": "web_search"}],
                },
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertTrue(upstream.closed)
        self.assertFalse(upstream.pulled_after_completed)
        self.assertIn("Final answer before upstream leakage.", dumped)
        self.assertNotIn("Post-completed commentary must not leak.", dumped)
        self.assertNotIn("second query after terminal", dumped)
        self.assertEqual(
            [chunk.get("type") for chunk in chunks].count("response.completed"),
            1,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_external_web_search_preamble_and_bridge_artifacts_are_hidden(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: example transporter inhibitor 2026\n"
                "Title: example transporter Source\n"
                "URL: https://example.test/mrp1\n"
                "Snippet: example transporter evidence is available.",
                ["https://example.test/mrp1"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            for _index in range(25):
                yield {
                    "type": "response.output_text.delta",
                    "delta": "Let我继续搜索 example transporter 的最新资料。",
                }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"example transporter inhibitor 2026"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call_output",
                    "call_id": "call_web",
                    "output": "internal bridge output must stay hidden",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "id": "msg_preamble",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Let我继续搜索 example transporter 的最新资料。",
                                    "annotations": [],
                                }
                            ],
                        },
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"example transporter inhibitor 2026"}',
                            "status": "completed",
                        },
                        {
                            "type": "function_call_output",
                            "call_id": "call_web",
                            "output": "internal bridge output must stay hidden",
                        },
                    ],
                },
            }

        async def original_generic_function(**kwargs):
            self.assertTrue(
                kwargs["litellm_metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
            return {
                "id": "resp_synth",
                "object": "response",
                "status": "completed",
                "output_text": "example transporter final answer. https://example.test/mrp1",
                "output": [
                    {
                        "id": "msg_synth",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "example transporter final answer. https://example.test/mrp1",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for example transporter.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertNotIn("Let我继续搜索 example transporter", dumped)
        self.assertNotIn("function_call_output", dumped)
        self.assertNotIn("internal bridge output must stay hidden", dumped)
        self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("example transporter final answer. https://example.test/mrp1", dumped)
        self.assertIn('"type": "response.completed"', dumped)
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_external_web_search_split_arguments_without_upstream_completed_runs_search(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "status": "in_progress",
                },
            }
            yield {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_web",
                "call_id": "call_web",
                "delta": '{"query":',
            }
            yield {
                "type": "response.function_call_arguments.delta",
                "item_id": "call_web",
                "call_id": "call_web",
                "delta": '"Sample City weather"}',
            }
            yield {
                "type": "response.function_call_arguments.done",
                "item_id": "call_web",
                "call_id": "call_web",
                "arguments": '{"query":"Sample City weather"}',
            }

        async def original_generic_function(**kwargs):
            self.assertTrue(
                kwargs["litellm_metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
            return {
                "id": "resp_synth",
                "object": "response",
                "status": "completed",
                "output_text": "Sample City is sunny. https://example.test/weather",
                "output": [
                    {
                        "id": "msg_synth",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Sample City is sunny. https://example.test/weather",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        self.assertNotIn('"type": "function_call", "id": "call_web"', dumped)
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("Sample City is sunny. https://example.test/weather", dumped)
        self.assertIn('"type": "response.completed"', dumped)
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_external_web_search_progress_is_visible_before_search_finishes(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        search_started = asyncio.Event()
        release_search = asyncio.Event()

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            search_started.set()
            await release_search.wait()
            return (
                "Web search results for query: slow visible search\n"
                "Title: Slow Source\n"
                "URL: https://example.test/slow\n"
                "Snippet: Search completed.",
                ["https://example.test/slow"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "status": "in_progress",
                },
            }
            yield {
                "type": "response.function_call_arguments.done",
                "item_id": "call_web",
                "call_id": "call_web",
                "arguments": '{"query":"slow visible search"}',
            }
            while True:
                await asyncio.sleep(3600)

        async def original_generic_function(**kwargs):
            return {
                "id": "resp_synth",
                "object": "response",
                "status": "completed",
                "output_text": "Done after visible progress.",
                "output": [
                    {
                        "id": "msg_synth",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Done after visible progress.",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for slow visible search.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        stream = hook.async_post_call_streaming_iterator_hook(
            user_api_key_dict=None,
            response=upstream_stream(),
            request_data=request_data,
        )
        chunks = []
        try:
            for _ in range(5):
                chunks.append(
                    hooks._jsonable(await asyncio.wait_for(stream.__anext__(), timeout=1))
                )

            dumped = json.dumps(chunks)
            self.assertFalse(release_search.is_set())
            self.assertIn('"type": "web_search_call"', dumped)
            self.assertIn("response.web_search_call.searching", dumped)
            self.assertIn("slow visible search", dumped)
            self.assertNotIn("response.web_search_call.completed", dumped)
            self.assertNotIn("Done after visible progress.", dumped)
            self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        finally:
            release_search.set()
            await stream.aclose()

    async def test_streaming_external_web_search_route_recovery_after_search_completes(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            self.assertEqual(request_data["model"], "legacy-chat")
            self.assertNotEqual(request_data["model"], "openai/vendor-chat")
            self.assertNotEqual(request_data["model"], "balanced-chat")
            self.assertIn("tools", request_data)
            self.assertNotIn("tool_choice", request_data)
            self.assertTrue(
                request_data["litellm_metadata"]["external_web_search_continuation"]
            )
            self.assertNotIn(
                "external_web_search_synthesis",
                request_data["litellm_metadata"],
            )
            self.assertIn("Retrieved evidence observed so far:", request_data["input"])
            self.assertIn("Weather Source", request_data["input"])
            self.assertIn("https://example.test/weather", request_data["input"])
            yield {
                "type": "response.output_text.delta",
                "delta": "Recovered final answer. https://example.test/weather",
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered final answer. https://example.test/weather",
                    "output": [
                        {
                            "id": "msg_recovered",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered final answer. https://example.test/weather",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"Sample City weather"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"Sample City weather"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        calls = 0

        async def original_generic_function(**kwargs):
            nonlocal calls
            calls += 1

            class ServiceUnavailableError(Exception):
                status_code = 503

            raise ServiceUnavailableError("temporary upstream failure")

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "litellm_metadata": {
                hooks._RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY: "balanced-chat",
            },
            "model_info": {
                "id": "failed-chatroute",
                "model_group": "legacy-chat",
                "model": "openai/vendor-chat",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(calls, 1)
        self.assertEqual(len(recovery_requests), 1)
        self.assertEqual(
            sum(1 for chunk in chunks if chunk.get("type") == "response.created"),
            1,
        )
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("Recovered final answer. https://example.test/weather", dumped)
        self.assertIn('"type": "response.completed"', dumped)
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_external_web_search_continuation_recovery_keeps_continuation_request(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: sample subject factor A factor B inhibition\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: Useful transporter evidence.",
                ["https://example.test/one"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            yield {
                "type": "response.output_text.delta",
                "delta": "Recovered final answer. https://example.test/one",
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered final answer. https://example.test/one",
                    "output": [
                        {
                            "id": "msg_recovered_continuation",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered final answer. https://example.test/one",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        class TemporaryUpstreamError(Exception):
            status_code = 504

        async def original_generic_function(**kwargs):
            raise TemporaryUpstreamError("upstream 504")

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"sample subject factor A factor B inhibition"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"sample subject factor A factor B inhibition"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Use web_search for sample subject factor A factor B.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "litellm_metadata": {hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True},
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(recovery_requests), 1)
        recovery_request = recovery_requests[0]
        self.assertTrue(
            recovery_request["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertNotIn("external_web_search_synthesis", recovery_request["litellm_metadata"])
        self.assertEqual(
            [tool.get("name") for tool in recovery_request["tools"]],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        self.assertIn("Decide the next step now", recovery_request["input"])
        dumped = json.dumps(chunks)
        self.assertIn("Recovered final answer. https://example.test/one", dumped)
        self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        self.assertNotIn('"type": "function_call", "id": "call_web"', dumped)

    async def test_streaming_external_web_search_missing_answer_preserves_inner_continuation_payload(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: latest Python stable\n"
                "Title: Python Downloads\n"
                "URL: https://www.python.org/downloads/\n"
                "Snippet: Latest stable release details.",
                ["https://www.python.org/downloads/"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_preserved_continuation",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered final answer. https://www.python.org/downloads/",
                    "output": [
                        {
                            "id": "msg_recovered_preserved_continuation",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered final answer. https://www.python.org/downloads/",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        class BridgeResolutionError(Exception):
            pass

        bridge_exception = BridgeResolutionError("bridge continuation failed before route classification")
        continuation_request = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Retrieved evidence observed so far:\nPython Downloads\n\nDecide the next step now.",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "parameters": {"type": "object", "properties": {}},
                }
            ],
            "litellm_metadata": {
                "external_web_search_continuation": True,
                "external_web_search_round": 1,
            },
        }
        async def upstream_stream():
            item = {
                "type": "web_search_call",
                "id": "ws_latest_python",
                "status": "completed",
                "action": {
                    "type": "search",
                    "query": "latest Python stable",
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": item,
            }
            yield {
                "type": "response.web_search_call.completed",
                "item_id": "ws_latest_python",
                "output_index": 0,
                "sequence_number": 1,
                "action": copy.deepcopy(item["action"]),
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": item,
            }
            raise bridge_exception

        request_data = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Use web_search for latest Python stable.",
            "stream": True,
            "tools": [{"type": "web_search"}],
        }
        hooks._external_web_search_set_pending_recovery_request(
            request_data,
            continuation_request,
        )

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(recovery_requests), 1)
        recovery_request = recovery_requests[0]
        self.assertTrue(
            recovery_request["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertNotIn("external_web_search_synthesis", recovery_request["litellm_metadata"])
        self.assertEqual(recovery_request["model"], "openai/vendor-chat")
        self.assertIn("Decide the next step now", recovery_request["input"])
        self.assertEqual(
            [tool.get("name") for tool in recovery_request["tools"]],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        dumped = json.dumps(chunks)
        self.assertIn("Recovered final answer. https://www.python.org/downloads/", dumped)
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_external_web_search_continuation_recovery_skips_duplicate_search_call(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        executed_queries = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            executed_queries.append(action.get("query"))
            return (
                "Web search results for query: sample subject factor A factor B inhibition\n"
                "Title: Source One\n"
                "URL: https://example.test/one\n"
                "Snippet: Useful transporter evidence.",
                ["https://example.test/one"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_duplicate_call",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_duplicate",
                            "call_id": "call_duplicate",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"sample subject factor A factor B inhibition"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        class TemporaryUpstreamError(Exception):
            status_code = 504

        original_calls = 0

        async def original_generic_function(**kwargs):
            nonlocal original_calls
            original_calls += 1
            if original_calls == 1:
                raise TemporaryUpstreamError("upstream 504")
            self.assertNotIn("tools", kwargs)
            return {
                "id": "resp_synth_after_duplicate",
                "object": "response",
                "status": "completed",
                "output_text": "Final after duplicate recovery. https://example.test/one",
                "output": [
                    {
                        "id": "msg_synth_after_duplicate",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Final after duplicate recovery. https://example.test/one",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"sample subject factor A factor B inhibition"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Use web_search for sample subject factor A factor B.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "litellm_metadata": {hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True},
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(executed_queries, ["sample subject factor A factor B inhibition"])
        self.assertEqual(original_calls, 2)
        self.assertEqual(len(recovery_requests), 1)
        self.assertTrue(
            recovery_requests[0]["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertEqual(
            recovery_requests[0]["litellm_metadata"]["external_web_search_completed_actions"],
            [{"type": "search", "query": "sample subject factor A factor B inhibition"}],
        )
        self.assertEqual(dumped.count("response.web_search_call.completed"), 1)
        self.assertIn("Final after duplicate recovery. https://example.test/one", dumped)
        self.assertNotIn('"type": "function_call"', dumped)

    async def test_streaming_external_web_search_continuation_recovery_executes_follow_up_call(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        executed_queries = []

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            query = action.get("query")
            executed_queries.append(query)
            url = "https://example.test/two" if "follow-up" in query else "https://example.test/one"
            return (
                f"Web search results for query: {query}\n"
                f"Title: Source for {query}\n"
                f"URL: {url}\n"
                "Snippet: Useful transporter evidence.",
                [url],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_followup_call",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_followup",
                            "call_id": "call_followup",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"sample subject factor A factor B follow-up"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        class TemporaryUpstreamError(Exception):
            status_code = 504

        original_calls = 0

        async def original_generic_function(**kwargs):
            nonlocal original_calls
            original_calls += 1
            if original_calls == 1:
                raise TemporaryUpstreamError("upstream 504")
            self.assertIn("tools", kwargs)
            self.assertIn("sample subject factor A factor B follow-up", kwargs["input"])
            return {
                "id": "resp_final_after_followup",
                "object": "response",
                "status": "completed",
                "output_text": "Final after follow-up. https://example.test/two",
                "output": [
                    {
                        "id": "msg_final_after_followup",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Final after follow-up. https://example.test/two",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"sample subject factor A factor B inhibition"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Use web_search for sample subject factor A factor B.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "litellm_metadata": {hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True},
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(
            executed_queries,
            [
                "sample subject factor A factor B inhibition",
                "sample subject factor A factor B follow-up",
            ],
        )
        self.assertEqual(original_calls, 2)
        self.assertEqual(len(recovery_requests), 1)
        self.assertTrue(
            recovery_requests[0]["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertEqual(dumped.count("response.web_search_call.completed"), 2)
        self.assertIn("Final after follow-up. https://example.test/two", dumped)
        self.assertNotIn('"type": "function_call"', dumped)

    async def test_streaming_external_web_search_empty_synthesis_stream_enters_route_recovery(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            yield {
                "type": "response.output_text.delta",
                "delta": "Recovered after empty synthesis. https://example.test/weather",
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_empty",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered after empty synthesis. https://example.test/weather",
                    "output": [
                        {
                            "id": "msg_recovered_empty",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered after empty synthesis. https://example.test/weather",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"Sample City weather"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"Sample City weather"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        original_calls = 0

        async def empty_synthesis_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_empty_synth",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_empty_synth",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            nonlocal original_calls
            original_calls += 1
            return empty_synthesis_stream()

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "chatroute-test",
                "order": 1,
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(original_calls, 2)
        self.assertEqual(len(recovery_requests), 1)
        self.assertEqual(
            [tool.get("name") for tool in recovery_requests[0].get("tools", [])],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        self.assertTrue(
            recovery_requests[0]["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertIn("Retrieved evidence observed so far:", recovery_requests[0]["input"])
        self.assertIn("Weather Source", recovery_requests[0]["input"])
        self.assertIn("https://example.test/weather", recovery_requests[0]["input"])
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("Recovered after empty synthesis. https://example.test/weather", dumped)
        self.assertIn('"type": "response.completed"', dumped)
        self.assertNotIn("final answer model was unavailable", dumped)
        self.assertNotIn("Top results:", dumped)

    async def test_streaming_external_web_search_route_recovery_poll_does_not_dump_results(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        original_keepalive_seconds = streaming_module._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
        streaming_module._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS = 0.0005

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            yield hooks._route_recovery_sse_keepalive(
                1,
                request_data=request_data,
                phase="test",
            )
            await asyncio.sleep(0.002)
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_poll",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered by route recovery.",
                    "output": [
                        {
                            "id": "msg_recovered_poll",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered by route recovery.",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS",
            original_keepalive_seconds,
        )
        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"Sample City weather"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"Sample City weather"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        async def empty_synthesis_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_empty_synth",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_empty_synth",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            return empty_synthesis_stream()

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(len(recovery_requests), 1)
        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertEqual(
            [tool.get("name") for tool in recovery_requests[0].get("tools", [])],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        self.assertTrue(
            recovery_requests[0]["litellm_metadata"]["external_web_search_continuation"]
        )
        self.assertIn("Retrieved evidence observed so far:", recovery_requests[0]["input"])
        self.assertIn("Weather Source", recovery_requests[0]["input"])
        self.assertIn("https://example.test/weather", recovery_requests[0]["input"])
        self.assertIn('"type": "response.completed"', dumped)
        self.assertIn("Recovered by route recovery.", dumped)
        self.assertNotIn('"type": "response.failed"', dumped)
        self.assertNotIn("Temporary upstream route failure", dumped)
        self.assertNotIn("Top results:", dumped)
        self.assertNotIn("Weather Source", dumped)

    async def test_streaming_external_web_search_empty_recovery_fails_without_second_poll(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        web_search_bridge_module = importlib.import_module("litellm_menu.responses_web_search_bridge")
        original_bridge_run_action = web_search_bridge_module._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_empty_recovery",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        hooks._external_web_search_run_action = fake_run_action
        web_search_bridge_module._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            web_search_bridge_module,
            "_external_web_search_run_action",
            original_bridge_run_action,
        )
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"Sample City weather"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "arguments": '{"query":"Sample City weather"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        async def empty_synthesis_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_empty_synth",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        async def original_generic_function(**kwargs):
            return empty_synthesis_stream()

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(recovery_requests), 1)
        assert_external_web_search_missing_answer_failed(self, chunks)
        dumped = json.dumps(chunks)
        self.assertNotIn("Temporary upstream route failure", dumped)
        self.assertNotIn("LiteLLM fallback retries", dumped)
        self.assertNotIn("Retry later", dumped)

    async def test_streaming_external_web_search_partial_answer_504_fails_without_retry(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        recovery_requests = []

        class GatewayTimeout(Exception):
            status_code = 504

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            raise AssertionError("visible partial streams must not enter route recovery")

        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp_original"}}
            yield {
                "type": "response.output_text.delta",
                "delta": "OpenAI homepage is ",
            }
            raise GatewayTimeout("upstream-status-504")

        request_data = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Original user request. Any instruction to call or use web_search has already been satisfied by the compatibility bridge:\nUse web_search now to search for OpenAI homepage URL.\n\nRetrieved evidence:\nWeb search results for query: OpenAI homepage URL\nTitle: OpenAI\nURL: https://openai.com/\nSnippet: OpenAI homepage.",
            "stream": True,
            "litellm_metadata": {
                "external_web_search_synthesis": True,
                "external_web_search_search_results": (
                    "Web search results for query: OpenAI homepage URL\n"
                    "Title: OpenAI\n"
                    "URL: https://openai.com/\n"
                    "Snippet: OpenAI homepage."
                ),
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        deltas = [
            chunk.get("delta")
            for chunk in chunks
            if isinstance(chunk, dict) and chunk.get("type") == "response.output_text.delta"
        ]
        self.assertEqual(deltas, ["OpenAI homepage is "])
        self.assertEqual(recovery_requests, [])
        self.assertEqual(chunks[-1]["type"], "response.failed")
        self.assertEqual(chunks[-1]["response"]["status"], "failed")
        self.assertNotIn("external_web_search_partial_resume", json.dumps(chunks))

    async def test_native_web_search_call_runtime_error_fails_without_external_bridge(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            raise AssertionError("native web_search runtime failures must not start external websearch")

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            raise AssertionError("native web_search runtime failures must not create external recovery payloads")
            yield

        hooks._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_native_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_native",
                    "status": "in_progress",
                    "action": {"type": "search", "query": "Sample City weather"},
                },
            }
            yield {
                "type": "response.web_search_call.completed",
                "item_id": "ws_native",
                "output_index": 0,
                "sequence_number": 1,
                "action": {"type": "search", "query": "Sample City weather"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_native",
                    "status": "completed",
                    "action": {"type": "search", "query": "Sample City weather"},
                },
            }
            error = TimeoutError("stream disconnected after web_search_call")
            error.status_code = 504
            raise error

        ssl_context = ssl.create_default_context()
        request_data = {
            "call_type": "aresponses",
            "model": "vendor-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "ssl_context": ssl_context,
            "litellm_metadata": {
                "proxy_server_request": {
                    "url": "http://127.0.0.1:4000/v1/responses",
                    "ssl_context": ssl_context,
                },
            },
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertIn('"type": "response.failed"', dumped)
        self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        self.assertNotIn("external_web_search_continuation", dumped)
        self.assertNotIn("Retrieved evidence observed so far", dumped)
        self.assertNotIn("Top results:", dumped)
        self.assertNotIn("Weather Source", dumped)
        self.assertNotIn("https://example.test/weather", dumped)

    async def test_native_web_search_unsupported_stream_error_uses_external_bridge(self) -> None:
        hooks, proxy_server = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        calls = []

        class ProviderBadRequest(Exception):
            status_code = 400

        unsupported_web_search_error = ProviderBadRequest(
            'OpenAIException - {"error":{"message":"Unsupported tool type: '
            'web_search","type":"invalid_request_error"}}'
        )

        async def external_bridge_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_external_bridge_planned",
                    "object": "response",
                    "status": "completed",
                    "output_text": "External fallback answered after unsupported native web_search.",
                    "output": [
                        {
                            "id": "msg_external_bridge_answer",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "External fallback answered after unsupported native web_search.",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return external_bridge_stream()

        proxy_server.llm_router = FakeRouter()

        async def upstream_stream():
            raise unsupported_web_search_error
            yield

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "id": "large-native-unknown",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            [tool.get("name") for tool in calls[0].get("tools", [])],
            [hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME],
        )
        self.assertTrue(
            calls[0]["litellm_metadata"].get("external_web_search_native_error_fallback")
        )
        self.assertTrue(calls[0]["litellm_metadata"].get(hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY))
        dumped = json.dumps(chunks)
        self.assertNotIn("Unsupported tool type", dumped)
        self.assertIn("External fallback answered after unsupported native web_search.", dumped)

    async def test_native_web_search_after_visible_text_stream_end_fails_without_recovery(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: 18Fr 28Fr drain tubing\n"
                "Title: Drain Source\n"
                "URL: https://example.test/drain\n"
                "Snippet: 18Fr is about 6mm and 28Fr is about 9.3mm.",
                ["https://example.test/drain"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            raise AssertionError("visible assistant text before EOF must not enter route recovery")
            yield

        hooks._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_native_preamble_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_text.delta",
                "delta": "收到，我继续查 6mm 和 9mm 两个规格。",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "msg_preamble",
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": "收到，我继续查 6mm 和 9mm 两个规格。",
                            "annotations": [],
                        }
                    ],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_native_preamble",
                    "status": "in_progress",
                    "action": {"type": "search", "query": "18Fr 28Fr drain tubing"},
                },
            }
            yield {
                "type": "response.web_search_call.completed",
                "item_id": "ws_native_preamble",
                "output_index": 1,
                "sequence_number": 1,
                "action": {"type": "search", "query": "18Fr 28Fr drain tubing"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_native_preamble",
                    "status": "completed",
                    "action": {"type": "search", "query": "18Fr 28Fr drain tubing"},
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Need both 6mm and 9mm tubing specs.",
            "stream": True,
            "tools": [{"type": "web_search"}],
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertTrue(
            any(
                chunk.get("delta") == "收到，我继续查 6mm 和 9mm 两个规格。"
                for chunk in chunks
                if isinstance(chunk, dict)
            )
        )
        self.assertIn('"type": "response.failed"', dumped)
        self.assertNotIn("Recovered final tubing answer.", dumped)
        self.assertNotIn("Drain Source", dumped)
        self.assertNotIn("https://example.test/drain", dumped)

    async def test_native_web_search_completed_with_followup_function_call_is_not_missing_answer(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            raise AssertionError("native web_search plus client tool calls must not start external websearch")

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            raise AssertionError("follow-up function_call must not enter search answer recovery")
            yield

        hooks._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        search_item = {
            "type": "web_search_call",
            "id": "ws_native_sources",
            "status": "completed",
            "action": {"type": "search", "query": "current package changelog"},
        }
        tool_item = {
            "type": "function_call",
            "id": "call_exec",
            "call_id": "call_exec",
            "name": "exec_command",
            "arguments": '{"cmd":"pwd"}',
            "status": "completed",
        }

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_native_tool_followup",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {**search_item, "status": "in_progress"},
            }
            yield {
                "type": "response.web_search_call.completed",
                "item_id": "ws_native_sources",
                "output_index": 0,
                "sequence_number": 1,
                "action": copy.deepcopy(search_item["action"]),
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": search_item,
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {**tool_item, "status": "in_progress"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": tool_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_native_tool_followup",
                    "object": "response",
                    "status": "completed",
                    "output": [search_item, tool_item],
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "Search for the current changelog, then inspect the local repo.",
            "stream": True,
            "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "exec_command"},
            ],
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertIn('"name": "exec_command"', dumped)
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertNotIn('"type": "response.failed"', dumped)
        self.assertNotIn("responses_web_search_missing_final_answer", dumped)
        self.assertNotIn("Top results:", dumped)

    async def test_provider_hosted_web_search_completed_with_inline_message_is_terminal(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: sample marker source\n"
                "Title: Sample Source\n"
                "URL: https://example.test/prmt5\n"
                "Snippet: Sample marker evidence was reported.",
                ["https://example.test/prmt5"],
                action,
            )

        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            raise AssertionError("completed assistant message must not enter route recovery")
            yield

        hooks._external_web_search_run_action = fake_run_action
        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll",
            original_route_recovery_poll,
        )

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_provider_hosted_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "openrouter:web_search",
                    "id": "ws_provider_hosted",
                    "status": "completed",
                    "query": "sample marker source",
                    "results": [
                        {
                            "url": "https://example.test/prmt5",
                            "title": "Sample Source",
                            "snippet": "Sample marker evidence was reported.",
                        }
                    ],
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_provider_hosted_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "openrouter:web_search",
                            "id": "ws_provider_hosted",
                            "status": "completed",
                            "query": "sample marker source",
                            "results": [
                                {
                                    "url": "https://example.test/prmt5",
                                    "title": "Sample Source",
                                    "snippet": "Sample marker evidence was reported.",
                                }
                            ],
                        },
                        {
                            "id": "msg_provider_hosted_progress",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Let me inspect the original source for the sample marker.",
                                    "annotations": [],
                                }
                            ],
                        },
                    ],
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "legacy-chat",
            "input": "Investigate the sample marker source.",
            "stream": True,
            "tools": [{"type": "web_search"}],
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertIn('"type": "response.completed"', dumped)
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertIn("sample marker source", dumped)
        self.assertIn("https://example.test/prmt5", dumped)
        self.assertIn(
            "Let me inspect the original source for the sample marker.",
            dumped,
        )
        self.assertNotIn("Recovered final sample answer.", dumped)
        self.assertNotIn('"type": "response.failed"', dumped)

    async def test_streaming_external_web_search_custom_tool_call_is_consumed_before_client(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            return (
                "Web search results for query: Sample City weather\n"
                "Title: Weather Source\n"
                "URL: https://example.test/weather\n"
                "Snippet: Sample City is sunny.",
                ["https://example.test/weather"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {"type": "response.output_text.delta", "delta": "Checking current weather..."}
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "custom_tool_call",
                    "id": "call_web",
                    "call_id": "call_web",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "input": '{"query":"Sample City weather"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_stream_raw",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "custom_tool_call",
                            "id": "call_web",
                            "call_id": "call_web",
                            "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                            "input": '{"query":"Sample City weather"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        async def original_generic_function(**kwargs):
            self.assertTrue(
                kwargs["litellm_metadata"][
                    hooks._WEB_SEARCH_EXTERNAL_SUPPRESS_POST_CALL_KEY
                ]
            )
            return {
                "id": "resp_synth",
                "object": "response",
                "status": "completed",
                "output_text": "Sample City is sunny. https://example.test/weather",
                "output": [
                    {
                        "id": "msg_synth",
                        "type": "message",
                        "status": "completed",
                        "role": "assistant",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "Sample City is sunny. https://example.test/weather",
                                "annotations": [],
                            }
                        ],
                    }
                ],
            }

        request_data = {
            "call_type": "aresponses",
            "model": "openai/vendor-chat",
            "input": "Sample City weather today?",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/chat", "openai/responses"],
            },
            "original_generic_function": original_generic_function,
        }

        chunks = [
            hooks._jsonable(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        self.assertNotIn('"type": "custom_tool_call", "id": "call_web"', dumped)
        self.assertIn('"type": "web_search_call"', dumped)
        self.assertIn("response.web_search_call.completed", dumped)
        self.assertIn("Sample City is sunny. https://example.test/weather", dumped)

    async def test_responses_api_with_optional_web_search_keeps_subagent_route(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        deployment = {
            "litellm_params": {
                "model": "openai/vendor-chat",
                "api_base": "https://api.provider-alpha.example/v1",
            },
            "model_info": {
                "id": "provider_alpha-generic-chat",
                "upstream_url_surface": "openai/chat",
            },
        }

        filtered = await hook.async_filter_deployments(
            "balanced-chat",
            [deployment],
            messages=None,
            request_kwargs={
                "input": "试开一个 subagent",
                "tools": [
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
            },
        )

        self.assertEqual(filtered, [deployment])

    async def test_responses_api_with_explicit_web_search_keeps_bridge_candidate(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        deployment = {
            "litellm_params": {
                "model": "openai/vendor-chat",
                "api_base": "https://api.provider-alpha.example/v1",
            },
            "model_info": {
                "id": "provider_alpha-generic-chat",
                "route_key": "provider_alpha / openai/vendor-chat / key=default",
                "upstream_url_surface": "openai/chat",
            },
        }

        filtered = await hook.async_filter_deployments(
            "balanced-chat",
            [deployment],
            messages=None,
            request_kwargs={
                "input": "web search 试试",
                "tools": [
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
            },
        )

        self.assertEqual(filtered, [deployment])

    async def test_streaming_web_search_stall_timeout_does_not_dump_direct_fallback(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original_run_action = hooks._external_web_search_run_action
        run_action_called = False

        async def fake_run_action(action, page_cache, page_fetch_tasks):
            nonlocal run_action_called
            run_action_called = True
            return (
                "Web search results for query: sample subject Signal\n"
                "Title: Signal Source\n"
                "URL: https://example.test/erk\n"
                "Snippet: Sample subject and Signal pathway evidence.",
                ["https://example.test/erk"],
                action,
            )

        hooks._external_web_search_run_action = fake_run_action
        self.addCleanup(setattr, hooks, "_external_web_search_run_action", original_run_action)

        async def upstream_stream():
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "too late"}

        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")
        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Use web_search for sample subject Signal. Answer briefly with source URLs.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "litellm_metadata": {
                hooks._WEB_SEARCH_EXTERNAL_BRIDGE_KEY: True,
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=upstream_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertFalse(run_action_called)
        self.assertNotIn('"type": "web_search_call"', dumped)
        self.assertNotIn("final answer model was unavailable", dumped)
        self.assertNotIn("Top results:", dumped)
        self.assertNotIn("Signal Source", dumped)
        self.assertNotIn("https://example.test/erk", dumped)
        self.assertIn('"type": "response.failed"', dumped)
        self.assertIn(
            "The upstream model route failed before a final assistant response was available.",
            dumped,
        )
        self.assertNotIn("Temporary upstream route failure", dumped)
        self.assertNotIn("LiteLLM fallback retries", dumped)
        self.assertNotIn("Retry later", dumped)
        assert_upstream_route_failed_terminal(self, chunks)


if __name__ == "__main__":
    unittest.main()
