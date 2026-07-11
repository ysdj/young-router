from __future__ import annotations

import copy

from hook_test_utils import *


class HookRouteRecoveryTests(HookTestCase):
    async def test_route_recovery_poll_payload_propagates_no_deployments_without_nested_stream(self) -> None:
        hooks, _proxy_server = load_hook_module()
        router_module = types.ModuleType("litellm.router")
        calls = []

        class NoDeploymentsError(Exception):
            status_code = 503

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

        async def original_generic_function(**kwargs):
            calls.append(kwargs.copy())
            raise NoDeploymentsError("No deployments available for selected model")

        helper_kwargs = {
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "litellm_metadata": {
                hooks._STREAM_ERROR_FALLBACK_METADATA_KEY: True,
                hooks._ROUTE_RECOVERY_POLL_METADATA_KEY: True,
            },
            "model_info": {
                "id": "last-route",
                "order": 3,
                "route_key": "compat_provider / openai/default-chat / key=x-pro",
            },
            "_target_order": 3,
            "_excluded_deployment_ids": ["cheap", "last-route"],
        }

        with self.assertRaises(NoDeploymentsError):
            await Router()._ageneric_api_call_with_fallbacks_helper(
                "default-chat",
                original_generic_function,
                **helper_kwargs,
            )

        self.assertEqual(len(calls), 1)
        self.assertTrue(hooks._is_route_recovery_poll_payload(calls[0]))
        self.assertFalse(
            hooks._should_return_route_recovery_stream(
                NoDeploymentsError("No deployments available for selected model"),
                calls[0],
                Router(),
            )
        )

    def test_route_recovery_polls_on_upstream_rate_limit(self) -> None:
        hooks, _proxy_server = load_hook_module()
        exc = RuntimeError("upstream returned too many requests; rate limit exceeded")
        exc.status_code = 429
        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "hi5.4"}],
            "stream": True,
        }

        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(hooks._should_return_route_recovery_stream(exc, request_data))

    def test_route_recovery_polls_on_upstream_balance_error(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class BalanceError(Exception):
            status_code = 403

        exc = BalanceError("insufficient account balance")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }

        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(hooks._should_return_route_recovery_stream(exc, request_data))

    def test_route_recovery_does_not_poll_on_context_size_error(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class ContextTooLarge(Exception):
            status_code = 400

        exc = ContextTooLarge(
            "This model's maximum context length is 262144 tokens. "
            "However, your prompt contains 262145 input tokens."
        )
        exc.failed_deployment_id = "order1-a"
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }

        self.assertTrue(hooks._is_context_size_error(exc))
        self.assertFalse(hooks._is_priority_deployment_failover_error(exc))
        self.assertFalse(hooks._is_route_recovery_poll_error(exc))
        self.assertFalse(hooks._should_return_route_recovery_stream(exc, request_data))

    def test_route_recovery_does_not_poll_on_upstream_model_not_found(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class ModelNotFoundError(Exception):
            status_code = 404

        exc = ModelNotFoundError(
            'OpenAIException - {"error":{"message":"Model \\"responses/example-chat\\" '
            'is not supported by any configured account in this group",'
            '"type":"model_not_found"}}'
        )
        exc.failed_deployment_id = "example-route"
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "example-route", "order": 1},
        }

        self.assertTrue(hooks._is_upstream_model_not_found_error(exc))
        self.assertFalse(hooks._is_route_recovery_poll_error(exc))
        self.assertFalse(hooks._should_return_route_recovery_stream(exc, request_data))

    async def test_route_recovery_stops_when_attempt_hits_upstream_model_not_found(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class ServiceUnavailableError(Exception):
            status_code = 503

        class ModelNotFoundError(Exception):
            status_code = 404

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                exc = ModelNotFoundError(
                    'OpenAIException - {"error":{"message":"Model \\"responses/example-chat\\" '
                    'is not supported by any configured account in this group",'
                    '"type":"model_not_found"}}'
                )
                exc.failed_deployment_id = "example-route"
                exc.upstream_surface_unsupported = True
                raise exc

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "example-route", "order": 1},
        }
        first_exception = ServiceUnavailableError("upstream temporarily unavailable")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 1)
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_poll_propagates_context_size_error_without_retrying(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class ContextTooLarge(Exception):
            status_code = 400

        class FakeRouter:
            async def aresponses(self, **payload):
                calls.append(payload)
                exc = ContextTooLarge(
                    "Request too large: input tokens exceed the model context window."
                )
                exc.failed_deployment_id = "order1-a"
                raise exc

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = RuntimeError("No deployments available for selected model")

        with self.assertRaises(ContextTooLarge):
            chunks = []
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            ):
                chunks.append(chunk)

        self.assertEqual(len(calls), 1)

    def test_route_recovery_polls_on_sanitized_final_failure_even_if_next_order_exists(self) -> None:
        hooks, _proxy_server = load_hook_module()

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"litellm_params": {"order": 1}, "model_info": {"id": "chatroute"}},
                    {"litellm_params": {"order": 2}, "model_info": {"id": "gpt54"}},
                ]

        exc = RuntimeError(
            "Temporary upstream route failure for balanced-chat "
            "(temporary upstream server error) after LiteLLM fallback retries. "
            "Retry later or choose another model route."
        )
        exc.status_code = 503
        exc.failed_deployment_id = "chatroute"
        exc.failed_deployment_order = 1
        setattr(exc, hooks._SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR, True)
        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }

        self.assertTrue(hooks._is_route_recovery_poll_error(exc))
        self.assertTrue(
            hooks._should_return_route_recovery_stream(exc, request_data, FakeRouter())
        )

    async def test_route_recovery_poll_resumes_stream(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered"}}

        class RouterRateLimitError(Exception):
            pass

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "metadata": {"request_id": "route-recovery-test"},
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = RouterRateLimitError("No deployments available for selected model")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertIn({"type": "response.output_text.delta", "delta": "recovered"}, chunks)
        self.assertIn({"type": "response.completed", "response": {"id": "resp-recovered"}}, chunks)
        self.assertFalse(any(isinstance(chunk, str) for chunk in chunks))

    async def test_route_recovery_poll_reuses_stream_error_fallback_marker(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered after poll"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered-after-poll"}}

        class RouterRateLimitError(Exception):
            pass

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "metadata": {"request_id": "route-recovery-repeat-test"},
            "litellm_metadata": {
                hooks._STREAM_ERROR_FALLBACK_METADATA_KEY: True,
            },
        }
        first_exception = RouterRateLimitError("No deployments available for selected model")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertTrue(calls[0]["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY])
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered after poll"},
            chunks,
        )

    async def test_route_recovery_poll_retries_until_success(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered after polling"}
            yield {"type": "response.completed", "response": {"id": "resp-polled"}}

        class ServiceUnavailable(Exception):
            status_code = 503

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                if len(calls) < 3:
                    raise ServiceUnavailable("upstream temporarily unavailable")
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 3)
        self.assertTrue(all(call["litellm_metadata"][hooks._STREAM_ERROR_FALLBACK_METADATA_KEY] for call in calls))
        self.assertFalse(any(isinstance(chunk, str) for chunk in chunks))
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered after polling"},
            chunks,
        )
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_route_recovery_poll_keepalive_does_not_cancel_pending_stream_read(self) -> None:
        hooks, _proxy_server = load_hook_module()
        attempts = []
        cancelled = False
        original_keepalive_seconds = hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
        hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS = 0.001
        self.addCleanup(
            setattr,
            hooks,
            "_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS",
            original_keepalive_seconds,
        )

        class ServiceUnavailable(Exception):
            status_code = 503

        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        async def fake_fallback_round(
            _request_data,
            exception,
            *,
            allow_repeated_attempt=False,
            route_recovery_poll=False,
        ):
            nonlocal cancelled
            attempts.append(
                {
                    "exception": exception,
                    "allow_repeated_attempt": allow_repeated_attempt,
                    "route_recovery_poll": route_recovery_poll,
                }
            )
            try:
                await asyncio.sleep(0.006)
            except asyncio.CancelledError:
                cancelled = True
                raise
            yield {"type": "response.output_text.delta", "delta": "delayed recovery"}
            yield {"type": "response.completed", "response": {"id": "resp-delayed"}}

        hooks._stream_streaming_error_fallback_round = fake_fallback_round
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.05")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(attempts), 1)
        self.assertTrue(attempts[0]["allow_repeated_attempt"])
        self.assertTrue(attempts[0]["route_recovery_poll"])
        self.assertFalse(cancelled)
        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertIn({"type": "response.output_text.delta", "delta": "delayed recovery"}, chunks)
        self.assertEqual(chunks[-1], {"type": "response.completed", "response": {"id": "resp-delayed"}})

    async def test_route_recovery_poll_keepalive_after_web_search_call_until_answer(self) -> None:
        hooks, _proxy_server = load_hook_module()
        original_keepalive_seconds = hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
        hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS = 0.001
        self.addCleanup(
            setattr,
            hooks,
            "_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS",
            original_keepalive_seconds,
        )

        class GatewayTimeout(Exception):
            status_code = 504

        first_exception = GatewayTimeout("upstream timed out before a final answer")

        async def fake_fallback_round(
            _request_data,
            exception,
            *,
            allow_repeated_attempt=False,
            route_recovery_poll=False,
        ):
            self.assertTrue(allow_repeated_attempt)
            self.assertTrue(route_recovery_poll)
            yield {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_recovery",
                    "status": "in_progress",
                    "action": {"type": "search", "query": "sample subject factor A factor B"},
                },
            }
            yield {
                "type": "response.web_search_call.completed",
                "item_id": "ws_recovery",
                "output_index": 0,
                "action": {"type": "search", "query": "sample subject factor A factor B"},
            }
            yield {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "type": "web_search_call",
                    "id": "ws_recovery",
                    "status": "completed",
                    "action": {"type": "search", "query": "sample subject factor A factor B"},
                },
            }
            await asyncio.sleep(0.006)
            yield {"type": "response.output_text.delta", "delta": "recovered synthesis answer"}
            yield {"type": "response.completed", "response": {"id": "resp-search-recovered"}}

        hooks._stream_streaming_error_fallback_round = fake_fallback_round
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.05")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "legacy-chat",
            "input": "Use web_search for sample subject transporters.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "model_info": {"id": "chatroute", "order": 1},
        }

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertTrue(any(hooks._is_route_recovery_sse_keepalive(chunk) for chunk in chunks))
        self.assertIn({"type": "response.output_text.delta", "delta": "recovered synthesis answer"}, chunks)
        self.assertEqual(chunks[-1], {"type": "response.completed", "response": {"id": "resp-search-recovered"}})

    async def test_route_recovery_poll_emits_sse_keepalive_during_long_intervals(self) -> None:
        hooks, _proxy_server = load_hook_module()
        attempts = []
        original_keepalive_seconds = hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
        original_min_delay_seconds = hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS
        hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS = 0.0005
        hooks._ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS = 0.0001
        self.addCleanup(
            setattr,
            hooks,
            "_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS",
            original_keepalive_seconds,
        )
        self.addCleanup(
            setattr,
            hooks,
            "_ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS",
            original_min_delay_seconds,
        )

        class ServiceUnavailable(Exception):
            status_code = 503

        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        async def fake_fallback_round(
            _request_data,
            exception,
            *,
            allow_repeated_attempt=False,
            route_recovery_poll=False,
        ):
            attempts.append(exception)
            raise ServiceUnavailable("still unavailable")
            if False:
                yield None

        hooks._stream_streaming_error_fallback_round = fake_fallback_round
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.02")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        keepalives = [
            chunk for chunk in chunks if hooks._is_route_recovery_sse_keepalive(chunk)
        ]
        self.assertGreaterEqual(len(attempts), 2)
        self.assertGreaterEqual(len(keepalives), 1)
        self.assertTrue(all(chunk.get("type") == "response.in_progress" for chunk in keepalives))
        self.assertTrue(
            all(
                chunk.get("response", {})
                .get("metadata", {})
                .get("litellm_menu_keepalive")
                == "route_recovery"
                for chunk in keepalives
            )
        )
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_poll_empty_retries_until_max_duration(self) -> None:
        hooks, _proxy_server = load_hook_module()
        attempts = []

        class ServiceUnavailable(Exception):
            status_code = 503

        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        async def fake_fallback_round(
            _request_data,
            exception,
            *,
            allow_repeated_attempt=False,
            route_recovery_poll=False,
        ):
            attempts.append(
                {
                    "exception": exception,
                    "allow_repeated_attempt": allow_repeated_attempt,
                    "route_recovery_poll": route_recovery_poll,
                }
            )
            if False:
                yield None

        hooks._stream_streaming_error_fallback_round = fake_fallback_round
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.02")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
        }

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertGreaterEqual(len(attempts), 2)
        self.assertIs(attempts[0]["exception"], first_exception)
        self.assertTrue(attempts[0]["allow_repeated_attempt"])
        self.assertTrue(all(attempt["route_recovery_poll"] for attempt in attempts))
        self.assertFalse(any(isinstance(chunk, str) for chunk in chunks))
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_external_web_search_synthesis_recovery_polls_single_route_without_fallback_text(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.02")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        class GatewayTimeout(Exception):
            status_code = 504

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "chatroute"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "chatroute-pro"},
                    }
                ]

            async def aresponses(self, **payload):
                calls.append(payload)
                raise GatewayTimeout("still timing out")

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "legacy-chat",
            "input": (
                "Original user request. Any instruction to call or use web_search "
                "has already been satisfied by the compatibility bridge:\n"
                "Use web_search for LiteLLM URL.\n\n"
                "Retrieved evidence:\n"
                "Web search results for query: LiteLLM GitHub\n"
                "Title: LiteLLM\nURL: https://github.com/BerriAI/litellm"
            ),
            "stream": True,
            "litellm_metadata": {
                "external_web_search_synthesis": True,
                "external_web_search_search_results": (
                    "Web search results for query: LiteLLM GitHub\n"
                    "Title: LiteLLM\nURL: https://github.com/BerriAI/litellm"
                ),
            },
            "metadata": {"request_id": "web-search-recovery-no-route"},
            "model_info": {"id": "chatroute", "order": 1},
        }
        first_exception = GatewayTimeout("upstream synthesis timeout")
        first_exception.failed_deployment_id = "chatroute"
        first_exception.failed_deployment_order = 1

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertGreaterEqual(len(calls), 1)
        self.assertTrue(calls[0]["litellm_metadata"]["external_web_search_synthesis"])
        self.assertNotIn("tools", calls[0])
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_external_web_search_synthesis_recovery_uses_available_fallback_route(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered web answer"}
            yield {"type": "response.completed", "response": {"id": "resp-web-recovered"}}

        class ServiceUnavailable(Exception):
            status_code = 503

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "chatroute"},
                    },
                    {
                        "litellm_params": {"order": 2},
                        "model_info": {"id": "chatroute-pro"},
                    },
                ]

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "legacy-chat",
            "input": (
                "Original user request. Any instruction to call or use web_search "
                "has already been satisfied by the compatibility bridge:\n"
                "Use web_search for LiteLLM URL.\n\n"
                "Retrieved evidence:\n"
                "Web search results for query: LiteLLM GitHub\n"
                "Title: LiteLLM\nURL: https://github.com/BerriAI/litellm"
            ),
            "stream": True,
            "litellm_metadata": {
                "external_web_search_synthesis": True,
                "external_web_search_search_results": (
                    "Web search results for query: LiteLLM GitHub\n"
                    "Title: LiteLLM\nURL: https://github.com/BerriAI/litellm"
                ),
            },
            "metadata": {"request_id": "web-search-recovery-with-route"},
            "model_info": {"id": "chatroute", "order": 1},
        }
        first_exception = ServiceUnavailable("upstream synthesis server error")
        first_exception.failed_deployment_id = "chatroute"
        first_exception.failed_deployment_order = 1

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(calls[0]["_target_order"], 2)
        self.assertIn({"type": "response.output_text.delta", "delta": "recovered web answer"}, chunks)

    async def test_external_web_search_synthesis_stream_failure_falls_back_to_non_stream(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class GatewayTimeout(Exception):
            status_code = 504

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {
                        "litellm_params": {"order": 1},
                        "model_info": {"id": "chatroute"},
                    }
                ]

            async def aresponses(self, **payload):
                calls.append(copy.deepcopy(payload))
                if payload.get("stream") is True:
                    exc = TimeoutError("LiteLLM Menu stream start timeout after 60s without the first stream event")
                    exc.status_code = 504
                    exc.body = {"reason": "stream_start_timeout"}
                    exc.failed_deployment_id = "chatroute"
                    exc.failed_deployment_order = 1
                    raise exc
                return {
                    "id": "resp-non-stream-web-search-recovered",
                    "object": "response",
                    "status": "completed",
                    "output_text": "Recovered final answer.",
                    "output": [
                        {
                            "id": "msg-non-stream-web-search-recovered",
                            "type": "message",
                            "status": "completed",
                            "role": "assistant",
                            "content": [
                                {
                                    "type": "output_text",
                                    "text": "Recovered final answer.",
                                    "annotations": [],
                                }
                            ],
                        }
                    ],
                }

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "legacy-chat",
            "input": (
                "Original user request. Any instruction to call or use web_search "
                "has already been satisfied by the compatibility bridge:\n"
                "Use web_search for LiteLLM URL.\n\n"
                "Retrieved evidence:\n"
                "Web search results for query: LiteLLM GitHub\n"
                "Title: LiteLLM\nURL: https://github.com/BerriAI/litellm"
            ),
            "stream": True,
            "stream_options": {"include_usage": True},
            "stream_timeout": 99,
            "litellm_metadata": {
                "external_web_search_synthesis": True,
                "external_web_search_search_results": (
                    "Web search results for query: LiteLLM GitHub\n"
                    "Title: LiteLLM\nURL: https://github.com/BerriAI/litellm"
                ),
            },
            "metadata": {"request_id": "web-search-recovery-non-stream"},
            "model_info": {"id": "chatroute", "order": 1},
        }
        first_exception = GatewayTimeout("upstream synthesis timeout")

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual([call.get("stream") for call in calls], [True, False])
        self.assertTrue(calls[1]["litellm_metadata"]["external_web_search_synthesis"])
        self.assertTrue(calls[1]["litellm_metadata"][hooks._ROUTE_RECOVERY_POLL_METADATA_KEY])
        self.assertEqual(calls[1].get("_target_order"), 1)
        self.assertNotIn("tools", calls[1])
        self.assertNotIn("_excluded_deployment_ids", calls[1])
        self.assertNotIn("stream_options", calls[1])
        self.assertNotIn("stream_timeout", calls[1])
        self.assertNotIn("route_recovery_attempt_timeout_seconds", calls[1]["litellm_metadata"])
        self.assertNotIn("route_recovery_max_seconds", calls[1]["litellm_metadata"])
        deltas = [
            chunk.get("delta")
            for chunk in chunks
            if isinstance(chunk, dict) and chunk.get("type") == "response.output_text.delta"
        ]
        self.assertEqual(deltas, ["Recovered final answer."])
        self.assertEqual(chunks[-1]["type"], "response.completed")

    async def test_route_recovery_poll_preserves_no_healthy_reset_across_attempts(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class BadRequestError(Exception):
            status_code = 400

        class RateLimitError(Exception):
            status_code = 429

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                raise BadRequestError(
                    "You passed in model=balanced-chat. There are no healthy deployments for this model. "
                    "Received Model Group=balanced-chat Available Model Group Fallbacks=None"
                )

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.02")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "balanced-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "metadata": {"request_id": "route-recovery-no-healthy-test"},
            "model_info": {"id": "79f0dc70", "order": 1},
        }
        first_exception = RateLimitError("upstream 429")
        first_exception.failed_deployment_id = "79f0dc70"

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertGreaterEqual(len(calls), 2)
        self.assertTrue(all("_excluded_deployment_ids" not in call for call in calls))
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_poll_refreshes_routes_after_exhausted_no_deployments(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered after route refresh"}
            yield {"type": "response.completed", "response": {"id": "resp-refreshed"}}

        class BadRequestError(Exception):
            status_code = 400

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"litellm_params": {"order": 2}, "model_info": {"id": "order2-a"}},
                    {"litellm_params": {"order": 2}, "model_info": {"id": "order2-b"}},
                    {"litellm_params": {"order": 3}, "model_info": {"id": "order3-a"}},
                    {"litellm_params": {"order": 3}, "model_info": {"id": "order3-b"}},
                ]

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                if len(calls) == 1:
                    raise BadRequestError(
                        "You passed in model=default-chat. There are no healthy deployments for this model. "
                        "Received Model Group=default-chat Available Model Group Fallbacks=None"
                    )
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order3-a", "order": 3},
            "_excluded_deployment_ids": ["order2-a", "order2-b", "order3-a", "order3-b"],
        }
        first_exception = BadRequestError(
            "You passed in model=default-chat. There are no healthy deployments for this model. "
            "Received Model Group=default-chat Available Model Group Fallbacks=None"
        )

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(calls), 2)
        self.assertEqual(
            calls[0].get("_excluded_deployment_ids"),
            ["order2-a", "order2-b", "order3-a", "order3-b"],
        )
        self.assertNotIn("_excluded_deployment_ids", calls[1])
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered after route refresh"},
            chunks,
        )

    async def test_route_recovery_poll_cycles_orders_after_no_deployment_refresh(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered on order 3"}
            yield {"type": "response.completed", "response": {"id": "resp-order3"}}

        class BadRequestError(Exception):
            status_code = 400

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
                    {"litellm_params": {"order": 1}, "model_info": {"id": "order1-a"}},
                    {"litellm_params": {"order": 2}, "model_info": {"id": "order2-a"}},
                    {"litellm_params": {"order": 3}, "model_info": {"id": "order3-a"}},
                ]

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                if len(calls) < 4:
                    raise BadRequestError(
                        "You passed in model=default-chat. There are no healthy deployments for this model. "
                        "Received Model Group=default-chat Available Model Group Fallbacks=None"
                    )
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order3-a", "order": 3},
            "_target_order": 3,
            "_excluded_deployment_ids": ["order1-a", "order2-a", "order3-a"],
        }
        first_exception = BadRequestError(
            "You passed in model=default-chat. There are no healthy deployments for this model. "
            "Received Model Group=default-chat Available Model Group Fallbacks=None"
        )

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual([call.get("_target_order") for call in calls], [3, 1, 2, 3])
        self.assertEqual(calls[0].get("_excluded_deployment_ids"), ["order1-a", "order2-a", "order3-a"])
        self.assertTrue(all("_excluded_deployment_ids" not in call for call in calls[1:]))
        self.assertIn({"type": "response.output_text.delta", "delta": "recovered on order 3"}, chunks)

    async def test_route_recovery_poll_marks_local_timeout_and_advances_order(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered on pro"}
            yield {"type": "response.completed", "response": {"id": "resp-pro"}}

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return [
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

            async def aresponses(self, **payload):
                calls.append(payload.copy())
                if payload.get("_target_order") != 3:
                    raise AssertionError("route recovery must advance past the stuck route")
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {
                "id": "cheap",
                "order": 1,
                "route_key": "compat_provider / openai/default-chat / key=x-cheap / order=1",
            },
        }
        first_exception = TimeoutError(
            "LiteLLM Menu stream start timeout after 60s without the first stream event"
        )
        first_exception.status_code = 504
        first_exception.body = {"reason": "stream_start_timeout"}

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(calls[0]["_target_order"], 3)
        self.assertEqual(calls[0]["_excluded_deployment_ids"], ["cheap"])
        self.assertIn({"type": "response.output_text.delta", "delta": "recovered on pro"}, chunks)

    async def test_route_recovery_poll_retries_failures_until_max_duration(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        class ServiceUnavailable(Exception):
            status_code = 503

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                raise ServiceUnavailable("No deployments available for selected model")

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.02")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertGreaterEqual(len(calls), 2)
        self.assertFalse(any(isinstance(chunk, str) for chunk in chunks))
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_poll_uses_stall_timeout_for_stream_start(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.012")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        class ServiceUnavailable(Exception):
            status_code = 503

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                await asyncio.sleep(60)

        proxy_server.llm_router = FakeRouter()
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "60")
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.01")
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertGreaterEqual(len(calls), 1)
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_poll_uses_stall_timeout_for_delayed_recovery(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "0.04")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "60")
        self.set_env(hooks._STALL_TIMEOUT_SECONDS_ENV, "0.03")

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "recovered despite fallback timeout"}
            yield {"type": "response.completed", "response": {"id": "resp-recovered"}}

        class ServiceUnavailable(Exception):
            status_code = 503

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                await asyncio.sleep(0.02)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertGreaterEqual(len(calls), 1)
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered despite fallback timeout"},
            chunks,
        )
        completed_events = [
            chunk
            for chunk in chunks
            if isinstance(chunk, dict) and chunk.get("type") == "response.completed"
        ]
        self.assertEqual(len(completed_events), 1)
        self.assertEqual(completed_events[0]["response"]["id"], "resp-recovered")

    async def test_route_recovery_poll_uses_request_metadata_timeout_overrides(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "60")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")
        self.set_env(hooks._REQUEST_TIMEOUT_SECONDS_ENV, "60")

        class ServiceUnavailable(Exception):
            status_code = 503

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                await asyncio.sleep(60)

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "legacy-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "model_info": {"id": "chatroute", "order": 1},
            "litellm_metadata": {
                "route_recovery_attempt_timeout_seconds": 0.003,
                "route_recovery_max_seconds": 0.009,
            },
        }
        first_exception = ServiceUnavailable("upstream temporarily unavailable")

        started = time.monotonic()
        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]
        elapsed = time.monotonic() - started

        self.assertGreaterEqual(len(calls), 1)
        self.assertLessEqual(len(calls), 4)
        self.assertLess(elapsed, 0.05)
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_does_not_replay_original_web_search_after_search_started(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "60")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        class GatewayTimeout(Exception):
            status_code = 504

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                raise AssertionError("original web_search request must not be replayed")

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "legacy-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "metadata": {"request_id": "web-search-started-no-replay"},
            "model_info": {"id": "chatroute", "order": 1},
        }
        hooks._mark_external_web_search_started_for_request(request_data)
        first_exception = GatewayTimeout("upstream timeout after web_search")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(calls, [])
        assert_external_web_search_missing_answer_failed(self, chunks)

    async def test_route_recovery_stops_replaying_original_web_search_after_attempt_starts_search(self) -> None:
        hooks, _proxy_server = load_hook_module()
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_attempt = streaming_module._stream_route_recovery_poll_attempt
        attempts = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        class GatewayTimeout(Exception):
            status_code = 504

        async def fake_attempt(request_data, exception, *, attempt, deadline=None):
            attempts.append(attempt)
            if attempt == 1:
                hooks._mark_external_web_search_started_for_request(request_data)
                raise GatewayTimeout("synthesis timed out after search")
            raise AssertionError("original web_search request must not be replayed")
            yield None

        streaming_module._stream_route_recovery_poll_attempt = fake_attempt
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll_attempt",
            original_attempt,
        )

        request_data = {
            "model": "legacy-chat",
            "input": "Use web_search for Sample City weather.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "metadata": {"request_id": "web-search-started-during-poll"},
            "model_info": {"id": "chatroute", "order": 1},
        }
        first_exception = GatewayTimeout("upstream timeout before first chunk")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(attempts, [1])
        assert_upstream_route_failed_terminal(self, chunks)

    async def test_route_recovery_continues_with_web_search_recovery_payload_after_attempt_timeout(self) -> None:
        hooks, _proxy_server = load_hook_module()
        streaming_module = importlib.import_module("litellm_menu.streaming")
        original_attempt = streaming_module._stream_route_recovery_poll_attempt
        attempts = []
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")
        self.set_env(hooks._RECOVERY_INTERVAL_SECONDS_ENV, "0.001")

        class GatewayTimeout(Exception):
            status_code = 504

        recovery_request = {
            "model": "legacy-chat",
            "input": (
                "Original user request. Any instruction to call or use web_search "
                "has already been satisfied by the compatibility bridge:\n"
                "Use web_search for sample subject transporters.\n\n"
                "Retrieved evidence:\n"
                "Web search results for query: sample subject factor A factor B\n"
                "Title: Example Source\nURL: https://example.test/source-one"
            ),
            "stream": True,
            "litellm_metadata": {
                "external_web_search_synthesis": True,
                "external_web_search_search_results": (
                    "Web search results for query: sample subject factor A factor B\n"
                    "Title: Example Source\nURL: https://example.test/source-one"
                ),
            },
            "metadata": {"request_id": "web-search-synthesis-retry"},
            "model_info": {"id": "chatroute", "order": 1},
        }

        async def fake_attempt(request_data, exception, *, attempt, deadline=None):
            attempts.append(copy.deepcopy(request_data))
            if attempt == 1:
                hooks._mark_external_web_search_started_for_request(request_data)
                exc = GatewayTimeout("synthesis timed out after search")
                hooks._external_web_search_set_recovery_request(exc, recovery_request)
                raise exc
            self.assertTrue(
                request_data["litellm_metadata"]["external_web_search_synthesis"]
            )
            self.assertNotIn("tools", request_data)
            yield {"type": "response.output_text.delta", "delta": "recovered synthesis answer"}
            yield {"type": "response.completed", "response": {"id": "resp-web-synthesis-recovered"}}

        streaming_module._stream_route_recovery_poll_attempt = fake_attempt
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll_attempt",
            original_attempt,
        )

        request_data = {
            "model": "legacy-chat",
            "input": "Use web_search for sample subject transporters.",
            "stream": True,
            "tools": [{"type": "web_search"}],
            "metadata": {"request_id": "web-search-original-started"},
            "model_info": {"id": "chatroute", "order": 1},
        }
        first_exception = GatewayTimeout("upstream timeout before first chunk")

        chunks = [
            chunk
            async for chunk in hooks._stream_route_recovery_poll(
                request_data,
                first_exception,
            )
        ]

        self.assertEqual(len(attempts), 2)
        self.assertIn("tools", attempts[0])
        self.assertTrue(
            attempts[1]["litellm_metadata"]["external_web_search_synthesis"]
        )
        self.assertNotIn("tools", attempts[1])
        self.assertIn(
            {"type": "response.output_text.delta", "delta": "recovered synthesis answer"},
            chunks,
        )
        self.assertIn(
            {"type": "response.completed", "response": {"id": "resp-web-synthesis-recovered"}},
            chunks,
        )

    async def test_route_recovery_stream_response_uses_poll(self) -> None:
        hooks, proxy_server = load_hook_module()
        calls = []

        async def recovered_stream():
            yield {"type": "response.output_text.delta", "delta": "router recovered"}
            yield {"type": "response.completed", "response": {"id": "resp-router-recovered"}}

        class RouterRateLimitError(Exception):
            pass

        class FakeRouter:
            def _get_all_deployments(self, model_name, team_id=None):
                return []

            async def aresponses(self, **payload):
                calls.append(payload)
                return recovered_stream()

        proxy_server.llm_router = FakeRouter()
        request_data = {
            "model": "default-chat",
            "input": [{"role": "user", "content": "Continue."}],
            "stream": True,
            "metadata": {"request_id": "router-route-recovery-test"},
            "model_info": {"id": "order1-a", "order": 1},
        }
        first_exception = RouterRateLimitError("No deployments available for selected model")

        response = hooks._route_recovery_stream_response(request_data, first_exception)
        chunks = [
            chunk
            async for chunk in hooks.LiteLLMMenuHook().async_post_call_streaming_iterator_hook(
                user_api_key_dict=None,
                response=response,
                request_data=request_data,
            )
        ]

        self.assertEqual(len(calls), 1)
        self.assertIn({"type": "response.output_text.delta", "delta": "router recovered"}, chunks)
        self.assertIn({"type": "response.completed", "response": {"id": "resp-router-recovered"}}, chunks)

    async def test_route_recovery_poll_normalizes_completed_usage_for_codex(self) -> None:
        hooks, _proxy_server = load_hook_module()
        streaming_module = sys.modules["litellm_menu.streaming"]
        original_attempt = streaming_module._stream_route_recovery_poll_attempt

        class GatewayTimeout(Exception):
            status_code = 504

        async def fake_attempt(request_data, exception, *, attempt, deadline=None):
            yield {"type": "response.output_text.delta", "delta": "recovered"}
            yield {
                "type": "response.completed",
                "response": {
                    "id": "resp-recovered-usage",
                    "object": "response",
                    "status": "completed",
                    "output": [],
                    "usage": {
                        "prompt_tokens": 11,
                        "completion_tokens": 5,
                        "total_tokens": 16,
                    },
                },
            }

        streaming_module._stream_route_recovery_poll_attempt = fake_attempt
        self.addCleanup(
            setattr,
            streaming_module,
            "_stream_route_recovery_poll_attempt",
            original_attempt,
        )
        self.set_env(hooks._RECOVERY_MAX_SECONDS_ENV, "1")

        chunks = [
            jsonable_stream_chunk(chunk)
            async for chunk in hooks._stream_route_recovery_poll(
                {
                    "model": "legacy-chat",
                    "input": "Continue.",
                    "stream": True,
                    "model_info": {"id": "chatroute", "order": 1},
                },
                GatewayTimeout("upstream timed out"),
            )
        ]

        usage = chunks[-1]["response"]["usage"]
        self.assertEqual(chunks[-1]["type"], "response.completed")
        self.assertEqual(
            usage,
            {
                "input_tokens": 11,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 16,
            },
        )
