from __future__ import annotations

from hook_test_utils import *


class HookStreamingTimeoutTests(HookTestCase):
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

    async def test_structured_compaction_waits_longer_for_first_stream_chunk(self) -> None:
        hooks, _ = load_hook_module()
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
        hook = hooks.LiteLLMMenuHook()

        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "recent-requests.jsonl"
            self.set_log_env(log_path)
            self.set_env("LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS", "0.01")
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

            self.assertEqual(
                chunks,
                [
                    {"type": "response.output_text.delta", "delta": "fallback ok"},
                    {"type": "response.completed", "response": {"id": "resp-fallback"}},
                ],
            )
            self.assertEqual(len(calls), 1)
            self.assertEqual(calls[0]["_excluded_deployment_ids"], ["order1-a"])
            self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
            self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_START_TIMEOUT_METADATA_KEY])

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
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
        hook = hooks.LiteLLMMenuHook()
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

        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "retry ok"},
                {"type": "response.completed", "response": {"id": "resp-retry"}},
            ],
        )
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
                for line in log_path.read_text(encoding="utf-8").splitlines()
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
        hook = hooks.LiteLLMMenuHook()
        self.set_env("LITELLM_MENU_STALL_TIMEOUT_SECONDS", "0.05")

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            await asyncio.sleep(0.01)
            yield {"type": "response.output_text.delta", "delta": "hello"}
            await asyncio.sleep(0.01)
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
            chunks,
            [
                {"type": "response.created", "response": {"id": "resp-original"}},
                {"type": "response.output_text.delta", "delta": "hello"},
                {"type": "response.completed", "response": {"id": "resp-original"}},
            ],
        )

    async def test_streaming_stall_timeout_resets_after_any_reasoning_event(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.reasoning_text.delta", "delta": "internal thinking"}
            await asyncio.sleep(0.05)
            yield {"type": "response.output_text.delta", "delta": "visible later"}
            yield {"type": "response.completed", "response": {"id": "resp-original"}}

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                raise AssertionError("reasoning stream activity must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "1")

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
                chunks,
                [
                    {"type": "response.output_text.delta", "delta": "visible later"},
                    {"type": "response.completed", "response": {"id": "resp-original"}},
                ],
            )
            self.assertEqual(calls, [])
            if log_path.exists():
                self.assertEqual(log_path.read_text(encoding="utf-8").strip(), "")

    async def test_streaming_reasoning_events_are_not_delivered(self) -> None:
        hooks, proxy_server = load_hook_module()

        class FakeRouter:
            async def aresponses(self, **_payload):
                raise AssertionError("active streams must not invoke fallback")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "1")

        async def original_stream():
            yield {"type": "response.output_text.delta", "delta": "\n\n"}
            yield {"type": "response.reasoning_summary_text.delta", "delta": "duplicate text"}
            yield {
                "type": "response.output_item.added",
                "item": {"id": "rs_1", "type": "reasoning", "summary": []},
            }
            yield {"type": "response.output_text.delta", "delta": "visible"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-original",
                    "output": [
                        {"id": "rs_1", "type": "reasoning", "summary": []},
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

        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "visible"},
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp-original",
                        "output": [
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
        hook = hooks.LiteLLMMenuHook()
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

        self.assertEqual(len(chunks), 2)
        self.assertEqual(chunks[0], {"type": "response.output_text.delta", "delta": "fallback ok"})
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
        hook = hooks.LiteLLMMenuHook()
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
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "fallback ok"},
                {"type": "response.completed", "response": {"id": "resp-fallback"}},
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0]["model"], "default-chat")
        self.assertEqual(calls[0]["_target_order"], 1)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["order1-a"])
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])

    async def test_dual_surface_incomplete_responses_stream_recovers_via_chat(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        candidate_results = []
        deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "order": 1,
            },
            "model_info": {
                "id": "dual-route",
                "model_group": "default-chat",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": [
                    "openai/responses",
                    "openai/chat",
                ],
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

        hook = hooks.LiteLLMMenuHook()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment]

            async def aresponses(self, **payload):
                calls.append(payload)
                candidates = await hook.async_filter_deployments(
                    "default-chat",
                    [deployment],
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
            "model_info": deployment["model_info"],
            "litellm_params": deployment["litellm_params"],
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

        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "chat recovered"},
                {
                    "type": "response.completed",
                    "response": {"id": "resp-chat-recovered"},
                },
            ],
        )
        self.assertEqual(len(calls), 1)
        self.assertEqual(candidate_results, [[deployment]])
        self.assertEqual(
            calls[0]["_litellm_menu_upstream_url_surface"],
            "openai/chat",
        )
        self.assertEqual(calls[0]["_target_order"], 1)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["other-route"])
        self.assertEqual([tool["name"] for tool in calls[0]["tools"]], ["exec_command"])
        self.assertNotIn(
            "additional_tools",
            json.dumps(calls[0]["input"], ensure_ascii=False),
        )
        self.assertIn(
            "id:dual-route|surface:openai/responses",
            hooks._DEPLOYMENT_COOLDOWNS,
        )
        self.assertNotIn(
            "id:dual-route|surface:openai/chat",
            hooks._DEPLOYMENT_COOLDOWNS,
        )

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
        hook = hooks.LiteLLMMenuHook()
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
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "recovered ok"},
                {"type": "response.completed", "response": {"id": "resp-recovered"}},
            ],
        )
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
        hook = hooks.LiteLLMMenuHook()
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
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "fallback after retries"},
                {"type": "response.completed", "response": {"id": "resp-fallback"}},
            ],
        )
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
        hook = hooks.LiteLLMMenuHook()
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
                ["response.created", "response.output_text.delta", "response.failed"],
            )
            self.assertEqual(chunks[1]["delta"], "partial answer")
            self.assertEqual(chunks[-1]["response"]["status"], "failed")
            self.assertEqual(chunks[-1]["response"]["error"]["code"], "upstream_route_failure")

            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            stuck_records = [record for record in records if record.get("status") == "stuck"]
            self.assertEqual(len(stuck_records), 1)
            self.assertEqual(stuck_records[0]["stuck"]["reason"], "stream_idle_timeout")
            self.assertEqual(stuck_records[0]["stuck"]["stream_idle_timeout_seconds"], 0.01)
            self.assertTrue(stuck_records[0]["stuck"]["stream_saw_chunk"])

    def test_route_recovery_polls_on_local_stream_timeout(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = TimeoutError("LiteLLM Menu stream idle timeout after 45s without a new chunk")
        exc.status_code = 504
        exc.body = {"reason": "stream_idle_timeout"}
        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }
        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(hooks._should_return_route_recovery_stream(exc, request_data))
        self.assertFalse(hooks._should_retry_final_upstream_route_error(exc, request_data))

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
                for line in log_path.read_text(encoding="utf-8").splitlines()
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
