from __future__ import annotations

from hook_test_utils import *


class HookStreamingToolEventTests(HookTestCase):
    def test_responses_completion_stream_patch_adds_completed_response(self) -> None:
        hooks, _ = load_hook_module()
        responses_module = types.ModuleType("litellm.responses")
        bridge_module = types.ModuleType("litellm.responses.litellm_completion_transformation")
        streaming_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        )

        class LiteLLMCompletionStreamingIterator:
            def __init__(self, value):
                self.value = value

        streaming_module.LiteLLMCompletionStreamingIterator = LiteLLMCompletionStreamingIterator
        sys.modules["litellm.responses"] = responses_module
        sys.modules["litellm.responses.litellm_completion_transformation"] = bridge_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        ] = streaming_module

        hooks._install_responses_completion_stream_patch()
        iterator = LiteLLMCompletionStreamingIterator("stream")

        self.assertEqual(iterator.value, "stream")
        self.assertIsNone(iterator.completed_response)
        self.assertTrue(
            getattr(
                LiteLLMCompletionStreamingIterator.__init__,
                hooks._RESPONSES_COMPLETION_STREAM_PATCH_ATTR,
            )
        )

    def test_responses_tool_search_bridge_patch_rewrites_non_streaming_tool_calls(self) -> None:
        hooks, _ = load_hook_module()
        responses_module = types.ModuleType("litellm.responses")
        bridge_module = types.ModuleType("litellm.responses.litellm_completion_transformation")
        transform_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.transformation"
        )
        streaming_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        )

        class LiteLLMCompletionResponsesConfig:
            @staticmethod
            def transform_chat_completion_response_to_responses_api_response(*args, **kwargs):
                return types.SimpleNamespace(
                    output=[
                        {
                            "type": "function_call",
                            "id": "call_search",
                            "call_id": "call_search",
                            "name": "tool_search",
                            "arguments": '{"query":"multi_agent_v1 spawn_agent"}',
                            "status": "completed",
                        },
                        {
                            "type": "function_call",
                            "id": "call_spawn",
                            "call_id": "call_spawn",
                            "name": "spawn_agent",
                            "arguments": "{}",
                            "status": "completed",
                        },
                    ]
                )

        class LiteLLMCompletionStreamingIterator:
            def _queue_tool_call_delta_events(self):
                pass

            def _queue_final_tool_call_done_events(self):
                pass

        transform_module.LiteLLMCompletionResponsesConfig = LiteLLMCompletionResponsesConfig
        streaming_module.LiteLLMCompletionStreamingIterator = LiteLLMCompletionStreamingIterator
        sys.modules["litellm.responses"] = responses_module
        sys.modules["litellm.responses.litellm_completion_transformation"] = bridge_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.transformation"
        ] = transform_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        ] = streaming_module

        hooks._install_responses_tool_search_bridge_patch()

        response = LiteLLMCompletionResponsesConfig.transform_chat_completion_response_to_responses_api_response(
            [],
            {
                "tools": [
                    {
                        "type": "function",
                        "name": "spawn_agent",
                        "parameters": {"type": "object"},
                        hooks._RESPONSES_BRIDGE_NAMESPACE_KEY: "multi_agent_v1",
                    }
                ]
            },
            {},
        )
        dumped = [hooks._jsonable(item) for item in response.output]

        self.assertEqual(dumped[0]["type"], "tool_search_call")
        self.assertEqual(dumped[0]["execution"], "client")
        self.assertEqual(
            dumped[0]["arguments"],
            {"query": "multi_agent_v1 spawn_agent"},
        )
        self.assertEqual(dumped[1]["type"], "function_call")
        self.assertEqual(dumped[1]["name"], "spawn_agent")
        self.assertEqual(dumped[1]["namespace"], "multi_agent_v1")

    async def test_native_responses_stream_rewrites_tool_search_function_call(self) -> None:
        hooks, _ = load_hook_module()

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_native",
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
                    "id": "call_search",
                    "call_id": "call_search",
                    "name": "tool_search",
                    "arguments": '{"query":"node repl","limit":3}',
                    "status": "in_progress",
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_search",
                    "call_id": "call_search",
                    "name": "tool_search",
                    "arguments": '{"query":"node repl","limit":3}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_native",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "call_search",
                            "call_id": "call_search",
                            "name": "tool_search",
                            "arguments": '{"query":"node repl","limit":3}',
                            "status": "completed",
                        }
                    ],
                },
            }

        request_data = {
            "model": "default-chat",
            "input": "use tool search",
            "stream": True,
            "tools": [{"type": "tool_search"}],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        added_item = chunks[1]["item"]
        done_item = chunks[2]["item"]
        completed_item = chunks[-1]["response"]["output"][0]
        self.assertEqual(added_item["type"], "tool_search_call")
        self.assertEqual(done_item["type"], "tool_search_call")
        self.assertEqual(completed_item["type"], "tool_search_call")
        self.assertEqual(
            done_item["arguments"],
            {"query": "node repl", "limit": 3},
        )
        self.assertEqual(completed_item["execution"], "client")

    async def test_native_responses_stream_maps_openrouter_hosted_search_item(self) -> None:
        hooks, _ = load_hook_module()

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_native",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "or_search_1",
                    "type": "openrouter:web_search",
                    "status": "in_progress",
                    "query": "latest Python release",
                    "results": [
                        {
                            "type": "search",
                            "url": "https://www.python.org/downloads/",
                        }
                    ],
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
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
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_native",
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
                        }
                    ],
                },
            }

        request_data = {
            "model": "legacy-chat",
            "input": "latest Python release",
            "stream": True,
            "tools": [{"type": "web_search"}],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        self.assertEqual(chunks[1]["item"]["type"], "web_search_call")
        self.assertEqual(chunks[2]["item"]["type"], "web_search_call")
        self.assertEqual(chunks[2]["item"]["query"], "latest Python release")
        self.assertEqual(
            chunks[2]["item"]["action"]["sources"][0]["url"],
            "https://www.python.org/downloads/",
        )
        self.assertEqual(chunks[-1]["type"], "response.failed")

    async def test_native_responses_stream_strips_raw_openrouter_tool_call_text(self) -> None:
        hooks, _ = load_hook_module()

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_raw_tool_text",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {"type": "response.output_text.delta", "delta": "让我再搜索。\n<tool"}
            yield {
                "type": "response.output_text.delta",
                "delta": (
                    "_call>openrouter_web_search<arg_key>query</arg_key>"
                    "<arg_value>sample marker listing</arg_value></tool_call>"
                ),
            }
            yield {"type": "response.output_text.delta", "delta": "\n结论可见。"}
            yield {
                "type": "response.output_text.done",
                "text": (
                    "让我再搜索。\n<tool_call>openrouter_web_search"
                    "<arg_key>query</arg_key><arg_value>sample marker listing</arg_value>"
                    "</tool_call>\n结论可见。"
                ),
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_raw_tool_text",
                    "object": "response",
                    "status": "completed",
                    "output_text": (
                        "让我再搜索。\n<tool_call>openrouter_web_search"
                        "<arg_key>query</arg_key><arg_value>sample marker listing</arg_value>"
                        "</tool_call>\n结论可见。"
                    ),
                    "output": [
                        {
                            "id": "msg_raw_tool_text",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": (
                                        "让我再搜索。\n<tool_call>openrouter_web_search"
                                        "<arg_key>query</arg_key>"
                                        "<arg_value>sample marker listing</arg_value>"
                                        "</tool_call>\n结论可见。"
                                    ),
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        request_data = {
            "model": "legacy-chat",
            "input": "需要联网搜索后回答。",
            "stream": True,
            "tools": [{"type": "web_search"}],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        visible_text = "\n".join(
            str(chunk.get("delta") or chunk.get("text") or "")
            for chunk in chunks
            if isinstance(chunk, dict)
            and chunk.get("type") in {"response.output_text.delta", "response.output_text.done"}
        )
        serialized = json.dumps(chunks, ensure_ascii=False)
        self.assertIn("让我再搜索", visible_text)
        self.assertIn("结论可见", visible_text)
        self.assertNotIn("<tool_call>", serialized)
        self.assertNotIn("openrouter_web_search", serialized)
        completed = [chunk for chunk in chunks if chunk.get("type") == "response.completed"][-1]
        self.assertEqual(
            completed["response"]["output"][0]["content"][0]["text"],
            "让我再搜索。\n\n结论可见。",
        )

    async def test_guarded_responses_stream_bridge_uses_bounded_completion_state(self) -> None:
        hooks, _ = load_hook_module()
        streaming_module = hooks._owners["_yield_guarded_original_stream"]
        original_completed_payload = (
            streaming_module._ResponsesStreamCompletionState.completed_payload
        )
        computer_facade_module = importlib.import_module("litellm_menu.computer_facade")
        original_resolve = (
            computer_facade_module._resolve_litellm_web_search_function_calls_stream_rounds
        )
        captured: dict[str, object] = {}

        def fake_completed_payload(self, request_data):
            captured["event_count"] = self.event_count
            captured["synthetic_text"] = self.synthetic_text
            return original_completed_payload(self, request_data)

        async def fake_resolve(payload, request_data, original_function):
            captured["payload"] = payload
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_resolved",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        streaming_module._ResponsesStreamCompletionState.completed_payload = (
            fake_completed_payload
        )
        computer_facade_module._resolve_litellm_web_search_function_calls_stream_rounds = fake_resolve
        self.addCleanup(
            setattr,
            streaming_module,
            "_ResponsesStreamCompletionState",
            streaming_module._ResponsesStreamCompletionState,
        )
        self.addCleanup(
            setattr,
            computer_facade_module,
            "_resolve_litellm_web_search_function_calls_stream_rounds",
            original_resolve,
        )
        self.addCleanup(
            setattr,
            streaming_module._ResponsesStreamCompletionState,
            "completed_payload",
            original_completed_payload,
        )

        long_delta = "x" * 128

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_bounded",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            for _ in range(100):
                yield {"type": "response.output_text.delta", "delta": long_delta}
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "function_call",
                    "id": "call_search",
                    "call_id": "call_search",
                    "name": hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME,
                    "arguments": '{"query":"node memory leak"}',
                    "status": "in_progress",
                },
            }

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "search after a long stream",
            "stream": True,
            "tools": [{"type": "web_search"}],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        payload = captured["payload"]
        synthetic_text = captured["synthetic_text"]
        self.assertEqual(captured["event_count"], 102)
        self.assertEqual(len(synthetic_text), hooks._STREAM_SYNTHETIC_TEXT_MAX_CHARS)
        self.assertEqual(synthetic_text, ("x" * (128 * 100))[-hooks._STREAM_SYNTHETIC_TEXT_MAX_CHARS:])
        self.assertIn("node memory leak", json.dumps(payload, sort_keys=True))
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_guarded_responses_stream_error_after_search_tool_only_output_recovers(self) -> None:
        hooks, proxy_server = load_hook_module()
        recovery_calls = []

        async def recovery_stream():
            yield {
                "type": "response.output_text.delta",
                "delta": "Recovered answer after search. https://example.test/sample-subject",
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_search_only",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered answer after search. https://example.test/sample-subject",
                    "output": [
                        {
                            "id": "msg_recovered_search_only",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered answer after search. https://example.test/sample-subject",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-search"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "order2-search"},
                    },
                ]

            async def aresponses(self, **payload):
                recovery_calls.append(payload)
                return recovery_stream()

        proxy_server.llm_router = FakeRouter()

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp_search_only",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "ws_1",
                    "type": "web_search_call",
                    "status": "in_progress",
                    "action": {"type": "search", "query": "sample subject factor A"},
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "ws_1",
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "query": "sample subject factor A"},
                },
            }
            raise TimeoutError("idle after web_search_call only output")

        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Search sample subject factor A",
            "stream": True,
            "model_info": {"id": "order1-search", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_calls, [])
        self.assertEqual(chunks[-1]["type"], "response.failed")
        self.assertNotIn("Recovered answer after search. https://example.test/sample-subject", dumped)
        self.assertIn('"type": "response.failed"', dumped)
        self.assertFalse(
            any(
                chunk.get("type") == "response.completed"
                and chunk.get("response", {}).get("output")
                and all(
                    item.get("type") == "web_search_call"
                    for item in chunk["response"]["output"]
                    if isinstance(item, dict)
                )
                for chunk in chunks
            )
        )

    async def test_guarded_responses_stream_search_only_runtime_error_does_not_external_bridge(self) -> None:
        hooks, _ = load_hook_module()
        streaming_module = hooks._owners["_yield_guarded_original_stream"]
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        recovery_requests = []

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(request_data.copy())
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_search_only_recovered",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered search-only answer.",
                    "output": [
                        {
                            "id": "msg_search_only_recovered",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered search-only answer.",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        streaming_module._stream_route_recovery_poll = fake_route_recovery_poll
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
                    "id": "resp_search_only_timeout",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "ws_timeout",
                    "type": "web_search_call",
                    "status": "completed",
                    "action": {"type": "search", "query": "sample subject factor A"},
                },
            }
            raise TimeoutError("idle after web_search_call only output")

        request_data = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "Search sample subject factor A",
            "stream": True,
            "tools": [
                {"type": "web_search"},
                {"type": "function", "name": "exec_command"},
            ],
            "model_info": {"id": "order1-search", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        dumped = json.dumps(chunks)
        self.assertEqual(recovery_requests, [])
        self.assertEqual(chunks[-1]["type"], "response.failed")
        self.assertNotIn("Recovered search-only answer.", dumped)
        self.assertNotIn(hooks._WEB_SEARCH_BRIDGE_FUNCTION_NAME, dumped)
        self.assertNotIn("external_web_search_continuation", dumped)
        self.assertIn('"type": "response.failed"', dumped)
        self.assertNotIn("Temporary upstream route failure", dumped)
        self.assertNotIn("Top results:", dumped)

    def test_responses_tool_search_bridge_patch_rewrites_streaming_tool_events(self) -> None:
        hooks, _ = load_hook_module()
        responses_module = types.ModuleType("litellm.responses")
        bridge_module = types.ModuleType("litellm.responses.litellm_completion_transformation")
        transform_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.transformation"
        )
        streaming_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        )

        class LiteLLMCompletionResponsesConfig:
            @staticmethod
            def transform_chat_completion_response_to_responses_api_response(*args, **kwargs):
                return types.SimpleNamespace(output=[])

        class LiteLLMCompletionStreamingIterator:
            def __init__(self):
                self._pending_tool_events = []

            def _queue_tool_call_delta_events(self, tool_calls):
                self._pending_tool_events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "item": {
                                "type": "function_call",
                                "id": "call_search",
                                "call_id": "call_search",
                                "name": "tool_search",
                                "arguments": "",
                                "status": "in_progress",
                            },
                        },
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "id": "call_search",
                                "call_id": "call_search",
                                "name": "tool_search",
                                "arguments": '{"query":"spawn_agent"}',
                                "status": "completed",
                            },
                        },
                    ]
                )

            def _queue_final_tool_call_done_events(self, response):
                self._pending_tool_events.extend(
                    [
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "id": "call_regular",
                                "call_id": "call_regular",
                                "name": "get_app_state",
                                "arguments": "{}",
                                "status": "completed",
                            },
                        },
                        {
                            "type": "response.output_item.done",
                            "item": {
                                "type": "function_call",
                                "id": "call_patch",
                                "call_id": "call_patch",
                                "name": "apply_patch",
                                "arguments": '{"input":"*** Begin Patch"}',
                                "status": "completed",
                            },
                        },
                    ]
                )

        transform_module.LiteLLMCompletionResponsesConfig = LiteLLMCompletionResponsesConfig
        streaming_module.LiteLLMCompletionStreamingIterator = LiteLLMCompletionStreamingIterator
        sys.modules["litellm.responses"] = responses_module
        sys.modules["litellm.responses.litellm_completion_transformation"] = bridge_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.transformation"
        ] = transform_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        ] = streaming_module

        hooks._install_responses_tool_search_bridge_patch()

        iterator = LiteLLMCompletionStreamingIterator()
        iterator.request_input = []
        iterator.responses_api_request = {
            "tools": [
                {
                    "type": "function",
                    "name": "get_app_state",
                    "parameters": {"type": "object"},
                    hooks._RESPONSES_BRIDGE_NAMESPACE_KEY: "mcp__computer_use",
                },
                {
                    "type": "function",
                    "name": "apply_patch",
                    "parameters": {"type": "object"},
                    hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True,
                },
            ]
        }
        iterator._queue_tool_call_delta_events([])
        added_item = hooks._jsonable(iterator._pending_tool_events[0]["item"])
        done_item = hooks._jsonable(iterator._pending_tool_events[1]["item"])

        self.assertEqual(added_item["type"], "tool_search_call")
        self.assertEqual(added_item["execution"], "client")
        self.assertEqual(added_item["arguments"], {})
        self.assertEqual(done_item["type"], "tool_search_call")
        self.assertEqual(done_item["arguments"], {"query": "spawn_agent"})

        iterator._queue_final_tool_call_done_events(None)
        regular_item = hooks._jsonable(iterator._pending_tool_events[2]["item"])
        self.assertEqual(regular_item["type"], "function_call")
        self.assertEqual(regular_item["name"], "get_app_state")
        self.assertEqual(regular_item["namespace"], "mcp__computer_use")
        custom_item = hooks._jsonable(iterator._pending_tool_events[3]["item"])
        self.assertEqual(custom_item["type"], "custom_tool_call")
        self.assertEqual(custom_item["name"], "apply_patch")
        self.assertEqual(custom_item["input"], "*** Begin Patch")

    def test_responses_tool_bridge_restores_streaming_custom_tool_input_events(self) -> None:
        hooks, _ = load_hook_module()
        responses_module = types.ModuleType("litellm.responses")
        bridge_module = types.ModuleType("litellm.responses.litellm_completion_transformation")
        transform_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.transformation"
        )
        streaming_module = types.ModuleType(
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        )

        class LiteLLMCompletionResponsesConfig:
            @staticmethod
            def transform_chat_completion_response_to_responses_api_response(*args, **kwargs):
                return types.SimpleNamespace(output=[])

        class LiteLLMCompletionStreamingIterator:
            def __init__(self):
                self._pending_tool_events = []

            def _queue_tool_call_delta_events(self, tool_calls):
                self._pending_tool_events.extend(
                    [
                        {
                            "type": "response.output_item.added",
                            "output_index": 1,
                            "item": {
                                "type": "function_call",
                                "id": "fc_patch",
                                "call_id": "call_patch",
                                "name": "apply_patch",
                                "arguments": "",
                                "status": "in_progress",
                            },
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc_patch",
                            "output_index": 1,
                            "delta": '{"input":"*** Begin',
                        },
                        {
                            "type": "response.function_call_arguments.delta",
                            "item_id": "fc_patch",
                            "output_index": 1,
                            "delta": ' Patch\\n*** End Patch"}',
                        },
                        {
                            "type": "response.function_call_arguments.done",
                            "item_id": "fc_patch",
                            "output_index": 1,
                            "arguments": '{"input":"*** Begin Patch\\n*** End Patch"}',
                        },
                    ]
                )

            def _queue_final_tool_call_done_events(self, response):
                pass

        transform_module.LiteLLMCompletionResponsesConfig = LiteLLMCompletionResponsesConfig
        streaming_module.LiteLLMCompletionStreamingIterator = LiteLLMCompletionStreamingIterator
        sys.modules["litellm.responses"] = responses_module
        sys.modules["litellm.responses.litellm_completion_transformation"] = bridge_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.transformation"
        ] = transform_module
        sys.modules[
            "litellm.responses.litellm_completion_transformation.streaming_iterator"
        ] = streaming_module

        hooks._install_responses_tool_search_bridge_patch()

        iterator = LiteLLMCompletionStreamingIterator()
        iterator.request_input = []
        iterator.responses_api_request = {
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "parameters": {"type": "object"},
                    hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True,
                }
            ]
        }
        iterator._queue_tool_call_delta_events([])
        events = [hooks._jsonable(event) for event in iterator._pending_tool_events]

        self.assertEqual(events[0]["type"], "response.output_item.added")
        self.assertEqual(events[0]["item"]["type"], "custom_tool_call")
        self.assertEqual(events[0]["item"]["id"], "ctc_patch")
        self.assertEqual(events[1]["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(events[1]["item_id"], "ctc_patch")
        self.assertEqual(events[1]["delta"], "*** Begin")
        self.assertEqual(events[2]["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(events[2]["delta"], " Patch\n*** End Patch")
        self.assertEqual(events[3]["type"], "response.custom_tool_call_input.done")
        self.assertEqual(events[3]["input"], "*** Begin Patch\n*** End Patch")
        self.assertNotIn("arguments", events[3])

    def test_responses_tool_bridge_preserves_streaming_bare_exec_patch_protocol(self) -> None:
        hooks, _ = load_hook_module()
        tracker = hooks._CustomToolInputDeltaTracker()
        patch = "*** Begin Patch\n*** Update File: example.txt\n*** End Patch"
        added = {
            "type": "response.output_item.added",
            "item": {
                "type": "function_call",
                "id": "fc_exec",
                "call_id": "call_exec",
                "name": "exec",
                "arguments": "",
                "status": "in_progress",
            },
        }
        done = {
            "type": "response.function_call_arguments.done",
            "item_id": "fc_exec",
            "arguments": json.dumps({"input": patch}),
        }
        delta = {
            "type": "response.function_call_arguments.delta",
            "item_id": "fc_exec",
            "delta": json.dumps({"input": patch})[:-1],
        }
        custom_tool_item_ids: set[str] = set()

        normalized_added = hooks._normalize_response_stream_tool_bridge_chunk(
            added,
            {},
            {"exec"},
            custom_tool_item_ids,
            tracker,
        )
        normalized_delta = hooks._normalize_response_stream_tool_bridge_chunk(
            delta,
            {},
            {"exec"},
            custom_tool_item_ids,
            tracker,
        )
        normalized_done = hooks._normalize_response_stream_tool_bridge_chunk(
            done,
            {},
            {"exec"},
            custom_tool_item_ids,
            tracker,
        )

        self.assertEqual(normalized_added["item"]["type"], "custom_tool_call")
        self.assertEqual(normalized_delta["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(normalized_delta["delta"], patch)
        self.assertEqual(normalized_done["type"], "response.custom_tool_call_input.done")
        self.assertEqual(normalized_done["input"], patch)

    def test_responses_tool_bridge_preserves_streaming_exec_javascript(self) -> None:
        hooks, _ = load_hook_module()
        tracker = hooks._CustomToolInputDeltaTracker()
        javascript = 'text(await tools.exec_command({"cmd":"pwd"}));'
        custom_tool_item_ids: set[str] = set()

        hooks._normalize_response_stream_tool_bridge_chunk(
            {
                "type": "response.output_item.added",
                "item": {
                    "type": "function_call",
                    "id": "fc_exec_js",
                    "call_id": "call_exec_js",
                    "name": "exec",
                    "arguments": "",
                    "status": "in_progress",
                },
            },
            {},
            {"exec"},
            custom_tool_item_ids,
            tracker,
        )
        normalized_delta = hooks._normalize_response_stream_tool_bridge_chunk(
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_exec_js",
                "delta": json.dumps({"input": javascript})[:-1],
            },
            {},
            {"exec"},
            custom_tool_item_ids,
            tracker,
        )
        normalized_done = hooks._normalize_response_stream_tool_bridge_chunk(
            {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_exec_js",
                "arguments": json.dumps({"input": javascript}),
            },
            {},
            {"exec"},
            custom_tool_item_ids,
            tracker,
        )

        self.assertEqual(normalized_delta["delta"], javascript)
        self.assertEqual(normalized_done["input"], javascript)

    async def test_guarded_responses_stream_restores_custom_tool_input_events(self) -> None:
        hooks, _ = load_hook_module()

        async def upstream_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp_patch", "status": "in_progress", "output": []},
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_patch",
                    "call_id": "call_patch",
                    "name": "apply_patch",
                    "arguments": "",
                    "status": "in_progress",
                },
            }
            yield {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_patch",
                "output_index": 1,
                "delta": '{"input":"*** Begin',
            }
            yield {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_patch",
                "output_index": 1,
                "delta": ' Patch"}',
            }
            yield {
                "type": "response.function_call_arguments.done",
                "item_id": "fc_patch",
                "output_index": 1,
                "arguments": '{"input":"*** Begin Patch"}',
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 1,
                "item": {
                    "type": "function_call",
                    "id": "fc_patch",
                    "call_id": "call_patch",
                    "name": "apply_patch",
                    "arguments": '{"input":"*** Begin Patch"}',
                    "status": "completed",
                },
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_patch",
                    "status": "completed",
                    "output": [
                        {
                            "type": "function_call",
                            "id": "fc_patch",
                            "call_id": "call_patch",
                            "name": "apply_patch",
                            "arguments": '{"input":"*** Begin Patch"}',
                            "status": "completed",
                        }
                    ],
                },
            }

        request_data = {
            "model": "default-chat",
            "input": "edit a file",
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "name": "apply_patch",
                    "parameters": {"type": "object"},
                    hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True,
                }
            ],
        }

        events = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_guarded_original_stream(
                [],
                upstream_stream(),
                request_data,
            )
        ]

        self.assertEqual(events[1]["type"], "response.output_item.added")
        self.assertEqual(events[1]["item"]["type"], "custom_tool_call")
        self.assertEqual(events[1]["item"]["id"], "ctc_patch")
        self.assertEqual(events[2]["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(events[2]["item_id"], "ctc_patch")
        self.assertEqual(events[2]["delta"], "*** Begin")
        self.assertEqual(events[3]["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(events[3]["delta"], " Patch")
        self.assertEqual(events[4]["type"], "response.custom_tool_call_input.done")
        self.assertEqual(events[4]["input"], "*** Begin Patch")
        self.assertEqual(events[5]["type"], "response.output_item.done")
        self.assertEqual(events[5]["item"]["type"], "custom_tool_call")
        self.assertEqual(events[5]["item"]["id"], "ctc_patch")
        self.assertEqual(events[-1]["response"]["output"][0]["type"], "custom_tool_call")
        self.assertEqual(events[-1]["response"]["output"][0]["id"], "ctc_patch")

    def test_completed_stream_output_does_not_duplicate_custom_tool_call_events(self) -> None:
        hooks, _ = load_hook_module()
        seen_item_ids: set[str] = set()
        pending_tool_items: dict[str, tuple[int, dict]] = {}

        added = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "call_apply_patch",
                "call_id": "call_apply_patch",
                "type": "custom_tool_call",
                "name": "apply_patch",
                "input": "*** Begin Patch",
                "status": "in_progress",
            },
        }
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "id": "call_apply_patch",
                        "call_id": "call_apply_patch",
                        "type": "custom_tool_call",
                        "name": "apply_patch",
                        "input": "*** Begin Patch",
                        "status": "completed",
                    }
                ],
            },
        }

        hooks._remember_stream_output_item_ids(added, seen_item_ids, pending_tool_items)
        synthetic_events = hooks._synthesized_missing_completed_tool_events(
            completed,
            seen_item_ids,
            pending_tool_items,
        )

        self.assertEqual(len(synthetic_events), 1)
        event = jsonable_stream_chunk(synthetic_events[0])
        self.assertEqual(event["type"], "response.output_item.done")
        self.assertEqual(event["item"]["type"], "custom_tool_call")
        self.assertEqual(event["item"]["name"], "apply_patch")
        self.assertEqual(pending_tool_items, {})
        self.assertIn("call_apply_patch", seen_item_ids)

    def test_completed_stream_output_matches_done_tool_call_by_call_id_alias(self) -> None:
        hooks, _ = load_hook_module()
        seen_item_ids: set[str] = set()
        pending_tool_items: dict[str, tuple[int, dict]] = {}

        done = {
            "type": "response.output_item.done",
            "output_index": 0,
            "item": {
                "id": "fc_exec",
                "call_id": "call_exec",
                "type": "function_call",
                "name": "exec_command",
                "arguments": "{\"cmd\":\"git push origin main\"}",
                "status": "completed",
            },
        }
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "call_id": "call_exec",
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"git push origin main\"}",
                        "status": "completed",
                    }
                ],
            },
        }

        hooks._remember_stream_output_item_ids(done, seen_item_ids, pending_tool_items)
        synthetic_events = hooks._synthesized_missing_completed_tool_events(
            completed,
            seen_item_ids,
            pending_tool_items,
        )

        self.assertEqual(synthetic_events, [])
        self.assertIn("fc_exec", seen_item_ids)
        self.assertIn("call_exec", seen_item_ids)

    def test_completed_stream_output_done_inherits_pending_tool_item_id(self) -> None:
        hooks, _ = load_hook_module()
        seen_item_ids: set[str] = set()
        pending_tool_items: dict[str, tuple[int, dict]] = {}

        added = {
            "type": "response.output_item.added",
            "output_index": 0,
            "item": {
                "id": "fc_exec",
                "call_id": "call_exec",
                "type": "function_call",
                "name": "exec_command",
                "arguments": "",
                "status": "in_progress",
            },
        }
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp_1",
                "status": "completed",
                "output": [
                    {
                        "call_id": "call_exec",
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": "{\"cmd\":\"git push origin main\"}",
                        "status": "completed",
                    }
                ],
            },
        }

        hooks._remember_stream_output_item_ids(added, seen_item_ids, pending_tool_items)
        synthetic_events = hooks._synthesized_missing_completed_tool_events(
            completed,
            seen_item_ids,
            pending_tool_items,
        )

        self.assertEqual(len(synthetic_events), 1)
        event = jsonable_stream_chunk(synthetic_events[0])
        self.assertEqual(event["type"], "response.output_item.done")
        self.assertEqual(event["item"]["id"], "fc_exec")
        self.assertEqual(event["item"]["call_id"], "call_exec")
        self.assertEqual(pending_tool_items, {})
        self.assertIn("fc_exec", seen_item_ids)
        self.assertIn("call_exec", seen_item_ids)

    async def test_nonstreaming_refusal_retries_same_model_with_forced_tool_choice(self) -> None:
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
            "input": [{"role": "user", "content": "画一张图：一只猫在桌上写字。"}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
        }
        original = {"output_text": "IMAGEGEN_RESULT status=FAIL blocker=IMAGEGEN_TOOL_UNAVAILABLE"}

        response = await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertEqual(calls[0]["tool_choice"], {"type": "image_generation"})
        self.assertFalse(calls[0]["stream"])
        self.assertNotIn("api_base", calls[0])
        self.assertNotIn("provider", calls[0])

    async def test_nonstreaming_refusal_excludes_current_deployment_and_preserves_edit_image(self) -> None:
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
            "model_info": {"id": "route-a", "route_key": "route-a-key", "order": 1},
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
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
        }
        original = {"output_text": "IMAGEGEN_RESULT status=FAIL blocker=IMAGEGEN_TOOL_UNAVAILABLE"}

        response = await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(response["output"][0]["type"], "image_generation_call")
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"], request_data["input"])
        self.assertEqual(calls[0]["tool_choice"], {"type": "image_generation"})
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["route-a"])
        self.assertNotIn("_target_order", calls[0])
        self.assertEqual(
            calls[0]["litellm_metadata"][hooks._IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY],
            1,
        )

    async def test_nonstreaming_refusal_respects_image_tool_fallback_max_attempts(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return {"output": [{"type": "image_generation_call", "result": "base64-image"}]}

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "画一张图"}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": False,
            "litellm_metadata": {
                hooks._IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY: 3,
            },
        }
        original = {"output_text": "IMAGEGEN_RESULT status=FAIL blocker=IMAGEGEN_TOOL_UNAVAILABLE"}

        with self.assertRaises(Exception):
            await hook.async_post_call_success_deployment_hook(request_data, original, call_type=None)

        self.assertEqual(calls, [])

    async def test_streaming_refusal_retries_same_model_with_forced_tool_choice(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "IMAGEGEN_RESULT status=FAIL "}
            yield {"type": "response.output_text.delta", "delta": "blocker=IMAGEGEN_TOOL_UNAVAILABLE"}

        async def fallback_stream():
            yield {"type": "image_generation_call", "result": "base64-image"}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "画一张图：一只猫在桌上写字。"}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": True,
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks, [{"type": "image_generation_call", "result": "base64-image"}])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertEqual(calls[0]["tool_choice"], {"type": "image_generation"})
        self.assertTrue(calls[0]["stream"])

    async def test_streaming_temporary_error_before_content_replays_via_router(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class ProviderConcurrencyError(Exception):
            pass

        error = ProviderConcurrencyError(
            "stream disconnected before completion: Concurrency limit exceeded for account, please retry later"
        )

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            raise error

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "fallback ok"}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                self.seen_model_name = model_name
                self.seen_team_id = team_id
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    },
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-b"},
                    },
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        router = FakeRouter()
        proxy_server.llm_router = router
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say pong only."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
            "metadata": {"user_api_key_team_id": "team-a"},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], {"type": "response.output_text.delta", "delta": "fallback ok"})
        self.assertEqual(jsonable_stream_chunk(chunks[-1])["type"], "response.completed")
        self.assertEqual(
            jsonable_stream_chunk(chunks[-1])["response"]["output"][0]["content"][0]["text"],
            "fallback ok",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertEqual(calls[0]["input"], request_data["input"])
        self.assertTrue(calls[0]["stream"])
        self.assertEqual(calls[0]["_target_order"], 1)
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn("metadata", calls[0])
        self.assertFalse(hasattr(router, "seen_model_name"))
        self.assertFalse(hasattr(router, "seen_team_id"))
        self.assertEqual(error.failed_deployment_id, "order1-a")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(error))
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))
        self.assertEqual(error.num_retries, 0)
