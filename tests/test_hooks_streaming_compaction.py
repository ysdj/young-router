from __future__ import annotations

import copy

from hook_test_utils import *


class HookStreamingCompactionTests(HookTestCase):
    async def test_local_checkpoint_compaction_accepts_plain_message_output(self) -> None:
        hooks, proxy_server = load_hook_module()

        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM."
                    ),
                }
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        self.assertFalse(
            hooks._request_has_structured_codex_compaction(request_data)
        )
        self.assertTrue(hooks._request_is_codex_compaction(request_data))

        async def original_stream():
            message_item = {
                "id": "msg-checkpoint",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "Checkpoint summary.",
                        "annotations": [],
                    }
                ],
            }
            yield {
                "type": "response.created",
                "response": {"id": "resp-checkpoint", "status": "in_progress"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": message_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-checkpoint",
                    "status": "completed",
                    "output": [message_item],
                },
            }

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                raise AssertionError("plain checkpoint output must not route-fallback")

        proxy_server.llm_router = UnexpectedRouter()
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"][0]["content"][0]["text"],
            "Checkpoint summary.",
        )

    async def test_local_checkpoint_compaction_does_not_enter_route_recovery(self) -> None:
        hooks, proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM."
                    ),
                }
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        calls = []

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                raise AssertionError("checkpoint compaction must not poll routes")

        proxy_server.llm_router = UnexpectedRouter()
        failure = RuntimeError("upstream stream ended before response.completed")
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(request_data, failure)
        ]

        self.assertEqual(calls, [])
        self.assertEqual([chunk["type"] for chunk in chunks], ["response.failed"])
        self.assertEqual(
            chunks[0]["response"]["error"]["code"],
            "upstream_route_failure",
        )

    def test_structured_compaction_synthetic_failure_preserves_upstream_status(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        for status_code, error_code in (
            (500, "upstream_compaction_failure"),
            (400, "upstream_route_failure"),
        ):
            stream_event = {
                "type": "response.failed",
                "response": {
                    "status": "failed",
                    "error": {
                        "code": error_code,
                        "message": f"upstream reported HTTP {status_code}",
                    },
                },
            }
            stream_exception = hooks._stream_chunk_error_exception(stream_event)
            self.assertIsNotNone(stream_exception)
            self.assertEqual(
                getattr(stream_exception, "status_code", None),
                status_code,
            )
            self.assertEqual(
                getattr(stream_exception, "body", {}).get("code"),
                error_code,
            )

            upstream_exception = RuntimeError(f"upstream compaction HTTP {status_code}")
            upstream_exception.status_code = status_code
            event = jsonable_stream_chunk(
                hooks._synthesized_failed_response_event(
                    request_data,
                    upstream_exception,
                )
            )
            error = event["response"]["error"]
            self.assertEqual(error["code"], "upstream_compaction_failure")
            self.assertIn(f"HTTP {status_code}", error["message"])

            incomplete = hooks._responses_incomplete_stream_exception(
                "terminal response event before response.completed",
                buffer=[event],
                request_data=request_data,
            )
            self.assertEqual(
                getattr(incomplete, "status_code", None),
                status_code,
            )

    def test_structured_compaction_stream_read_error_does_not_invent_502(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        terminal = {
            "type": "error",
            "error": {
                "type": "upstream_error",
                "code": "stream_read_error",
                "message": "stream_read_error",
                "detail": "upstream stream ended before response.completed",
            },
        }

        incomplete = hooks._responses_incomplete_stream_exception(
            "stream ended before response.completed",
            buffer=[terminal],
            request_data=request_data,
        )

        self.assertIsNone(getattr(incomplete, "status_code", None))
        self.assertTrue(hooks._is_priority_deployment_failover_error(incomplete))
        self.assertEqual(
            hooks._trace_exception(incomplete)["reason"],
            "upstream-stream-incomplete",
        )
        self.assertEqual(
            hooks._recovery_policy_for_exception(incomplete),
            hooks._RECOVERY_POLICY_COOLDOWN,
        )
        failed = jsonable_stream_chunk(
            hooks._synthesized_failed_response_event(request_data, incomplete)
        )
        error = failed["response"]["error"]
        self.assertEqual(error["code"], "upstream_compaction_failure")
        self.assertNotIn("HTTP 502", error["message"])
        self.assertNotIn("HTTP", error["message"])

    async def test_structured_compaction_forwarded_terminal_failure_is_not_retried(self) -> None:
        hooks, proxy_server = load_hook_module()

        async def failed_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-failed", "status": "in_progress", "output": []},
            }
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-failed",
                    "status": "failed",
                    "error": {
                        "type": "server_error",
                        "code": "upstream_compaction_failure",
                        "message": "upstream reported HTTP 502",
                    },
                },
            }

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                raise AssertionError("terminal compaction failure must not start a fresh retry")

        proxy_server.llm_router = UnexpectedRouter()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=failed_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual([chunk["type"] for chunk in chunks], ["response.created", "response.failed"])
        self.assertEqual(
            chunks[-1]["response"]["error"]["code"],
            "upstream_compaction_failure",
        )

    async def test_structured_compaction_stream_http_failure_uses_one_native_route_fallback(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def failed_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-original", "status": "in_progress", "output": []},
            }
            error = RuntimeError("upstream returned HTTP 502")
            error.status_code = 502
            raise error

        async def recovered_stream():
            compaction_item = {
                "id": "cmp-recovered",
                "type": "compaction",
                "encrypted_content": "encrypted-recovered-summary",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered",
                    "status": "completed",
                    "output": [compaction_item],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(copy.deepcopy(payload))
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "thread_id": "thread-compaction-http-fallback",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=failed_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"][0]["encrypted_content"],
            "encrypted-recovered-summary",
        )

    async def test_structured_compaction_does_not_hop_after_timeout_fallback(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        deployments = [
            {
                "litellm_params": {"model": "openai/default-chat", "order": 1},
                "model_info": {"id": "compaction-peer-one", "order": 1},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 1},
                "model_info": {"id": "compaction-peer-two", "order": 1},
            },
        ]

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            error = RuntimeError("upstream returned HTTP 502")
            error.status_code = 502
            raise error

        async def incomplete_fallback_stream():
            yield {"type": "response.created", "response": {"id": "resp-fallback"}}
            error = TimeoutError("upstream stream idle timeout")
            error.status_code = 504
            error.body = {"reason": "stream_idle_timeout"}
            raise error

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def aresponses(self, **payload):
                calls.append(copy.deepcopy(payload))
                excluded_ids = set(payload.get("_excluded_deployment_ids") or [])
                selected = next(
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded_ids
                )
                hooks._remember_selected_deployment(selected)
                return incomplete_fallback_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "model_info": {
                "id": "compaction-primary",
                "order": 1,
                "route_key": "compat_provider / openai/default-chat / key=primary",
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual([chunk["type"] for chunk in chunks], ["response.failed"])
        self.assertEqual(
            chunks[-1]["response"]["error"]["code"],
            "upstream_compaction_failure",
        )

    async def test_structured_compaction_done_item_clean_eof_synthesizes_terminal_event(self) -> None:
        hooks, proxy_server = load_hook_module()

        async def original_stream():
            response = {
                "id": "resp-original",
                "object": "response",
                "status": "in_progress",
                "output": [],
            }
            compaction = {
                "id": "cmp-complete",
                "type": "compaction",
                "encrypted_content": "encrypted-summary",
            }
            yield {"type": "response.created", "response": response}
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": compaction,
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction,
            }

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                raise AssertionError("completed compaction item must not be retried")

        proxy_server.llm_router = UnexpectedRouter()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "thread_id": "thread-compaction-eof",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"][0],
            {
                "id": "cmp-complete",
                "type": "compaction",
                "encrypted_content": "encrypted-summary",
            },
        )

    async def test_structured_compaction_done_item_idle_timeout_synthesizes_terminal_event(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")

        async def stalled_after_done_stream():
            compaction = {
                "id": "cmp-stalled-terminal",
                "type": "compaction",
                "encrypted_content": "encrypted-summary",
            }
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp-stalled-terminal",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction,
            }
            await asyncio.sleep(1)

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                raise AssertionError("completed compaction item must not be retried")

        proxy_server.llm_router = UnexpectedRouter()
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "thread_id": "thread-compaction-stalled-terminal",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=stalled_after_done_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["output"][0]["type"], "compaction")

    async def test_structured_compaction_completed_item_is_not_treated_as_empty(self) -> None:
        hooks, proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        completed = {
            "type": "response.completed",
            "response": {
                "id": "resp-encrypted-completed",
                "object": "response",
                "status": "completed",
                "output": [
                    {
                        "id": "cmp-encrypted-completed",
                        "type": "compaction",
                        "encrypted_content": "encrypted-summary",
                    }
                ],
            },
        }

        self.assertFalse(
            hooks._responses_completed_chunk_has_usable_output(
                completed,
                {"input": [{"role": "user", "content": "ordinary request"}]},
            )
        )
        self.assertTrue(
            hooks._responses_completed_chunk_has_usable_output(completed, request_data)
        )
        self.assertFalse(
            hooks._responses_completed_chunk_has_usable_output(
                {
                    "type": "response.completed",
                    "output": completed["response"]["output"],
                },
                request_data,
            )
        )
        retained_and_compacted = copy.deepcopy(completed)
        retained_and_compacted["response"]["output"].insert(
            0,
            {
                "id": "msg-retained",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "retained"}],
            },
        )
        self.assertTrue(
            hooks._responses_completed_chunk_has_usable_output(
                retained_and_compacted,
                request_data,
            )
        )
        duplicate_compaction = copy.deepcopy(completed)
        duplicate_compaction["response"]["output"].append(
            {
                "id": "cmp-duplicate",
                "type": "compaction",
                "encrypted_content": "encrypted-duplicate-summary",
            }
        )
        self.assertFalse(
            hooks._responses_completed_chunk_has_usable_output(
                duplicate_compaction,
                request_data,
            )
        )
        duplicate_malformed_compaction = copy.deepcopy(completed)
        duplicate_malformed_compaction["response"]["output"].append(
            {"id": "cmp-malformed", "type": "compaction"}
        )
        self.assertFalse(
            hooks._responses_completed_chunk_has_usable_output(
                duplicate_malformed_compaction,
                request_data,
            )
        )

        async def original_stream():
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp-encrypted-completed",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield completed

        class UnexpectedRouter:
            async def aresponses(self, **payload):
                raise AssertionError("completed compaction response must not be retried")

        proxy_server.llm_router = UnexpectedRouter()
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(chunks[-1], completed)

    async def test_structured_compaction_retries_one_message_false_success(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        async def original_stream():
            message_item = {
                "id": "msg-false-success",
                "type": "message",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "text": "summary text"}
                ],
            }
            yield {
                "type": "response.created",
                "response": {"id": "resp-false-success", "status": "in_progress"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": message_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-false-success",
                    "status": "completed",
                    "output": [message_item],
                },
            }

        async def recovered_stream():
            compaction_item = {
                "id": "cmp-recovered",
                "type": "compaction",
                "encrypted_content": "encrypted-recovered-summary",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered",
                    "status": "completed",
                    "output": [compaction_item],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("resp-false-success", json.dumps(chunks))
        self.assertNotIn("msg-false-success", json.dumps(chunks))
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"], [
                {
                    "id": "cmp-recovered",
                    "type": "compaction",
                    "encrypted_content": "encrypted-recovered-summary",
                }
            ],
        )

    async def test_structured_compaction_route_recovery_buffers_message_false_success(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        false_success_item = {
            "id": "msg-route-false-success",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "summary text"}],
        }

        async def recovery_stream():
            yield {"type": "response.output_text.delta", "delta": "summary text"}
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": false_success_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-route-false-success",
                    "status": "completed",
                    "output": [false_success_item],
                },
            }

        async def recovery_round(*_args, **_kwargs):
            async for chunk in recovery_stream():
                yield chunk

        hooks._stream_streaming_error_fallback_round = recovery_round
        exception = RuntimeError("retry structured compaction")
        exception.status_code = 502

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll_attempt(
                request_data,
                exception,
                attempt=1,
            )
        ]

        self.assertEqual(chunks, [])

    async def test_route_recovery_accepts_compaction_done_item_at_clean_eof(self) -> None:
        hooks, _proxy_server = load_hook_module()

        async def recovered_stream_round(
            request_data,
            exception,
            *,
            allow_repeated_attempt=False,
            route_recovery_poll=False,
        ):
            compaction = {
                "id": "cmp-recovery-eof",
                "type": "compaction",
                "encrypted_content": "encrypted-recovery-summary",
            }
            yield {
                "type": "response.created",
                "response": {
                    "id": "resp-recovery-eof",
                    "object": "response",
                    "status": "in_progress",
                    "output": [],
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction,
            }

        hooks._stream_streaming_error_fallback_round = recovered_stream_round
        request_data = {
            "model": "default-chat",
            "input": [
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "client_metadata": {
                "thread_id": "thread-compaction-recovery-eof",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        original_exception = RuntimeError("upstream stream ended early")
        original_exception.status_code = 502

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll_attempt(
                request_data,
                original_exception,
                attempt=1,
            )
        ]

        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"][0]["encrypted_content"],
            "encrypted-recovery-summary",
        )

    async def test_compaction_done_item_does_not_hide_explicit_failed_terminal(self) -> None:
        hooks, _proxy_server = load_hook_module()

        async def failed_stream_round(*_args, **_kwargs):
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "cmp-before-failure",
                    "type": "compaction",
                    "encrypted_content": "encrypted-but-failed",
                },
            }
            yield {
                "type": "response.failed",
                "response": {
                    "id": "resp-explicit-failure",
                    "status": "failed",
                    "error": {"message": "explicit upstream failure"},
                },
            }

        hooks._stream_streaming_error_fallback_round = failed_stream_round
        request_data = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        original_exception = RuntimeError("retry compaction")
        original_exception.status_code = 502

        with self.assertRaises(RuntimeError) as captured:
            _ = [
                chunk
                async for chunk in hooks._stream_route_recovery_poll_attempt(
                    request_data,
                    original_exception,
                    attempt=1,
                )
            ]
        self.assertIn("response.failed", str(captured.exception))

    async def test_codex_compaction_incomplete_responses_stream_retries_responses_route(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "compact ok"}
            yield {"type": "response.completed", "response": {"id": "resp-fallback"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                }
            ],
            "stream": True,
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
            },
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_PREEMPTIVE_METADATA_KEY, metadata)
        self.assertEqual(jsonable_stream_chunk(chunks[-1])["type"], "response.completed")

    async def test_codex_compaction_streaming_retry_preserves_native_request_shape(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        client_metadata = {
            "thread_id": "thread-test-0001",
            "session_id": "thread-test-0001",
            "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            "x-codex-window-id": "thread-test-0001:7",
        }

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            compaction_item = {
                "id": "cmp-native-shape",
                "type": "compaction",
                "encrypted_content": "encrypted-native-shape",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-fallback",
                    "status": "completed",
                    "output": [compaction_item],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 20000 tokens. Preserve only unresolved work."
                    ),
                },
                {"type": "compaction_trigger", "id": "compact-native-shape"},
            ],
            "stream": True,
            "reasoning": {"effort": "medium"},
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "client_metadata": client_metadata,
            "prompt_cache_key": "thread-test-0001",
            "extra_headers": {"X-Trace": "keep-me"},
            "proxy_server_request": {
                "headers": {
                    "accept": "text/event-stream",
                    "originator": "Codex Desktop",
                    "session-id": "thread-test-0001",
                    "thread-id": "thread-test-0001",
                    "user-agent": "Codex Desktop/0.142.3",
                    "x-client-request-id": "thread-test-0001",
                    "x-codex-beta-features": "remote_compaction_v2",
                    "x-codex-turn-metadata": '{"request_kind":"compaction"}',
                    "x-codex-window-id": "thread-test-0001:7",
                    "x-openai-internal-codex-responses-lite": "true",
                }
            },
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
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

        self.assertEqual(len(calls), 1)
        self.assertNotIn("max_output_tokens", calls[0])
        self.assertEqual(calls[0]["reasoning"], {"effort": "medium"})
        self.assertEqual(calls[0]["tools"], [])
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertFalse(calls[0]["parallel_tool_calls"])
        self.assertEqual(calls[0]["client_metadata"], client_metadata)
        self.assertEqual(calls[0]["extra_body"]["client_metadata"], client_metadata)
        self.assertEqual(calls[0]["prompt_cache_key"], "thread-test-0001")
        headers = {key.lower(): value for key, value in calls[0]["extra_headers"].items()}
        self.assertEqual(headers["x-trace"], "keep-me")
        self.assertEqual(headers["accept"], "text/event-stream")
        self.assertEqual(headers["originator"], "Codex Desktop")
        self.assertEqual(headers["session-id"], "thread-test-0001")
        self.assertEqual(headers["thread-id"], "thread-test-0001")
        self.assertEqual(headers["user-agent"], "Codex Desktop/0.142.3")
        self.assertEqual(headers["x-client-request-id"], "thread-test-0001")
        self.assertEqual(headers["x-codex-beta-features"], "remote_compaction_v2")
        self.assertEqual(headers["x-codex-turn-metadata"], '{"request_kind":"compaction"}')
        self.assertEqual(headers["accept-encoding"], "identity")
        self.assertEqual(
            headers["x-openai-internal-codex-responses-lite"],
            "true",
        )
        self.assertEqual(headers["x-codex-window-id"], "thread-test-0001:7")
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn("codex_compaction_optimized", metadata)
        self.assertNotIn("codex_compaction_max_output_tokens", metadata)
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_codex_compaction_generic_peer_then_streaming_fallback_preserves_state(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._SAME_DEPLOYMENT_RETRIES_ENV, "0")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0")
        router_module = types.ModuleType("litellm.router")
        generic_calls = []
        generic_helper_state_ids = []
        generic_call_state_ids = []
        streaming_calls = []
        client_metadata = {
            "thread_id": "thread-fallback-state",
            "session_id": "thread-fallback-state",
            "x-codex-turn-metadata": '{"request_kind":"compaction"}',
        }
        request_input = [
            {
                "type": "additional_tools",
                "role": "developer",
                "tools": [
                    {"type": "custom", "name": "exec"},
                    {"type": "custom", "name": "read"},
                ],
            },
            {"type": "message", "role": "user", "content": "history"},
            {"type": "compaction_trigger", "id": "compact-now"},
        ]
        tools = [
            {"type": "custom", "name": "exec"},
            {"type": "custom", "name": "read"},
        ]
        deployments = [
            {
                "litellm_params": {"model": "openai/default-chat", "order": 1},
                "model_info": {"id": "route-primary", "order": 1},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 1},
                "model_info": {"id": "route-peer", "order": 1},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 1},
                "model_info": {"id": "route-streaming", "order": 1},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 1},
                "model_info": {"id": "route-final", "order": 1},
            },
        ]

        class UpstreamError(Exception):
            status_code = 500

        async def peer_incomplete_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-peer", "status": "in_progress"},
            }

        async def streaming_incomplete_stream():
            yield {
                "type": "response.created",
                "response": {"id": "resp-streaming", "status": "in_progress"},
            }

        async def final_stream():
            compaction_item = {
                "id": "cmp-final",
                "type": "compaction",
                "encrypted_content": "encrypted-final",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-final",
                    "status": "completed",
                    "output": [compaction_item],
                },
            }

        class GenericRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            def _update_kwargs_with_deployment(
                self,
                deployment,
                kwargs,
                function_name=None,
            ):
                kwargs["model_info"] = deployment["model_info"] | {
                    "order": deployment["litellm_params"]["order"],
                }

            async def make_call(self, original_function, *args, **kwargs):
                response = original_function(*args, **kwargs)
                if hasattr(response, "__await__"):
                    response = await response
                return response

            async def _ageneric_api_call_with_fallbacks_helper(
                self,
                model,
                original_generic_function,
                **kwargs,
            ):
                generic_helper_state_ids.append(id(kwargs))
                excluded_ids = set(kwargs.get("_excluded_deployment_ids") or [])
                selected = next(
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded_ids
                )
                self._update_kwargs_with_deployment(
                    selected,
                    kwargs,
                    function_name="ageneric_api_call_with_fallbacks",
                )
                return await original_generic_function(**kwargs)

        router_module.Router = GenericRouter
        sys.modules["litellm.router"] = router_module
        hooks._install_selected_deployment_marker_patch()
        hooks._install_generic_deployment_failover_patch()

        class StreamingRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def aresponses(self, **payload):
                streaming_calls.append(copy.deepcopy(payload))
                excluded_ids = set(payload.get("_excluded_deployment_ids") or [])
                selected = next(
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded_ids
                )
                hooks._remember_selected_deployment(selected)
                if selected["model_info"]["id"] == "route-streaming":
                    return streaming_incomplete_stream()
                if selected["model_info"]["id"] != "route-final":
                    raise AssertionError("unexpected streaming fallback deployment")
                return final_stream()

        proxy_server.llm_router = StreamingRouter()

        async def original_generic_function(**kwargs):
            generic_call_state_ids.append(id(kwargs))
            generic_calls.append(copy.deepcopy(kwargs))
            deployment_id = kwargs["model_info"]["id"]
            if deployment_id == "route-primary":
                raise UpstreamError("upstream returned HTTP 500")
            self.assertEqual(deployment_id, "route-peer")
            return peer_incomplete_stream()

        initial_request = {
            "input": request_input,
            "stream": True,
            "service_tier": "priority",
            "tools": tools,
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "client_metadata": client_metadata,
            "extra_headers": {"X-Trace": "keep-me"},
            "litellm_metadata": {
                "codex_fast_default_service_tier": "priority",
            },
            "model_info": {
                "id": "route-peer",
                "order": 1,
                "route_key": "provider / openai/default-chat / key=peer / order=1",
            },
        }
        request_data = copy.deepcopy(initial_request)
        request_data["model"] = "default-chat"
        router = GenericRouter()
        response = await router.make_call(
            router._ageneric_api_call_with_fallbacks_helper,
            "default-chat",
            original_generic_function,
            **initial_request,
        )
        self.assertNotIn("_excluded_deployment_ids", request_data)
        response_marker = hooks._selected_deployment_marker_from_response(response)
        self.assertIsNotNone(response_marker)
        assert response_marker is not None
        self.assertEqual(
            response_marker["_excluded_deployment_ids"],
            ["route-primary"],
        )
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=request_data,
            )
        ]

        self.assertEqual(
            [call["model_info"]["id"] for call in generic_calls],
            ["route-primary", "route-peer"],
        )
        self.assertEqual(len(generic_helper_state_ids), 2)
        self.assertEqual(len(generic_call_state_ids), 2)
        self.assertTrue(
            all(
                helper_state_id != call_state_id
                for helper_state_id, call_state_id in zip(
                    generic_helper_state_ids,
                    generic_call_state_ids,
                )
            )
        )
        self.assertEqual(
            generic_calls[1]["_excluded_deployment_ids"],
            ["route-primary"],
        )
        self.assertEqual(len(streaming_calls), 2)
        first_retry, second_retry = streaming_calls
        self.assertEqual(first_retry["service_tier"], "priority")
        self.assertEqual(
            first_retry["_excluded_deployment_ids"],
            ["route-peer", "route-primary"],
        )
        self.assertEqual(
            second_retry["_excluded_deployment_ids"],
            ["route-peer", "route-primary", "route-streaming"],
        )
        for retry in streaming_calls:
            self.assertEqual(retry["service_tier"], "priority")
            self.assertEqual(retry["input"], request_input)
            self.assertEqual(retry["client_metadata"], client_metadata)
            self.assertEqual(retry["tools"], tools)
            self.assertEqual(retry["tool_choice"], "auto")
            self.assertFalse(retry["parallel_tool_calls"])
            self.assertEqual(retry["extra_headers"]["X-Trace"], "keep-me")
        self.assertEqual(
            request_data["_excluded_deployment_ids"],
            ["route-peer", "route-primary", "route-streaming"],
        )
        self.assertNotIn("response.failed", [chunk["type"] for chunk in chunks])
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            chunks[-1]["response"]["output"],
            [
                {
                    "id": "cmp-final",
                    "type": "compaction",
                    "encrypted_content": "encrypted-final",
                }
            ],
        )

    def test_streaming_fallback_only_restores_injected_default_service_tier(self) -> None:
        hooks, _proxy_server = load_hook_module()
        base_request = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "continue"}],
            "stream": True,
        }

        non_codex_payload = hooks._build_streaming_error_fallback_payload(
            base_request,
            method_name="aresponses",
        )
        self.assertIsNotNone(non_codex_payload)
        assert non_codex_payload is not None
        self.assertNotIn("service_tier", non_codex_payload)

        explicit_standard_payload = hooks._build_streaming_error_fallback_payload(
            {
                **base_request,
                "service_tier": "standard",
                "litellm_metadata": {
                    "codex_fast_default_service_tier": "priority",
                },
            },
            method_name="aresponses",
        )
        self.assertIsNotNone(explicit_standard_payload)
        assert explicit_standard_payload is not None
        self.assertEqual(explicit_standard_payload["service_tier"], "standard")

    async def test_structured_codex_compaction_failure_stops_after_bounded_fallback(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def incomplete_fallback_stream():
            yield {"type": "response.created", "response": {"id": "resp-fallback"}}

        async def recovered_stream():
            compaction_item = {
                "id": "cmp-route-recovered",
                "type": "compaction",
                "encrypted_content": "encrypted-route-recovered",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered",
                    "status": "completed",
                    "output": [compaction_item],
                },
            }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {
                            "model": "openai/default-chat",
                            "order": 1,
                        },
                        "model_info": {
                            "id": "third-party-large",
                            "order": 1,
                        },
                    }
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                if len(calls) > 1:
                    return recovered_stream()
                return incomplete_fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [{"type": "custom", "name": "exec"}],
                },
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                },
                {"type": "compaction_trigger", "id": "compact-now"},
            ],
            "stream": True,
            "tools": [],
            "tool_choice": {"type": "custom", "name": "exec"},
            "parallel_tool_calls": False,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
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

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        for call in calls:
            self.assertNotIn("use_chat_completions_api", call)
            self.assertEqual(call["input"][0]["type"], "additional_tools")
            self.assertEqual(
                call["tool_choice"],
                {"type": "custom", "name": "exec"},
            )
            self.assertFalse(call["parallel_tool_calls"])
        self.assertEqual([chunk.get("type") for chunk in chunks], ["response.failed"])
        self.assertEqual(
            chunks[-1]["response"]["error"]["code"],
            "upstream_compaction_failure",
        )

    def test_structured_compaction_disables_long_recovery_window(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "43200")
        ordinary_codex_request = {
            "call_type": "aresponses",
            "input": [{"role": "user", "content": "continue"}],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
        }
        structured_compaction_request = {
            **ordinary_codex_request,
            "input": [
                {"role": "user", "content": "continue"},
                {"type": "compaction_trigger"},
            ],
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        self.assertEqual(
            hooks._recovery_max_seconds_for_request(ordinary_codex_request),
            43200.0,
        )
        self.assertEqual(
            hooks._recovery_max_seconds_for_request(structured_compaction_request),
            0.0,
        )
        error = RuntimeError("temporary upstream rate limit")
        error.status_code = 429
        self.assertFalse(
            hooks._should_return_route_recovery_stream(
                error,
                structured_compaction_request,
            )
        )


    async def test_codex_compaction_rate_limit_still_enters_route_recovery_poll(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "compact recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                }
            ],
            "stream": True,
            "prompt_cache_key": "thread-rate-limit-kept",
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
            },
        }
        first_exception = RuntimeError("upstream returned too many requests; rate limit exceeded")
        first_exception.status_code = 429

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual(calls[0]["prompt_cache_key"], "thread-rate-limit-kept")
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "compact recovered"},
            chunks,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["id"], "resp-recovered")

    async def test_structured_compaction_recovery_does_not_replay_signed_history(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            compaction_item = {
                "id": "cmp-recovered",
                "type": "compaction_summary",
                "encrypted_content": "opaque-encrypted-summary",
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered",
                    "status": "completed",
                    "output": [compaction_item],
                },
            }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(copy.deepcopy(payload))
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        tools = [
            {"type": "custom", "name": "exec", "description": "run a command"},
            {"type": "function", "name": "wait", "parameters": {"type": "object"}},
            {
                "type": "namespace",
                "name": "collaboration",
                "tools": [{"type": "function", "name": "list_agents"}],
            },
        ]
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": tools,
                },
                {"type": "message", "role": "user", "content": "history"},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "prompt_cache_key": "thread-compaction-failing-cache",
            "extra_body": {
                "prompt_cache_key": "thread-compaction-failing-cache",
                "client_metadata": {"thread_id": "thread-compaction-cache-recovery"},
            },
            "client_metadata": {
                "thread_id": "thread-compaction-cache-recovery",
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
            },
        }
        original_request = copy.deepcopy(request_data)
        first_exception = RuntimeError("temporary upstream server error")
        first_exception.status_code = 500

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(calls, [])
        self.assertEqual(
            request_data["prompt_cache_key"],
            "thread-compaction-failing-cache",
        )
        self.assertEqual(
            request_data["extra_body"]["prompt_cache_key"],
            "thread-compaction-failing-cache",
        )
        self.assertEqual(request_data["input"], original_request["input"])
        self.assertEqual(chunks[-1]["type"], "response.failed")
        self.assertEqual(
            chunks[-1]["response"]["error"]["code"],
            "upstream_compaction_failure",
        )

    async def test_codex_compaction_recovery_restores_model_from_metadata(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "compact recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "input": [
                {
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM that will resume the task."
                    ),
                }
            ],
            "stream": True,
            "reasoning": {"effort": "xhigh"},
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "litellm_metadata": {"model_group": "default-chat"},
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        }
        first_exception = RuntimeError("upstream returned too many requests; rate limit exceeded")
        first_exception.status_code = 429

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual(calls[0]["tools"], [])
        self.assertEqual(calls[0]["tool_choice"], "auto")
        self.assertFalse(calls[0]["parallel_tool_calls"])
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["id"], "resp-recovered")

    async def test_codex_compaction_incomplete_fallback_stream_returns_failed_terminal_event_when_recovery_disabled(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0")

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            yield {"type": "response.created", "response": {"id": "resp-fallback"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                }
            ],
            "stream": True,
            "model_info": {
                "id": "third-party-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
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

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual([chunk.get("type") for chunk in chunks], ["response.failed"])
        self.assertEqual(chunks[-1]["response"]["status"], "failed")
        self.assertNotIn("resp-fallback", json.dumps(chunks))

    async def test_codex_compaction_responses_endpoint_unsupported_stays_native(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "compact ok"}
            yield {"type": "response.completed", "response": {"id": "resp-fallback"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                }
            ],
            "stream": True,
            "model_info": {
                "id": "chat-only-large",
                "provider": "compat_provider",
                "route_key": "compat_provider / openai/default-chat / key=x-plus",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat"],
            },
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY, metadata)
        self.assertEqual(jsonable_stream_chunk(chunks[-1])["type"], "response.completed")

    async def test_codex_compaction_route_recovery_preserves_stream_selected_deployment(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class BalanceError(Exception):
            status_code = 403

        async def compat_provider_incomplete_stream():
            yield {"type": "response.created", "response": {"id": "resp-compat_provider"}}

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "compact recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "backup_provider-x-plus",
                    "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "compat_provider-r-plus",
                    "route_key": "compat_provider / openai/default-chat / key=r-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {
                    "id": "backup_provider-x-pro",
                    "route_key": "backup_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def aresponses(self, **payload):
                excluded = set(payload.get("_excluded_deployment_ids") or [])
                candidates = [
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded
                ]
                target_order = payload.get("_target_order")
                if target_order is not None:
                    candidates = [
                        deployment
                        for deployment in candidates
                        if deployment["litellm_params"]["order"] == target_order
                    ]
                selected = candidates[0]
                hooks._remember_selected_deployment(selected)
                payload["model_info"] = selected["model_info"] | {
                    "order": selected["litellm_params"]["order"],
                }
                calls.append(payload.copy())
                if selected["model_info"]["id"] == "backup_provider-x-plus":
                    raise BalanceError("insufficient account balance")
                if selected["model_info"]["id"] == "compat_provider-r-plus":
                    return compat_provider_incomplete_stream()
                return pro_stream()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM."
                    ),
                }
            ],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "model_info": {
                "id": "backup_provider-x-plus",
                "order": 2,
                "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
            },
        }
        first_exception = BalanceError("insufficient account balance")
        first_exception.failed_deployment_id = "backup_provider-x-plus"
        first_exception.failed_deployment_order = 2

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        selected_ids = [call.get("model_info", {}).get("id") for call in calls]
        self.assertEqual(selected_ids, ["compat_provider-r-plus", "backup_provider-x-pro"])
        self.assertEqual(calls[1]["_target_order"], 3)
        self.assertEqual(
            calls[1]["_excluded_deployment_ids"],
            ["backup_provider-x-plus", "compat_provider-r-plus"],
        )
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["id"], "resp-pro")

    async def test_codex_compaction_route_recovery_wraps_to_lower_order_after_last_order_failure(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        selected_ids = []
        wrapped_to_lower_order = False

        class UpstreamError(Exception):
            status_code = 500

        class StopProbe(Exception):
            pass

        deployments = [
            {
                "litellm_params": {"order": 1},
                "model_info": {
                    "id": "openai-base",
                    "route_key": "openai / openai/default-chat / key=base / order=1",
                },
            },
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "backup_provider-x-plus",
                    "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "compat_provider-r-plus",
                    "route_key": "compat_provider / openai/default-chat / key=r-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {
                    "id": "backup_provider-x-pro",
                    "route_key": "backup_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]
        all_deployment_ids = {
            deployment["model_info"]["id"] for deployment in deployments
        }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def aresponses(self, **payload):
                nonlocal wrapped_to_lower_order
                calls.append(payload.copy())
                excluded = set(payload.get("_excluded_deployment_ids") or [])
                candidates = [
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded
                ]
                target_order = payload.get("_target_order")
                if target_order is not None:
                    candidates = [
                        deployment
                        for deployment in candidates
                        if deployment["litellm_params"]["order"] == target_order
                    ]
                selected = candidates[0]
                selected_id = selected["model_info"]["id"]
                if selected_id == "openai-base":
                    wrapped_to_lower_order = True
                    raise StopProbe("route recovery wrapped to lower order")
                selected_ids.append(selected_id)
                hooks._remember_selected_deployment(selected)
                payload["model_info"] = selected["model_info"] | {
                    "order": selected["litellm_params"]["order"],
                }
                raise UpstreamError(f"upstream 500 from {selected_id}")

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM."
                    ),
                }
            ],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "model_info": {
                "id": "backup_provider-x-plus",
                "order": 2,
                "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
            },
        }
        first_exception = UpstreamError("upstream 500 from backup_provider-x-plus")
        first_exception.failed_deployment_id = "backup_provider-x-plus"
        first_exception.failed_deployment_order = 2

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(selected_ids, ["compat_provider-r-plus", "backup_provider-x-pro"])
        self.assertEqual(calls[0]["_target_order"], 2)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["backup_provider-x-plus"])
        self.assertEqual(calls[1]["_target_order"], 3)
        self.assertEqual(
            calls[1]["_excluded_deployment_ids"],
            ["backup_provider-x-plus", "compat_provider-r-plus"],
        )
        self.assertEqual(calls[2]["_target_order"], 1)
        self.assertEqual(
            calls[2]["_excluded_deployment_ids"],
            ["backup_provider-x-plus", "backup_provider-x-pro", "compat_provider-r-plus"],
        )
        self.assertTrue(wrapped_to_lower_order)
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_codex_compaction_route_recovery_refreshes_after_no_deployments_poll(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        selected_ids = []
        saw_all_excluded = False
        refreshed_with_lower_order = False

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "compact recovered after route refresh"}
            yield {"type": "response.completed", "response": {"id": "resp-compaction-refreshed"}}

        class UpstreamError(Exception):
            status_code = 500

        class NoDeploymentsError(Exception):
            pass

        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "backup_provider-x-plus",
                    "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "compat_provider-r-plus",
                    "route_key": "compat_provider / openai/default-chat / key=r-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {
                    "id": "backup_provider-x-pro",
                    "route_key": "backup_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]
        all_deployment_ids = {
            deployment["model_info"]["id"] for deployment in deployments
        }

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def aresponses(self, **payload):
                nonlocal saw_all_excluded, refreshed_with_lower_order
                calls.append(payload.copy())
                excluded = set(payload.get("_excluded_deployment_ids") or [])
                if all_deployment_ids <= excluded:
                    saw_all_excluded = True
                    raise NoDeploymentsError("No deployments available for requested model")
                candidates = [
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded
                ]
                target_order = payload.get("_target_order")
                if target_order is not None:
                    candidates = [
                        deployment
                        for deployment in candidates
                        if deployment["litellm_params"]["order"] == target_order
                    ]
                selected = candidates[0]
                if saw_all_excluded and selected["litellm_params"]["order"] < 3:
                    refreshed_with_lower_order = True
                selected_id = selected["model_info"]["id"]
                selected_ids.append(selected_id)
                hooks._remember_selected_deployment(selected)
                payload["model_info"] = selected["model_info"] | {
                    "order": selected["litellm_params"]["order"],
                }
                if saw_all_excluded:
                    return recovered_stream()
                raise UpstreamError(f"upstream 500 from {selected_id}")

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM."
                    ),
                }
            ],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "model_info": {
                "id": "backup_provider-x-plus",
                "order": 2,
                "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
            },
        }
        first_exception = UpstreamError("upstream 500 from backup_provider-x-plus")
        first_exception.failed_deployment_id = "backup_provider-x-plus"
        first_exception.failed_deployment_order = 2

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(selected_ids, ["compat_provider-r-plus", "backup_provider-x-pro", "backup_provider-x-plus"])
        self.assertTrue(saw_all_excluded)
        self.assertTrue(refreshed_with_lower_order)
        self.assertTrue(all_deployment_ids <= set(calls[-2].get("_excluded_deployment_ids") or []))
        self.assertNotIn("_excluded_deployment_ids", calls[-1])
        self.assertEqual(calls[-1].get("_target_order"), 2)
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "compact recovered after route refresh"},
            chunks,
        )

    async def test_codex_compaction_route_recovery_captures_async_selected_deployment_box(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        selected_ids = []

        class UpstreamError(Exception):
            status_code = 500

        async def failed_stream():
            yield {"type": "response.created", "response": {"id": "resp-compat_provider"}}
            yield {"type": "response.failed", "response": {"id": "resp-compat_provider", "status": "failed"}}

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "compact recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "backup_provider-x-plus",
                    "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 2},
                "model_info": {
                    "id": "compat_provider-r-plus",
                    "route_key": "compat_provider / openai/default-chat / key=r-plus / order=2",
                },
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {
                    "id": "backup_provider-x-pro",
                    "route_key": "backup_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                excluded = set(payload.get("_excluded_deployment_ids") or [])
                candidates = [
                    deployment
                    for deployment in deployments
                    if deployment["model_info"]["id"] not in excluded
                ]
                target_order = payload.get("_target_order")
                if target_order is not None:
                    candidates = [
                        deployment
                        for deployment in candidates
                        if deployment["litellm_params"]["order"] == target_order
                    ]
                selected = candidates[0]
                selected_id = selected["model_info"]["id"]
                async def selected_stream():
                    selected_ids.append(selected_id)
                    hooks._remember_selected_deployment(selected)
                    stream = failed_stream() if selected_id == "compat_provider-r-plus" else pro_stream()
                    async for chunk in stream:
                        yield chunk

                return selected_stream()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "You are performing a CONTEXT CHECKPOINT COMPACTION. "
                        "Create a handoff summary for another LLM."
                    ),
                }
            ],
            "stream": True,
            "tools": [],
            "tool_choice": "auto",
            "parallel_tool_calls": False,
            "model_info": {
                "id": "backup_provider-x-plus",
                "order": 2,
                "route_key": "backup_provider / openai/default-chat / key=x-plus / order=2",
            },
        }
        first_exception = UpstreamError("upstream 500 from backup_provider-x-plus")
        first_exception.failed_deployment_id = "backup_provider-x-plus"
        first_exception.failed_deployment_order = 2

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(selected_ids, ["compat_provider-r-plus", "backup_provider-x-pro"])
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["backup_provider-x-plus"])
        self.assertEqual(calls[1]["_target_order"], 3)
        self.assertEqual(
            calls[1]["_excluded_deployment_ids"],
            ["backup_provider-x-plus", "compat_provider-r-plus"],
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["id"], "resp-pro")

    async def test_direct_openai_compaction_incomplete_stream_does_not_force_chat_bridge(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "native retry ok"}
            yield {"type": "response.completed", "response": {"id": "resp-fallback"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "role": "user",
                    "content": (
                        "Create a compact handoff summary for resuming this Codex session. "
                        "Target at most 1024 tokens. Preserve only unresolved work."
                    ),
                }
            ],
            "stream": True,
            "api_base": "https://api.openai.com/v1",
            "model_info": {
                "id": "openai-large",
                "provider": "openai",
                "route_key": "openai / default-chat",
            },
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertNotIn("use_chat_completions_api", calls[0])
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_METADATA_KEY, metadata)
        self.assertNotIn(hooks._RESPONSES_CHAT_BRIDGE_FALLBACK_REASON_KEY, metadata)
        self.assertEqual(jsonable_stream_chunk(chunks[-1])["type"], "response.completed")
