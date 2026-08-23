from __future__ import annotations

from hook_test_utils import *


class HookRoutingTests(HookTestCase):
    async def test_encrypted_history_continuation_keeps_eligible_routes(self) -> None:
        hooks, _proxy_server = load_hook_module()
        deployments = [
            {"model_info": {"id": "route-low", "order": 1}},
            {"model_info": {"id": "route-target", "order": 2}},
        ]
        replay = {
            "call_type": "aresponses",
            "input": [
                {
                    "type": "compaction",
                    "id": "cmp-existing",
                    "encrypted_content": "opaque-history",
                },
                {"type": "message", "role": "user", "content": "continue"},
            ],
            "client_metadata": {
                "session_id": "thread-encrypted-history",
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
            "_target_order": 2,
        }

        selected = await hooks.LiteLLMMenuHook().async_filter_deployments(
            "default-chat",
            deployments,
            messages=None,
            request_kwargs=replay,
        )

        self.assertEqual(
            [item["model_info"]["id"] for item in selected], ["route-target"]
        )

    def test_recovery_diagnostic_classifies_user_actionable_failures_without_error_text(self) -> None:
        hooks, _proxy_server = load_hook_module()

        cases = [
            (RuntimeError("insufficient credits for this account"), "billing"),
            (RuntimeError("API key unauthorized"), "authentication"),
            (RuntimeError("Cannot connect to host api.example.test"), "network"),
            (RuntimeError("rate limit exceeded"), "rate_limit"),
            (TimeoutError("upstream request timed out"), "timeout"),
            (RuntimeError("temporary upstream issue"), "unknown"),
        ]
        for error, expected_kind in cases:
            with self.subTest(expected_kind=expected_kind):
                diagnostic = hooks._recovery_diagnostic(error)
                self.assertEqual(diagnostic["kind"], expected_kind)
                self.assertIn("title", diagnostic)
                self.assertIn("detail", diagnostic)
                self.assertNotIn("api.example.test", json.dumps(diagnostic))

        forbidden = RuntimeError("provider rejected secret-token-should-not-appear")
        diagnostic = hooks._recovery_diagnostic(forbidden)
        self.assertNotIn("secret-token-should-not-appear", json.dumps(diagnostic))

    def test_recovery_policy_defaults_match_error_boundaries(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, None)

        balance = RuntimeError("insufficient account balance")
        balance.status_code = 403
        rate_limit = RuntimeError("rate limit exceeded")
        rate_limit.status_code = 429
        overload = RuntimeError("upstream overloaded")
        overload.status_code = 503
        network = RuntimeError("Cannot connect to host api.example.test")
        network.status_code = 500
        start_timeout = TimeoutError("stream did not start")
        start_timeout.status_code = 504
        start_timeout.body = {"reason": "stream_start_timeout"}
        idle_timeout = TimeoutError("stream idle timeout")
        idle_timeout.status_code = 504
        idle_timeout.body = {"reason": "stream_idle_timeout"}
        request_error = RuntimeError("OpenAIException invalid_request_error: invalid input")
        request_error.status_code = 400

        self.assertEqual(hooks._recovery_policy_for_exception(balance), "recovery_cooldown")
        self.assertEqual(hooks._recovery_policy_for_exception(rate_limit), "recovery")
        self.assertEqual(hooks._recovery_policy_for_exception(overload), "recovery")
        self.assertEqual(hooks._recovery_policy_for_exception(network), "recovery")
        self.assertEqual(hooks._recovery_policy_for_exception(start_timeout), "recovery_cooldown")
        self.assertEqual(hooks._recovery_policy_for_exception(idle_timeout), "recovery")
        self.assertEqual(hooks._recovery_policy_for_exception(request_error), "error")
        self.assertFalse(hooks._should_count_deployment_failure_for_cooldown(network))
        self.assertFalse(hooks._should_count_deployment_failure_for_cooldown(rate_limit))
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(start_timeout))

    def test_recovery_policy_is_runtime_configurable(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("Cannot connect to host api.example.test")
        error.status_code = 500
        self.set_env("LITELLM_MENU_RECOVERY_POLICY_NETWORK", "recovery_cooldown")

        self.assertEqual(hooks._recovery_policy_for_exception(error), "recovery_cooldown")
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(error))

        rate_limit = RuntimeError("rate limit exceeded")
        rate_limit.status_code = 429
        self.set_env(hooks._RECOVERY_POLICY_RATE_LIMIT_ENV, "recovery_cooldown")
        self.assertEqual(
            hooks._recovery_policy_for_exception(rate_limit), "recovery_cooldown"
        )
        self.assertTrue(hooks._should_count_deployment_failure_for_cooldown(rate_limit))

    def test_deterministic_request_error_never_enters_recovery_even_when_marked_failed(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("OpenAIException invalid_request_error: invalid input[89].id")
        error.status_code = 400
        error.failed_deployment_id = "route-a"

        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(hooks._is_route_recovery_poll_error(error))

    def test_structured_compaction_body_capacity_error_uses_only_route_failover(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError(
            "OpenAIException - invalid request: request body storage capacity exhausted"
        )
        error.status_code = 400
        structured_compaction_request = {
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
        ordinary_request = {
            **structured_compaction_request,
            "input": structured_compaction_request["input"][:-1],
            "client_metadata": {
                "x-codex-turn-metadata": '{"request_kind":"turn"}',
            },
        }

        self.assertTrue(hooks._is_upstream_request_body_storage_capacity_error(error))
        self.assertFalse(hooks._is_priority_deployment_failover_error(error))
        self.assertTrue(
            hooks._is_request_scoped_priority_deployment_failover_error(
                error,
                structured_compaction_request,
            )
        )
        self.assertFalse(
            hooks._is_request_scoped_priority_deployment_failover_error(
                error,
                ordinary_request,
            )
        )
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(
            hooks._should_return_route_recovery_stream(
                error,
                structured_compaction_request,
            )
        )
        self.assertEqual(
            hooks._trace_exception(error)["reason"],
            "upstream-request-body-capacity",
        )

    def test_unknown_custom_tool_type_is_not_a_deployment_failover(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError("OpenAIException invalid_request_error: unknown tool type: custom")
        error.status_code = 400
        error.failed_deployment_id = "route-a"

        self.assertFalse(hooks._is_deployment_compatible_bad_request_error(error))
        self.assertFalse(hooks._is_priority_deployment_failover_error(error))
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")

    def test_upstream_high_risk_rejection_never_enters_recovery(self) -> None:
        hooks, _proxy_server = load_hook_module()
        error = RuntimeError(
            "OpenAIException - the request was rejected because it was considered high risk"
        )
        error.status_code = 400
        error.failed_deployment_id = "route-a"

        self.assertTrue(hooks._is_terminal_prompt_or_policy_error(error))
        self.assertFalse(hooks._is_deployment_compatible_bad_request_error(error))
        self.assertEqual(hooks._recovery_policy_for_exception(error), "error")
        self.assertFalse(hooks._is_route_recovery_poll_error(error))

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

    def test_mark_exception_defers_recoverable_route_exclusion_until_budget_exhausts(self) -> None:
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
        self.assertEqual(request_kwargs["_excluded_deployment_ids"], ["already-failed"])
        self.assertEqual(error.num_retries, 0)

    def test_mark_exception_defers_timeout_route_exclusion_until_budget_exhausts(self) -> None:
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

    def test_mark_exception_defers_rate_limit_route_exclusion_until_budget_exhausts(self) -> None:
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
        self.assertFalse(hasattr(error, "excluded_deployment_ids"))
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

    def test_deployment_cooldown_is_shared_across_client_protocols(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(directory) / "deployment-cooldowns.json"),
            )
            deployment = {
                "litellm_params": {"model": "provider/default-route"},
                "model_info": {"id": "shared-route"},
            }
            failed_request = {
                "model": "default-chat",
                "input": [{"role": "user", "content": "Continue."}],
                "stream": True,
                "proxy_server_request": {"path": "/v1/responses"},
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }
            failure = RuntimeError("temporary upstream failure")
            failure.status_code = 503

            hooks._mark_exception_for_deployment_failover(failure, failed_request)

            requests = {
                "responses": {
                    "model": "default-chat",
                    "input": [{"role": "user", "content": "Continue."}],
                    "stream": True,
                    "proxy_server_request": {"path": "/v1/responses"},
                },
                "chat_completions": {
                    "model": "default-chat",
                    "messages": [{"role": "user", "content": "Continue."}],
                    "stream": True,
                    "proxy_server_request": {"path": "/v1/chat/completions"},
                },
                "anthropic_messages": {
                    "model": "default-chat",
                    "messages": [{"role": "user", "content": "Continue."}],
                    "max_tokens": 64,
                    "stream": True,
                    "proxy_server_request": {"path": "/v1/messages"},
                },
            }
            for protocol, request_data in requests.items():
                with self.subTest(protocol=protocol):
                    available, cooled, filtered = hooks._with_active_deployment_cooldowns(
                        [deployment],
                        request_kwargs=request_data,
                    )
                    self.assertEqual(available, [])
                    self.assertEqual(len(cooled), 1)
                    self.assertTrue(filtered)

            successful_messages_request = {
                **requests["anthropic_messages"],
                "litellm_params": deployment["litellm_params"],
                "model_info": deployment["model_info"],
            }
            hooks._record_deployment_success_for_cooldown(
                successful_messages_request
            )
            available, cooled, filtered = hooks._with_active_deployment_cooldowns(
                [deployment],
                request_kwargs=requests["responses"],
            )
            self.assertEqual(available, [deployment])
            self.assertEqual(cooled, [])
            self.assertFalse(filtered)

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

    def test_deployment_cooldown_defaults_split_ordinary_and_compaction_writes(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV, None)
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV, None)

        ordinary_request = {"model": "default-chat", "input": [{"role": "user", "content": "work"}]}
        compaction_request = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
        }

        self.assertTrue(
            hooks._deployment_cooldown_recording_enabled_for_request(ordinary_request)
        )
        self.assertFalse(
            hooks._deployment_cooldown_recording_enabled_for_request(compaction_request)
        )

    def test_compaction_cooldown_setting_keeps_one_shared_pool(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV, "0")
        hooks._DEPLOYMENT_COOLDOWNS.clear()
        self.addCleanup(hooks._DEPLOYMENT_COOLDOWNS.clear)
        deployment = {
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "cooldown-shared-route"},
        }
        ordinary_request = {
            "model": "default-chat",
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "cooldown-shared-route"},
        }
        compaction_request = {
            "model": "default-chat",
            "input": [{"type": "compaction_trigger", "id": "compact-now"}],
            "litellm_params": {"model": "openai/x-cheap"},
            "model_info": {"id": "cooldown-shared-route"},
        }

        ordinary_error = RuntimeError("temporary upstream failure")
        ordinary_error.status_code = 503
        hooks._mark_exception_for_deployment_failover(ordinary_error, ordinary_request)
        cooldown_key = "id:cooldown-shared-route"
        self.assertIn(cooldown_key, hooks._DEPLOYMENT_COOLDOWNS)

        available, cooled, filtered = hooks._with_active_deployment_cooldowns(
            [deployment], request_kwargs=compaction_request
        )
        self.assertEqual(available, [])
        self.assertEqual(len(cooled), 1)
        self.assertTrue(filtered)

        compaction_error = RuntimeError("temporary upstream failure")
        compaction_error.status_code = 503
        hooks._mark_exception_for_deployment_failover(compaction_error, compaction_request)
        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS[cooldown_key]["failures"], 1)

        self.set_env(hooks._DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV, "1")
        enabled_compaction_error = RuntimeError("temporary upstream failure")
        enabled_compaction_error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            enabled_compaction_error, compaction_request
        )
        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS[cooldown_key]["failures"], 2)

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

    async def test_route_recovery_does_not_half_open_cooled_candidates(self) -> None:
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
        self.assertEqual(filtered, [])

        available, cooled, cooldown_filtered = hooks._with_active_deployment_cooldowns(
            deployments,
            request_kwargs=recovery_request,
        )
        self.assertTrue(cooldown_filtered)
        self.assertEqual(available, [])
        self.assertEqual(len(cooled), 2)
        self.assertFalse(any(entry.get("half_open_probe") is True for entry in cooled))

    async def test_interactive_route_recovery_waits_when_all_candidates_are_cooling_down(self) -> None:
        hooks, proxy_server = load_hook_module()
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        recovery_state_path = Path(temp_dir.name) / "route-recovery-state.json"
        self.set_env(hooks._ROUTE_RECOVERY_STATE_FILE_ENV, str(recovery_state_path))
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "0.05")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.15")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.005")
        deployments = [
            {"litellm_params": {"model": "openai/x-cheap"}, "model_info": {"id": "x-cheap"}},
            {"litellm_params": {"model": "openai/x-plus"}, "model_info": {"id": "x-plus"}},
        ]

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments if model_name == "default-chat" else []

        proxy_server.llm_router = Router()
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

        request_data = {
            "model": "default-chat",
            "stream": True,
            "input": [{"role": "user", "content": "Continue."}],
            "_target_order": 1,
            "_excluded_deployment_ids": ["x-cheap", "x-plus"],
        }
        self.assertTrue(
            hooks._route_recovery_poll_cooldown_wait(
                request_data,
                ignore_constraints=True,
            )
        )

        calls = []
        first_cooldown_until = None

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered after cooldown"}
            yield {"type": "response.completed", "response": {"id": "resp-after-cooldown"}}

        async def upstream_after_cooldown(**payload):
            self.assertIsNotNone(first_cooldown_until)
            assert first_cooldown_until is not None
            self.assertGreaterEqual(time.time(), first_cooldown_until)
            calls.append(payload.copy())
            return recovered_stream()

        proxy_server.llm_router.aresponses = upstream_after_cooldown
        failure = RuntimeError("temporary upstream failure")
        failure.status_code = 503
        failure.failed_deployment_id = "x-cheap"
        failure.failed_deployment_order = 1
        request_data["_route_recovery_ignore_local_constraints"] = True
        stream = hooks._stream_route_recovery_poll(request_data, failure)
        first_chunk = jsonable_stream_chunk(await anext(stream))
        self.assertTrue(hooks._is_route_recovery_sse_keepalive(first_chunk))
        self.assertEqual(first_chunk["metadata"]["phase"], "cooldown")
        self.assertEqual(calls, [])
        waiting_state = json.loads(recovery_state_path.read_text(encoding="utf-8"))
        record = next(iter(waiting_state["recoveries"].values()))
        self.assertEqual(record["status"], "waiting")
        self.assertGreater(record["cooldown_until"], time.time())
        self.assertGreater(record["cooldown_remaining_seconds"], 0)
        first_cooldown_until = record["cooldown_until"]

        await asyncio.sleep(0.01)
        next_chunk = jsonable_stream_chunk(await anext(stream))
        self.assertTrue(hooks._is_route_recovery_sse_keepalive(next_chunk))
        self.assertEqual(next_chunk["metadata"]["phase"], "cooldown")
        self.assertEqual(calls, [])

        chunks = [first_chunk, next_chunk]
        async for chunk in stream:
            chunks.append(jsonable_stream_chunk(chunk))
        self.assertEqual(len(calls), 1)
        self.assertNotIn("_excluded_deployment_ids", calls[0])
        self.assertNotIn("_target_order", calls[0])
        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered after cooldown"},
            chunks,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_stream_fallback_recovery_waits_for_a_shared_cooldown(self) -> None:
        hooks, proxy_server = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "0.05")
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.15")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.005")
        deployments = [
            {"litellm_params": {"order": 1}, "model_info": {"id": "route-a"}},
            {"litellm_params": {"order": 2}, "model_info": {"id": "route-b"}},
        ]

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return deployments if model_name == "default-chat" else []

            async def aresponses(self, **payload):
                self_calls.append((time.time(), payload.copy()))
                async def recovered_stream():
                    yield {"type": "response.output_text.delta", "delta": "recovered"}
                    yield {"type": "response.completed", "response": {"id": "resp-recovered"}}
                return recovered_stream()

        proxy_server.llm_router = Router()
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

        available, cooled, _ = hooks._with_active_deployment_cooldowns(
            deployments,
            request_kwargs={"model": "default-chat"},
        )
        self.assertEqual(available, [])
        cooldown_until = min(entry["cooldown_until"] for entry in cooled)
        self_calls = []

        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "_target_order": 1,
            "_excluded_deployment_ids": ["route-a", "route-b"],
        }
        initial_failure = RuntimeError("No deployments available for selected model")
        initial_failure.status_code = 503
        initial_failure.failed_deployment_id = "route-a"
        initial_failure.failed_deployment_order = 1
        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._yield_streaming_error_fallback_or_raise(
                request_data,
                initial_failure,
            )
        ]

        self.assertEqual(len(self_calls), 1)
        self.assertGreaterEqual(self_calls[0][0], cooldown_until)
        self.assertNotIn("_target_order", self_calls[0][1])
        self.assertNotIn("_excluded_deployment_ids", self_calls[0][1])
        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_route_recovery_prefers_healthy_peer_over_cooled_candidates(self) -> None:
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

    async def test_deployment_cooldown_does_not_count_rate_limit_or_request_errors_by_default(self) -> None:
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

    async def test_deployment_cooldown_counts_server_timeouts_but_not_stream_idle_errors(self) -> None:
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
        self.assertEqual(filtered, [deployments[1]])

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

        class ReadError(Exception):
            pass

        disconnected = ReadError("connection closed.")
        self.assertTrue(hooks._exception_indicates_network_connectivity_error(disconnected))
        self.assertTrue(hooks._is_priority_deployment_failover_error(disconnected))
        self.assertTrue(hooks._should_retry_same_deployment_before_fallback(disconnected))

        sending_request = RuntimeError("Connection failed: error sending request")
        self.assertTrue(
            hooks._is_network_recovery_exception(sending_request)
        )
        self.assertEqual(
            hooks._recovery_policy_for_exception(sending_request),
            "recovery",
        )

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

    async def test_deployment_cooldown_applies_to_the_selected_deployment(self) -> None:
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

        self.assertEqual(responses_filtered, [])
        self.assertEqual(chat_filtered, [])
        self.assertEqual(
            hooks._request_surface_for_deployment({}, deployment),
            "openai/responses",
        )
        self.assertIn(
            "id:dual-protocol-route",
            hooks._DEPLOYMENT_COOLDOWNS,
        )
        self.assertFalse(any("|surface:" in key for key in hooks._DEPLOYMENT_COOLDOWNS))

    def test_surface_adapter_uses_exact_model_for_all_three_protocols(self) -> None:
        hooks, _ = load_hook_module()

        self.assertEqual(
            hooks._surface_adapter_model("openai/vendor/model", "openai/responses"),
            "openai/vendor/model",
        )
        self.assertEqual(
            hooks._surface_adapter_model("chatgpt/gpt-5.4", "openai/responses"),
            "chatgpt/gpt-5.4",
        )
        request = {"litellm_params": {"model": "chatgpt/gpt-5.4"}}
        hooks._apply_surface_adapter_to_request(
            request, "openai/responses", "chatgpt/gpt-5.4"
        )
        self.assertEqual(request["custom_llm_provider"], "chatgpt")
        self.assertEqual(request["litellm_params"]["model"], "chatgpt/gpt-5.4")

    def test_surface_adapter_relaxed_choice_removes_nested_forcing_copies(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "openai/vendor/model",
            "tool_choice": {"type": "function", "name": "inspect"},
            "extra_body": {
                "tool_choice": {"type": "function", "name": "inspect"},
                "function_call": {"name": "inspect"},
                "keep": True,
            },
            "litellm_params": {
                "tool_choice": {"type": "function", "name": "inspect"},
                "function_call": {"name": "inspect"},
                "keep": True,
            },
            hooks._PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY: True,
        }

        hooks._apply_surface_adapter_to_request(
            request,
            "openai/chat",
            "openai/vendor/model",
        )

        self.assertEqual(request["tool_choice"], "auto")
        for key in ("extra_body", "litellm_params"):
            self.assertNotIn("tool_choice", request[key])
            self.assertNotIn("function_call", request[key])
            self.assertTrue(request[key]["keep"])

    def test_fallback_mode_starts_with_the_client_protocol(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(
                {"call_type": "aresponses", "input": "hello"}, deployment
            ),
            "openai/responses",
        )
        self.assertEqual(
            hooks._request_surface_for_deployment(
                {"call_type": "messages", "messages": [{"role": "user"}]},
                deployment,
            ),
            "anthropic",
        )

    def test_fixed_mode_ignores_a_stale_fallback_target(self) -> None:
        hooks, _ = load_hook_module()
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "fixed-route",
                "upstream_url_surface": "anthropic",
                "upstream_protocol_mode": "fixed",
            },
        }

        request = {
            "call_type": "aresponses",
            "input": "hello",
            "_litellm_menu_upstream_url_surface": "openai/chat",
            "_litellm_menu_upstream_url_surface_deployment_id": "fixed-route",
            "_litellm_menu_surface_target_deployment_id": "fixed-route",
        }

        self.assertEqual(
            hooks._request_surface_for_deployment(request, deployment),
            "anthropic",
        )

    def test_protocol_fallback_stays_on_the_same_deployment(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment]

        request = {
            "model": "default-chat",
            "call_type": "messages",
            "messages": [{"role": "user", "content": "hello"}],
            "_target_order": 1,
            "_litellm_menu_upstream_url_surface": "anthropic",
            "_litellm_menu_attempted_upstream_url_surfaces": ["anthropic"],
            "_litellm_menu_upstream_url_surface_deployment_id": "kimi-route",
            "model_info": deployment["model_info"],
            "litellm_params": deployment["litellm_params"],
        }
        error = RuntimeError("messages endpoint not found")
        error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(Router(), error, request)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["_target_order"], 1)
        self.assertEqual(entry["_litellm_menu_upstream_url_surface"], "openai/chat")
        self.assertEqual(
            entry["_litellm_menu_surface_target_deployment_id"], "kimi-route"
        )
        self.assertEqual(
            request["_litellm_menu_protocol_fallback_from_surface"], "anthropic"
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    def test_invalid_parameter_combination_uses_protocol_fallback(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        deployment = {
            "litellm_params": {"model": "openai/vendor-model", "order": 0},
            "model_info": {
                "id": "dual-protocol-route",
                "order": 0,
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment]

        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "stream": True,
            "_litellm_menu_upstream_url_surface": "openai/responses",
            "_litellm_menu_attempted_upstream_url_surfaces": [
                "openai/responses"
            ],
            "_litellm_menu_upstream_url_surface_deployment_id": (
                "dual-protocol-route"
            ),
            "model_info": deployment["model_info"],
            "litellm_params": deployment["litellm_params"],
        }
        error = RuntimeError(
            'OpenAIException - {"error":{"code":"server_error",'
            '"message":"请求参数组合无效"}}'
        )
        error.status_code = 400

        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(
                error,
                request,
            )
        )
        hooks._mark_exception_for_upstream_surface_failover(error, request)
        entry = hooks._ordered_deployment_fallback_entry(
            Router(),
            error,
            request,
        )

        self.assertIsNotNone(entry)
        self.assertEqual(
            entry["_litellm_menu_upstream_url_surface"],
            "openai/chat",
        )
        self.assertEqual(
            entry["_litellm_menu_surface_target_deployment_id"],
            "dual-protocol-route",
        )
        self.assertFalse(hooks._DEPLOYMENT_COOLDOWNS)

    def test_failed_protocol_fallback_counts_once_without_recovery(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(directory) / "deployment-cooldowns.json"),
            )
            request = {
                "model": "default-chat",
                "call_type": "aresponses",
                "input": "hello",
                "stream": True,
                "_litellm_menu_upstream_url_surface": "openai/chat",
                "_litellm_menu_attempted_upstream_url_surfaces": [
                    "openai/responses",
                    "openai/chat",
                ],
                "_litellm_menu_upstream_url_surface_deployment_id": (
                    "dual-protocol-route"
                ),
                "_litellm_menu_protocol_fallback_from_surface": (
                    "openai/responses"
                ),
                "_litellm_menu_protocol_fallback_client_surface": (
                    "openai/responses"
                ),
                "model_info": {
                    "id": "dual-protocol-route",
                    "order": 0,
                    "upstream_url_surface": "openai/chat",
                    "upstream_protocol_mode": "fallback",
                },
                "litellm_params": {
                    "model": "openai/vendor-model",
                    "order": 0,
                },
            }
            error = RuntimeError("fallback request rejected")
            error.status_code = 400

            hooks._mark_exception_for_deployment_failover(error, request)
            hooks._mark_exception_for_deployment_failover(error, request)

            state = hooks._DEPLOYMENT_COOLDOWNS["id:dual-protocol-route"]
            self.assertEqual(state["failures"], 1)
            self.assertEqual(state.get("cooldown_until", 0), 0)
            self.assertEqual(
                hooks._recovery_policy_for_exception(error),
                hooks._RECOVERY_POLICY_ERROR,
            )
            self.assertFalse(hooks._is_route_recovery_poll_error(error))
            self.assertFalse(
                hooks._protocol_fallback_attempt_active(request)
            )

    def test_protocol_fallback_wrapper_failure_does_not_count_cooldown_twice(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "stream": True,
            "_litellm_menu_upstream_url_surface": "openai/chat",
            "_litellm_menu_upstream_url_surface_deployment_id": "dual-protocol-route",
            "_litellm_menu_protocol_fallback_from_surface": "openai/responses",
            "_litellm_menu_protocol_fallback_client_surface": "openai/responses",
            "model_info": {
                "id": "dual-protocol-route",
                "order": 0,
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
            "litellm_params": {
                "model": "openai/vendor-model",
                "order": 0,
            },
        }
        first = RuntimeError("responses protocol failed")
        first.status_code = 400
        hooks._mark_exception_for_deployment_failover(first, request)

        wrapper = RuntimeError("chat wrapper failed")
        wrapper.status_code = 503
        hooks._mark_exception_for_deployment_failover(wrapper, request)

        state = hooks._DEPLOYMENT_COOLDOWNS["id:dual-protocol-route"]
        self.assertEqual(state["failures"], 1)
        self.assertEqual(state.get("cooldown_until", 0), 0)

    def test_protocol_fallback_failure_marker_does_not_hide_a_different_route(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "2")
        request = {
            "model": "default-chat",
            "model_info": {
                "id": "route-a",
                "order": 0,
                "upstream_url_surface": "openai/chat",
            },
            "litellm_params": {"model": "openai/vendor-model"},
            "_litellm_menu_upstream_url_surface": "openai/chat",
            "_litellm_menu_protocol_fallback_from_surface": "openai/responses",
        }
        first = RuntimeError("protocol chain failed")
        first.status_code = 400
        hooks._mark_exception_for_deployment_failover(first, request)

        request["model_info"] = {
            "id": "route-b",
            "order": 1,
            "upstream_url_surface": "openai/chat",
        }
        request["litellm_params"] = {"model": "openai/other-model"}
        wrapper = RuntimeError("different route failed")
        wrapper.status_code = 503
        hooks._mark_exception_for_deployment_failover(wrapper, request)

        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS["id:route-a"]["failures"], 1)
        self.assertEqual(hooks._DEPLOYMENT_COOLDOWNS["id:route-b"]["failures"], 1)

    def test_successful_protocol_fallback_is_remembered_for_runtime_ttl(self) -> None:
        hooks, _ = load_hook_module()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            self.set_env(hooks._DEPLOYMENT_COOLDOWN_FILE_ENV, str(Path(directory) / "routing.json"))
            self.set_env(hooks._PROTOCOL_FALLBACK_TTL_SECONDS_ENV, "600")
            failed_request = {
                "model": "default-chat",
                "_litellm_menu_upstream_url_surface": "anthropic",
                "_litellm_menu_upstream_url_surface_deployment_id": "kimi-route",
                "model_info": deployment["model_info"],
                "litellm_params": deployment["litellm_params"],
            }
            initial_error = RuntimeError("initial protocol rejected")
            initial_error.status_code = 503
            hooks._mark_exception_for_deployment_failover(
                initial_error,
                failed_request,
            )
            self.assertIn("id:kimi-route", hooks._DEPLOYMENT_COOLDOWNS)

            request = {
                "model": "default-chat",
                "call_type": "messages",
                "messages": [{"role": "user", "content": "hello"}],
                "_litellm_menu_upstream_url_surface": "openai/chat",
                "_litellm_menu_upstream_url_surface_deployment_id": "kimi-route",
                "_litellm_menu_protocol_fallback_from_surface": "anthropic",
                "_litellm_menu_protocol_fallback_client_surface": "anthropic",
                "model_info": deployment["model_info"],
            }
            hooks._record_protocol_fallback_success(request)
            self.assertNotIn("id:kimi-route", hooks._DEPLOYMENT_COOLDOWNS)

            next_request = {
                "call_type": "messages",
                "messages": [{"role": "user", "content": "again"}],
            }
            self.assertEqual(
                hooks._request_surface_for_deployment(next_request, deployment),
                "openai/chat",
            )

    def test_invalid_protocol_fallback_advances_to_the_next_order(self) -> None:
        hooks, _ = load_hook_module()
        primary = {
            "litellm_params": {"model": "openai/kimi-k3", "order": 1},
            "model_info": {
                "id": "kimi-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        secondary = {
            "litellm_params": {"model": "openai/gpt-5", "order": 2},
            "model_info": {
                "id": "gpt-route",
                "upstream_url_surface": "openai/responses",
                "upstream_protocol_mode": "fallback",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [primary, secondary]

        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "input": "hello",
            "_target_order": 1,
            "_litellm_menu_upstream_url_surface": "openai/chat",
            "_litellm_menu_attempted_upstream_url_surfaces": [
                "openai/responses",
                "openai/chat",
            ],
            "_litellm_menu_upstream_url_surface_deployment_id": "kimi-route",
            "model_info": primary["model_info"],
            "litellm_params": primary["litellm_params"],
        }
        error = RuntimeError("chat endpoint not found")
        error.status_code = 404
        hooks._mark_exception_for_upstream_surface_failover(error, request)

        entry = hooks._ordered_deployment_fallback_entry(Router(), error, request)

        self.assertIsNotNone(entry)
        self.assertEqual(entry["_target_order"], 2)
        self.assertEqual(entry["_excluded_deployment_ids"], ["kimi-route"])
        self.assertNotIn("_litellm_menu_upstream_url_surface", entry)
        self.assertEqual(
            hooks._surface_adapter_model("anthropic/vendor/model", "openai/chat"),
            "openai/vendor/model",
        )

    def test_protocol_incompatibility_skips_to_a_same_order_peer(self) -> None:
        hooks, _ = load_hook_module()
        deployment_a = {
            "litellm_params": {"model": "openai/vendor-a", "order": 1},
            "model_info": {
                "id": "route-a",
                "upstream_url_surface": "openai/responses",
            },
        }
        deployment_b = {
            "litellm_params": {"model": "openai/vendor-b", "order": 1},
            "model_info": {
                "id": "route-b",
                "upstream_url_surface": "openai/chat",
            },
        }

        class Router:
            def _get_all_deployments(self, model_name, team_id=None):
                return [deployment_a, deployment_b]

        request = {
            "model": "default-chat",
            "_target_order": 1,
            "_litellm_menu_upstream_url_surface": "openai/responses",
            "model_info": deployment_a["model_info"],
            "litellm_params": deployment_a["litellm_params"],
        }

        first_error = RuntimeError("responses endpoint not found")
        first_error.status_code = 404
        hooks._mark_exception_for_deployment_failover(first_error, request)
        first = hooks._ordered_deployment_fallback_entry(Router(), first_error, request)

        self.assertEqual(first["_target_order"], 1)
        self.assertEqual(first["_excluded_deployment_ids"], ["route-a"])
        self.assertEqual(
            first["_litellm_menu_verified_fallback_deployment_ids"],
            ["route-b"],
        )
        self.assertNotIn("_litellm_menu_upstream_url_surface", first)
        self.assertFalse(any("surface_target" in key for key in first))

    async def test_deployment_is_filtered_after_its_selected_protocol_fails(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_FAILURES_ENV, "1")
        self.set_env(hooks._DEPLOYMENT_COOLDOWN_SECONDS_ENV, "300")
        deployment = {
            "litellm_params": {"model": "openai/vendor-a", "order": 1},
            "model_info": {
                "id": "three-surface-route",
                "upstream_url_surface": "anthropic",
            },
        }

        error = RuntimeError("temporary anthropic failure")
        error.status_code = 503
        hooks._mark_exception_for_deployment_failover(
            error,
            {
                "model": "default-chat",
                "_litellm_menu_upstream_url_surface": "anthropic",
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
        self.assertEqual(filtered, [])

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

    def test_forced_tool_choice_rejection_uses_configured_protocol_fallback(self) -> None:
        hooks, _ = load_hook_module()
        request = {
            "model": "default-chat",
            "call_type": "aresponses",
            "tool_choice": {"type": "function", "name": "inspect"},
            "_litellm_menu_upstream_url_surface": "openai/responses",
            "model_info": {
                "id": "dual-protocol-route",
                "upstream_url_surface": "openai/chat",
                "upstream_protocol_mode": "fallback",
            },
        }
        error = RuntimeError(
            "当前模型或上游不支持指定工具的强制选择方式，请改用 tool_choice=auto"
        )
        error.status_code = 400

        self.assertTrue(
            hooks._is_forced_tool_choice_unsupported_error(error, request)
        )
        self.assertTrue(
            hooks._is_current_upstream_surface_incompatible_error(error, request)
        )

        auto_request = {**request, "tool_choice": "auto"}
        self.assertFalse(
            hooks._is_forced_tool_choice_unsupported_error(error, auto_request)
        )
        self.assertFalse(
            hooks._is_current_upstream_surface_incompatible_error(error, auto_request)
        )

        no_choice_request = {
            key: value for key, value in request.items() if key != "tool_choice"
        }
        self.assertFalse(
            hooks._is_forced_tool_choice_unsupported_error(error, no_choice_request)
        )

    def test_ambiguous_forced_choice_retry_requires_a_tool_payload(self) -> None:
        hooks, _ = load_hook_module()
        error = RuntimeError("请求参数组合无效")
        error.status_code = 400
        request = {
            "tool_choice": {"type": "function", "name": "inspect"},
        }

        self.assertFalse(
            hooks._is_forced_tool_choice_auto_retry_error(error, request)
        )

        request["tools"] = [{"type": "function", "name": "inspect"}]
        self.assertTrue(
            hooks._is_forced_tool_choice_auto_retry_error(error, request)
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
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))

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
        self.assertFalse(hooks._should_sanitize_final_upstream_route_error(exc))
        self.assertFalse(hooks._should_retry_same_deployment_before_fallback(exc))

    def test_image_generation_tool_runtime_fallback_uses_remaining_route_once(self) -> None:
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

        self.assertEqual(
            entry,
            {
                "model": "default-chat",
                "_target_order": 1,
                "_excluded_deployment_ids": ["route-a"],
                hooks._VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY: ["route-b"],
            },
        )

    async def test_image_tool_capability_rejection_is_cached_across_workers(self) -> None:
        hooks, _proxy_server = load_hook_module()
        hook = hooks.LiteLLMMenuHook()
        self.set_env(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_TTL_SECONDS_ENV, "600")
        hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()
        self.addCleanup(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear)
        deployments = [
            {"model_info": {"id": "image-route-a", "order": 1}},
            {"model_info": {"id": "image-route-b", "order": 2}},
        ]
        request_data = {
            "model": "default-chat",
            "tools": [{"type": "image_generation"}],
            "model_info": {"id": "image-route-a", "order": 1},
        }
        error = RuntimeError("invalid_request_error: unsupported tool type image_generation")
        error.status_code = 422

        with tempfile.TemporaryDirectory() as temp_dir:
            self.set_env(
                hooks._DEPLOYMENT_COOLDOWN_FILE_ENV,
                str(Path(temp_dir) / "deployment-cooldowns.json"),
            )
            hooks._mark_exception_for_deployment_failover(error, request_data)
            self.assertIn("id:image-route-a", hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED)
            hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()

            filtered = await hook.async_filter_deployments(
                "default-chat",
                deployments,
                messages=None,
                request_kwargs={
                    "model": "default-chat",
                    "tools": [{"type": "image_generation"}],
                },
            )

        self.assertEqual(filtered, [deployments[1]])

    def test_image_tool_memory_ignores_parameter_and_transient_errors(self) -> None:
        hooks, _proxy_server = load_hook_module()
        self.set_env(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_TTL_SECONDS_ENV, "600")
        hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()
        self.addCleanup(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED.clear)
        request_data = {
            "model": "default-chat",
            "tools": [{"type": "image_generation"}],
            "model_info": {"id": "image-route-a", "order": 1},
        }
        parameter_error = RuntimeError(
            "invalid_request_error: unsupported image size 1792x1024"
        )
        parameter_error.status_code = 400
        timeout_error = RuntimeError("gateway timeout while generating image")
        timeout_error.status_code = 504

        self.assertFalse(
            hooks._is_image_generation_tool_capability_error(parameter_error)
        )
        self.assertFalse(
            hooks._is_image_generation_tool_capability_error(timeout_error)
        )
        hooks._mark_exception_for_deployment_failover(parameter_error, request_data)
        hooks._mark_exception_for_deployment_failover(timeout_error, request_data)

        self.assertEqual(hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED, {})

    def test_all_image_tool_routes_unsupported_returns_terminal_client_error(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"model_info": {"id": "image-route-a", "order": 1}},
                    {"model_info": {"id": "image-route-b", "order": 2}},
                ]

        error = RuntimeError("invalid_request_error: unsupported tool type image_generation")
        error.status_code = 422
        error.failed_deployment_id = "image-route-b"
        error.failed_deployment_order = 2
        request_data = {
            "model": "default-chat",
            "stream": True,
            "tools": [{"type": "image_generation"}],
            "litellm_metadata": {
                hooks._IMAGE_GENERATION_TOOL_UNSUPPORTED_METADATA_KEY: [
                    "id:image-route-a",
                    "id:image-route-b",
                ]
            },
        }

        entry = hooks._ordered_deployment_fallback_entry(
            FakeRouter(),
            error,
            request_data,
        )
        event = hooks._synthesized_failed_response_event(request_data, error)

        self.assertIsNone(entry)
        self.assertTrue(
            hooks._is_image_generation_all_deployments_unsupported_error(error)
        )
        self.assertEqual(event["response"]["error"]["type"], "invalid_request_error")
        self.assertEqual(
            event["response"]["error"]["code"],
            "image_generation_tool_unavailable",
        )

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

    def test_order_parsing_preserves_zero_negative_and_fractional_values(self) -> None:
        hooks, _proxy_server = load_hook_module()

        self.assertEqual(hooks._coerce_order(0), 0)
        self.assertEqual(hooks._coerce_order("-2.5"), -2.5)
        self.assertEqual(hooks._coerce_order("0.25"), 0.25)
        self.assertIsNone(hooks._coerce_order("not-a-number"))
        self.assertEqual(
            hooks._deployment_order_from_request(
                {"model_info": {"route_key": "provider / upstream / order=-0.5"}}
            ),
            -0.5,
        )

    def test_fractional_orders_sort_and_rotate_numerically(self) -> None:
        hooks, _proxy_server = load_hook_module()
        deployments = [
            {"litellm_params": {"order": 0.25}, "model_info": {"id": "quarter"}},
            {"litellm_params": {"order": -1.5}, "model_info": {"id": "negative"}},
            {"litellm_params": {"order": 0}, "model_info": {"id": "zero"}},
        ]

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

        def original_get_all_deployments(self, model_name, team_id=None):
            return deployments

        FakeRouter._get_all_deployments._original_get_all_deployments = original_get_all_deployments
        self.assertEqual(
            hooks._configured_deployment_orders(FakeRouter(), {"model": "default-chat"}),
            [-1.5, 0, 0.25],
        )
        self.assertEqual(hooks._next_configured_order([-1.5, 0, 0.25], -1.5), 0)
        self.assertEqual(hooks._next_configured_order([-1.5, 0, 0.25], 0), 0.25)

if __name__ == "__main__":
    unittest.main()
