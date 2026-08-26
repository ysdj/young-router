from __future__ import annotations

import copy

from hook_test_utils import *


class HookStreamingFailoverTests(HookTestCase):
    async def test_chat_route_exhaustion_propagates_error_instead_of_cancellation(self) -> None:
        hooks, _proxy_server = load_hook_module()
        streaming_module = sys.modules["litellm_menu.streaming"]
        original_fallback = streaming_module._stream_streaming_error_fallback
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_streaming_error_fallback",
            original_fallback,
        )
        # Isolate the terminal propagation branch; route-recovery exhaustion
        # with an active polling budget is covered by route-recovery tests.
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0")

        class NoDeploymentsError(Exception):
            pass

        async def failed_fallback(_request_data, _exception):
            raise NoDeploymentsError("No deployments available for selected model")
            yield None

        streaming_module._stream_streaming_error_fallback = failed_fallback
        request_data = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "proxy_server_request": {"path": "/v1/chat/completions"},
        }

        with self.assertRaisesRegex(
            NoDeploymentsError,
            "No deployments available for selected model",
        ):
            async for _chunk in hooks._yield_streaming_error_fallback_or_raise(
                request_data,
                RuntimeError("upstream failed"),
            ):
                pass

    async def test_chat_completions_clean_eof_before_terminal_falls_back(self) -> None:
        hooks, proxy_server = load_hook_module()
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        calls = []

        async def incomplete_stream():
            yield {
                "id": "chatcmpl-incomplete",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant"},
                        "finish_reason": None,
                    }
                ],
            }

        async def recovered_stream():
            yield {
                "id": "chatcmpl-recovered",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Recovered."},
                        "finish_reason": None,
                    }
                ],
            }
            yield {
                "id": "chatcmpl-recovered",
                "object": "chat.completion.chunk",
                "choices": [
                    {"index": 0, "delta": {}, "finish_reason": "stop"}
                ],
            }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"model": "provider/default-chat"},
                        "model_info": {"id": "chat-original"},
                    },
                    {
                        "litellm_params": {"model": "provider/default-chat"},
                        "model_info": {"id": "chat-recovered"},
                    },
                ]

            async def acompletion(self, **payload):
                calls.append(payload)
                hooks._remember_selected_deployment(
                    {
                        "litellm_params": {"model": "provider/default-chat"},
                        "model_info": {"id": "chat-recovered", "order": 1},
                    }
                )
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "proxy_server_request": {"path": "/v1/chat/completions"},
            "model_info": {"id": "chat-original", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=incomplete_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["chat-original"])
        self.assertEqual(chunks[0]["id"], "chatcmpl-recovered")
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")
        self.assertNotIn("chatcmpl-incomplete", json.dumps(chunks))
        self.assertIn("id:chat-original", hooks._DEPLOYMENT_COOLDOWNS)

    async def test_anthropic_clean_eof_before_message_stop_falls_back(self) -> None:
        hooks, proxy_server = load_hook_module()
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        calls = []
        incomplete_start = (
            b'event: message_start\n'
            b'data: {"type":"message_start","message":{"id":"msg-incomplete"}}\n\n'
        )
        recovered_chunks = [
            (
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg-recovered"}}\n\n'
            ),
            (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","delta":'
                b'{"type":"text_delta","text":"Recovered."}}\n\n'
            ),
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n',
        ]

        async def incomplete_stream():
            yield incomplete_start

        async def recovered_stream():
            for chunk in recovered_chunks:
                yield chunk

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"model": "provider/default-chat"},
                        "model_info": {"id": "messages-original"},
                    },
                    {
                        "litellm_params": {"model": "provider/default-chat"},
                        "model_info": {"id": "messages-recovered"},
                    },
                ]

            async def anthropic_messages(self, **payload):
                calls.append(payload)
                hooks._remember_selected_deployment(
                    {
                        "litellm_params": {"model": "provider/default-chat"},
                        "model_info": {"id": "messages-recovered", "order": 1},
                    }
                )
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "Continue."}],
            "max_tokens": 64,
            "stream": True,
            "proxy_server_request": {"path": "/v1/messages"},
            "model_info": {"id": "messages-original", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=incomplete_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["messages-original"])
        self.assertEqual(chunks, recovered_chunks)
        self.assertNotIn(incomplete_start, chunks)
        self.assertIn("id:messages-original", hooks._DEPLOYMENT_COOLDOWNS)

    async def test_native_stream_partial_output_without_terminal_cools_route(self) -> None:
        hooks, _proxy_server = load_hook_module()
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        request_data = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "proxy_server_request": {"path": "/v1/chat/completions"},
            "model_info": {"id": "partial-chat-route", "order": 1},
        }

        async def incomplete_stream():
            yield {
                "id": "chatcmpl-partial",
                "object": "chat.completion.chunk",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "Partial"},
                        "finish_reason": None,
                    }
                ],
            }

        delivered = []
        with self.assertRaisesRegex(
            RuntimeError,
            "Chat Completions stream ended before a terminal finish_reason",
        ):
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=incomplete_stream(),
                request_data=request_data,
            ):
                delivered.append(chunk)

        self.assertEqual(delivered[0]["choices"][0]["delta"]["content"], "Partial")
        self.assertIn("id:partial-chat-route", hooks._DEPLOYMENT_COOLDOWNS)

    async def test_incomplete_responses_stream_after_tool_activity_cools_route_immediately(self) -> None:
        hooks, _proxy_server = load_hook_module()
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, None)
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Run the command."}],
            "stream": True,
            "model_info": {
                "id": "partial-tool-route",
                "route_key": "provider / openai/default-chat / key=primary / order=1",
                "order": 1,
            },
        }

        async def incomplete_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-partial", "status": "in_progress"},
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "call-partial",
                    "type": "function_call",
                    "name": "exec",
                    "arguments": "{",
                },
            }
            yield {
                "type": "response.incomplete",
                "response": {
                    "id": "resp-partial",
                    "status": "incomplete",
                    "incomplete_details": {"reason": "stream_interrupted"},
                },
            }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=incomplete_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.failed")
        cooldown = hooks._DEPLOYMENT_COOLDOWNS["id:partial-tool-route"]
        self.assertEqual(cooldown["failures"], 1)
        self.assertGreater(cooldown["cooldown_until"], time.time())

    def test_route_recovery_rejects_reasoning_only_completed_response(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }
        reasoning_only = {
            "type": "response.completed",
            "response": {
                "id": "resp-reasoning-only",
                "status": "completed",
                "output": [
                    {
                        "id": "reasoning-only",
                        "type": "reasoning",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "internal reasoning",
                            }
                        ],
                    }
                ],
            },
        }
        tool_call = {
            "type": "response.completed",
            "response": {
                "id": "resp-tool-call",
                "status": "completed",
                "output": [
                    {
                        "id": "call-tool",
                        "type": "function_call",
                        "name": "exec_command",
                        "arguments": "{}",
                    }
                ],
            },
        }

        self.assertFalse(
            hooks._stream_chunk_has_deliverable_route_recovery_output(
                reasoning_only, request_data
            )
        )
        self.assertTrue(
            hooks._stream_chunk_has_deliverable_route_recovery_output(
                tool_call, request_data
            )
        )

    async def test_reasoning_only_clean_eof_fails_over_instead_of_completing(self) -> None:
        hooks, proxy_server = load_hook_module()
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, None)
        calls = []

        async def reasoning_only_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-reasoning-eof", "status": "in_progress"},
            }
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "reasoning-eof",
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            }
            for index in range(20):
                yield {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "reasoning-eof",
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": f"step {index}",
                }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "reasoning-eof",
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "internal reasoning only",
                        }
                    ],
                },
            }

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered"}
            yield {
                "type": "response.completed",
                "response": {"id": "resp-recovered", "status": "completed"},
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()

        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {
                "id": "reasoning-eof-route",
                "route_key": "provider / openai/default-chat / key=primary / order=1",
                "order": 1,
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=reasoning_only_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "recovered"},
                {
                    "type": "response.completed",
                    "response": {"id": "resp-recovered", "status": "completed"},
                },
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertIn("id:reasoning-eof-route", hooks._DEPLOYMENT_COOLDOWNS)

    async def test_route_recovery_reasoning_only_completion_returns_failure(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.01")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        calls = []

        async def reasoning_only_stream():
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-reasoning-only",
                    "status": "completed",
                    "output": [{"id": "reasoning-only", "type": "reasoning", "summary": []}],
                },
            }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return reasoning_only_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }
        failure = RuntimeError("temporary upstream failure")
        failure.status_code = 503

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(request_data, failure)
        ]

        self.assertGreaterEqual(len(calls), 1)
        self.assertEqual([chunk["type"] for chunk in chunks], ["response.failed"])

    async def test_empty_non_streaming_responses_result_enters_route_recovery(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered",
                    "status": "completed",
                    "output": [
                        {
                            "id": "msg-recovered",
                            "type": "message",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "recovered",
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
                        "model_info": {"id": "route-recovery"},
                    }
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "route-original", "order": 1},
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response={
                    "id": "resp-empty",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[0]["delta"], "recovered")
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(len(calls), 1)
        self.assertNotIn("resp-empty", json.dumps(chunks))

    async def test_non_streaming_responses_tool_call_is_not_empty(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Call the tool."}],
            "stream": True,
        }
        response = {
            "id": "resp-tool",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "call-tool",
                    "type": "function_call",
                    "name": "exec",
                    "arguments": "{}",
                }
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["output"][0]["type"], "function_call")

    async def test_non_streaming_compaction_response_is_not_empty(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        response = {
            "id": "resp-compaction",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "id": "cmp-summary",
                    "type": "compaction",
                    "encrypted_content": "encrypted-summary",
                }
            ],
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"][0]["encrypted_content"],
            "encrypted-summary",
        )

    async def test_non_streaming_reasoning_only_response_enters_route_recovery(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "answer"}
            yield {"type": "response.completed", "response": {"id": "resp-answer"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response={
                    "id": "resp-reasoning-only",
                    "object": "response",
                    "status": "completed",
                    "output": [
                        {
                            "id": "reasoning-1",
                            "type": "reasoning",
                            "summary": [{"type": "summary_text", "text": "internal"}],
                        }
                    ],
                },
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[0]["delta"], "answer")
        self.assertEqual(len(calls), 1)

    async def test_context_truncation_stream_closes_after_completed_event(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class HangingAfterCompletionStream:
            def __init__(self) -> None:
                self.events = iter(
                    [
                        {"type": "response.created", "response": {"id": "resp-done"}},
                        {"type": "response.output_text.delta", "delta": "done"},
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-done",
                                "status": "completed",
                                "output": [],
                            },
                        },
                    ]
                )
                self.closed = False
                self.read_count = 0
                self.completed_response = None

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.read_count += 1
                try:
                    event = next(self.events)
                except StopIteration:
                    await asyncio.sleep(60)
                    raise StopAsyncIteration
                if event.get("type") == "response.completed":
                    self.completed_response = event
                return event

            async def aclose(self) -> None:
                self.closed = True

        source = HangingAfterCompletionStream()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "finish"}],
            "stream": True,
        }
        wrapped = hooks._with_responses_context_truncation_fallback_stream(
            source,
            request_data,
            lambda **_kwargs: None,
        )

        async def collect() -> list:
            return [chunk async for chunk in wrapped]

        chunks = await asyncio.wait_for(collect(), 0.5)

        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            ["response.created", "response.output_text.delta", "response.completed"],
        )
        self.assertTrue(source.closed)
        self.assertEqual(source.read_count, 3)
        self.assertEqual(
            wrapped.completed_response,
            source.completed_response["response"],
        )

    async def test_empty_completed_event_is_not_treated_as_success(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class EmptyCompletedStream:
            def __init__(self) -> None:
                self.events = iter(
                    [
                        {
                            "type": "response.created",
                            "response": {"id": "resp-empty"},
                        },
                        {
                            "type": "response.completed",
                            "response": {
                                "id": "resp-empty",
                                "status": "completed",
                                "output": [],
                            },
                        },
                    ]
                )
                self.closed = False

            def __aiter__(self):
                return self

            async def __anext__(self):
                try:
                    return next(self.events)
                except StopIteration:
                    raise AssertionError("the wrapper must stop at the empty terminal")

            async def aclose(self) -> None:
                self.closed = True

        source = EmptyCompletedStream()
        wrapped = hooks._with_responses_context_truncation_fallback_stream(
            source,
            {
                "model": "default-chat",
                "input": [{"role": "user", "content": "finish"}],
                "stream": True,
            },
            lambda **_kwargs: None,
        )

        with self.assertRaisesRegex(
            RuntimeError,
            "Responses stream incomplete: response.completed without usable output",
        ):
            async for _chunk in wrapped:
                pass

        self.assertTrue(source.closed)

    async def test_context_truncation_stream_closes_after_failed_event(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class HangingAfterFailureStream:
            def __init__(self) -> None:
                self.events = iter(
                    [
                        {
                            "type": "response.created",
                            "response": {"id": "resp-failed", "status": "in_progress"},
                        },
                        {
                            "type": "response.failed",
                            "response": {
                                "id": "resp-failed",
                                "status": "failed",
                                "error": {
                                    "code": "invalid_request_error",
                                    "message": "synthetic failure",
                                },
                            },
                        },
                    ]
                )
                self.closed = False
                self.read_count = 0

            def __aiter__(self):
                return self

            async def __anext__(self):
                self.read_count += 1
                try:
                    return next(self.events)
                except StopIteration:
                    await asyncio.sleep(60)
                    raise StopAsyncIteration

            async def aclose(self) -> None:
                self.closed = True

        source = HangingAfterFailureStream()
        wrapped = hooks._with_responses_context_truncation_fallback_stream(
            source,
            {
                "model": "default-chat",
                "input": [{"role": "user", "content": "finish"}],
                "stream": True,
            },
            lambda **_kwargs: None,
        )

        async def collect() -> list:
            return [chunk async for chunk in wrapped]

        chunks = await asyncio.wait_for(collect(), 0.5)

        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            ["response.created", "response.failed"],
        )
        self.assertTrue(source.closed)
        self.assertEqual(source.read_count, 2)
        self.assertEqual(wrapped.completed_response, chunks[-1]["response"])

    async def test_structured_compaction_uses_longer_stream_start_deadline(self) -> None:
        hooks, _proxy_server = load_hook_module()
        routing_module = importlib.import_module("litellm_menu.routing")
        previous_compaction_timeout = (
            routing_module._CODEX_COMPACTION_STREAM_START_TIMEOUT_DEFAULT_SECONDS
        )
        routing_module._CODEX_COMPACTION_STREAM_START_TIMEOUT_DEFAULT_SECONDS = 0.05
        self.addCleanup(
            setattr,
            routing_module,
            "_CODEX_COMPACTION_STREAM_START_TIMEOUT_DEFAULT_SECONDS",
            previous_compaction_timeout,
        )
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "1")

        async def delayed_stream():
            yield {"type": "response.completed", "response": {"id": "resp-compact"}}

        async def original_generic_function(**_kwargs):
            await asyncio.sleep(0.02)
            return delayed_stream()

        request = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "continue"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "_target_order": 1,
        }

        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(request),
            0.05,
        )
        response = await hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )(**request)
        chunks = [chunk async for chunk in response]

        self.assertEqual(
            chunks,
            [{"type": "response.completed", "response": {"id": "resp-compact"}}],
        )

    def test_structured_compaction_stream_start_deadline_respects_request_override(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "120")
        request = {
            "call_type": "aresponses",
            "input": [{"type": "compaction_trigger"}],
            "stream": True,
            "litellm_metadata": {
                "route_recovery_attempt_timeout_seconds": 17,
            },
        }

        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(request),
            17.0,
        )

    def test_streaming_fallback_preserves_leading_additional_tools_and_controls(self) -> None:
        hooks, _proxy_server = load_hook_module()
        additional_tools = {
            "type": "additional_tools",
            "role": "developer",
            "tools": [
                {"type": "custom", "name": "exec"},
                {
                    "type": "namespace",
                    "name": "collaboration",
                    "tools": [{"type": "function", "name": "spawn_agent"}],
                },
            ],
        }
        request = {
            "model": "default-chat",
            "input": [
                additional_tools,
                {"role": "user", "content": "Inspect the workspace."},
            ],
            "stream": True,
            "tools": [],
            "tool_choice": {"type": "custom", "name": "exec"},
            "parallel_tool_calls": False,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
        }

        payload = hooks._build_streaming_error_fallback_payload(
            request,
            method_name="aresponses",
        )

        self.assertIsNotNone(payload)
        assert payload is not None
        self.assertEqual(payload["input"][0], additional_tools)
        self.assertEqual(payload["tools"], [])
        self.assertEqual(
            payload["tool_choice"],
            {"type": "custom", "name": "exec"},
        )
        self.assertFalse(payload["parallel_tool_calls"])

    async def test_context_fallback_preserves_history_on_same_deployment(self) -> None:
        hooks, _proxy_server = load_hook_module()
        calls = []
        original_input = [
            {
                "type": "message",
                "id": "developer-keep",
                "role": "developer",
                "content": "d" * 100,
            },
            {
                "type": "custom_tool_call",
                "id": "old-call",
                "call_id": "call-old",
                "name": "exec",
                "input": "old input",
            },
            {
                "type": "custom_tool_call_output",
                "id": "old-output",
                "call_id": "call-old",
                "output": "o" * 900,
            },
            {
                "type": "message",
                "id": "assistant-recent",
                "role": "assistant",
                "content": "a" * 100,
            },
            {
                "type": "function_call",
                "id": "recent-call",
                "call_id": "call-recent",
                "name": "inspect",
                "arguments": "r" * 100,
            },
            {
                "type": "function_call_output",
                "id": "recent-output",
                "call_id": "call-recent",
                "output": "v" * 300,
            },
            {
                "type": "message",
                "id": "latest-user",
                "role": "user",
                "content": "continue from the latest result",
            },
        ]

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            if len(calls) == 1:
                exc = RuntimeError(
                    "context_length_exceeded: input exceeds the context window"
                )
                exc.status_code = 400
                raise exc
            return {"ok": True}

        wrapped = hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )
        response = await wrapped(
            call_type="aresponses",
            model="configured-upstream-model",
            input=original_input,
            stream=True,
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"turn"}'
            },
            model_info={
                "id": "same-deployment",
                "route_key": "provider / configured-upstream-model / key=primary",
            },
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["truncation"], "auto")
        self.assertEqual(calls[1]["model"], "configured-upstream-model")
        self.assertEqual(calls[1]["model_info"]["id"], "same-deployment")
        self.assertIs(calls[1]["input"], original_input)
        self.assertNotIn("_excluded_deployment_ids", calls[1])

    def test_context_fallback_with_token_counts_preserves_history(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "This model's maximum context length is 100,000 tokens, "
            "but the input contains 200,000 tokens."
        )
        request = {
            "call_type": "aresponses",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": f"old-{index}-" + ("x" * 20_000),
                }
                for index in range(5)
            ]
            + [
                {
                    "type": "message",
                    "role": "user",
                    "content": "latest request",
                }
            ],
            "stream": True,
        }

        retry = hooks._responses_context_truncation_fallback_kwargs(exc, request)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry["truncation"], "auto")
        self.assertIs(retry["input"], request["input"])

    def test_context_fallback_honors_explicit_auto_without_rewriting_history(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "context_length_exceeded: input exceeds the context window"
        )
        request = {
            "call_type": "aresponses",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": "old history " + ("x" * 450_000),
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": "latest request",
                },
            ],
            "stream": True,
            "truncation": "auto",
        }

        retry = hooks._responses_context_truncation_fallback_kwargs(exc, request)

        self.assertIsNotNone(retry)
        assert retry is not None
        self.assertEqual(retry["truncation"], "auto")
        self.assertIs(retry["input"], request["input"])

    async def test_generic_response_wrapper_retries_context_error_with_native_truncation(self) -> None:
        hooks, _proxy_server = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        calls = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            exc = RuntimeError(
                "context_length_exceeded: your input exceeds the context window"
            )
            exc.status_code = 400
            raise exc

        first_output = [
            {"type": "input_text", "text": "alpha-" + ("a" * 125000)},
            {"type": "input_text", "text": "-omega"},
        ]
        second_output = [
            {"type": "input_text", "text": "start-" + ("b" * 125000)},
            {"type": "input_text", "text": "-finish"},
        ]
        original_input = [
            {"type": "message", "role": "user", "content": "Continue."},
            {
                "type": "custom_tool_call",
                "id": "ctc_keep",
                "call_id": "call_keep",
                "name": "exec",
                "input": "const result = await tools.exec_command({});",
            },
            {
                "type": "custom_tool_call_output",
                "id": "ctco_keep",
                "call_id": "call_keep",
                "output": first_output,
            },
            {
                "type": "custom_tool_call_output",
                "id": "ctco_second",
                "call_id": "call_second",
                "output": second_output,
            },
        ]

        async def succeeds_with_truncation(**kwargs):
            calls.append(kwargs.copy())
            if len(calls) == 1:
                exc = RuntimeError(
                    "context_length_exceeded: your input exceeds the context window"
                )
                exc.status_code = 400
                raise exc
            return {"ok": True}

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            succeeds_with_truncation,
            call_type="aresponses",
            input=original_input,
            stream=True,
            client_metadata={
                "thread_id": "thread-context-retry",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            model_info={"id": "same-deployment"},
        )

        self.assertEqual(response, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[0]["input"], original_input)
        self.assertNotIn("truncation", calls[0])
        self.assertEqual(calls[1]["truncation"], "auto")
        self.assertEqual(calls[1]["model_info"], {"id": "same-deployment"})
        self.assertIs(calls[1]["input"], original_input)
        self.assertTrue(
            calls[1]["litellm_metadata"][
                hooks._RESPONSES_CONTEXT_TRUNCATION_FALLBACK_METADATA_KEY
            ]
        )
        self.assertEqual(original_input[2]["output"], first_output)

    async def test_generic_response_wrapper_does_not_override_explicit_truncation(self) -> None:
        hooks, _proxy_server = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        calls = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            exc = RuntimeError(
                "context_length_exceeded: your input exceeds the context window"
            )
            exc.status_code = 400
            raise exc

        large_output = "x" * 250000
        with self.assertRaises(RuntimeError):
            await hooks._wrap_generic_function_for_deployment_failover(
                original_generic_function,
                outer_request_kwargs={"truncation": "disabled"},
            )(
                input=[
                    {"type": "message", "role": "user", "content": "Continue."},
                    {
                        "type": "function_call_output",
                        "call_id": "call_large",
                        "output": large_output,
                    },
                ],
                stream=True,
                truncation="disabled",
                client_metadata={
                    "thread_id": "thread-context-retry",
                    "x-codex-turn-metadata": '{"request_kind":"turn"}',
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["input"][1]["output"], large_output)

    async def test_generic_response_wrapper_does_not_context_retry_compaction_request(self) -> None:
        hooks, _proxy_server = load_hook_module()
        calls = []

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            exc = RuntimeError(
                "context_length_exceeded: your input exceeds the context window"
            )
            exc.status_code = 400
            raise exc

        wrapped = hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )
        with self.assertRaises(RuntimeError):
            await wrapped(
                call_type="aresponses",
                model="default-chat",
                input=[
                    {"type": "message", "role": "user", "content": "history"},
                    {"type": "compaction_trigger", "id": "compact-now"},
                ],
                stream=True,
                client_metadata={
                    "x-codex-turn-metadata": '{"request_kind":"compaction"}'
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("truncation", calls[0])

    async def test_stream_context_terminal_retries_before_failed_events_are_delivered(self) -> None:
        hooks, _proxy_server = load_hook_module()
        calls = []

        async def first_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-too-large", "status": "in_progress"},
            }
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-too-large",
                    "status": "failed",
                    "error": {
                        "code": "context_length_exceeded",
                        "type": "invalid_request_error",
                        "message": "Your input exceeds the context window of this model.",
                    },
                },
            }

        async def retry_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-truncated", "status": "in_progress"},
            }
            yield {"type": "response.output_text.delta", "delta": "continued"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-truncated",
                    "status": "completed",
                    "output": [
                        {
                            "type": "message",
                            "role": "assistant",
                            "content": [
                                {"type": "output_text", "text": "continued"}
                            ],
                        }
                    ],
                },
            }

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            return first_stream() if len(calls) == 1 else retry_stream()

        wrapped = hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )
        response = await wrapped(
            call_type="aresponses",
            model="configured-upstream-model",
            input=[{"type": "message", "role": "user", "content": "continue"}],
            stream=True,
            client_metadata={
                "thread_id": "thread-stream-context",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            model_info={"id": "same-deployment", "order": 2},
        )
        chunks = [chunk async for chunk in response]

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["truncation"], "auto")
        self.assertEqual(calls[1]["model"], "configured-upstream-model")
        self.assertEqual(
            calls[1]["model_info"],
            {"id": "same-deployment", "order": 2},
        )
        serialized = json.dumps(chunks)
        self.assertNotIn("resp-too-large", serialized)
        self.assertNotIn("response.failed", serialized)
        self.assertIn("resp-truncated", serialized)
        self.assertIn("continued", serialized)
        self.assertEqual(response.completed_response["id"], "resp-truncated")
        self.assertEqual(response.completed_response["status"], "completed")

    async def test_stream_context_fallback_exception_does_not_loop(self) -> None:
        hooks, _proxy_server = load_hook_module()
        calls = []

        async def context_exception_stream():
            raise RuntimeError(
                "context_length_exceeded: input exceeds the context window"
            )
            yield  # pragma: no cover

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            return context_exception_stream()

        wrapped = hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )
        response = await wrapped(
            call_type="aresponses",
            model="configured-upstream-model",
            input=[{"type": "message", "role": "user", "content": "continue"}],
            stream=True,
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"turn"}'
            },
            model_info={"id": "same-deployment"},
        )

        with self.assertRaisesRegex(RuntimeError, "context_length_exceeded"):
            async for _chunk in response:
                pass

        self.assertEqual(len(calls), 2)
        self.assertNotIn("truncation", calls[0])
        self.assertEqual(calls[1]["truncation"], "auto")

    async def test_second_context_terminal_never_becomes_deployment_failover(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        def context_failed_stream(response_id):
            async def stream():
                yield {
                    "type": "response.created",
                    "response": {"id": response_id, "status": "in_progress"},
                }
                yield {
                    "type": "response.failed",
                    "response": {
                        "id": response_id,
                        "status": "failed",
                        "error": {
                            "type": "invalid_request_error",
                            "code": "context_length_exceeded",
                            "message": "Input exceeds the context window.",
                        },
                    },
                }

            return stream()

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            return context_failed_stream(f"resp-{len(calls)}")

        class FakeRouter:
            async def aresponses(self, **_payload):
                raise AssertionError("context fallback must not re-enter Router")

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "call_type": "aresponses",
            "model": "configured-upstream-model",
            "input": [
                {"type": "message", "role": "user", "content": "continue"}
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}'
            },
            "model_info": {
                "id": "same-deployment",
                "route_key": "provider / configured-upstream-model / key=primary",
            },
        }
        wrapped = hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )
        response = await wrapped(**request_data)

        with self.assertRaises(RuntimeError) as raised:
            async for _chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=request_data,
            ):
                pass

        self.assertEqual(len(calls), 2)
        self.assertEqual(calls[1]["truncation"], "auto")
        self.assertTrue(hooks._is_context_size_error(raised.exception))
        self.assertFalse(hasattr(raised.exception, "failed_deployment_id"))
        self.assertNotIn("_excluded_deployment_ids", request_data)

    async def test_stream_context_error_after_visible_output_is_not_replayed(self) -> None:
        hooks, _proxy_server = load_hook_module()
        calls = []

        async def visible_then_context_error():
            yield {
                "type": "response.created",
                "response": {"id": "resp-visible", "status": "in_progress"},
            }
            yield {"type": "response.output_text.delta", "delta": "visible"}
            raise RuntimeError(
                "context_length_exceeded: input exceeds the context window"
            )

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            return visible_then_context_error()

        wrapped = hooks._wrap_generic_function_for_deployment_failover(
            original_generic_function
        )
        response = await wrapped(
            call_type="aresponses",
            model="configured-upstream-model",
            input=[{"type": "message", "role": "user", "content": "continue"}],
            stream=True,
            client_metadata={
                "x-codex-turn-metadata": '{"request_kind":"turn"}'
            },
            model_info={"id": "same-deployment"},
        )

        chunks = []
        with self.assertRaisesRegex(RuntimeError, "context_length_exceeded"):
            async for chunk in response:
                chunks.append(chunk)

        self.assertEqual(len(calls), 1)
        self.assertEqual(
            chunks[-1],
            {"type": "response.output_text.delta", "delta": "visible"},
        )
    async def test_pre_call_deployment_hook_preserves_upstream_metadata_when_opted_in(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        original = {
            "metadata": {"trace_id": "client-trace"},
            "litellm_metadata": {"model_group": "default-chat"},
            "model_info": {"forward_metadata_to_upstream": True},
        }

        modified = await hook.async_pre_call_deployment_hook(original, call_type=None)

        self.assertIsNotNone(modified)
        assert modified is not None
        self.assertEqual(modified["metadata"], {"trace_id": "client-trace"})
        self.assertEqual(modified["litellm_metadata"]["trace_id"], "client-trace")
        self.assertEqual(modified["litellm_metadata"]["model_group"], "default-chat")

    async def test_generic_response_wrapper_marks_plain_upstream_403_for_failover(self) -> None:
        hooks, _ = load_hook_module()

        class UpstreamForbidden(Exception):
            status_code = 403

        error = UpstreamForbidden("plain forbidden")

        async def original_generic_function(**kwargs):
            raise error

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)
        wrapped = request_kwargs["original_generic_function"]
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        self.assertIs(request_kwargs["original_generic_function"], wrapped)
        with self.assertRaises(UpstreamForbidden) as raised:
            await wrapped(model="default-chat", model_info={"id": "failed-deployment"})

        self.assertIs(raised.exception, error)
        self.assertEqual(error.failed_deployment_id, "failed-deployment")
        self.assertEqual(error.num_retries, 0)

    async def test_generic_response_wrapper_times_out_before_stream_object(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "60")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")

        async def original_generic_function(**kwargs):
            await asyncio.sleep(60)

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(TimeoutError) as context:
            await request_kwargs["original_generic_function"](
                model="balanced-chat",
                input=[{"role": "user", "content": "hi"}],
                stream=True,
                model_info={"id": "slow-before-stream"},
            )

        exc = context.exception
        self.assertEqual(getattr(exc, "status_code", None), 504)
        self.assertEqual(getattr(exc, "body", {}).get("reason"), "stream_start_timeout")
        self.assertEqual(getattr(exc, "failed_deployment_id", None), "slow-before-stream")
        self.assertNotIn("_excluded_deployment_ids", request_kwargs)

    def test_responses_function_bridge_uses_first_event_timeout(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "123")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "45")
        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "hi",
            "stream": True,
            "tools": [
                {"type": "custom", "name": "apply_patch", "description": "Edit files."},
                {"type": "tool_search"},
            ],
            "model_info": {
                "id": "third-party-responses",
                "upstream_url_surface": "openai/responses",
                "supports_responses_client_tools": False,
                "supports_responses_function_tools": True,
                "supported_upstream_url_surfaces": ["openai/responses", "openai/chat"],
            },
        }

        bridge_kwargs = hooks._responses_function_tool_bridge_preemptive_kwargs(
            request_kwargs
        )

        self.assertIsNotNone(bridge_kwargs)
        assert bridge_kwargs is not None
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(bridge_kwargs),
            45.0,
        )
        self.assertEqual(hooks._request_timeout_seconds(), 123.0)

    def test_responses_function_bridge_stream_start_timeout_does_not_get_chat_retry(self) -> None:
        hooks, _ = load_hook_module()
        request_kwargs = {
            "call_type": "aresponses",
            "model": "balanced-chat",
            "input": "hi",
            "stream": True,
            "tools": [
                {"type": "custom", "name": "apply_patch", "description": "Edit files."},
                {"type": "tool_search"},
            ],
            "model_info": {
                "id": "third-party-responses",
                "provider": "third-party",
                "upstream_url_surface": "openai/responses",
                "supports_responses_client_tools": False,
                "supports_responses_function_tools": True,
                "supported_upstream_url_surfaces": ["openai/responses", "openai/chat"],
            },
        }
        bridge_kwargs = hooks._responses_function_tool_bridge_preemptive_kwargs(
            request_kwargs
        )
        self.assertIsNotNone(bridge_kwargs)
        assert bridge_kwargs is not None
        exc = TimeoutError("stream did not start")
        exc.status_code = 504
        exc.body = {"reason": "stream_start_timeout"}

        self.assertIsNone(hooks._responses_chat_bridge_retry_kwargs(exc, request_kwargs, None))
        self.assertIsNone(hooks._responses_chat_bridge_retry_kwargs(exc, bridge_kwargs, None))

    async def test_generic_wrapper_does_not_chat_bridge_after_responses_function_bridge_start_timeout(self) -> None:
        hooks, _ = load_hook_module()
        calls = []
        exc = TimeoutError("stream did not start")
        exc.status_code = 504
        exc.body = {"reason": "stream_start_timeout"}

        async def original_generic_function(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise exc
            return {"ok": True}

        request_kwargs = {"original_generic_function": original_generic_function}
        hooks._with_generic_deployment_failover_wrapper(request_kwargs)

        with self.assertRaises(TimeoutError):
            await request_kwargs["original_generic_function"](
                call_type="aresponses",
                model="balanced-chat",
                input="hi",
                stream=True,
                tools=[
                    {"type": "custom", "name": "apply_patch", "description": "Edit files."},
                    {"type": "tool_search"},
                ],
                model_info={
                    "id": "third-party-responses",
                    "provider": "third-party",
                    "upstream_url_surface": "openai/responses",
                    "supports_responses_client_tools": False,
                    "supports_responses_function_tools": True,
                    "supported_upstream_url_surfaces": ["openai/responses", "openai/chat"],
                },
            )

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(
            calls[0]["litellm_metadata"][
                hooks._RESPONSES_FUNCTION_TOOL_BRIDGE_METADATA_KEY
            ]
        )

    def test_streaming_and_chat_bridge_fallback_payloads_preserve_xhigh(self) -> None:
        hooks, _ = load_hook_module()

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [{"role": "user", "content": "hi"}],
            "stream": True,
            "reasoning": {"effort": "xhigh"},
        }
        streaming_payload = hooks._build_streaming_error_fallback_payload(
            request_data,
            method_name="aresponses",
        )
        self.assertIsNotNone(streaming_payload)
        assert streaming_payload is not None
        self.assertEqual(streaming_payload["reasoning"]["effort"], "xhigh")

        class ResponsesNotFound(Exception):
            status_code = 404

        bridge_payload = hooks._responses_chat_bridge_retry_kwargs(
            ResponsesNotFound("OpenAIException - not found"),
            request_data,
            None,
        )
        self.assertIsNotNone(bridge_payload)
        assert bridge_payload is not None
        self.assertEqual(bridge_payload["reasoning"]["effort"], "xhigh")

    async def test_generic_streaming_fallback_times_out_silent_route_and_reaches_next_order(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "60")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")
        sleeps = []
        original_sleep = hooks.asyncio.sleep

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        hooks.asyncio.sleep = fake_sleep
        self.addCleanup(setattr, hooks.asyncio, "sleep", original_sleep)
        router_module = types.ModuleType("litellm.router")
        calls = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                for deployment in (
                    {"id": "cheap", "order": 1},
                    {"id": "plus", "order": 2},
                    {"id": "pro", "order": 3},
                ):
                    excluded = set(kwargs.get("_excluded_deployment_ids") or [])
                    if deployment["id"] in excluded:
                        continue
                    try:
                        response_kwargs = {
                            "stream": True,
                            "input": [{"role": "user", "content": "hi"}],
                            "model_info": {
                                "id": deployment["id"],
                                "order": deployment["order"],
                                "route_key": f"compat_provider / openai/default-chat / key=x-{deployment['id']}",
                            },
                        }
                        return await original_generic_function(
                            **response_kwargs,
                        )
                    except Exception as exc:
                        if hooks._is_priority_deployment_failover_error(exc):
                            await hooks._sleep_before_final_route_retry(
                                model,
                                exc,
                                kwargs,
                                attempt=len(calls),
                                max_retries=5,
                                configured_delay_seconds=0,
                            )
                            continue
                        raise
                raise RuntimeError("no route reached")

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        class ServiceUnavailable(Exception):
            status_code = 503

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "from pro"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            deployment_id = kwargs["model_info"]["id"]
            if deployment_id == "cheap":
                raise ServiceUnavailable("service temporarily unavailable")
            if deployment_id == "plus":
                await original_sleep(60)
                return pro_stream()
            return pro_stream()

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            original_generic_function,
        )
        chunks = [chunk async for chunk in response]

        self.assertEqual(
            [call["model_info"]["id"] for call in calls],
            ["cheap", "plus", "pro"],
        )
        self.assertNotIn("_excluded_deployment_ids", calls[1])
        self.assertNotIn("_excluded_deployment_ids", calls[2])
        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "from pro"},
                {"type": "response.completed", "response": {"id": "resp-pro"}},
            ],
        )

    async def test_generic_streaming_fallback_uses_configured_first_event_timeout(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "60")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")
        sleeps = []
        original_sleep = hooks.asyncio.sleep

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        hooks.asyncio.sleep = fake_sleep
        self.addCleanup(setattr, hooks.asyncio, "sleep", original_sleep)
        router_module = types.ModuleType("litellm.router")
        calls = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                for deployment in (
                    {"id": "cheap", "order": 1},
                    {"id": "plus", "order": 2},
                    {"id": "pro", "order": 3},
                ):
                    excluded = set(kwargs.get("_excluded_deployment_ids") or [])
                    if deployment["id"] in excluded:
                        continue
                    try:
                        return await original_generic_function(
                            stream=True,
                            input=[{"role": "user", "content": "hi"}],
                            model_info={
                                "id": deployment["id"],
                                "order": deployment["order"],
                                "route_key": f"route / {deployment['id']}",
                            },
                        )
                    except Exception as exc:
                        if hooks._is_priority_deployment_failover_error(exc):
                            await hooks._sleep_before_final_route_retry(
                                model,
                                exc,
                                kwargs,
                                attempt=len(calls),
                                max_retries=5,
                                configured_delay_seconds=0,
                            )
                            continue
                        raise
                raise RuntimeError("no route reached")

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        class ServiceUnavailable(Exception):
            status_code = 503

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "from pro"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            deployment_id = kwargs["model_info"]["id"]
            if deployment_id == "cheap":
                raise ServiceUnavailable("service temporarily unavailable")
            if deployment_id == "plus":
                await original_sleep(60)
                return pro_stream()
            return pro_stream()

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            original_generic_function,
        )
        chunks = [chunk async for chunk in response]

        self.assertEqual(
            [call["model_info"]["id"] for call in calls],
            ["cheap", "plus", "pro"],
        )
        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "from pro"},
                {"type": "response.completed", "response": {"id": "resp-pro"}},
            ],
        )

    async def test_generic_responses_stream_final_route_failure_returns_failed_event(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0")
        router_module = types.ModuleType("litellm.router")
        calls = []

        class Router:
            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        class ServiceUnavailable(Exception):
            status_code = 503

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            raise ServiceUnavailable("upstream temporarily unavailable")

        class FakeRouter:
            async def aresponses(self, **_payload):
                raise AssertionError("terminal failed stream must not retry through router")

        proxy_server.llm_router = FakeRouter()
        helper_kwargs = {
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {
                "id": "failing-route",
                "order": 3,
                "route_key": "compat_provider / openai/default-chat / key=x-pro",
            },
        }

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            original_generic_function,
            **helper_kwargs,
        )
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=helper_kwargs,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual([chunk.get("type") for chunk in chunks], ["response.failed"])
        self.assertEqual(chunks[0]["response"]["status"], "failed")
        self.assertEqual(
            chunks[0]["response"]["error"]["code"],
            "upstream_route_failure",
        )
        self.assertEqual(chunks[0]["response"]["model"], "default-chat")

    async def test_generic_responses_stream_final_route_failure_advances_order_before_failed_stream(self) -> None:
        hooks, _proxy_server = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        calls = []

        deployments = [
            {
                "litellm_params": {"order": 1},
                "model_info": {
                    "id": "cheap",
                    "route_key": "compat_provider / openai/default-chat / key=x-cheap / order=1",
                },
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {
                    "id": "pro",
                    "route_key": "compat_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                excluded = set(kwargs.get("_excluded_deployment_ids") or [])
                target_order = kwargs.get("_target_order")
                candidates = [
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded
                ]
                if target_order is not None:
                    candidates = [
                        deployment
                        for deployment in candidates
                        if deployment["litellm_params"]["order"] == target_order
                    ]
                selected = candidates[0]
                call_kwargs = kwargs.copy()
                call_kwargs["model_info"] = selected["model_info"] | {
                    "order": selected["litellm_params"]["order"],
                }
                return await original_generic_function(**call_kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "from pro"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            if kwargs["model_info"]["id"] == "cheap":
                exc = TimeoutError("stream start timeout")
                exc.status_code = 504
                exc.body = {"reason": "stream_start_timeout"}
                raise exc
            return pro_stream()

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            original_generic_function,
            input=[{"role": "user", "content": "continue"}],
            stream=True,
        )
        chunks = [chunk async for chunk in response]

        self.assertEqual([call["model_info"]["id"] for call in calls], ["cheap", "pro"])
        self.assertEqual(calls[1]["_target_order"], 3)
        self.assertEqual(calls[1]["_excluded_deployment_ids"], ["cheap"])
        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "from pro"},
                {"type": "response.completed", "response": {"id": "resp-pro"}},
            ],
        )

    async def test_generic_responses_stream_final_route_failure_enters_route_recovery_poll_after_stream_fallback_marker(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.004")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0.01")
        router_module = types.ModuleType("litellm.router")
        original_calls = []
        recovery_calls = []

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                return await original_generic_function(**kwargs)

        router_module.Router = Router
        sys.modules["litellm.router"] = router_module
        hooks._install_generic_deployment_failover_patch()

        class ServiceUnavailable(Exception):
            status_code = 503

        async def original_generic_function(**kwargs):
            original_calls.append(kwargs.copy())
            raise ServiceUnavailable("upstream temporarily unavailable")

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                recovery_calls.append(payload)
                raise ServiceUnavailable("No deployments available for selected model")

        proxy_server.llm_router = FakeRouter()
        helper_kwargs = {
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "litellm_metadata": {
                hooks._STREAM_ERROR_FALLBACK_METADATA_KEY: True,
            },
            "model_info": {
                "id": "failing-route",
                "order": 3,
                "route_key": "compat_provider / openai/default-chat / key=x-pro",
            },
        }

        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "default-chat",
            original_generic_function,
            **helper_kwargs,
        )
        self.assertTrue(hooks._is_route_recovery_stream_response(response))

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=helper_kwargs,
            )
        ]

        self.assertEqual(len(original_calls), 1)
        self.assertGreaterEqual(len(recovery_calls), 1)
        self.assertTrue(
            all(
                call["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY]
                for call in recovery_calls
            )
        )
        self.assertFalse(any(isinstance(chunk, str) for chunk in chunks))
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_responses_stream_web_search_tool_without_evidence_preserves_context_in_route_recovery(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.004")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_route_recovery_poll = streaming_module._stream_route_recovery_poll
        recovery_requests = []

        class ServiceUnavailable(Exception):
            status_code = 503

        async def fake_route_recovery_poll(request_data, exception):
            recovery_requests.append(copy.deepcopy(request_data))
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp_recovered_context",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered with original context.",
                    "output": [
                        {
                            "id": "msg_recovered_context",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered with original context.",
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
        request_input = [
            {"role": "user", "content": "先调查 externalwebsearch 的失败。"},
            {"role": "assistant", "content": "已经定位到 streaming fallback。"},
            {"role": "user", "content": "继续"},
        ]
        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": request_input,
            "stream": True,
            "tools": [{"type": "web_search"}],
        }
        exception = ServiceUnavailable("upstream temporarily unavailable")
        response = hooks._route_recovery_stream_response(request_data, exception)

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=request_data,
            )
        ]

        self.assertEqual(len(recovery_requests), 1)
        self.assertEqual(recovery_requests[0]["input"], request_input)
        self.assertIsInstance(recovery_requests[0]["input"], list)
        self.assertNotIn("Original user request", json.dumps(recovery_requests[0]))
        self.assertNotIn("Retrieved evidence:", json.dumps(recovery_requests[0]))
        self.assertNotIn("external_web_search_synthesis", recovery_requests[0].get("litellm_metadata", {}))
        self.assertEqual(chunks[-1]["type"], "response.completed")
