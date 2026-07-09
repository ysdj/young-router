from __future__ import annotations

from hook_test_utils import *


class HookStreamingResponseEventTests(HookTestCase):
    async def test_responses_stream_with_text_delta_is_not_buffered_until_completed(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("active text streams must not invoke fallback")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {"type": "response.output_text.delta", "delta": "hello"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say hello."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(
            chunks,
            [
                {"type": "response.created", "response": {"id": "resp-original"}},
                {"type": "response.output_text.delta", "delta": "hello"},
                {"type": "response.completed", "response": {"id": "resp-original"}},
            ],
        )

    async def test_responses_stream_text_plan_completed_is_passed_through(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("completed text streams must not be reinterpreted by phrase matching")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            text = "I'll start by reading the skill, then executing the canonical entry command."
            yield {"type": "response.created", "response": {"id": "resp-preamble"}}
            yield {"type": "response.output_text.delta", "delta": text}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-preamble",
                    "object": "response",
                    "status": "completed",
                    "output_text": text,
                    "output": [
                        {
                            "id": "msg-preamble",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": text, "annotations": []}
                            ],
                        }
                    ],
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "legacy-chat",
            "input": [{"role": "user", "content": "Use the provided local document context only"}],
            "stream": True,
            "tools": [{"type": "function", "name": "exec_command"}],
            "model_info": {"id": "chatroute", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        dumped = json.dumps(chunks, ensure_ascii=False)
        self.assertIn("I'll start by reading the skill", dumped)
        self.assertNotIn("actual work done", dumped)
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_responses_stream_serializes_upstream_completed_event_as_json(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("complete streams must not replay")

        class ResponseEvent(str, Enum):
            COMPLETED = "response.completed"

        class FakeCompletedEvent:
            def model_dump(self, *args, **kwargs):
                return {
                    "type": ResponseEvent.COMPLETED,
                    "response": {
                        "id": "resp-original",
                        "object": "response",
                        "status": "completed",
                        "output": [
                            {
                                "id": "msg-1",
                                "type": "message",
                                "status": "completed",
                                "role": "assistant",
                                "content": [
                                    {"type": "output_text", "text": "HI_OK"},
                                ],
                            }
                        ],
                    },
                    "model": "default-chat",
                }

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "HI_OK"}
            yield FakeCompletedEvent()

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say HI_OK."}],
            "stream": True,
            "model_info": {"id": "order3-x-pro", "order": 3},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertIsInstance(chunks[-1], dict)
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertNotIn("<ResponseEvent", str(chunks[-1]))
        json.dumps(chunks[-1])

    async def test_responses_stream_completed_usage_is_codex_compatible(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("complete streams must not replay")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "done"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-original",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "prompt_tokens": 7,
                        "completion_tokens": 3,
                        "total_tokens": 10,
                    },
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say done."}],
            "stream": True,
            "model_info": {"id": "order3-x-pro", "order": 3},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        usage = chunks[-1]["response"]["usage"]
        self.assertEqual(
            usage,
            {
                "input_tokens": 7,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 3,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 10,
            },
        )

    async def test_responses_stream_completed_usage_is_normalized_inside_sse_text(self) -> None:
        hooks, _proxy_server = load_hook_module()
        event = {
            "type": "response.completed",
            "response": {
                "id": "resp-sse",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {
                    "prompt_tokens": 17,
                    "completion_tokens": 4,
                    "total_tokens": 21,
                },
            },
        }
        chunk = f"data: {json.dumps(event)}\n\n"

        normalized = hooks._responses_stream_chunk_for_delivery(chunk)
        parsed = jsonable_stream_chunk(normalized)

        self.assertEqual(
            parsed["response"]["usage"],
            {
                "input_tokens": 17,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 4,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 21,
            },
        )

    async def test_responses_stream_completed_usage_is_normalized_inside_sse_bytes(self) -> None:
        hooks, _proxy_server = load_hook_module()
        event = {
            "type": "response.completed",
            "response": {
                "id": "resp-sse-bytes",
                "object": "response",
                "status": "completed",
                "output": [],
                "usage": {
                    "prompt_tokens": 19,
                    "completion_tokens": 6,
                    "total_tokens": 25,
                },
            },
        }
        chunk = f"data: {json.dumps(event)}\n\n".encode("utf-8")

        normalized = hooks._responses_stream_chunk_for_delivery(chunk)
        parsed = jsonable_stream_chunk(normalized.decode("utf-8"))

        self.assertEqual(parsed["response"]["usage"]["input_tokens"], 19)
        self.assertEqual(parsed["response"]["usage"]["output_tokens"], 6)

    async def test_responses_stream_terminal_event_after_text_yields_failed_event(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("partial streams must not replay after yielding")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {"type": "response.output_text.delta", "delta": "12345"}
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-original",
                    "status": "failed",
                    "error": {"message": "upstream stopped early"},
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Print 12345."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]
        json_chunks = [jsonable_stream_chunk(chunk) for chunk in chunks]

        self.assertEqual(
            [chunk["type"] for chunk in json_chunks],
            ["response.created", "response.output_text.delta", "response.failed"],
        )
        self.assertEqual(json_chunks[1]["delta"], "12345")
        self.assertEqual(json_chunks[-1]["response"]["status"], "failed")

    async def test_responses_stream_output_limit_incomplete_with_output_completes_without_retry(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("output-token terminal events with output must not replay")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "type": "response.incomplete",
                "response": {
                    "id": "resp-original",
                    "object": "response",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "max_output_tokens"},
                    "output": [
                        {
                            "id": "msg-summary",
                            "type": "message",
                            "status": "incomplete",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Compaction summary is usable.",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": "Create a compact handoff summary for resuming this Codex session.",
                }
            ],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual([chunk["type"] for chunk in chunks], ["response.created", "response.completed"])
        completed = chunks[-1]
        self.assertEqual(completed["response"]["status"], "completed")
        self.assertNotIn("incomplete_details", completed["response"])
        self.assertEqual(completed["response"]["output"][0]["status"], "completed")
        self.assertIn("Compaction summary is usable.", json.dumps(completed))

    async def test_responses_stream_output_limit_error_message_after_text_completes_without_retry(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("max_output_tokens terminal events with text must not replay")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {"type": "response.output_text.delta", "delta": "Partial summary that Codex can store."}
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-original",
                    "status": "failed",
                    "error": {
                        "message": (
                            "stream disconnected before completion: "
                            "Incomplete response returned, reason: max_output_tokens"
                        )
                    },
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Compact this session."}],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            ["response.created", "response.output_text.delta", "response.completed"],
        )
        self.assertEqual(chunks[-1]["response"]["status"], "completed")
        self.assertIn("Partial summary that Codex can store.", json.dumps(chunks[-1]))

    async def test_responses_stream_tool_call_terminal_completes_without_route_recovery(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("complete tool calls must not enter route recovery")

        proxy_server.llm_router = FakeRouter()

        tool_item = {
            "id": "call_exec",
            "call_id": "call_exec",
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"cmd":"pwd"}',
            "status": "completed",
        }

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": tool_item | {"status": "in_progress"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": tool_item,
            }
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-original",
                    "status": "failed",
                    "error": {"message": "terminal event before response.completed"},
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Run pwd."}],
            "stream": True,
            "tools": [{"type": "function", "name": "exec_command"}],
            "tool_choice": "auto",
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["status"], "completed")
        self.assertEqual(chunks[-1]["response"]["output"], [tool_item])

    async def test_responses_stream_buffered_output_limit_terminal_with_tool_call_completes_without_recovery(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("buffered max-output terminal streams must not enter route recovery")

        proxy_server.llm_router = FakeRouter()

        tool_item = {
            "id": "call_exec",
            "call_id": "call_exec",
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"cmd":"pwd"}',
            "status": "completed",
        }

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": tool_item,
            }
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-original",
                    "status": "failed",
                    "error": {
                        "message": (
                            "stream disconnected before completion: "
                            "Incomplete response returned, reason: max_output_tokens"
                        )
                    },
                },
            }

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Run pwd."}],
            "stream": True,
            "tools": [{"type": "function", "name": "exec_command"}],
            "tool_choice": "auto",
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["status"], "completed")
        self.assertEqual(chunks[-1]["response"]["output"], [tool_item])

    async def test_responses_stream_output_limit_terminal_without_output_falls_back_with_compacted_history(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)

                async def fallback_stream():
                    yield {
                        "type": "response.completed",
                        "response": {
                            "id": "resp-fallback",
                            "status": "completed",
                            "output": [
                                {
                                    "id": "msg-fallback",
                                    "type": "message",
                                    "status": "completed",
                                    "role": "assistant",
                                    "content": [
                                        {
                                            "type": "output_text",
                                            "text": "Recovered after compacting history.",
                                            "annotations": [],
                                        }
                                    ],
                                }
                            ],
                        },
                    }

                return fallback_stream()

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-original",
                    "status": "failed",
                    "error": {
                        "message": (
                            "stream disconnected before completion: "
                            "Incomplete response returned, reason: max_output_tokens"
                        )
                    },
                },
            }

        hook = hooks.LiteLLMMenuHook()
        first_output = "alpha-" + ("a" * 125000) + "-omega"
        second_output = "start-" + ("b" * 125000) + "-finish"
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "Compact this session."},
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
            ],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "client_metadata": {
                "thread_id": "thread-test-0002",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["status"], "completed")
        self.assertEqual(len(calls), 1)
        retry_input = calls[0]["input"]
        self.assertEqual(retry_input[1], request_data["input"][1])
        self.assertEqual(retry_input[2]["call_id"], "call_keep")
        self.assertEqual(
            len(retry_input[2]["output"]),
            hooks._CODEX_TOOL_OUTPUT_COMPACT_ITEM_CHARS,
        )
        self.assertIn("original_chars=", retry_input[2]["output"])
        self.assertTrue(retry_input[2]["output"].startswith("alpha-"))
        self.assertTrue(retry_input[2]["output"].endswith("-omega"))
        self.assertEqual(request_data["input"][2]["output"], first_output)

    async def test_responses_stream_error_after_text_yields_failed_event_without_retry(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class GatewayTimeout(Exception):
            status_code = 504

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                raise AssertionError("visible partial streams must not invoke fallback")

        proxy_server.llm_router = FakeRouter()

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {"type": "response.output_text.delta", "delta": "partial answer"}
            raise GatewayTimeout("upstream-status-504")

        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Print a partial answer."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(calls, [])
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            ["response.created", "response.output_text.delta", "response.failed"],
        )
        self.assertEqual(chunks[1]["delta"], "partial answer")
        self.assertEqual(chunks[-1]["response"]["status"], "failed")

    async def test_streaming_hook_normalizes_completed_usage_at_delivery_boundary(self) -> None:
        hooks, _proxy_server = load_hook_module()
        original_yield_start_buffered = hooks._yield_start_buffered_stream_with_error_fallback

        async def fake_yield_start_buffered(response, request_data):
            yield {"type": "response.output_text.delta", "delta": "done"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-boundary",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "prompt_tokens": 13,
                        "completion_tokens": 8,
                        "total_tokens": 21,
                    },
                },
            }

        hooks._yield_start_buffered_stream_with_error_fallback = fake_yield_start_buffered
        self.addCleanup(
            setattr,
            hooks,
            "_yield_start_buffered_stream_with_error_fallback",
            original_yield_start_buffered,
        )

        hook = hooks.LiteLLMMenuHook()
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=object(),
                request_data={"model": "legacy-chat", "input": "Say done.", "stream": True},
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["usage"],
            {
                "input_tokens": 13,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 8,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 21,
            },
        )

    async def test_image_streaming_temporary_error_while_buffering_replays_via_router(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class ProviderConcurrencyError(Exception):
            pass

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "warming up"}
            raise ProviderConcurrencyError("Concurrency limit exceeded, try again later")

        async def fallback_stream():
            yield {"type": "image_generation_call", "result": "base64-image"}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "画一张图。"}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": True,
            "model_info": {"id": "image-deployment-a", "order": 3},
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
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_image_streaming_error_chunk_while_buffering_replays_via_router(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "warming up"}
            yield {
                "error": {
                    "message": "Concurrency limit exceeded, try again later",
                    "type": "rate_limit_error",
                }
            }

        async def fallback_stream():
            yield {"type": "image_generation_call", "result": "base64-image"}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "画一张图。"}],
            "tools": [{"type": "image_generation"}],
            "tool_choice": "auto",
            "stream": True,
            "model_info": {"id": "image-deployment-a", "order": 3},
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
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_streaming_capacity_error_chunk_replays_via_router(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "error": {
                    "message": "Selected model is at capacity. Please try a different model.",
                    "type": "server_error",
                }
            }

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "fallback ok"}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say pong only."}],
            "stream": True,
            "model_info": {"id": "capacity-full-deployment", "order": 2},
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
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_streaming_non_temporary_error_is_not_replayed(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("non-temporary stream errors must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        error = ValueError("malformed local stream chunk")

        async def original_stream():
            raise error
            yield {"type": "response.output_text.delta", "delta": "unreachable"}

        with self.assertRaises(ValueError):
            [
                chunk
                async for chunk in hook.async_post_call_streaming_iterator_hook(
                    user_api_key_dict=None,
                    response=original_stream(),
                    request_data={
                        "model": "default-chat",
                        "input": [{"role": "user", "content": "Say pong only."}],
                        "stream": True,
                    },
                )
            ]


if __name__ == "__main__":
    unittest.main()
