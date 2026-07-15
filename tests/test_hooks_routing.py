from __future__ import annotations

from hook_test_utils import *


class HookRoutingTests(HookTestCase):
    async def test_filter_deployments_keeps_image_tool_candidates_for_runtime_probe(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        deployments = [
            {
                "litellm_params": {"model": "openai/dynamic-text"},
                "model_info": {
                    "id": "dynamic-a",
                    "provider": "any-provider",
                    "supports_responses_image_generation_tool": False,
                },
            },
            {
                "litellm_params": {"model": "openai/dynamic-image"},
                "model_info": {
                    "id": "dynamic-b",
                    "provider": "another-provider",
                    "supports_responses_image_generation_tool": True,
                },
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[],
            request_kwargs={"tools": [{"type": "image_generation"}]},
        )

        self.assertEqual(filtered, deployments)

    async def test_filter_deployments_keeps_candidates_without_current_image_capability(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        deployments = [
            {
                "litellm_params": {"model": "openai/dynamic-text"},
                "model_info": {"id": "dynamic-a"},
            },
            {
                "litellm_params": {"model": "openai/dynamic-image"},
                "model_info": {"id": "dynamic-b"},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[],
            request_kwargs={"tools": [{"type": "image_generation"}]},
        )

        self.assertEqual(filtered, deployments)

    async def test_filter_deployments_preserves_user_surface_order_for_codex_tools(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        chat_only = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.backup.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "chat-only",
                "provider": "backup_provider",
                "api_key_name": "x-plus",
                "upstream_url_surface": "openai/chat",
                "supported_upstream_url_surfaces": ["openai/chat", "anthropic"],
            },
        }
        responses = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://headers.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "responses",
                "provider": "compat_provider",
                "api_key_name": "x-plus",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        }
        deployments = [chat_only, responses]

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=[],
            request_kwargs={
                "call_type": "aresponses",
                "stream": True,
                "client_metadata": {"session_id": "codex-thread"},
                "tools": [
                    {"type": "function", "name": "exec_command"},
                    {"type": "custom", "name": "apply_patch"},
                    {"type": "web_search"},
                ],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_filter_deployments_keeps_chat_surface_when_no_responses_candidate(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        deployments = [
            {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "chat-only",
                    "provider": "backup_provider",
                    "api_key_name": "x-plus",
                    "upstream_url_surface": "openai/chat",
                    "supported_upstream_url_surfaces": ["openai/chat"],
                },
            }
        ]

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=[],
            request_kwargs={
                "call_type": "aresponses",
                "stream": True,
                "client_metadata": {"session_id": "codex-thread"},
                "tools": [{"type": "function", "name": "exec_command"}],
            },
        )

        self.assertEqual(filtered, deployments)

    async def test_no_healthy_deployments_bad_request_is_route_exhaustion(self) -> None:
        hooks, _ = load_hook_module()

        class BadRequestError(Exception):
            status_code = 400

        error = BadRequestError(
            "You passed in model=balanced-chat. There are no healthy deployments for this model. "
            "Received Model Group=balanced-chat Available Model Group Fallbacks=None"
        )

        self.assertTrue(hooks._is_no_deployments_available_error(error))
        self.assertTrue(hooks._is_route_recovery_poll_error(error))

    def test_mark_exception_preserves_existing_exclusions_and_adds_failed_deployment(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        request_kwargs = {
            "_excluded_deployment_ids": ["already-failed"],
            "model_info": {"id": "newly-failed", "order": 2},
        }

        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "newly-failed")
        self.assertEqual(error.failed_deployment_order, 2)
        self.assertEqual(
            request_kwargs["_excluded_deployment_ids"],
            ["already-failed", "newly-failed"],
        )
        self.assertEqual(error.num_retries, 0)

    def test_mark_exception_keeps_timeout_route_retryable_without_excluding_deployment(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("upstream gateway timeout after 60s")
        error.status_code = 504
        request_kwargs = {
            "_excluded_deployment_ids": ["already-failed"],
            "model_info": {
                "id": "chatroute",
                "order": 1,
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
        }

        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "chatroute")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["already-failed"])
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(error))

    def test_mark_exception_keeps_rate_limit_route_retryable_without_excluding_deployment(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("upstream 429 rate limit exceeded; retry after 10 seconds")
        error.status_code = 429
        request_kwargs = {
            "_excluded_deployment_ids": ["already-failed"],
            "model_info": {
                "id": "chatroute",
                "order": 1,
                "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
            },
        }

        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        self.assertEqual(error.failed_deployment_id, "chatroute")
        self.assertEqual(error.failed_deployment_order, 1)
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["already-failed"])
        self.assertEqual(error.excluded_deployment_ids, ["already-failed"])
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(error))

    async def test_deployment_cooldown_respects_configured_failure_threshold(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
            {"litellm_params": {"model": "openai/x-pro"}, "model_info": {"id": "x-pro"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _ in range(2):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments[1:])

    async def test_deployment_cooldown_defaults_to_two_failures(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_persists_across_worker_memory(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as temp_dir:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(temp_dir) / "deployment-cooldowns.json"),
            )
            route_key = "backup_provider / openai/default-chat / host=api.backup.example / key=x-plus / order=2"
            deployments = [
                {
                    "litellm_params": {
                        "model": "openai/default-chat",
                        "api_base": "https://api.backup.example/v1",
                        "order": 2,
                    },
                    "model_info": {
                        "id": "stable-backup_provider",
                        "provider": "backup_provider",
                        "api_key_name": "x-plus",
                        "route_key": route_key,
                    },
                },
                {
                    "litellm_params": {
                        "model": "openai/default-chat",
                        "api_base": "https://headers.example/v1",
                        "order": 2,
                    },
                    "model_info": {
                        "id": "healthy-compat_provider",
                        "provider": "compat_provider",
                        "api_key_name": "x-plus",
                    },
                },
            ]
            request_kwargs = {
                "model": "default-chat",
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "stable-backup_provider",
                    "provider": "backup_provider",
                    "api_key_name": "x-plus",
                    "route_key": route_key,
                },
            }

            for _ in range(2):
                error = RuntimeError("insufficient account balance")
                error.status_code = 403
                hooks._mark_exception_for_deployment_failover(error, request_kwargs)

            cooldown_payload = json.loads(
                Path(temp_dir, "deployment-cooldowns.json").read_text(encoding="utf-8")
            )
            cooldown_state = cooldown_payload["cooldowns"]["id:stable-backup_provider"]
            self.assertEqual(cooldown_state["model_group"], "default-chat")
            self.assertEqual(cooldown_state["provider"], "backup_provider")
            self.assertEqual(cooldown_state["upstream_model"], "openai/default-chat")
            self.assertEqual(cooldown_state["api_base_host"], "api.backup.example")
            self.assertEqual(cooldown_state["deployment_order"], 2)

            hooks._DEPLOYMENT_COOLDOWNS.clear()

            filtered = await hook.async_filter_deployments(
                "default-chat",
                deployments,
                messages=None,
                request_kwargs={},
            )
            self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_success_clears_failure_count(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-pro"}, "model_info": {"id": "x-pro"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _ in range(3):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

        await hook.async_log_success_event(
            request_kwargs,
            {"ok": True},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_stream_start_does_not_clear_deployment_cooldown(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        request_kwargs = {
            "model": "default-chat",
            "stream": True,
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)
        self.assertTrue(hooks._DEPLOYMENT_COOLDOWNS)

        await hook.async_log_success_event(
            request_kwargs,
            {"type": "response.created", "response": {"status": "in_progress"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        await hook.async_log_stream_event(
            request_kwargs,
            {"type": "response.created", "response": {"status": "in_progress"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        self.assertTrue(hooks._DEPLOYMENT_COOLDOWNS)

        await hook.async_log_stream_event(
            request_kwargs,
            {"type": "response.completed", "response": {"status": "completed"}},
            datetime.now(timezone.utc),
            datetime.now(timezone.utc),
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    async def test_deployment_cooldown_does_not_count_sanitized_wrapper(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-pro"}, "model_info": {"id": "x-pro"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for _ in range(2):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        sanitized = RuntimeError("sanitized wrapper")
        sanitized.status_code = 503
        setattr(sanitized, hooks._SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR, True)
        hooks._mark_exception_for_deployment_failover(sanitized, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_filters_all_cooled_candidates_globally(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "3")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]

        for deployment in deployments:
            request_kwargs = {
                "model": "default-chat",
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }
            for _ in range(3):
                error = RuntimeError("temporary upstream failure")
                error.status_code = 503
                hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [])

    async def test_route_recovery_half_opens_one_candidate_when_all_are_cooled(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]

        for deployment in deployments:
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                },
            )

        recovery_request = {
            "litellm_metadata": {
                hooks._ROUTE_RECOVERY_POLL_METADATA_KEY: True,
            },
        }
        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs=recovery_request,
        )
        self.assertEqual(len(filtered), 1)
        self.assertIn(filtered[0], deployments)

        available, cooled, cooldown_filtered = hooks._with_active_deployment_cooldowns(
            deployments,
            request_kwargs=recovery_request,
        )
        self.assertTrue(cooldown_filtered)
        self.assertEqual(available, filtered)
        self.assertEqual(
            [entry.get("half_open_probe") is True for entry in cooled].count(True),
            1,
        )

    async def test_route_recovery_still_prefers_healthy_peer_over_half_open_probe(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "litellm_params": deployments[0]["litellm_params"],
                "model_info": deployments[0]["model_info"],
            },
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={
                "litellm_metadata": {
                    hooks._ROUTE_RECOVERY_POLL_METADATA_KEY: True,
                },
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_expires_after_ttl(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "0.01")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

        await asyncio.sleep(0.02)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_deployment_cooldown_does_not_count_request_shape_context_or_rate_limit_errors(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        errors = []
        context_error = RuntimeError(
            "This model's maximum context length is 4096 tokens, but your prompt contains 5000 tokens."
        )
        context_error.status_code = 400
        errors.append(context_error)

        request_shape_error = RuntimeError(
            "OpenAIException invalid_request_error: system messages are not allowed"
        )
        request_shape_error.status_code = 400
        errors.append(request_shape_error)

        rate_limit_error = RuntimeError("rate limit exceeded; retry after 10 seconds")
        rate_limit_error.status_code = 429
        errors.append(rate_limit_error)

        for error in errors:
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_deployment_cooldown_does_not_count_timeout_or_long_wait_errors(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        timeout_error = RuntimeError("upstream gateway timeout")
        timeout_error.status_code = 504
        hooks._mark_exception_for_deployment_failover(timeout_error, request_kwargs)

        long_wait_error = RuntimeError("upstream returned 500 after 60s")
        long_wait_error.status_code = 500
        hooks._mark_exception_for_deployment_failover(
            long_wait_error,
            {**request_kwargs, "duration_ms": 60000},
        )

        stream_idle_error = TimeoutError("stream idle timeout")
        stream_idle_error.status_code = 504
        stream_idle_error.body = {"reason": "stream_idle_timeout"}
        hooks._mark_exception_for_deployment_failover(stream_idle_error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_deployment_cooldown_does_not_count_network_connectivity_errors(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        for message in (
            "InternalServerError: OpenAIException - Cannot connect to host api.example.test:443 ssl:default",
            "InternalServerError: OpenAIException - Server disconnected",
        ):
            error = RuntimeError(message)
            error.status_code = 500
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)
        self.assertNotIn("_excluded_deployment_ids", request_kwargs)

    def test_network_connectivity_error_is_retryable_but_not_hard_deployment_failure(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "InternalServerError: OpenAIException - Cannot connect to host api.example.test:443 ssl:default"
        )
        exc.status_code = 500

        self.assertTrue(hooks._exception_indicates_network_connectivity_error(exc))
        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(exc))
        self.assertFalse(hooks._should_count_deployment_failure_for_cooldown(exc))
        self.assertEqual(hooks._trace_exception(exc)["reason"], "upstream-network-connectivity")

    async def test_deployment_cooldown_does_not_count_stream_start_timeout_after_chunks(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        saw_chunk_error = TimeoutError("stream did not start before timeout")
        saw_chunk_error.status_code = 504
        saw_chunk_error.body = {"reason": "stream_start_timeout", "saw_chunk": True}
        hooks._mark_exception_for_deployment_failover(saw_chunk_error, request_kwargs)

        buffered_chunk_error = TimeoutError("stream did not start before timeout")
        buffered_chunk_error.status_code = 504
        buffered_chunk_error.body = {"reason": "stream_start_timeout", "buffered_chunks": 104}
        hooks._mark_exception_for_deployment_failover(buffered_chunk_error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, deployments)

    async def test_deployment_cooldown_counts_local_stream_start_timeout(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
            "duration_ms": 526918,
        }

        for _ in range(2):
            error = TimeoutError("stream did not start before timeout")
            error.status_code = 504
            error.body = {"reason": "stream_start_timeout"}
            hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_counts_quota_or_auth_failures(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
        }

        error = RuntimeError("insufficient_quota: account balance exhausted")
        error.status_code = 403
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_counts_long_quota_or_auth_failures(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]
        request_kwargs = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "x-cheap"},
            "duration_ms": 60000,
        }

        error = RuntimeError("insufficient_quota: account balance exhausted after 60 seconds")
        error.status_code = 403
        hooks._mark_exception_for_deployment_failover(error, request_kwargs)

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_does_not_cross_deployment_ids_with_same_route_key(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        route_key = "compat_provider / openai/default-chat / key=x-plus / order=2"
        deployments = [
            {
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"id": "new-route-id-a", "route_key": route_key},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"id": "new-route-id-b", "route_key": route_key},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 3},
                "model_info": {
                    "id": "healthy-pro",
                    "route_key": "compat_provider / openai/default-chat / key=x-pro / order=3",
                },
            },
        ]

        for _ in range(2):
            error = RuntimeError("temporary upstream failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "litellm_params": {"model": "openai/default-chat", "order": 2},
                    "model_info": {"id": "new-route-id-a", "route_key": route_key},
                },
            )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1], deployments[2]])

    async def test_deployment_cooldown_uses_route_key_when_deployment_id_missing(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        route_key = "legacy / openai/default-chat / key=x-plus / order=2"
        deployments = [
            {
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"route_key": route_key},
            },
            {
                "litellm_params": {"model": "openai/default-chat", "order": 3},
                "model_info": {"route_key": "legacy / openai/default-chat / key=x-pro / order=3"},
            },
        ]

        error = RuntimeError("temporary upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "litellm_params": {"model": "openai/default-chat", "order": 2},
                "model_info": {"route_key": route_key},
            },
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )
        self.assertEqual(filtered, [deployments[1]])

    async def test_deployment_cooldown_isolated_between_responses_and_chat_surfaces(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.example.test/v1",
                "order": 1,
            },
            "model_info": {
                "id": "dual-protocol-route",
                "provider": "primary",
                "api_key_name": "default",
                "upstream_url_surface": "openai/responses",
                "supported_upstream_url_surfaces": [
                    "openai/responses",
                    "openai/chat",
                ],
            },
        }
        error = RuntimeError("temporary responses upstream failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "call_type": "aresponses",
                "model": "default-chat",
                "stream": True,
                "_litellm_menu_upstream_url_surface": "openai/responses",
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            },
        )

        responses_filtered = await hook.async_filter_deployments(
            "default-chat",
            [deployment],
            messages=None,
            request_kwargs={
                "call_type": "aresponses",
                "model": "default-chat",
                "stream": True,
            },
        )
        chat_filtered = await hook.async_filter_deployments(
            "default-chat",
            [deployment],
            messages=None,
            request_kwargs={
                "call_type": "aresponses",
                "model": "default-chat",
                "stream": True,
                "_litellm_menu_upstream_url_surface": "openai/chat",
            },
        )

        self.assertEqual(responses_filtered, [deployment])
        self.assertEqual(chat_filtered, [deployment])
        self.assertEqual(
            hooks._request_surface_for_deployment({}, deployment),
            "openai/chat",
        )
        self.assertIn(
            "id:dual-protocol-route|surface:openai/responses",
            hooks._DEPLOYMENT_COOLDOWNS,
        )
        self.assertNotIn(
            "id:dual-protocol-route|surface:openai/chat",
            hooks._DEPLOYMENT_COOLDOWNS,
        )

    def test_surface_adapter_uses_exact_model_for_all_three_protocols(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._surface_adapter_model("openai/vendor/model", "openai/responses"),
            "openai/vendor/model",
        )
        self.assertEqual(
            hooks._surface_adapter_model("openai/responses/vendor/model", "anthropic"),
            "anthropic/vendor/model",
        )
        self.assertEqual(
            hooks._surface_adapter_model("anthropic/vendor/model", "openai/chat"),
            "openai/vendor/model",
        )

    def test_ordered_surface_fallback_exhausts_a_before_same_order_b(self) -> None:
        hooks, _ = load_hook_module()
        deployment_a = {
            "litellm_params": {"model": "openai/vendor-a", "order": 1},
            "model_info": {
                "id": "route-a",
                "supported_upstream_url_surfaces": [
                    "openai/responses",
                    "anthropic",
                    "openai/chat",
                ],
            },
        }
        deployment_b = {
            "litellm_params": {"model": "openai/vendor-b", "order": 1},
            "model_info": {
                "id": "route-b",
                "supported_upstream_url_surfaces": ["openai/responses"],
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment_a, deployment_b]

        request = {
            "model": "default-chat",
            "_target_order": 1,
            "_litellm_menu_upstream_url_surface": "openai/responses",
            "_litellm_menu_attempted_upstream_url_surfaces": ["openai/responses"],
            "_litellm_menu_upstream_url_surface_deployment_id": "route-a",
            "model_info": deployment_a["model_info"],
            "litellm_params": deployment_a["litellm_params"],
        }

        first_error = RuntimeError("responses endpoint not found")
        first_error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(first_error, request)
        first = hooks._ordered_deployment_fallback_entry(Router(), first_error, request)

        self.assertEqual(first["_litellm_menu_upstream_url_surface"], "anthropic")
        self.assertEqual(first["_litellm_menu_surface_target_deployment_id"], "route-a")
        self.assertNotIn("route-a", first.get("_excluded_deployment_ids", []))

        request.update({key: value for key, value in first.items() if key != "model"})
        request["_litellm_menu_upstream_url_surface_deployment_id"] = "route-a"
        second_error = RuntimeError("anthropic messages endpoint not found")
        second_error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(second_error, request)
        second = hooks._ordered_deployment_fallback_entry(Router(), second_error, request)

        self.assertEqual(second["_litellm_menu_upstream_url_surface"], "openai/chat")
        self.assertEqual(second["_litellm_menu_surface_target_deployment_id"], "route-a")

        request.update({key: value for key, value in second.items() if key != "model"})
        request["_litellm_menu_upstream_url_surface_deployment_id"] = "route-a"
        third_error = RuntimeError("chat completions endpoint not found")
        third_error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(third_error, request)
        third = hooks._ordered_deployment_fallback_entry(Router(), third_error, request)

        self.assertEqual(third["_target_order"], 1)
        self.assertEqual(third["_excluded_deployment_ids"], ["route-a"])
        self.assertNotIn("_litellm_menu_upstream_url_surface", third)

    async def test_deployment_is_filtered_only_after_all_three_surfaces_cool_down(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/vendor-a", "order": 1},
            "model_info": {
                "id": "three-surface-route",
                "supported_upstream_url_surfaces": [
                    "openai/responses",
                    "anthropic",
                    "openai/chat",
                ],
            },
        }

        for index, surface in enumerate(
            ["openai/responses", "anthropic", "openai/chat"],
            start=1,
        ):
            error = RuntimeError(f"temporary {surface} failure")
            error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                error,
                {
                    "model": "default-chat",
                    "_litellm_menu_upstream_url_surface": surface,
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                },
            )
            filtered = await hook.async_filter_deployments(
                "default-chat",
                [deployment],
                messages=None,
                request_kwargs={},
            )
            self.assertEqual(filtered, [deployment] if index < 3 else [])

    def test_current_surface_incompatibility_is_narrowly_classified(self) -> None:
        hooks, _ = load_hook_module()
        request = {"_litellm_menu_upstream_url_surface": "openai/responses"}
        endpoint_error = RuntimeError("endpoint not found")
        endpoint_error.status_code = 404
        schema_error = RuntimeError(
            "Invalid Responses API request: invalid_union; expected string, received array"
        )
        schema_error.status_code = 400
        policy_error = RuntimeError("request violates content policy")
        policy_error.status_code = 400

        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(endpoint_error, request)
        )
        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(schema_error, request)
        )
        self.assertFalse(
            hooks._is_current_upstream_surface_incompatible_error(policy_error, request)
        )

    def test_route_key_canonicalizes_api_base_host(self) -> None:
        hooks, _ = load_hook_module()
        old_route_key = "compat_provider / openai/default-chat / key=x-plus / order=2"
        backup_provider = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://api.backup.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "backup_provider-a",
                "provider": "compat_provider",
                "api_key_name": "x-plus",
                "route_key": old_route_key,
            },
        }
        compat_provider = {
            "litellm_params": {
                "model": "openai/default-chat",
                "api_base": "https://headers.example/v1",
                "order": 2,
            },
            "model_info": {
                "id": "compat_provider-a",
                "provider": "compat_provider",
                "api_key_name": "x-plus",
                "route_key": old_route_key,
            },
        }

        backup_provider_key = hooks._deployment_route_key_from_deployment(backup_provider)
        compat_provider_key = hooks._deployment_route_key_from_deployment(compat_provider)

        self.assertEqual(
            backup_provider_key,
            "provider=compat_provider / upstream=openai/default-chat / host=api.backup.example / key=x-plus / order=2",
        )
        self.assertEqual(
            compat_provider_key,
            "provider=compat_provider / upstream=openai/default-chat / host=headers.example / key=x-plus / order=2",
        )
        self.assertNotEqual(backup_provider_key, compat_provider_key)
        self.assertEqual(
            hooks._deployment_route_key_from_request(
                {
                    "litellm_params": backup_provider["litellm_params"],
                    "model_info": backup_provider["model_info"],
                }
            ),
            backup_provider_key,
        )

    def test_route_key_includes_public_model_group_when_available(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "model_name": "llmwebsearch",
            "litellm_params": {
                "model": "openai/vendor/vendor-chat",
                "api_base": "https://openrouter.ai/api/v1",
                "order": 1,
            },
            "model_info": {
                "id": "openrouter-chat",
                "provider": "openrouter",
                "api_key_name": "default",
            },
        }

        self.assertEqual(
            hooks._deployment_route_key_from_deployment(deployment),
            "model=llmwebsearch / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
        )
        self.assertEqual(
            hooks._deployment_route_key_from_request(
                {
                    "model": "llmwebsearch",
                    "litellm_params": deployment["litellm_params"],
                    "model_info": deployment["model_info"],
                }
            ),
            "model=llmwebsearch / provider=openrouter / upstream=openai/vendor/vendor-chat / host=openrouter.ai / key=default / order=1",
        )

    async def test_deployment_cooldown_deployment_id_does_not_cross_api_base_hosts(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        old_route_key = "compat_provider / openai/default-chat / key=x-plus / order=2"
        deployments = [
            {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "new-backup_provider",
                    "provider": "compat_provider",
                    "api_key_name": "x-plus",
                    "route_key": old_route_key,
                },
            },
            {
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "new-compat_provider",
                    "provider": "compat_provider",
                    "api_key_name": "x-plus",
                    "route_key": old_route_key,
                },
            },
        ]
        error = RuntimeError("temporary upstream failure")
        error.status_code = 503

        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "litellm_params": {
                    "model": "openai/default-chat",
                    "api_base": "https://api.backup.example/v1",
                    "order": 2,
                },
                "model_info": {
                    "id": "old-backup_provider",
                    "provider": "compat_provider",
                    "api_key_name": "x-plus",
                    "route_key": old_route_key,
                },
            },
        )

        filtered = await hook.async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs={},
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_does_not_apply_order_before_router(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://api.primary.example/v1",
                    "order": 1,
                },
                "model_info": {"id": "primary_provider", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "compat_provider-pro", "supports_responses_image_generation_tool": True},
            },
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={"original_generic_function": aresponses},
        )

        self.assertEqual(filtered, deployments)

    async def test_responses_api_fallback_target_order_is_honored(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://api.backup.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "_target_order": 3,
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_filter_deployments_honors_weighted_failover_exclusions_before_preferences(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()

        def aresponses():
            pass

        deployments = [
            {
                "litellm_params": {
                    "api_base": "https://headers.example/v1",
                    "order": 2,
                },
                "model_info": {"id": "compat_provider-normal", "supports_responses_image_generation_tool": False},
            },
            {
                "litellm_params": {
                    "api_base": "https://api.backup.example/v1",
                    "order": 3,
                },
                "model_info": {"id": "backup_provider", "supports_responses_image_generation_tool": True},
            },
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=None,
            request_kwargs={
                "original_generic_function": aresponses,
                "_excluded_deployment_ids": ["compat_provider-normal"],
            },
        )

        self.assertEqual(filtered, [deployments[1]])

    async def test_filter_deployments_ignores_prompt_without_structured_tool(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        deployments = [
            {"model_info": {"supports_responses_image_generation_tool": False}},
            {"model_info": {"supports_responses_image_generation_tool": True}},
        ]

        filtered = await hook.async_filter_deployments(
            "runtime-model-alias",
            deployments,
            messages=[{"role": "user", "content": "image_generation tool request please"}],
            request_kwargs={},
        )

        self.assertEqual(filtered, deployments)

    async def test_no_deployments_for_order_continues_to_next_order(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def original_stream():
            yield {"type": "response.created", "response": {"id": "resp-original"}}
            exc = RuntimeError("upstream 503 on cheap")
            exc.status_code = 503
            raise exc

        async def pro_stream():
            yield {"type": "response.output_text.delta", "delta": "pro ok"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        class RouterRateLimitError(Exception):
            pass

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "cheap-a"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "plus-a"},
                    },
                    {
                        "litellm_params": {"order": 3},
                        "model_info": {"id": "pro-a"},
                    },
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                if payload.get("_target_order") == 2:
                    raise RouterRateLimitError("No deployments available for selected model")
                if payload.get("_target_order") == 3:
                    return pro_stream()
                raise AssertionError(f"unexpected target order: {payload.get('_target_order')}")

        proxy_server.llm_router = FakeRouter()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Say hi."}],
            "stream": True,
            "model_info": {"id": "cheap-a", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hook.async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=original_stream(),
                request_data=request_data,
            )
        ]

        self.assertEqual([call.get("_target_order") for call in calls], [2, 3])
        self.assertEqual(
            chunks,
            [
                {"type": "response.output_text.delta", "delta": "pro ok"},
                {"type": "response.completed", "response": {"id": "resp-pro"}},
            ],
        )

    def test_route_recovery_next_poll_order_uses_unfiltered_configured_orders(self) -> None:
        hooks, _ = load_hook_module()
        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {"id": "plus-a"},
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {"id": "pro-a"},
            },
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

        def original_get_all_deployments(self, model_name, team_id=None):
            return deployments

        FakeRouter._get_all_deployments._original_get_all_deployments = original_get_all_deployments
        exc = RuntimeError("No deployments available for selected model")
        exc.failed_deployment_order = 3
        request_data = {
            "model": "default-chat",
            "_excluded_deployment_ids": ["plus-a", "pro-a"],
        }
        token = hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.set({"plus-a", "pro-a"})
        try:
            next_order = hooks._route_recovery_next_poll_order(
                FakeRouter(),
                request_data,
                exc,
            )
        finally:
            hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

        self.assertEqual(next_order, 2)

    def test_ordered_deployment_fallback_uses_unfiltered_configured_deployments(self) -> None:
        hooks, _ = load_hook_module()
        deployments = [
            {
                "litellm_params": {"order": 2},
                "model_info": {"id": "plus-a"},
            },
            {
                "litellm_params": {"order": 3},
                "model_info": {"id": "pro-a"},
            },
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

        def original_get_all_deployments(self, model_name, team_id=None):
            return deployments

        FakeRouter._get_all_deployments._original_get_all_deployments = original_get_all_deployments
        exc = RuntimeError("upstream 502 on plus")
        exc.status_code = 502
        exc.failed_deployment_id = "plus-a"
        exc.failed_deployment_order = 2
        request_data = {
            "model": "default-chat",
            "_excluded_deployment_ids": ["plus-a"],
        }
        token = hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.set({"plus-a", "pro-a"})
        try:
            fallback_entry = hooks._ordered_deployment_fallback_entry(
                FakeRouter(),
                exc,
                request_data,
            )
        finally:
            hooks._CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

        self.assertEqual(
            fallback_entry,
            {
                "model": "default-chat",
                "_target_order": 3,
                "_excluded_deployment_ids": ["plus-a"],
                hooks._VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY: ["pro-a"],
            },
        )

    def test_route_recovery_ignores_compatible_bad_request(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "OpenAIException invalid_request_error: system messages are not allowed"
        )
        exc.status_code = 400
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        self.assertFalse(hooks._is_route_recovery_poll_error(exc))
        self.assertFalse(hooks._should_return_route_recovery_stream(exc, request_data))

    def test_image_parameter_bad_request_is_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "OpenAIException invalid_request_error: unsupported image size 1792x1024"
        )
        exc.status_code = 400

        self.assertTrue(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertTrue(hooks._is_priority_deployment_failover_error(exc))
        self.assertTrue(hooks._should_sanitize_final_upstream_route_error(exc))

    def test_image_generation_tool_unsupported_422_is_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "invalid_request_error: unsupported tool type image_generation"
        )
        exc.status_code = 422

        self.assertTrue(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertTrue(hooks._is_priority_deployment_failover_error(exc))

    def test_responses_schema_bad_request_is_not_deployment_failover(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            'OpenAIException - {"error":{"code":"invalid_prompt",'
            '"message":"Invalid Responses API request"},'
            '"metadata":{"raw":"[{\\n \\\"code\\\": \\\"invalid_union\\\",'
            '\\n \\\"errors\\\": [[{\\n \\\"expected\\\": \\\"string\\\",'
            '\\n \\\"code\\\": \\\"invalid_type\\\",'
            '\\n \\\"message\\\": \\\"Invalid input: expected string, received array\\\"'
            '}]]}]"}}'
        )
        exc.status_code = 400

        self.assertTrue(hooks._is_responses_schema_unsupported_error(exc))
        self.assertFalse(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertFalse(hooks._is_deployment_compatible_bad_request_error(exc))
        self.assertFalse(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertEqual(
            hooks._trace_exception(exc)["reason"],
            "responses-schema-unsupported",
        )

    def test_ssl_verification_error_is_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "APIConnectionError: [SSL: CERTIFICATE_VERIFY_FAILED] certificate verify failed: self signed certificate"
        )

        self.assertTrue(hooks._is_ssl_verification_error(exc))
        self.assertFalse(hooks._exception_indicates_network_connectivity_error(exc))
        self.assertTrue(hooks._is_priority_deployment_failover_error(exc))
        self.assertTrue(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertFalse(hooks._should_retry_same_deployment_before_fallback(exc))

    def test_image_generation_tool_runtime_fallback_attempt_limit(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"model_info": {"id": "route-a", "order": 1}},
                    {"model_info": {"id": "route-b", "order": 1}},
                ]

        exc = RuntimeError("invalid_request_error: unsupported tool type image_generation")
        exc.status_code = 422
        exc.failed_deployment_id = "route-a"
        exc.failed_deployment_order = 1
        request_kwargs = {
            "model": "default-chat",
            "tools": [{"type": "image_generation"}],
            "litellm_metadata": {
                hooks._IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY: 3,
            },
        }

        entry = hooks._ordered_deployment_fallback_entry(FakeRouter(), exc, request_kwargs)

        self.assertIsNone(entry)

    def test_prompt_policy_error_is_not_fallback_eligible(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError(
            "OpenAIException invalid_request_error: prompt violates content policy"
        )
        exc.status_code = 400

        self.assertTrue(hooks._is_terminal_prompt_or_policy_error(exc))
        self.assertFalse(hooks._is_image_parameter_or_capability_bad_request_error(exc))
        self.assertFalse(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))

    def test_deployment_order_falls_back_to_route_key_order(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_kwargs = {
            "model_info": {
                "id": "image-order2",
                "route_key": "backup_provider / openai/gpt-image-2 / key=x-image / order=2",
            }
        }

        self.assertEqual(hooks._deployment_order_from_request(request_kwargs), 2)
        self.assertTrue(hooks._request_allows_failed_deployment_order(request_kwargs))

if __name__ == "__main__":
    unittest.main()
