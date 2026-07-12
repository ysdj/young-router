from __future__ import annotations

from hook_test_utils import *


class HookStreamingCompactionTests(HookTestCase):
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
                        "Target at most 20000 tokens. Preserve only unresolved work."
                    ),
                }
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
        self.assertEqual(
            calls[0]["prompt_cache_key"],
            "thread-test-0001",
        )
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
            headers["x-codex-window-id"],
            "thread-test-0001:7",
        )
        metadata = calls[0]["litellm_metadata"]
        self.assertTrue(metadata[hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn("codex_compaction_optimized", metadata)
        self.assertNotIn("codex_compaction_max_output_tokens", metadata)
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_codex_compaction_incomplete_fallback_stream_enters_route_recovery_poll(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}

        async def incomplete_fallback_stream():
            yield {"type": "response.created", "response": {"id": "resp-fallback"}}

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "compact recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered"}}

        class FakeRouter:
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

        self.assertEqual(len(calls), 2)
        self.assertNotIn("use_chat_completions_api", calls[0])
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertNotIn("use_chat_completions_api", calls[1])
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["id"], "resp-recovered")

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
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "compact recovered"},
            chunks,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(chunks[-1]["response"]["id"], "resp-recovered")

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
