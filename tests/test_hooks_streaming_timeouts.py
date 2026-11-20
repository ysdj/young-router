from __future__ import annotations

from unittest import mock

from hook_test_utils import *

from young_router import runtime_store


class HookStreamingTimeoutTests(HookTestCase):
    async def test_inner_watchdog_timeout_is_not_recorded_as_an_outer_expiry(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "5")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "5")
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0")
        request = {"model": "default-chat", "litellm_call_id": "nested-watchdog"}
        inner_exception = None

        async def upstream():
            nonlocal inner_exception
            inner_exception = hooks._stream_idle_timeout_exception(
                request, idle_seconds=0.01, saw_chunk=True
            )
            raise inner_exception
            yield  # Keep the source an async iterator whose next read fails.

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            for chunk_count in (0, 1):
                with self.subTest(chunk_count=chunk_count):
                    with self.assertRaises(TimeoutError) as raised:
                        async for _chunk in hooks._stream_with_idle_timeout(
                            upstream(), request, initial_chunk_count=chunk_count
                        ):
                            pass
                    self.assertIs(raised.exception, inner_exception)

            records = [
                json.loads(line)
                for line in runtime_store.read_records(log_path, limit=10_000)
                if line.strip()
            ]
            stuck = [record for record in records if record.get("status") == "stuck"]
            self.assertEqual(len(stuck), 2)
            self.assertTrue(all(
                record["stuck"]["stream_idle_timeout_seconds"] == 0.01
                for record in stuck
            ))

    async def test_dispatch_expiry_retains_route_after_inner_context_is_reset(self) -> None:
        hooks, _ = load_hook_module()
        # The dispatch budget is the request's first-event budget, so a short
        # first-event timeout is what makes this attempt expire.
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")
        self.addCleanup(self.set_env, hooks._STREAM_START_TIMEOUT_SECONDS_ENV, None)
        request = {"model": "default-chat", "messages": [], "stream": True}

        async def dispatch():
            token = hooks._CURRENT_SELECTED_DEPLOYMENT.set(None)
            try:
                hooks._remember_selected_deployment(
                    {
                        "model_info": {"id": "silent-route", "order": 1},
                        "litellm_params": {"model": "anthropic/test"},
                    },
                    surface="anthropic",
                )
                await asyncio.sleep(1)
            finally:
                hooks._CURRENT_SELECTED_DEPLOYMENT.reset(token)

        with self.assertRaises(TimeoutError) as raised:
            await hooks._await_upstream_dispatch(dispatch(), request)
        self.assertEqual(
            "silent-route", getattr(raised.exception, "failed_deployment_id", None)
        )

    async def test_codex_chat_bridge_reports_restored_reasoning(self) -> None:
        """Codex must know whether the adapted upstream replay has reasoning."""

        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def acompletion(self, **_kwargs):
                async def chunks():
                    yield {
                        "choices": [{"delta": {"content": "answer"}}],
                        "usage": {"total_tokens": 120},
                    }

                return chunks()

        proxy_server.llm_router = FakeRouter()

        async def repaired_response(**_kwargs):
            return {
                "id": "resp_repaired",
                "object": "response",
                "status": "completed",
                "output_text": "done",
                "output": [
                    {
                        "type": "message",
                        "role": "assistant",
                        "content": [{"type": "output_text", "text": "done"}],
                    }
                ],
            }

        repair = await hooks._execute_responses_chat_bridge_call(
            repaired_response,
            {
                "model": "default-chat",
                "input": [
                    {"role": "user", "content": "continue"},
                    {
                        "type": "reasoning",
                        "summary": [],
                        "encrypted_content": "opaque-reasoning",
                    },
                ],
                "stream": True,
                "client_metadata": {
                    "x-codex-turn-metadata": '{"request_kind":"turn"}',
                },
                "model_info": {
                    "upstream_url_surface": "openai/chat",
                },
                "reasoning_effort": "high",
            },
            start_event="test_start",
            error_event="test_error",
        )
        self.assertTrue(hooks._restored_reasoning_included(repair))
        headers = await hooks.YoungRouterHook().async_post_call_response_headers_hook(
            data={},
            user_api_key_dict=None,
            response=repair,
            request_headers={},
        )
        self.assertEqual({"x-reasoning-included": "true"}, headers)
        restored = await repair.__anext__()
        self.assertNotIn("headers", restored["response"])

    async def test_native_responses_answer_carries_no_restored_reasoning_header(
        self,
    ) -> None:
        """Only an adapted replay may claim the upstream counted the reasoning."""

        hooks, _ = load_hook_module()

        async def native_stream():
            yield {"type": "response.completed", "response": {"status": "completed"}}

        hook = hooks.YoungRouterHook()
        self.assertEqual(
            {},
            await hook.async_post_call_response_headers_hook(
                data={},
                user_api_key_dict=None,
                response=native_stream(),
                request_headers={},
            ),
        )
    async def test_first_event_compaction_and_stream_idle_timeouts_are_independent(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "10")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.2")
        self.set_env(
            hooks._CODEX_COMPACTION_STREAM_START_TIMEOUT_SECONDS_ENV,
            "0.4",
        )
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")

        ordinary_request = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "continue"}],
            "stream": True,
        }
        compaction_request = {
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "continue"},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(ordinary_request),
            0.2,
        )
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(compaction_request),
            0.4,
        )
        self.assertEqual(hooks._stall_timeout_seconds(), 0.01)

        async def stalls_after_first_event():
            yield {"type": "response.created", "response": {"id": "resp-idle"}}
            await asyncio.sleep(1)

        stream = hooks._stream_with_idle_timeout(
            stalls_after_first_event(),
            ordinary_request,
        ).__aiter__()
        first = await stream.__anext__()
        self.assertEqual(first["type"], "response.created")
        with self.assertRaises(TimeoutError) as captured:
            await stream.__anext__()
        self.assertEqual(
            getattr(captured.exception, "body", {}).get("reason"),
            "stream_idle_timeout",
        )

    async def test_structured_compaction_uses_compaction_budget_after_first_event(self) -> None:
        hooks, _ = load_hook_module()
        routing_module = importlib.import_module("young_router.proxy.routing")
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
        request_data = {
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "continue"},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": "{\"request_kind\":\"compaction\"}",
            },
        }

        self.assertEqual(
            hooks._stream_idle_timeout_seconds_for_request(request_data),
            0.05,
        )

        async def upstream():
            compaction_item = {
                "id": "cmp-compact",
                "type": "compaction",
                "encrypted_content": "opaque-encrypted-summary",
            }
            yield {"type": "response.created", "response": {"id": "resp-compact"}}
            await asyncio.sleep(0.02)
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": compaction_item,
            }
            yield {
                "type": "response.completed",
                "response": {"id": "resp-compact", "output": [compaction_item]},
            }

        chunks = [
            chunk
            async for chunk in hooks._stream_with_idle_timeout(
                upstream(),
                request_data,
            )
        ]

        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.output_item.done",
                "response.completed",
            ],
        )

    async def test_structured_compaction_waits_longer_for_first_stream_chunk(self) -> None:
        hooks, _ = load_hook_module()
        routing_module = importlib.import_module("young_router.proxy.routing")
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
        # The first chunk arrives later than this ordinary first-event budget;
        # only the compaction budget above can let it through.
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")
        request_data = {
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "continue"},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }

        async def upstream():
            await asyncio.sleep(0.02)
            yield {"type": "response.created", "response": {"id": "resp-compact"}}
            await asyncio.sleep(0.005)
            yield {"type": "response.completed", "response": {"id": "resp-compact"}}

        chunks = [
            chunk
            async for chunk in hooks._stream_with_idle_timeout(
                upstream(),
                request_data,
            )
        ]

        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            ["response.created", "response.completed"],
        )

    async def test_stream_records_first_meaningful_delta_time(self) -> None:
        hooks, _ = load_hook_module()
        request_data = {"model": "default-chat"}

        async def upstream():
            yield {"type": "response.created", "response": {"status": "in_progress"}}
            yield {"type": "response.reasoning_summary_text.delta", "delta": "working"}

        chunks = [
            chunk
            async for chunk in hooks._stream_with_idle_timeout(upstream(), request_data)
        ]

        self.assertEqual(len(chunks), 2)
        self.assertIsInstance(
            request_data.get(hooks._FIRST_STREAM_OUTPUT_TIME_KEY),
            datetime,
        )

    async def test_codex_responses_initial_keepalive_opens_stream_without_cancelling_upstream_read(self) -> None:
        hooks, _ = load_hook_module()
        original_grace = hooks._CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS
        hooks._CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS = 0.001
        self.addCleanup(
            setattr,
            hooks,
            "_CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS",
            original_grace,
        )
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.05")
        upstream_cancelled = False
        upstream_closed = False

        async def upstream():
            nonlocal upstream_cancelled, upstream_closed
            try:
                await asyncio.sleep(0.006)
                yield {"type": "response.output_text.delta", "delta": "ready"}
                yield {"type": "response.completed", "response": {"id": "resp-ready"}}
            except asyncio.CancelledError:
                upstream_cancelled = True
                raise
            finally:
                upstream_closed = True

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
        }

        stream = hooks._yield_codex_responses_initial_keepalive_stream(
            upstream(),
            request_data,
        ).__aiter__()
        chunks = [jsonable_stream_chunk(await stream.__anext__())]
        self.assertTrue(hooks._is_codex_responses_initial_sse_keepalive(chunks[0]))
        chunks.append(jsonable_stream_chunk(await stream.__anext__()))
        self.assertIsInstance(
            request_data.get(hooks._FIRST_STREAM_OUTPUT_TIME_KEY),
            datetime,
        )
        chunks.extend(
            [
                jsonable_stream_chunk(chunk)
                async for chunk in stream
            ]
        )

        self.assertEqual(
            [hooks._stream_chunk_type(chunk) for chunk in chunks[1:]],
            ["response.output_text.delta", "response.completed"],
        )
        self.assertEqual(
            chunks[0],
            {"type": "response.metadata", "metadata": {"phase": "initial_wait"}},
        )
        self.assertFalse(upstream_cancelled)
        self.assertTrue(upstream_closed)

    async def test_a_chat_reasoning_frame_is_stream_activity_not_silence(self) -> None:
        """The gap budget measures upstream silence, and thinking is not silence.

        Chat Completions names its thinking carrier ``reasoning_content`` rather
        than a Responses ``*.delta`` event, so the reasoning frames a client
        renders as a live 深度思考 block used to read as *silence*: a route that
        thought for longer than ``YOUNG_ROUTER_STALL_TIMEOUT_SECONDS`` in one
        unbroken run was torn down, cooled down, and reported to the user as
        "无法连接到服务器" while it was healthy and still working.  Every frame
        here arrives well inside the budget, so the turn must simply finish.
        """

        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.2")

        def reasoning_chunk(index: int) -> dict:
            return {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "model": "default-chat",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "reasoning_content": f"think {index} ",
                        },
                        "finish_reason": None,
                    }
                ],
            }

        # Every gap stays well inside the budget while the *thinking run* as a
        # whole outlasts it several times over, so the only question the test
        # asks is whether arriving thinking resets the deadline.
        async def original_stream():
            for index in range(16):
                await asyncio.sleep(0.04)
                yield reasoning_chunk(index)
            yield {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "model": "default-chat",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": "the answer"},
                        "finish_reason": None,
                    }
                ],
            }
            yield {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "model": "default-chat",
                "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            }

        chunks = [
            chunk
            async for chunk in hooks._stream_with_idle_timeout(
                original_stream(),
                {"model": "default-chat", "messages": [], "stream": True},
            )
        ]

        self.assertEqual(len(chunks), 18)
        self.assertEqual(
            chunks[-2]["choices"][0]["delta"],
            {"content": "the answer"},
        )
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "stop")

    def test_reasoning_progress_is_read_from_every_thinking_carrier(self) -> None:
        """Only thinking counts, and every name a route states it under counts.

        The predicate that resets the gap budget reads thinking from the Chat
        and Anthropic carriers, while a frame that carries nothing -- a
        ``role`` marker, a ``finish_reason`` frame end, an empty value, or a
        comment-style keepalive -- stays silence so a genuinely stalled route
        is still cut off.
        """

        hooks, _proxy_server = load_hook_module()

        def chat(delta: dict, finish_reason: object = None) -> dict:
            return {
                "id": "chatcmpl-1",
                "object": "chat.completion.chunk",
                "model": "default-chat",
                "choices": [
                    {"index": 0, "delta": delta, "finish_reason": finish_reason}
                ],
            }

        self.assertTrue(
            hooks._stream_chunk_has_meaningful_delta(
                chat({"reasoning_content": "thinking"})
            )
        )
        self.assertTrue(
            hooks._stream_chunk_has_meaningful_delta(chat({"reasoning": "thinking"}))
        )
        self.assertTrue(
            hooks._stream_chunk_has_meaningful_delta(
                chat({"thinking_blocks": [{"type": "thinking", "thinking": "t"}]})
            )
        )
        self.assertTrue(
            hooks._stream_chunk_has_meaningful_delta(
                chat({"reasoning_items": [{"type": "reasoning_text", "text": "t"}]})
            )
        )
        self.assertTrue(
            hooks._stream_chunk_has_meaningful_delta(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "thinking_delta", "thinking": "hmm"},
                }
            )
        )

        for silent in (
            chat({"role": "assistant"}),
            chat({}, "stop"),
            chat({}),
            chat({"reasoning_content": ""}),
            chat({"reasoning_content": "   "}),
            chat({"thinking_blocks": [], "content": ""}),
            {
                "type": "content_block_delta",
                "index": 0,
                "delta": {"type": "input_json_delta", "partial_json": "{\"a\""},
            },
            {"type": "response.created", "response": {"id": "resp"}},
            b": young_router keepalive sequence=1\n\n",
        ):
            self.assertFalse(
                hooks._stream_chunk_has_meaningful_delta(silent),
                f"{silent!r} states nothing and must not extend the gap budget",
            )

    async def test_whitespace_delta_does_not_extend_stream_idle_deadline(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")

        async def upstream():
            yield {"type": "response.created", "response": {"id": "resp"}}
            await asyncio.sleep(0.005)
            yield {"type": "response.output_text.delta", "delta": "\n\n"}
            await asyncio.sleep(0.02)
            yield {"type": "response.output_text.delta", "delta": "late"}

        with self.assertRaises(TimeoutError) as captured:
            async for _chunk in hooks._stream_with_idle_timeout(
                upstream(),
                {"model": "default-chat"},
            ):
                pass
        self.assertEqual(
            getattr(captured.exception, "body", {}).get("reason"),
            "stream_idle_timeout",
        )

    async def test_codex_responses_initial_keepalive_preserves_first_event_deadline(self) -> None:
        hooks, _ = load_hook_module()
        original_grace = hooks._CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS
        hooks._CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS = 0.001
        self.addCleanup(
            setattr,
            hooks,
            "_CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS",
            original_grace,
        )
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")
        upstream_cancelled = False

        async def upstream():
            nonlocal upstream_cancelled
            try:
                await asyncio.sleep(1)
                yield {"type": "response.output_text.delta", "delta": "too late"}
            except asyncio.CancelledError:
                upstream_cancelled = True
                raise

        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"turn"}'},
        }
        stream = hooks._yield_codex_responses_initial_keepalive_stream(
            upstream(),
            request_data,
        ).__aiter__()

        first = jsonable_stream_chunk(await stream.__anext__())
        self.assertTrue(hooks._is_codex_responses_initial_sse_keepalive(first))
        with self.assertRaises(TimeoutError) as captured:
            await stream.__anext__()
        self.assertEqual(
            getattr(captured.exception, "body", {}).get("reason"),
            "stream_start_timeout",
        )
        self.assertTrue(upstream_cancelled)

    def test_codex_responses_initial_keepalive_excludes_structured_compaction(self) -> None:
        hooks, _ = load_hook_module()
        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "Continue."},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "client_metadata": {"x-codex-turn-metadata": '{"request_kind":"compaction"}'},
        }

        self.assertFalse(
            hooks._should_emit_codex_responses_initial_sse_keepalive(request_data)
        )

    async def test_streaming_start_timeout_replays_via_router_and_logs_stuck(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "too late"}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "fallback ok"}
            yield {"type": "response.completed", "response": {"id": "resp-fallback"}}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
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

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "0")
        hook = hooks.YoungRouterHook()

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            self.set_env("YOUNG_ROUTER_STREAM_START_TIMEOUT_SECONDS", "0.01")
            request_data = {
                "model": "default-chat",
                "input": [{"role": "user", "content": "Say pong only."}],
                "stream": True,
                "model_info": {"id": "order1-a", "order": 1},
                "litellm_call_id": "idle-call-1",
                "litellm_params": {
                    "metadata": {
                        "headers": {
                            "session-id": "thread-idle",
                        },
                    },
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

            # The turn opens under the router's id before any route answers;
            # the recovered answer arrives inside that same turn.
            self.assertEqual(
                [chunk["type"] for chunk in chunks],
                [
                    "response.created",
                    "response.in_progress",
                    "response.output_text.delta",
                    "response.completed",
                ],
            )
            self.assertEqual(chunks[2]["delta"], "fallback ok")
            self.assertEqual(
                chunks[-1]["response"]["id"], chunks[0]["response"]["id"]
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["_excluded_deployment_ids"], ["order1-a"])
            self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
            self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_START_TIMEOUT_METADATA_KEY])

            records = [
                json.loads(line)
                for line in runtime_store.read_records(log_path, limit=10_000)
                if line.strip()
            ]
            stuck_records = [record for record in records if record.get("status") == "stuck"]
            self.assertEqual(len(stuck_records), 1)
            self.assertEqual(stuck_records[0]["stuck"]["reason"], "stream_start_timeout")
            self.assertEqual(stuck_records[0]["stuck"]["stream_start_timeout_seconds"], 0.01)
            self.assertFalse(stuck_records[0]["stuck"]["stream_saw_chunk"])
            self.assertEqual(stuck_records[0]["session"]["id"], "thread-idle")

    async def test_streaming_idle_timeout_retries_single_deployment_without_excluding_it(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "too late"}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "retry ok"}
            yield {"type": "response.completed", "response": {"id": "resp-retry"}}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {},
                        "model_info": {"id": "only-route"},
                    },
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")

        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Say ok."}],
            "stream": True,
            "model_info": {"id": "only-route", "route_key": "provider_beta / openai/vendor-chat / key=default"},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        # The router opens the turn before the retry; see the note on
        # ``_response_opening_events``.
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[2]["delta"], "retry ok")
        self.assertEqual(chunks[-1]["response"]["id"], chunks[0]["response"]["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "openai/vendor-chat")
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertNotIn("_target_order", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_IDLE_TIMEOUT_METADATA_KEY])

    async def test_stream_request_timeout_logs_selected_deployment_context(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                hooks._remember_selected_deployment(
                    {
                        "litellm_params": {
                            "api_base": "https://chat-provider.example/v1",
                            "model": "openai/vendor-chat",
                            "order": 1,
                        },
                        "model_info": {
                            "id": "79f0dc70",
                            "provider": "provider_chat",
                            "api_key_name": "default",
                            "route_key": "provider_chat / openai/vendor-chat / key=default",
                        },
                    }
                )
                await asyncio.sleep(0.05)
                return hooks._empty_async_iterator()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0.05")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "model": "balanced-chat",
                "input": [{"role": "user", "content": "Continue."}],
                "stream": True,
                "litellm_call_id": "selected-timeout-call",
            }
            original_error = TimeoutError("temporary upstream stall")
            original_error.status_code = 504

            with self.assertRaises(TimeoutError):
                await hooks._streaming_error_fallback_response(
                    request_data,
                    original_error,
                )

            records = [
                json.loads(line)
                for line in runtime_store.read_records(log_path, limit=10_000)
                if line.strip()
            ]
            stuck_records = [
                record
                for record in records
                if record.get("status") == "stuck"
                and record.get("request_id") == "selected-timeout-call"
            ]
            self.assertNotEqual(stuck_records, [])
            stuck = stuck_records[-1]
            self.assertEqual(stuck["deployment_id"], "79f0dc70")
            self.assertEqual(stuck["provider"], "provider_chat")
            self.assertEqual(stuck["upstream_model"], "openai/vendor-chat")
            self.assertEqual(stuck["api_base_host"], "chat-provider.example")
            self.assertEqual(stuck["request_id"], "selected-timeout-call")
            self.assertEqual(
                stuck["route_key"],
                "model=balanced-chat / provider=provider_chat / upstream=openai/vendor-chat / host=chat-provider.example / key=default / order=1",
            )
            self.assertNotIn("unknown-provider", stuck["route_key"])

    async def test_streaming_idle_timeout_resets_after_each_chunk(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **payload):
                raise AssertionError("active streams must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env("YOUNG_ROUTER_STALL_TIMEOUT_SECONDS", "0.05")

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            # Each gap stays under the idle budget while the whole stream
            # outlasts it, so only a per-chunk reset keeps the turn alive.
            for _ in range(4):
                await asyncio.sleep(0.02)
                yield {"type": "response.output_text.delta", "delta": "hello"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

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
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[2]["delta"], "hello")
        # One turn, one id: the upstream's own id is replaced by the announced one.
        self.assertEqual(chunks[-1]["response"]["id"], chunks[0]["response"]["id"])

    async def test_streaming_stall_timeout_resets_after_any_reasoning_event(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            # Each gap stays under the idle budget while the whole thinking
            # run outlasts it, so only a reasoning reset keeps the turn alive.
            await asyncio.sleep(0.03)
            yield {"type": "response.reasoning_text.delta", "delta": "internal thinking"}
            await asyncio.sleep(0.03)
            yield {"type": "response.reasoning_text.delta", "delta": "still thinking"}
            await asyncio.sleep(0.03)
            yield {"type": "response.output_text.delta", "delta": "visible later"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                raise AssertionError("reasoning stream activity must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.05")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "model": "default-chat",
                "input": [{"role": "user", "content": "Say pong only."}],
                "stream": True,
                "model_info": {"id": "order1-a", "order": 1},
                "litellm_call_id": "stream-start-call-1",
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
                [chunk["type"] for chunk in chunks],
                [
                    "response.created",
                    "response.in_progress",
                    "response.output_text.delta",
                    "response.completed",
                ],
            )
            self.assertEqual(chunks[2]["delta"], "visible later")
            self.assertEqual(chunks[-1]["response"]["id"], chunks[0]["response"]["id"])
            self.assertEqual(calls, [])
            self.assertEqual([], runtime_store.read_records(log_path, limit=10))

    async def test_streaming_reasoning_summary_is_delivered_but_raw_reasoning_is_not(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **_payload):
                raise AssertionError("active streams must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "1")

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "\n\n"}
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": "rs_1",
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                    "content": [{"type": "reasoning_text", "text": "private"}],
                },
            }
            yield {
                "type": "response.reasoning_summary_part.added",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            }
            yield {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "delta": "Verifying tool choice compatibility in auto mode",
            }
            yield {
                "type": "response.reasoning_text.delta",
                "item_id": "rs_1",
                "output_index": 0,
                "content_index": 0,
                "delta": "private chain of thought",
            }
            yield {
                "type": "response.reasoning_summary_text.done",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "text": "Verifying tool choice compatibility in auto mode",
            }
            yield {
                "type": "response.reasoning_summary_part.done",
                "item_id": "rs_1",
                "output_index": 0,
                "summary_index": 0,
                "part": {
                    "type": "summary_text",
                    "text": "Verifying tool choice compatibility in auto mode",
                },
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": "rs_1",
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [
                        {
                            "type": "summary_text",
                            "text": "Verifying tool choice compatibility in auto mode",
                        }
                    ],
                    "content": [{"type": "reasoning_text", "text": "private"}],
                    "encrypted_content": "opaque-signed-reasoning",
                },
            }
            yield {"type": "response.output_text.delta", "delta": "visible"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-original",
                    "output": [
                        {
                            "id": "rs_1",
                            "type": "reasoning",
                            "status": "completed",
                            "summary": [
                                {
                                    "type": "summary_text",
                                    "text": "Verifying tool choice compatibility in auto mode",
                                }
                            ],
                            "content": [
                                {"type": "reasoning_text", "text": "private"}
                            ],
                            "encrypted_content": "opaque-signed-reasoning",
                        },
                        {
                            "id": "msg_1",
                            "type": "message",
                            "content": [
                                {"type": "output_text", "text": "visible"},
                            ],
                        },
                    ],
                },
            }

        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Say visible."}],
            "stream": True,
            # The summary frames below are delivered because this client named
            # a summary to see; only an absent or off-valued one means the
            # client wants no reasoning frames at all.
            "reasoning": {"effort": "high", "summary": "raw"},
            "model_info": {"id": "chatroute", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        # The router opens the turn itself; the answer frames follow inside it.
        self.assertEqual(
            [chunk["type"] for chunk in chunks[:2]],
            ["response.created", "response.in_progress"],
        )
        self.assertEqual(
            chunks[2:],
            [
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {
                        "id": "rs_1",
                        "type": "reasoning",
                        "status": "in_progress",
                        "summary": [],
                        # The thinking rides the field the next replay is read
                        # from; the summary channel stays untouched.
                        "content": [{"type": "reasoning_text", "text": "private"}],
                    },
                },
                {
                    "type": "response.reasoning_summary_part.added",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {"type": "summary_text", "text": ""},
                },
                {
                    "type": "response.reasoning_summary_text.delta",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "delta": "Verifying tool choice compatibility in auto mode",
                },
                {
                    "type": "response.reasoning_summary_text.done",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "text": "Verifying tool choice compatibility in auto mode",
                },
                {
                    "type": "response.reasoning_summary_part.done",
                    "item_id": "rs_1",
                    "output_index": 0,
                    "summary_index": 0,
                    "part": {
                        "type": "summary_text",
                        "text": "Verifying tool choice compatibility in auto mode",
                    },
                },
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": {
                        "id": "rs_1",
                        "type": "reasoning",
                        "status": "completed",
                        "summary": [
                            {
                                "type": "summary_text",
                                "text": "Verifying tool choice compatibility in auto mode",
                            }
                        ],
                        # The thinking also rides the field the next replay is
                        # read from, so the client's stored item is accepted
                        # back on the following turn.
                        "content": [{"type": "reasoning_text", "text": "private"}],
                        "encrypted_content": "opaque-signed-reasoning",
                    },
                },
                {"type": "response.output_text.delta", "delta": "visible"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": chunks[0]["response"]["id"],
                        "output": [
                            {
                                "id": "rs_1",
                                "type": "reasoning",
                                "status": "completed",
                                "summary": [
                                    {
                                        "type": "summary_text",
                                        "text": "Verifying tool choice compatibility in auto mode",
                                    }
                                ],
                                "content": [
                                    {"type": "reasoning_text", "text": "private"}
                                ],
                                "encrypted_content": "opaque-signed-reasoning",
                            },
                            {
                                "id": "msg_1",
                                "type": "message",
                                "content": [
                                    {"type": "output_text", "text": "visible"},
                                ],
                            },
                        ],
                    },
                },
            ],
        )

    async def test_streaming_reasoning_summary_reaches_a_client_that_asked_for_auto(
        self,
    ) -> None:
        """``summary: auto`` is a request to see thinking, not a private one.

        pi asks exactly this way (``effort`` plus ``summary: auto``) and renders
        a live thinking block from these frames.  Delivering them only for
        ``raw`` dropped every thinking block such a client had asked for, while
        the provider's unedited ``reasoning_text`` stays hidden either way.
        """

        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **_payload):
                raise AssertionError("an active stream must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()

        async def original_stream():
            yield {
                "type": "response.reasoning_summary_part.added",
                "item_id": "rs_auto",
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            }
            yield {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "rs_auto",
                "output_index": 0,
                "summary_index": 0,
                "delta": "Checking the arithmetic",
            }
            yield {
                "type": "response.reasoning_text.delta",
                "item_id": "rs_auto",
                "output_index": 0,
                "content_index": 0,
                "delta": "private chain of thought",
            }
            yield {"type": "response.output_text.delta", "delta": "391"}
            yield {"type": "response.completed", "response": {"id": "resp-auto"}}

        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "What is 17*23?"}],
            "stream": True,
            "reasoning": {"effort": "max", "summary": "auto"},
            "model_info": {"id": "chatroute", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        # The turn opens under the router's id; the thinking frames follow.
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_text.delta",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[3]["delta"], "Checking the arithmetic")

        # The same frames go nowhere for a request that named no summary.
        silent_request = dict(request_data)
        silent_request.pop("reasoning")
        silent_chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=silent_request,
            )
        ]
        # This request asked for no summary, so no thinking frame appears --
        # whatever frames the router itself opens the turn with.
        silent_types = [chunk["type"] for chunk in silent_chunks]
        self.assertEqual(
            [name for name in silent_types if "reasoning" in name],
            [],
        )
        self.assertEqual(silent_types[-1], "response.completed")

    async def test_streaming_error_chunk_before_content_replays_via_router(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "error": {
                    "message": "Concurrency limit exceeded for account, please retry later",
                    "type": "rate_limit_error",
                    "code": "concurrency_limit",
                }
            }

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "fallback ok"}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
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

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say pong only."}],
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
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(
            chunks[2],
            {"type": "response.output_text.delta", "delta": "fallback ok"},
        )
        self.assertEqual(jsonable_stream_chunk(chunks[-1])["type"], "response.completed")
        self.assertEqual(
            jsonable_stream_chunk(chunks[-1])["response"]["output"][0]["content"][0]["text"],
            "fallback ok",
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertEqual(calls[0]["_target_order"], 1)
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_responses_stream_ending_before_completed_replays_via_router(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "fallback ok"}
            yield {"type": "response.completed", "response": {"id": "resp-fallback"}}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
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

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say pong only."}],
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

        # See above: the router opens the turn before the replay.
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[2]["delta"], "fallback ok")
        self.assertEqual(chunks[-1]["response"]["id"], chunks[0]["response"]["id"])
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertEqual(calls[0]["_target_order"], 1)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["order1-a"])
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_incomplete_responses_stream_recovers_via_chat_deployment(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        candidate_results = []
        responses_deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "order": 1,
            },
            "model_info": {
                "id": "responses-route",
                "model_group": "default-chat",
                "upstream_url_surface": "openai/responses",
            },
        }
        chat_deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "order": 1,
            },
            "model_info": {
                "id": "chat-route",
                "model_group": "default-chat",
                "upstream_url_surface": "openai/chat",
            },
        }

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "chat recovered"}
            yield {
                "type": "response.completed",
                "response": {"id": "resp-chat-recovered"},
            }

        hook = hooks.YoungRouterHook()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [responses_deployment, chat_deployment]

            async def aresponses(self, **payload):
                calls.append(payload)
                candidates = await hook.async_filter_deployments(
                    "default-chat",
                    [responses_deployment, chat_deployment],
                    messages=None,
                    request_kwargs=payload,
                )
                candidate_results.append(candidates)
                return recovered_stream()
        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        request_data = {
            "model": "default-chat",
            "input": [
                {
                    "type": "additional_tools",
                    "role": "developer",
                    "tools": [
                        {
                            "type": "custom",
                            "name": "exec_command",
                            "description": "Run a command.",
                        }
                    ],
                },
                {"role": "user", "content": "Inspect the workspace."},
            ],
            "tools": [],
            "stream": True,
            "model_info": responses_deployment["model_info"],
            "litellm_params": responses_deployment["litellm_params"],
            "_excluded_deployment_ids": ["other-route"],
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        # See above: the router opens the turn before the replay.
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(candidate_results, [[chat_deployment]])
        self.assertNotIn("_young_router_upstream_url_surface", calls[0])
        self.assertEqual(calls[0]["_target_order"], 1)
        self.assertEqual(
            calls[0]["_excluded_deployment_ids"],
            ["other-route", "responses-route"],
        )
        self.assertEqual(
            calls[0]["_young_router_verified_fallback_deployment_ids"],
            ["chat-route"],
        )
        self.assertIn(
            "id:responses-route",
            hooks._DEPLOYMENT_COOLDOWNS,
        )
        self.assertFalse(any("|surface:" in key for key in hooks._DEPLOYMENT_COOLDOWNS))

    async def test_responses_empty_completed_stream_enters_route_recovery_without_leaking_empty_terminal(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-empty-original",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        async def empty_fallback_stream():
            yield {"type": "response.created", "response": {"id": "resp-empty-fallback"}}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-empty-fallback",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                },
            }

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered ok"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered"}}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "order1-a"},
                    }
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                if len(calls) == 1:
                    return empty_fallback_stream()
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "0")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "legacy-chat",
            "input": [{"role": "user", "content": "Continue."}],
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
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[2]["delta"], "recovered ok")
        self.assertEqual(chunks[-1]["response"]["id"], chunks[0]["response"]["id"])
        self.assertEqual(len(calls), 2)
        self.assertNotIn("resp-empty-original", json.dumps(chunks))
        self.assertNotIn("resp-empty-fallback", json.dumps(chunks))
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertTrue(calls[1]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_no_deployments_after_empty_stream_enters_route_recovery_poll(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def fallback_stream():
            yield {"type": "response.output_text.delta", "delta": "fallback after retries"}
            yield {"type": "response.completed", "response": {"id": "resp-fallback"}}

        class RouterRateLimitError(Exception):
            pass

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                if len(calls) <= 2:
                    raise RouterRateLimitError(
                        "No deployments available for selected model, Try again in 45 seconds."
                    )
                return fallback_stream()

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
            "_excluded_deployment_ids": ["stale-round-exclusion"],
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
            [chunk["type"] for chunk in chunks],
            [
                "response.created",
                "response.in_progress",
                "response.output_text.delta",
                "response.completed",
            ],
        )
        self.assertEqual(chunks[2]["delta"], "fallback after retries")
        self.assertEqual(chunks[-1]["response"]["id"], chunks[0]["response"]["id"])
        self.assertEqual(len(calls), 3)
        self.assertEqual(
            calls[0]["_excluded_deployment_ids"],
            ["order1-a", "stale-round-exclusion"],
        )
        self.assertEqual(
            calls[1]["_excluded_deployment_ids"],
            ["order1-a", "stale-round-exclusion"],
        )
        self.assertNotIn("_excluded_deployment_ids", calls[2])

    async def test_streaming_idle_after_visible_output_yields_failed_terminal_event(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                raise AssertionError("visible partial streams must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            yield {"type": "response.output_text.delta", "delta": "partial answer"}
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "too late"}

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "model": "default-chat",
                "input": [{"role": "user", "content": "Print a partial answer."}],
                "stream": True,
                "model_info": {"id": "order1-a", "order": 1},
                "litellm_call_id": "idle-after-visible-call",
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
                [
                    "response.created",
                    "response.in_progress",
                    "response.output_text.delta",
                    "response.failed",
                ],
            )
            self.assertEqual(chunks[2]["delta"], "partial answer")
            self.assertEqual(chunks[-1]["response"]["status"], "failed")
            # The watchdog is this worker's own verdict, so the event carries
            # its own code instead of the unclassified ``upstream_route_failure``
            # -- a client retries on it, and an unclassified stop it drops.
            self.assertEqual(
                chunks[-1]["response"]["error"]["code"],
                "upstream_stream_idle_timeout",
            )
            self.assertIn(
                "ended without",
                chunks[-1]["response"]["error"]["message"],
            )

            records = [
                json.loads(line)
                for line in runtime_store.read_records(log_path, limit=10_000)
                if line.strip()
            ]
            stuck_records = [record for record in records if record.get("status") == "stuck"]
            self.assertEqual(len(stuck_records), 1)
            self.assertEqual(stuck_records[0]["stuck"]["reason"], "stream_idle_timeout")
            self.assertEqual(stuck_records[0]["stuck"]["stream_idle_timeout_seconds"], 0.01)
            self.assertTrue(stuck_records[0]["stuck"]["stream_saw_chunk"])

    def test_route_recovery_polls_on_local_stream_timeout(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = TimeoutError("Young Router stream idle timeout after 45s without a new chunk")
        exc.status_code = 504
        exc.body = {"reason": "stream_idle_timeout"}
        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }
        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(hooks._should_return_route_recovery_stream(exc, request_data))
        self.assertTrue(hooks._should_retry_final_upstream_route_error(exc, request_data))

    def test_route_recovery_wait_timeout_after_buffered_chunks_is_idle_timeout(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }

        exc = hooks._stream_route_recovery_wait_timeout_exception(
            request_data,
            timeout_seconds=120,
            buffered_chunks=104,
        )

        self.assertEqual(exc.body["reason"], "stream_idle_timeout")
        self.assertEqual(exc.body["idle_seconds"], 120)
        self.assertTrue(exc.body["saw_chunk"])

    async def test_route_recovery_wait_timeout_logs_selected_recovery_deployment_context(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {
                            "api_base": "https://api.backup.example/v1",
                            "model": "openai/default-chat",
                            "order": 2,
                        },
                        "model_info": {
                            "id": "249cf3df",
                            "provider": "backup_provider",
                            "api_key_name": "x-plus",
                            "route_key": "model=default-chat / provider=backup_provider / upstream=openai/default-chat / host=api.backup.example / key=x-plus / order=2",
                            "order": 2,
                        },
                    },
                    {
                        "litellm_params": {
                            "api_base": "https://headers.example/v1",
                            "model": "openai/default-chat",
                            "order": 2,
                        },
                        "model_info": {
                            "id": "799a0021",
                            "provider": "compat_provider",
                            "api_key_name": "r-plus",
                            "route_key": "model=default-chat / provider=compat_provider / upstream=openai/default-chat / host=headers.example / key=r-plus / order=2",
                            "order": 2,
                        },
                    },
                ]

            async def aresponses(self, **payload):
                hooks._remember_selected_deployment(
                    {
                        "litellm_params": {
                            "api_base": "https://headers.example/v1",
                            "model": "openai/default-chat",
                            "order": 2,
                        },
                        "model_info": {
                            "id": "799a0021",
                            "provider": "compat_provider",
                            "api_key_name": "r-plus",
                            "route_key": "model=default-chat / provider=compat_provider / upstream=openai/default-chat / host=headers.example / key=r-plus / order=2",
                            "order": 2,
                        },
                    }
                )

                async def silent_stream():
                    await asyncio.sleep(0.05)
                    if False:
                        yield None

                return silent_stream()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.01")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.03")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            request_data = {
                "model": "default-chat",
                "input": [{"role": "user", "content": "Continue."}],
                "stream": True,
                "litellm_call_id": "route-recovery-selected-timeout",
                "model_info": {
                    "id": "249cf3df",
                    "provider": "backup_provider",
                    "api_key_name": "x-plus",
                    "route_key": "model=default-chat / provider=backup_provider / upstream=openai/default-chat / host=api.backup.example / key=x-plus / order=2",
                    "order": 2,
                },
                "litellm_params": {
                    "api_base": "https://api.backup.example/v1",
                    "model": "openai/default-chat",
                    "order": 2,
                },
            }
            first_exception = TimeoutError("upstream rate limit exceeded")
            first_exception.status_code = 429
            first_exception.failed_deployment_id = "249cf3df"
            first_exception.failed_deployment_order = 2

            _ = [
                chunk
                async for chunk in hooks._stream_route_recovery_poll(
                    request_data,
                    first_exception,
                )
            ]

            records = [
                json.loads(line)
                for line in runtime_store.read_records(log_path, limit=10_000)
                if line.strip()
            ]
            stuck_records = [
                record
                for record in records
                if record.get("status") == "stuck"
                and record.get("request_id") == "route-recovery-selected-timeout"
            ]
            self.assertNotEqual(stuck_records, [])
            stuck = stuck_records[-1]
            self.assertEqual(stuck["deployment_id"], "799a0021")
            self.assertEqual(stuck["provider"], "compat_provider")
            self.assertEqual(stuck["api_base_host"], "headers.example")
            self.assertEqual(stuck["upstream_model"], "openai/default-chat")
            self.assertEqual(
                stuck["route_key"],
                "model=default-chat / provider=compat_provider / upstream=openai/default-chat / host=headers.example / key=r-plus / order=2",
            )
            self.assertEqual(stuck["request_id"], "route-recovery-selected-timeout")
            self.assertEqual(stuck["stuck"]["reason"], "stream_start_timeout")

    def test_heavy_replay_prefix_scales_stream_start_timeout(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.2")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0")
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_TIMEOUT_SECONDS_ENV, "0.5")
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV, "1024")

        ordinary_request = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "continue"}],
            "stream": True,
        }
        heavy_request = {
            "model": "default-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": "x" * 4096},
                    ],
                }
            ],
            "stream": True,
        }
        chat_request = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "x" * 4096}],
            "stream": True,
        }

        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(ordinary_request),
            0.2,
        )
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(heavy_request),
            0.5,
        )
        # Non-Responses shapes never receive the heavy budget.
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(chat_request),
            0.2,
        )

        # A non-positive threshold disables heavy scaling entirely.
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV, "0")
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(heavy_request),
            0.2,
        )

        # The request timeout still caps the scaled budget.
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV, "1024")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0.3")
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(heavy_request),
            0.3,
        )

        # A disabled stream-start watchdog is not re-enabled by weight.
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "2")
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(heavy_request),
            2,
        )

    def test_heavy_replay_budget_keeps_compaction_budget_and_metadata_override(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.2")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0")
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_TIMEOUT_SECONDS_ENV, "0.5")
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV, "1024")

        compaction_request = {
            "model": "default-chat",
            "input": [
                {"role": "user", "content": "continue"},
                {"type": "compaction_trigger"},
            ],
            "stream": True,
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"compaction"}',
            },
        }
        self.set_env(
            hooks._CODEX_COMPACTION_STREAM_START_TIMEOUT_SECONDS_ENV,
            "0.4",
        )
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(compaction_request),
            0.4,
        )

        override_request = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "x" * 4096}],
            "stream": True,
            "litellm_metadata": {
                "route_recovery_attempt_timeout_seconds": 0.7,
            },
        }
        self.assertEqual(
            hooks._stream_start_timeout_seconds_for_request(override_request),
            0.7,
        )

    async def test_a_heavy_replay_keeps_its_idle_cap_after_the_local_announcement(
        self,
    ) -> None:
        """The announced ``response.created`` must not shrink the heavy budget.

        LiteLLM emits ``response.created`` locally before the upstream starts
        prefilling, so the first real wait is measured against the idle cap
        while ``saw_chunk`` is already true.  Live 2026-11-18: a request
        replaying 261,000 tokens announced at 13 s, the station prefilled for
        minutes, and the 120 s idle cap parked it mid-prefill -- every retry
        met the same cap on a different route, so the thread never finished a
        turn.
        """

        hooks, _ = load_hook_module()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.05")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0")
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_TIMEOUT_SECONDS_ENV, "0.6")
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV, "1024")

        heavy_request = {
            "model": "default-chat",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "x" * 4096}],
                }
            ],
            "stream": True,
        }
        ordinary_request = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "continue"}],
            "stream": True,
        }

        self.assertEqual(
            hooks._stream_idle_timeout_seconds_for_request(heavy_request),
            0.6,
        )
        self.assertEqual(
            hooks._stream_idle_timeout_seconds_for_request(ordinary_request),
            0.05,
        )

        async def announces_then_prefills():
            yield {"type": "response.created", "response": {"id": "resp-heavy"}}
            await asyncio.sleep(0.2)
            yield {"type": "response.output_text.delta", "delta": "done"}

        chunks = [
            chunk
            async for chunk in hooks._stream_with_idle_timeout(
                announces_then_prefills(),
                heavy_request,
            )
        ]
        self.assertEqual(
            [chunk["type"] for chunk in chunks],
            ["response.created", "response.output_text.delta"],
        )

        # The ordinary request still stops on the same upstream shape.
        with self.assertRaises(TimeoutError) as captured:
            [
                chunk
                async for chunk in hooks._stream_with_idle_timeout(
                    announces_then_prefills(),
                    ordinary_request,
                )
            ]
        self.assertEqual(
            getattr(captured.exception, "body", {}).get("reason"),
            "stream_idle_timeout",
        )

    def test_request_replay_payload_bytes_is_cached_in_existing_metadata(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "x" * 2048}],
            "litellm_metadata": {},
        }
        first = hooks._request_replay_payload_bytes(request)
        self.assertIsNotNone(first)
        self.assertGreater(first, 2048)
        cached_key = hooks._REPLAY_PAYLOAD_BYTES_METADATA_KEY
        self.assertEqual(request["litellm_metadata"][cached_key], first)

        # A cached value short-circuits re-serialization even after the body
        # changes, and raw request bodies are never mutated with a new key.
        request["input"] = [{"role": "user", "content": "short"}]
        self.assertEqual(hooks._request_replay_payload_bytes(request), first)
        bare = {"model": "default-chat", "input": [{"role": "user", "content": "y"}]}
        hooks._request_replay_payload_bytes(bare)
        self.assertNotIn(cached_key, bare)
        self.assertIsNone(hooks._request_replay_payload_bytes({"model": "default-chat"}))

    async def test_the_dispatch_budget_ends_an_attempt_no_stream_budget_covers(self) -> None:
        """The phase before the response object is timed by the same silence clock.

        Measured on the live gateway 2026-11-15: a route whose host stopped
        answering held a turn for 271 s, because the first-event and stream-idle
        clocks both start on the first frame of a response that host never sent.
        The dispatch budget covers that phase with the request's first-event
        budget -- the same 120 s of silence a route gets after its head.
        """

        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0.2")
        self.addCleanup(
            self.set_env, hooks._STREAM_START_TIMEOUT_SECONDS_ENV, None
        )
        request = {"model": "default-chat", "input": "hi", "stream": True}

        async def never_reaches_the_host():
            await hooks.asyncio.sleep(30)

        with self.assertRaises(TimeoutError) as raised:
            await hooks._await_upstream_dispatch(
                never_reaches_the_host(),
                request,
            )

        body = getattr(raised.exception, "body", {})
        self.assertEqual("stream_start_timeout", body.get("reason"))
        self.assertFalse(body.get("saw_chunk"))
        # The route answered with nothing, so a sibling may serve the same
        # bytes and the route is parked -- the two decisions this shape carries.
        self.assertEqual(
            hooks._recovery_policy_for_exception(raised.exception),
            hooks._RECOVERY_POLICY_COOLDOWN,
        )

    async def test_the_dispatch_budget_leaves_a_finished_attempt_alone(self) -> None:
        hooks, _ = load_hook_module()
        request = {"model": "default-chat", "input": "hi", "stream": True}

        async def answers_immediately():
            return "response-object"

        self.assertEqual(
            "response-object",
            await hooks._await_upstream_dispatch(answers_immediately(), request),
        )

    async def test_an_upstream_timeout_keeps_its_own_reason(self) -> None:
        """``asyncio.TimeoutError`` is the builtin one since 3.11.

        A route that raised its own ``TimeoutError`` must keep that failure and
        its route identity; rewriting it into this proxy's silence is how a real
        cause disappears from the log (measured on the first draft of this
        budget, which used ``wait_for``).
        """

        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "5")
        self.addCleanup(self.set_env, hooks._STREAM_START_TIMEOUT_SECONDS_ENV, None)
        request = {"model": "default-chat", "input": "hi", "stream": True}

        async def upstream_refused():
            exc = TimeoutError("upstream said nothing for its own 30s")
            exc.body = {"reason": "upstream_own_reason"}
            raise exc

        with self.assertRaises(TimeoutError) as raised:
            await hooks._await_upstream_dispatch(upstream_refused(), request)

        self.assertEqual("upstream_own_reason", getattr(raised.exception, "body", {}).get("reason"))
        self.assertEqual("upstream said nothing for its own 30s", str(raised.exception))

    async def test_the_dispatch_budget_keeps_the_selected_route_visible(self) -> None:
        """The budget must not move the attempt into a child task.

        A new task gets a copy of the context, so every ``ContextVar`` write the
        attempt makes would stay inside it -- and this proxy keeps the route that
        answered in one.  A copy turns "route-peer answered" into "no route
        identity", losing the log row, the cooldown key and the delivery marker
        together.
        """

        hooks, _ = load_hook_module()
        request = {"model": "default-chat", "input": "hi", "stream": True}
        box: dict = {}

        async def answers_from_one_route():
            hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.set(box)
            box["deployment_id"] = "route-peer"
            return "response-object"

        await hooks._await_upstream_dispatch(answers_from_one_route(), request)

        self.assertEqual(
            {"deployment_id": "route-peer"},
            hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.get(),
        )

    def test_the_dispatch_budget_is_the_first_event_budget(self) -> None:
        """Both clocks end at the response head, so they are one clock.

        The old budget was the connect allowance plus 1/16 KB of upload.  A
        relay that buffers its answer behind a queue writes no head for
        however long the queue takes, so that allowance cut a healthy
        gemini-3.8-flash route at 11.52 s on 2026-11-20 while its first byte
        was 13.69 s away.  A streaming dispatch now waits the request's own
        first-event budget: 120 s ordinary, 300 s for a heavy Responses
        replay.
        """

        hooks, _ = load_hook_module()

        small = {"input": "hi"}
        heavy = {"input": [{"role": "user", "content": "x" * 3_000_000}]}
        chat = {"messages": [{"role": "user", "content": "hi"}]}

        self.assertAlmostEqual(
            120.0, hooks._upstream_dispatch_budget_seconds(small), places=2
        )
        self.assertAlmostEqual(
            hooks._stream_start_timeout_seconds_for_request(small),
            hooks._upstream_dispatch_budget_seconds(small),
            places=2,
        )
        # A 3 MB Responses replay earns the 300 s heavy first-event budget, so
        # its head wait is that budget and not the 192 s upload allowance.
        self.assertGreater(hooks._upstream_dispatch_budget_seconds(heavy), 150.0)
        self.assertLessEqual(
            hooks._upstream_dispatch_budget_seconds(heavy),
            hooks._stream_start_timeout_seconds_for_request(heavy),
        )
        # A Chat body is a streaming dispatch like any other; it used to be
        # computed from ``input`` alone and paid the bare connect budget.
        self.assertAlmostEqual(
            hooks._stream_start_timeout_seconds_for_request(chat),
            hooks._upstream_dispatch_budget_seconds(chat),
            places=2,
        )

    def test_the_dispatch_budget_is_unbounded_when_every_start_budget_is_off(
        self,
    ) -> None:
        """With no start budget there is no dispatch deadline either.

        Both zeros are an explicit request to leave the bound to the transport;
        the removed connect setting must not come back as a hidden 10 s cap.
        """

        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "0")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0")
        self.addCleanup(self.set_env, hooks._STREAM_START_TIMEOUT_SECONDS_ENV, None)
        self.addCleanup(self.set_env, hooks._REQUEST_TIMEOUT_SECONDS_ENV, None)

        self.assertEqual(
            0.0, hooks._upstream_dispatch_budget_seconds({"input": "hi"})
        )

    def test_the_head_hedge_waits_the_connect_and_upload_allowance(self) -> None:
        """The old connect budget becomes a race trigger, not a deadline.

        A 25 KB chat body gets 10 s plus its upload allowance (about 11.5 s),
        which is where the healthy gemini-3.8-flash route's 13.69 s head used
        to be cut.  The trigger is a race: the primary keeps running.  A heavy
        replay is capped by the frame hedge's 30 s, so it is not chased at 10 s.
        """

        hooks, _ = load_hook_module()

        small = {"input": "hi"}
        chat = {"messages": [{"role": "user", "content": "x" * 25_000}]}
        heavy = {"input": [{"role": "user", "content": "x" * 1_000_000}]}

        self.assertAlmostEqual(
            10.0, hooks._head_hedge_delay_seconds(small), places=2
        )
        self.assertGreater(hooks._head_hedge_delay_seconds(chat), 11.0)
        self.assertLess(hooks._head_hedge_delay_seconds(chat), 12.0)
        self.assertAlmostEqual(
            20.0, hooks._head_hedge_delay_seconds(heavy), places=2
        )

        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0")
        self.addCleanup(self.set_env, hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, None)
        self.assertEqual(0.0, hooks._head_hedge_delay_seconds(small))

    def test_the_dispatch_budget_never_outlasts_the_first_event_budget(self) -> None:
        """The dispatch budget is exactly the first-event budget, not more.

        A shorter deadline cuts a prefill the first-event budget exists to
        allow, and a longer one waits for a head this proxy would already have
        given up on after the head arrived.
        """

        hooks, _ = load_hook_module()
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "30")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "0")
        # Without this the heavy-replay widening would raise the first-event
        # budget to its own 300 s, and the cap being tested would not bind.
        self.set_env(hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV, "0")
        self.addCleanup(self.set_env, hooks._STREAM_START_TIMEOUT_SECONDS_ENV, None)
        self.addCleanup(self.set_env, hooks._REQUEST_TIMEOUT_SECONDS_ENV, None)
        self.addCleanup(
            self.set_env,
            hooks._HEAVY_REPLAY_STREAM_START_THRESHOLD_BYTES_ENV,
            None,
        )

        body = {"input": [{"role": "user", "content": "x" * 3_000_000}]}
        self.assertAlmostEqual(
            30.0, hooks._upstream_dispatch_budget_seconds(body), places=2
        )

    def test_a_claude_sized_replay_gets_time_to_be_prefilled(self) -> None:
        """The measured case: 2.7 MB of Claude Code history needed 26 s.

        The budget used to give that body 12.6 s and the thread failed on every
        deployment with ``stream_start_timeout`` while the upstream was in fact
        reading the prompt.  It is now the request's own first-event budget.
        """

        hooks, _ = load_hook_module()

        body = {"messages": [{"role": "user", "content": "x" * 2_760_000}]}
        self.assertGreater(hooks._upstream_dispatch_budget_seconds(body), 26.0)


class _HedgeStream:
    """One fake upstream response: an async iterable with a route marker."""

    def __init__(self, chunks):
        self._chunks = chunks
        self.closed = False

    def __aiter__(self):
        async def generator():
            for chunk in self._chunks:
                await asyncio.sleep(0)
                yield chunk
        return generator()

    async def aclose(self):
        self.closed = True


class HookStreamingHedgeTests(HookTestCase):
    def _request(self):
        return {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "proxy_server_request": {"path": "/v1/responses"},
            "model_info": {"id": "order1-a", "order": 1},
            "litellm_params": {"model": "openai/gpt-x"},
        }

    async def test_a_silent_first_attempt_is_overtaken_by_a_sibling(self) -> None:
        """The hedge answers while the first route is still silent.

        Live 2026-11-19: a turn spent 300 s on a route that never sent a byte,
        then 300 s on the next one, and the client reconnected before either
        answered.  Waiting is not wrong by itself -- this group's healthy turns
        prefilled for 15-94 s -- so the first attempt is never abandoned; a
        sibling is simply asked too, and the first frame decides.
        """

        hooks, proxy_server = load_hook_module()
        calls = []
        hedge_stream = _HedgeStream(
            [
                {"type": "response.output_text.delta", "delta": "hedge answer"},
                {"type": "response.output_text.delta", "delta": " and more"},
                {"type": "response.completed", "response": {"id": "resp-hedge"}},
            ]
        )
        hedge_stream._young_router_selected_deployment_marker = {
            "deployment_id": "order1-b",
            "model_info": {"id": "order1-b", "order": 1},
        }

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return hedge_stream

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.05")

        async def original_stream():
            await asyncio.sleep(1.0)
            yield {"type": "response.output_text.delta", "delta": "slow primary"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        request_data = self._request()
        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        deltas = [chunk.get("delta") for chunk in chunks if chunk.get("delta")]
        self.assertIn("hedge answer", deltas)
        self.assertIn(" and more", deltas)
        self.assertEqual(chunks[-1].get("type"), "response.completed")
        self.assertNotIn("slow primary", deltas)
        self.assertEqual(len(calls), 1)
        self.assertIn("order1-a", calls[0]["_excluded_deployment_ids"])
        # The turn belongs to the route that actually answered.
        self.assertEqual(
            hooks._request_model_info(request_data).get("id"), "order1-b"
        )

    async def test_codex_keepalive_keeps_the_hedge_active(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        hedge = _HedgeStream([
            {"type": "response.output_text.delta", "delta": "fast"},
            {"type": "response.output_text.delta", "delta": " continuation"},
            {"type": "response.completed", "response": {"id": "resp-hedge"}},
        ])
        hedge._young_router_selected_deployment_marker = {
            "model_info": {"id": "order1-b", "order": 1}
        }

        class FakeRouter:
            async def aresponses(self, **_payload):
                return hedge

        proxy_server.llm_router = FakeRouter()

        async def primary():
            await asyncio.sleep(1)
            yield {"type": "response.output_text.delta", "delta": "slow"}

        request = self._request()
        request["client_metadata"] = {"x-codex-turn-metadata": '{"request_kind":"turn"}'}
        chunks = [
            chunk async for chunk in hooks.YoungRouterHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None, response=primary(), request_data=request
            )
        ]
        deltas = [chunk.get("delta") for chunk in chunks if isinstance(chunk, dict)]
        self.assertIn("fast", deltas)
        self.assertIn(" continuation", deltas)
        self.assertEqual(chunks[-1].get("type"), "response.completed")

    async def test_simultaneous_primary_and_hedge_dispatch_closes_loser(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        ready = asyncio.Event()
        hedge = _HedgeStream([])
        hedge._young_router_selected_deployment_marker = {
            "model_info": {"id": "order1-b"}
        }

        async def primary():
            await ready.wait()
            yield {"type": "response.output_text.delta", "delta": "primary"}

        async def dispatch(_request):
            ready.set()
            await asyncio.sleep(0)
            return hedge, self._request()

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            iterator = primary().__aiter__()
            winner, first, _ = await streaming._hedged_first_chunk(
                iterator, self._request(), start_timeout_seconds=1
            )
            await winner.aclose()
        self.assertEqual(first["delta"], "primary")
        self.assertTrue(hedge.closed)

    async def test_releasing_one_hedge_route_preserves_the_other_lease(self) -> None:
        hooks, _ = load_hook_module()
        routing = sys.modules["young_router.proxy.routing"]
        routing._HELD_CONCURRENCY_LEASES.clear()
        self.addCleanup(routing._HELD_CONCURRENCY_LEASES.clear)
        routing._HELD_CONCURRENCY_LEASES["turn"] = {"id:route-a|turn", "id:route-b|turn"}
        request = {
            "litellm_params": {"litellm_call_id": "turn"},
            "model_info": {"id": "route-a"},
        }

        with mock.patch.object(routing._concurrency_module, "release") as release:
            routing._release_request_concurrency_slots(request, deployment_only=True)
            self.assertEqual(routing._HELD_CONCURRENCY_LEASES["turn"], {"id:route-b|turn"})
            self.assertEqual(release.call_args.args[0], ["id:route-a|turn"])
            routing._release_request_concurrency_slots(request)
            self.assertEqual(release.call_args.args[0], ["id:route-b|turn"])

    async def test_a_primary_that_answers_first_never_dispatches_a_hedge(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return _HedgeStream([])

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.05")

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "primary answer"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=self._request(),
            )
        ]

        self.assertEqual(calls, [])
        self.assertIn(
            "primary answer",
            [chunk.get("delta") for chunk in chunks if chunk.get("delta")],
        )

    async def test_the_hedge_switch_turns_the_sibling_dispatch_off(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                return _HedgeStream([])

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STREAM_HEDGE_ENABLED_ENV, "0")
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")

        async def original_stream():
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "primary answer"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=self._request(),
            )
        ]

        self.assertEqual(calls, [])
        self.assertIn(
            "primary answer",
            [chunk.get("delta") for chunk in chunks if chunk.get("delta")],
        )

    async def test_a_failed_hedge_leaves_the_first_attempt_running(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                raise RuntimeError("sibling route unavailable")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.YoungRouterHook()
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")

        async def original_stream():
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "primary answer"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=self._request(),
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertIn(
            "primary answer",
            [chunk.get("delta") for chunk in chunks if chunk.get("delta")],
        )


class HookHeadHedgeTests(HookTestCase):
    """The dispatch itself is raced, because the head is where the silence is."""

    def setUp(self) -> None:
        super().setUp()
        cooldown_dir = tempfile.TemporaryDirectory()
        self.addCleanup(cooldown_dir.cleanup)
        self.set_env(
            "YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FILE",
            str(Path(cooldown_dir.name) / "deployment-cooldowns.json"),
        )

    def _request(self) -> dict:
        return {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "proxy_server_request": {"path": "/v1/responses"},
            "model_info": {"id": "order1-a", "order": 1},
            "litellm_params": {"model": "openai/gpt-x", "litellm_call_id": "call-head-hedge"},
        }

    def _hedge_response(self, deployment_id: str):
        stream = _HedgeStream([])
        stream._young_router_selected_deployment_marker = {
            "model_info": {"id": deployment_id, "order": 1},
        }
        return stream, {"model_info": {"id": deployment_id, "order": 1}}

    def _chat_request(self) -> dict:
        return {
            "model": "default-chat",
            "call_type": "acompletion",
            "messages": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "proxy_server_request": {"path": "/v1/chat/completions"},
            "model_info": {"id": "order1-a", "order": 1},
            "litellm_params": {"model": "openai/gpt-x", "litellm_call_id": "call-chat-head-hedge"},
        }

    async def test_a_chat_head_is_raced_like_a_responses_head(self) -> None:
        """A chat relay buffers its head too, and there is no frame to watch.

        Measured 2026-11-20: a healthy wawapii gemini-3.8-flash chat route's
        first byte was 13.69 s away, and a hung sibling answered 524 after
        126 s.  The head race is the only thing that keeps one hung relay from
        holding a chat turn for the whole first-event budget.
        """

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.05")
        request = self._chat_request()
        calls: list = []
        hedge_response, hedge_payload = self._hedge_response("order1-b")

        async def slow_dispatch():
            await asyncio.sleep(5)
            return "primary-response"

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return hedge_response, hedge_payload

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(
                    slow_dispatch(), request
                ),
                2,
            )

        self.assertIs(hedge_response, response)
        self.assertEqual(["head"], calls)
        self.assertEqual("order1-b", hooks._request_model_info(request).get("id"))

    async def test_an_image_generation_tool_is_not_raced_for_a_head(self) -> None:
        """A lost race would generate a second image, not a second prefill.

        The image is the expensive part and it cannot be un-billed by
        cancelling the loser, so a turn carrying the image tool keeps its one
        route even when that route is slow to write a head.
        """

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._chat_request()
        request["tools"] = [{"type": "image_generation"}]
        calls: list = []

        async def slow_dispatch():
            await asyncio.sleep(0.2)
            return "primary-response"

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return self._hedge_response("order1-b")

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(
                    slow_dispatch(), request
                ),
                2,
            )

        self.assertEqual("primary-response", response)
        self.assertEqual([], calls)

    async def test_a_native_image_model_is_not_raced_for_a_head(self) -> None:
        """A chat turn sent straight at an image model has no tool to detect."""

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._chat_request()
        request["model"] = "gemini-2.5-flash-image"
        calls: list = []

        async def slow_dispatch():
            await asyncio.sleep(0.2)
            return "primary-response"

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return self._hedge_response("order1-b")

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(
                    slow_dispatch(), request
                ),
                2,
            )

        self.assertEqual("primary-response", response)
        self.assertEqual([], calls)

    async def test_an_image_generation_turn_keeps_its_first_frame_unraced(self) -> None:
        """The silence while an image is generated is the image working.

        This hedge waits for the *first frame*, which for an image turn is
        already after the image request started; firing here would ask a
        sibling to generate the same image again.
        """

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._request()
        request["tools"] = [{"type": "image_generation"}]
        calls: list = []

        async def primary():
            await asyncio.sleep(0.2)
            yield {"type": "response.output_text.delta", "delta": "image"}

        async def dispatch(_request):
            calls.append("frame")
            return _HedgeStream([]), request

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            winner, first, _ = await streaming._hedged_first_chunk(
                primary().__aiter__(), request, start_timeout_seconds=2
            )
            await winner.aclose()

        self.assertEqual("image", first["delta"])
        self.assertEqual([], calls)

    async def test_a_route_that_loses_the_head_race_is_charged_and_parked(self) -> None:
        """A hung primary must not keep being selected first forever.

        The hedge winner serves the turn, so the losing route is cancelled
        rather than failed -- but it produced no head for the whole delay.  The
        loss is one ordinary failure: the configured threshold (2) parks it for
        the ordinary cooldown, and the loss never inherits the heavy-replay
        300 s park.
        """

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        self.set_env(hooks._STREAM_START_TIMEOUT_COOLDOWN_SECONDS_ENV, "1800")
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.05")
        hedge_response, hedge_payload = self._hedge_response("order1-b")

        async def dispatch(request_data, *, stage="first_frame"):
            return hedge_response, hedge_payload

        async def hung_primary():
            # The live router records the selected deployment in the shared
            # box before it starts waiting; the charge reads that identity.
            box = hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.get()
            if isinstance(box, dict):
                box["marker"] = {
                    "model_info": {"id": "order1-a", "order": 1},
                    "litellm_params": {"model": "openai/gpt-x"},
                }
            await asyncio.sleep(5)
            return "primary-response"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            for attempt in range(2):
                hooks._CURRENT_SELECTED_DEPLOYMENT_BOX.set({})
                request = self._request()
                response = await asyncio.wait_for(
                    streaming._await_upstream_dispatch_with_head_hedge(
                        hung_primary(), request
                    ),
                    2,
                )
                self.assertIs(hedge_response, response)
                state = hooks._DEPLOYMENT_COOLDOWNS.get("id:order1-a")
                self.assertIsNotNone(state, attempt)
                self.assertEqual(attempt + 1, state["failures"])

        self.assertGreater(float(state["cooldown_until"]), time.time())
        self.assertLessEqual(
            float(state["cooldown_until"]) - time.time(),
            301.0,
            "a head-race loss must not inherit the heavy-replay 1800 s park",
        )

    async def test_the_head_race_runs_because_the_dispatch_budget_outlives_the_delay(
        self,
    ) -> None:
        """The race must be able to start before the attempt's own deadline.

        The head trigger is the old connect allowance plus the upload allowance
        (about 11.5 s for a 25 KB body), capped by the frame hedge delay; the
        dispatch deadline is the first-event budget (120 s).  A trigger that
        fired after the deadline would never race anything.
        """

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.2")
        request = self._request()
        calls: list = []
        hedge_response, hedge_payload = self._hedge_response("order1-b")

        async def slow_primary():
            await asyncio.sleep(5)
            return "primary-response"

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return hedge_response, hedge_payload

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(
                    slow_primary(), request
                ),
                3,
            )

        self.assertIs(hedge_response, response)
        self.assertEqual(["head"], calls)

    async def test_a_slow_head_is_raced_by_a_sibling_dispatch(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.05")
        request = self._request()
        calls: list = []
        hedge_response, hedge_payload = self._hedge_response("order1-b")

        async def slow_dispatch():
            await asyncio.sleep(5)
            return "primary-response"

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return hedge_response, hedge_payload

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(
                    slow_dispatch(), request
                ),
                2,
            )

        self.assertIs(hedge_response, response)
        self.assertEqual(["head"], calls)
        # The turn belongs to the route whose head arrived first.
        self.assertEqual("order1-b", hooks._request_model_info(request).get("id"))
        self.assertEqual(
            "order1-b", hooks._remembered_attempt_route(request).get("deployment_id")
        )

    async def test_a_head_that_answers_first_never_asks_a_sibling(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._request()
        calls: list = []

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return None

        async def fast_dispatch():
            return "primary-response"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(
                    fast_dispatch(), request
                ),
                2,
            )
            await asyncio.sleep(0.05)

        self.assertEqual("primary-response", response)
        self.assertEqual([], calls)

    async def test_a_primary_head_closes_a_sibling_that_also_returned(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        routing = sys.modules["young_router.proxy.routing"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        ready = asyncio.Event()
        request = self._request()
        primary, _ = self._hedge_response("order1-a")
        sibling, payload = self._hedge_response("order1-b")

        async def primary_dispatch():
            await ready.wait()
            return primary

        async def sibling_dispatch(_request, *, stage):
            ready.set()
            return sibling, payload

        with (
            mock.patch.object(streaming, "_dispatch_stream_hedge", sibling_dispatch),
            mock.patch.object(routing, "_release_request_concurrency_slots") as release,
        ):
            result = await streaming._await_upstream_dispatch_with_head_hedge(primary_dispatch(), request)

        self.assertIs(primary, result)
        self.assertFalse(primary.closed)
        self.assertTrue(sibling.closed)
        release.assert_called_once()
        self.assertEqual(release.call_args.kwargs, {"deployment_only": True})
        self.assertEqual(release.call_args.args[0]["model_info"]["id"], "order1-b")

    async def test_a_failed_head_hedge_leaves_the_primary_running(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._request()

        async def dispatch(request_data, *, stage="first_frame"):
            return None

        async def primary():
            await asyncio.sleep(0.2)
            return "primary-response"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await asyncio.wait_for(
                streaming._await_upstream_dispatch_with_head_hedge(primary(), request),
                2,
            )

        self.assertEqual("primary-response", response)

    async def test_the_hedge_switch_turns_the_head_race_off(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0")
        request = self._request()
        calls: list = []

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return self._hedge_response("order1-b")

        async def primary():
            return "primary-response"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await streaming._await_upstream_dispatch_with_head_hedge(
                primary(), request
            )

        self.assertEqual("primary-response", response)
        self.assertEqual([], calls)

    async def test_a_hedged_dispatch_does_not_hedge_again(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._request()
        request["litellm_metadata"] = {hooks._HEAD_HEDGE_METADATA_KEY: True}
        calls: list = []

        async def dispatch(request_data, *, stage="first_frame"):
            calls.append(stage)
            return self._hedge_response("order1-b")

        async def primary():
            await asyncio.sleep(0.05)
            return "primary-response"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await streaming._await_upstream_dispatch_with_head_hedge(
                primary(), request
            )

        self.assertEqual("primary-response", response)
        self.assertEqual([], calls)

    async def test_a_turn_asks_at_most_one_sibling_across_its_dispatch_layers(
        self,
    ) -> None:
        """One head race per turn, however many wrapper layers run it.

        A turn passes the same dispatch through several layers (the tool
        adapter, the Responses helper, its bridge), and the head race runs in
        each of them with its own timer and the parent's request data.  The
        payload marker does not reach those sibling layers, so live 2026-11-20
        one replay asked three siblings inside four seconds.  The claim is
        keyed by the turn's identifiers and shared across the layers.
        """

        hooks, proxy_server = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        streaming._HEAD_HEDGED_REQUESTS.clear()
        self.addCleanup(streaming._HEAD_HEDGED_REQUESTS.clear)
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.02")
        request = self._chat_request()
        hedge_response, _hedge_payload = self._hedge_response("order1-b")
        dispatched: list = []

        class FakeRouter:
            async def acompletion(self, **kwargs):
                dispatched.append(kwargs)
                return hedge_response

        proxy_server.llm_router = FakeRouter()

        async def primary():
            await asyncio.sleep(0.3)
            return "primary-response"

        results = await asyncio.gather(
            streaming._await_upstream_dispatch_with_head_hedge(
                primary(), request
            ),
            streaming._await_upstream_dispatch_with_head_hedge(
                primary(), request
            ),
        )

        self.assertEqual(1, len(dispatched))
        self.assertIn("primary-response", results)

    async def test_the_winner_marks_the_losing_hedge_before_it_cancels(self) -> None:
        """Only the race's own cancellation may be read as a lost hedge.

        The cancellation path settles the hedge's log row as "another route
        answered first" -- but a cancellation because the client left or the
        process stopped must keep the ending that path owns.  The winner marks
        the task it cancels; nothing else sets that mark.
        """

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        streaming._HEAD_HEDGED_REQUESTS.clear()
        self.addCleanup(streaming._HEAD_HEDGED_REQUESTS.clear)
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        request = self._chat_request()
        hedge_tasks: list = []

        async def dispatch(request_data, *, stage="first_frame"):
            hedge_tasks.append(asyncio.current_task())
            await asyncio.sleep(10)

        async def primary():
            await asyncio.sleep(0.05)
            return "primary-response"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            response = await streaming._await_upstream_dispatch_with_head_hedge(
                primary(), request
            )

        self.assertEqual("primary-response", response)
        self.assertEqual(1, len(hedge_tasks))
        self.assertTrue(
            getattr(hedge_tasks[0], "_young_router_hedge_lost", False)
        )

    async def test_a_hedge_does_not_hedge_again_when_the_router_drops_the_marker(
        self,
    ) -> None:
        """The guard is a context fact, not only a payload field.

        LiteLLM rebuilds the kwargs between router and deployment, and on
        2026-11-20 the head-hedge marker did not survive that rebuild: one
        heavy Codex replay asked three siblings inside four seconds, each a
        full prefill, and each loser left a 发送中 row behind.  A dispatch that
        runs inside a hedge must stay a hedge whatever shape its kwargs arrive
        in.
        """

        hooks, proxy_server = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        streaming._HEAD_HEDGED_REQUESTS.clear()
        self.addCleanup(streaming._HEAD_HEDGED_REQUESTS.clear)
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.05")
        request = self._chat_request()
        eligible: list = []
        nested: list = []

        async def inner_primary():
            await asyncio.sleep(0.2)
            return "inner-response"

        async def head_hedge(request_data):
            nested.append(request_data)
            return None

        class FakeRouter:
            async def acompletion(self, **kwargs):
                rebuilt = {
                    key: value
                    for key, value in kwargs.items()
                    if key != "litellm_metadata"
                }
                rebuilt["litellm_metadata"] = {}
                eligible.append(streaming._head_hedge_eligible(rebuilt))
                with mock.patch.object(streaming, "_head_hedge", head_hedge):
                    return await streaming._await_upstream_dispatch_with_head_hedge(
                        inner_primary(), rebuilt
                    )

        proxy_server.llm_router = FakeRouter()

        result = await asyncio.wait_for(
            streaming._dispatch_stream_hedge(request, stage="head"),
            3,
        )

        self.assertIsNotNone(result)
        self.assertEqual([False], eligible)
        self.assertEqual([], nested)

    def test_the_hedge_marker_is_read_from_the_params_the_router_keeps(
        self,
    ) -> None:
        """A rebuilt request keeps ``metadata`` under ``litellm_params``."""

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        request = self._chat_request()
        request["litellm_metadata"] = {}
        request["litellm_params"]["metadata"] = {
            hooks._HEAD_HEDGE_METADATA_KEY: True,
        }

        self.assertFalse(streaming._head_hedge_eligible(request))

    def test_a_hedge_dispatch_row_is_marked_as_internal(self) -> None:
        """The request log can tell a sibling race from a hop of the turn."""

        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        routing = sys.modules["young_router.proxy.routing"]
        request = self._chat_request()
        token = streaming._CURRENT_HEAD_HEDGE_ATTEMPT.set(True)
        try:
            record = routing._request_log_record("pending", request)
        finally:
            streaming._CURRENT_HEAD_HEDGE_ATTEMPT.reset(token)

        self.assertEqual("head_hedge", record.get("internal_attempt"))

    async def test_a_head_that_never_arrives_is_cut_by_the_dispatch_budget(self) -> None:
        hooks, _ = load_hook_module()
        streaming = sys.modules["young_router.proxy.streaming"]
        self.set_env(hooks._STREAM_HEDGE_DELAY_SECONDS_ENV, "0.01")
        self.set_env(hooks._STREAM_START_TIMEOUT_SECONDS_ENV, "1")
        request = self._request()

        async def dispatch(request_data, *, stage="first_frame"):
            return None

        async def never_answers():
            await asyncio.sleep(30)
            return "too late"

        with mock.patch.object(streaming, "_dispatch_stream_hedge", dispatch):
            with self.assertRaisesRegex(TimeoutError, "stream start timeout"):
                await asyncio.wait_for(
                    streaming._await_upstream_dispatch_with_head_hedge(
                        never_answers(), request
                    ),
                    5,
                )
