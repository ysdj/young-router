"""Thinking-mode tool-choice refusals are request compatibility errors."""

from __future__ import annotations

from hook_test_utils import *


_THINKING_TOOL_CHOICE_MESSAGE = (
    "litellm.BadRequestError: OpenAIException - thinking mode does not support "
    "this tool_choice (request_id: 6a1c0f6a-4d3d-4b4a-9f31-2c7d1e2f6a10)"
)
_DS_ROUTE_KEY = (
    "model=deepseek-v4.1-flash / provider=ds / upstream=openai/deepseek-flash / "
    "host=api.deepseek.com / key=r"
)


class _BadRequest(Exception):
    status_code = 400


def _tool_choice_error(message: str = _THINKING_TOOL_CHOICE_MESSAGE) -> Exception:
    return _BadRequest(message)


def _route_request(route_key: str = _DS_ROUTE_KEY, model_id: str = "ce96bf46"):
    return {
        "call_type": "aresponses",
        "model": "deepseek-v4.1-flash",
        "input": [{"role": "user", "content": "继续"}],
        "stream": True,
        "tools": [
            {"type": "function", "name": "bash", "parameters": {"type": "object"}},
        ],
        "tool_choice": "auto",
        "reasoning": {"effort": "max", "summary": "auto"},
        "model_info": {
            "id": model_id,
            "route_key": route_key,
            "provider": "ds",
            "upstream_url_surface": "openai/chat",
            "supports_responses_client_tools": False,
            "supports_responses_function_tools": True,
            "supported_upstream_url_surfaces": ["openai/chat"],
        },
    }


class ThinkingToolChoiceRefusalTests(HookTestCase):
    """``load_hook_module`` reloads the proxy modules: the routing module under
    test is the one the bridge code holds, never a stale import."""

    def test_the_refusal_is_classified_on_its_wording(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module

        self.assertTrue(
            routing._is_thinking_tool_choice_unsupported_error(_tool_choice_error())
        )
        # A validation refusal names the same field but is about the client's own
        # body, and a route that would have answered it keeps its place.
        self.assertFalse(
            routing._is_thinking_tool_choice_unsupported_error(
                _tool_choice_error(
                    "litellm.BadRequestError: OpenAIException - tool_choice "
                    "references a tool that does not exist"
                )
            )
        )
        # No thinking in the sentence: an ordinary 400 this proxy must not park
        # a route for.
        self.assertFalse(
            routing._is_thinking_tool_choice_unsupported_error(
                _tool_choice_error("tool_choice is not supported on this endpoint")
            )
        )

    def test_a_refusal_is_a_request_error(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._RECOVERY_POLICY_REQUEST_ERROR_ENV, "cooldown")
        self.set_env(routing._SLOW_FAILURE_RECOVERY_SECONDS_ENV, "1")
        token = routing._CURRENT_ATTEMPT_STARTED_AT.set(time.monotonic() - 10)
        self.addCleanup(routing._CURRENT_ATTEMPT_STARTED_AT.reset, token)

        self.assertEqual(
            routing._recovery_policy_for_exception(_tool_choice_error()),
            routing._RECOVERY_POLICY_ERROR,
        )

    async def test_unresolved_official_wire_is_repaired_after_the_specific_refusal(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = {
            "model": "fixture",
            "messages": [{"role": "user", "content": "check the fixture"}],
            "tool_choice": "required",
            "thinking": {"type": "enabled"},
        }
        refusal = _tool_choice_error()
        refusal.request = types.SimpleNamespace(
            method="POST", url="https://api.deepseek.com/v1/chat/completions"
        )
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                raise refusal
            return "tool call returned"

        self.assertEqual("tool call returned", await contract._call_with_deepseek_tool_choice(upstream, request))
        self.assertEqual(["required", "auto"], [call["tool_choice"] for call in calls])
        self.assertNotIn("extra_body", calls[0])
        self.assertEqual({"type": "enabled"}, calls[1]["thinking"])
        self.assertIs(request["messages"], calls[1]["messages"])

    async def test_known_official_route_adapts_before_sending_without_changing_reasoning(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = {
            **_route_request(),
            "api_base": "https://api.deepseek.com/v1",
            "tool_choice": "required",
            "extra_body": {
                "tool_choice": "required",
                "function_call": {"name": "bash"},
                "temperature": 0.5,
            },
        }
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs)
            raise refusal

        refusal = _tool_choice_error()
        refusal.request = types.SimpleNamespace(method="GET", url="https://litellm.ai")
        with self.assertRaises(_BadRequest) as error:
            await contract._call_with_deepseek_tool_choice(upstream, request)

        self.assertIs(refusal, error.exception)
        self.assertEqual(["auto"], [call["tool_choice"] for call in calls])
        self.assertEqual({"temperature": 0.5}, calls[0]["extra_body"])
        self.assertEqual(request["reasoning"], calls[0]["reasoning"])
        self.assertIs(request["tools"], calls[0]["tools"])

    async def test_injected_choice_is_repaired_when_the_official_wire_was_unknown(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = contract._with_injected_tool_choice({
            "model": "fixture",
            "messages": [{"role": "user", "content": "check the fixture"}],
            "tool_choice": "auto",
            "reasoning_effort": "high",
        }, "required")
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                refusal = _tool_choice_error()
                refusal.request = types.SimpleNamespace(
                    method="POST", url="https://api.deepseek.com/v1/chat/completions"
                )
                raise refusal
            return "tool call returned"

        self.assertEqual(
            "tool call returned", await contract._call_with_deepseek_tool_choice(upstream, request)
        )
        self.assertEqual(["required", "auto"], [call["tool_choice"] for call in calls])
        self.assertEqual("high", calls[1]["reasoning_effort"])

    async def test_messages_adaptation_preserves_thinking_and_uses_messages_auto_shape(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = {
            "api_base": "https://api.deepseek.com/anthropic",
            "call_type": "anthropic_messages",
            "messages": [{"role": "user", "content": "check"}],
            "tool_choice": {"type": "tool", "name": "inspect"},
            "thinking": {"type": "enabled", "budget_tokens": 1024},
        }
        calls = []
        async def upstream(**kwargs):
            calls.append(kwargs)
            return "tool call returned"

        self.assertEqual("tool call returned", await contract._call_with_deepseek_tool_choice(upstream, request))
        self.assertEqual(1, len(calls))
        self.assertEqual({"type": "auto"}, calls[0]["tool_choice"])
        self.assertEqual(request["thinking"], calls[0]["thinking"])

    async def test_synthetic_error_uses_selected_default_deepseek_provider(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        self.set_env("DEEPSEEK_API_BASE", "")
        request = {"model": "fixture", "tool_choice": "required", "reasoning_effort": "high"}
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs)
            if len(calls) == 1:
                refusal = _tool_choice_error()
                refusal.request = types.SimpleNamespace(method="GET", url="https://litellm.ai")
                raise refusal
            return "ok"

        def selected_route(active):
            return {**active, "litellm_params": {"model": "deepseek/fixture"}}

        self.assertEqual(
            "ok", await contract._call_with_deepseek_tool_choice(upstream, request, prepare_retry=selected_route)
        )
        self.assertEqual(["required", "auto"], [call["tool_choice"] for call in calls])
        self.assertEqual("high", calls[1]["reasoning_effort"])
        self.set_env("DEEPSEEK_API_BASE", "https://relay.example/v1")
        self.assertFalse(contract._request_targets_official_deepseek(selected_route(request)))

    async def test_explicitly_disabled_thinking_keeps_forced_choice(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = {
            "api_base": "https://api.deepseek.com/v1",
            "tool_choice": "required",
            "thinking": {"type": "disabled"},
        }
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs)
            return "ok"

        self.assertEqual("ok", await contract._call_with_deepseek_tool_choice(upstream, request))
        self.assertEqual(["required"], [call["tool_choice"] for call in calls])

    async def test_official_chat_preserves_native_thinking_in_wire_body(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        request = {
            "api_base": "https://api.deepseek.com/v1",
            "call_type": "acompletion",
            "tool_choice": "required",
            "thinking": {"type": "enabled"},
            "extra_body": {"temperature": 0.5},
        }
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs)
            return "ok"

        self.assertEqual("ok", await contract._call_with_deepseek_tool_choice(upstream, request))
        self.assertEqual("auto", calls[0]["tool_choice"])
        self.assertEqual({"type": "enabled"}, calls[0]["thinking"])
        self.assertEqual({"thinking": {"type": "enabled"}, "temperature": 0.5}, calls[0]["extra_body"])
        self.assertEqual({"temperature": 0.5}, request["extra_body"])

        request["extra_body"]["thinking"] = {"type": "disabled"}
        self.assertFalse(contract._deepseek_thinking_rejects_forced_choice(request))

    async def test_replay_repair_keeps_official_tool_choice_relaxed(self) -> None:
        hooks, _ = load_hook_module()
        contract = hooks._codex_contract_module
        replay = hooks._reasoning_replay_module
        request = {
            "api_base": "https://api.deepseek.com/v1",
            "model": "deepseek-v4.1-flash",
            "input": [
                {"type": "message", "role": "user", "content": "check"},
                {
                    "type": "reasoning",
                    "id": "rs-replay",
                    "summary": [],
                    "encrypted_content": "opaque",
                },
                {
                    "type": "function_call",
                    "call_id": "call-probe",
                    "name": "probe",
                    "arguments": "{}",
                },
                {
                    "type": "function_call_output",
                    "call_id": "call-probe",
                    "output": "ok",
                },
            ],
            "tools": [{"type": "function", "name": "probe"}],
            "tool_choice": "required",
            "reasoning": {"effort": "high", "summary": "auto"},
        }
        calls = []

        async def upstream(**kwargs):
            calls.append(kwargs.copy())
            if len(calls) == 1:
                raise _BadRequest(
                    "the `reasoning_text` in the thinking mode must be passed back"
                )
            return "ok"

        result = await replay.call_with_replay_repair(
            lambda: contract._call_with_deepseek_tool_choice(upstream, request),
            request,
        )

        self.assertEqual("ok", result)
        self.assertEqual(["auto", "auto"], [call["tool_choice"] for call in calls])
        self.assertEqual(
            replay.REASONING_TEXT_PLACEHOLDER,
            calls[1]["input"][1]["content"][0]["text"],
        )

    def test_refusals_do_not_park_the_route(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        routing._DEPLOYMENT_COOLDOWNS.clear()
        threshold = routing._deployment_cooldown_failure_threshold()
        self.assertGreater(threshold, 0)

        for _ in range(threshold):
            routing._record_deployment_failure_for_cooldown(
                _tool_choice_error(),
                _route_request(),
            )

        states = [
            state
            for state in routing._DEPLOYMENT_COOLDOWNS.values()
            if isinstance(state, dict) and state.get("cooldown_until")
        ]
        self.assertFalse(states, routing._DEPLOYMENT_COOLDOWNS)
        routing._DEPLOYMENT_COOLDOWNS.clear()

    def _parked_states(self, routing):
        return [
            state
            for state in routing._DEPLOYMENT_COOLDOWNS.values()
            if isinstance(state, dict) and state.get("cooldown_until")
        ]

    def test_an_empty_completion_is_not_charged_to_the_route(self) -> None:
        """A turn that spent its budget thinking says nothing about the route.

        On the live host one ``max_output_tokens: 16`` probe -- ``input: "hi"``
        with thinking on -- reached the output limit before it wrote any answer,
        so the proxy ended the read loop on its own empty-completion verdict.
        That was reported as a cut stream, and a cut stream is quarantined on
        the first occurrence: ``api.deepseek.com`` was parked for 300 s and
        every request in that window fell to a relay seven times slower (2298 ms
        to first byte against 240 ms).  The client-facing code stays what it was;
        only the route's health record is spared.
        """

        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        streaming = hooks._streaming_module
        routing._DEPLOYMENT_COOLDOWNS.clear()

        for _ in range(5):
            empty = streaming._responses_incomplete_stream_exception(
                "the completion carried no answer",
                buffer=[],
                request_data=_route_request(),
            )
            empty.empty_completion = True
            routing._record_deployment_failure_for_cooldown(empty, _route_request())

        self.assertFalse(self._parked_states(routing), routing._DEPLOYMENT_COOLDOWNS)

        # The same stream label without that verdict is still a cut stream, and
        # a cut stream parks on the first occurrence.
        cut = streaming._responses_incomplete_stream_exception(
            "stream ended before response.completed",
            buffer=[],
            request_data=_route_request(),
        )
        routing._record_deployment_failure_for_cooldown(cut, _route_request())
        self.assertTrue(self._parked_states(routing), routing._DEPLOYMENT_COOLDOWNS)
        routing._DEPLOYMENT_COOLDOWNS.clear()

    def test_a_validation_refusal_never_parks_anything(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        routing._DEPLOYMENT_COOLDOWNS.clear()

        for _ in range(5):
            routing._record_deployment_failure_for_cooldown(
                _tool_choice_error(
                    "litellm.BadRequestError: OpenAIException - tool_choice "
                    "references a tool that does not exist"
                ),
                _route_request(),
            )

        self.assertFalse(
            [
                state
                for state in routing._DEPLOYMENT_COOLDOWNS.values()
                if isinstance(state, dict) and state.get("cooldown_until")
            ]
        )

    def test_the_refusal_is_not_repeated_on_the_same_route(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._SAME_DEPLOYMENT_RETRIES_ENV, "2")

        self.assertEqual(
            routing._same_deployment_retry_budget(_tool_choice_error()), 0
        )
        self.assertEqual(
            2,
            routing._same_deployment_retry_budget(
                _tool_choice_error(
                    "litellm.BadRequestError: OpenAIException - tool_choice "
                    "references a tool that does not exist"
                )
            ),
        )

    def test_unrelated_rate_limit_keeps_the_configured_delay(self) -> None:

        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        recovery_interval = routing._stream_route_exhaustion_retry_delay_seconds()
        self.assertGreater(recovery_interval, 0.2)

        self.assertEqual(
            routing._route_exhaustion_retry_delay_for_exception(
                _tool_choice_error(), recovery_interval
            ),
            recovery_interval,
        )
        # A rate-limit answer keeps the configured delay when it names no
        # retry-after.  The wrapper class is a 400, so the status has to be set
        # here or the 429 branch never runs.
        rate_limited = _tool_choice_error(
            "litellm.RateLimitError: OpenAIException - slow down"
        )
        rate_limited.status_code = 429
        self.assertEqual(
            recovery_interval,
            routing._route_exhaustion_retry_delay_for_exception(
                rate_limited, recovery_interval
            ),
        )

    async def test_the_retry_reaches_the_route_that_refused(self) -> None:
        """The extra attempt is worthless if the route is still excluded.

        Measured live on a single-route group: the retry ran (``final_route_retry``
        with a 0.2 s delay) and the next attempt answered "no deployments
        available" without contacting an upstream, because the failover marking
        had already put that one route in the exclusion list.  The retry must put
        it back -- and only it, so another route's verdict still stands.
        """

        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._SAME_DEPLOYMENT_RETRIES_ENV, "0")
        self.set_env(routing._RECOVERY_INTERVAL_SECONDS_ENV, "0")

        refusal = _tool_choice_error()
        refusal.failed_deployment_id = "ce96bf46"
        request = _route_request()
        request["_excluded_deployment_ids"] = ["ce96bf46", "someone-else"]
        request["litellm_metadata"] = {
            "excluded_deployment_ids": ["ce96bf46", "someone-else"]
        }

        routing._allow_the_failed_deployment_again(refusal, request)

        self.assertEqual(["someone-else"], request["_excluded_deployment_ids"])
        self.assertEqual(
            ["someone-else"],
            request["litellm_metadata"]["excluded_deployment_ids"],
        )

        # The last exclusion goes away entirely rather than leaving an empty
        # list the router would read as "every route is excluded".
        request["_excluded_deployment_ids"] = ["ce96bf46"]
        routing._allow_the_failed_deployment_again(refusal, request)
        self.assertNotIn("_excluded_deployment_ids", request)

    async def test_a_single_route_refusal_is_not_replayed(self) -> None:

        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._SAME_DEPLOYMENT_RETRIES_ENV, "0")
        self.set_env(routing._RECOVERY_INTERVAL_SECONDS_ENV, "0")
        routing._DEPLOYMENT_COOLDOWNS.clear()

        router_module = types.ModuleType("litellm.router")
        attempts = []

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
            attempts.append(kwargs.get("_young_router_upstream_url_surface"))
            if len(attempts) == 1:
                error = _tool_choice_error()
                error.failed_deployment_id = "ce96bf46"
                routing._mark_exception_for_deployment_failover(error, kwargs)
                raise error
            return {"served": True}

        request = _route_request()
        request.pop("model", None)
        response = await Router()._ageneric_api_call_with_fallbacks_helper(
            "deepseek-v4.1-flash",
            original_generic_function,
            **request,
        )
        events = [jsonable_stream_chunk(chunk) async for chunk in response]

        self.assertEqual(1, len(attempts))
        self.assertEqual("response.failed", events[-1]["type"])
        self.assertFalse(
            [
                state
                for state in routing._DEPLOYMENT_COOLDOWNS.values()
                if isinstance(state, dict) and state.get("cooldown_until")
            ]
        )


class InternalSearchCallKeepsNoRetryTests(HookTestCase):
    """The proxy's own web-search call spends no same-route retry.

    Its caller is the turn that asked for the search, which has its own way on,
    so the helper used to give such a call a budget of zero.  The per-failure
    budget that replaced the per-call one computed that zero and then never
    read it, so an internal call was retried like any other.
    """

    async def _attempts(self, metadata: dict) -> int:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._SAME_DEPLOYMENT_RETRIES_ENV, "1")
        self.set_env(routing._RECOVERY_INTERVAL_SECONDS_ENV, "0")
        routing._DEPLOYMENT_COOLDOWNS.clear()

        router_module = types.ModuleType("litellm.router")
        attempts: list[int] = []

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
            attempts.append(1)
            error = _tool_choice_error("temporary server failure")
            error.status_code = 503
            error.failed_deployment_id = "ce96bf46"
            routing._mark_exception_for_deployment_failover(error, kwargs)
            raise error

        request = _route_request()
        request.pop("model", None)
        request.setdefault("litellm_metadata", {}).update(metadata)
        # How the helper reports the final failure (raised, or answered as a
        # terminal) is not what this pins; how often the route was asked is.
        try:
            await Router()._ageneric_api_call_with_fallbacks_helper(
                "deepseek-v4.1-flash",
                original_generic_function,
                **request,
            )
        except Exception:
            pass
        return len(attempts)

    async def test_an_ordinary_turn_is_asked_again(self) -> None:
        # The control: the same failure on an ordinary turn does earn retries,
        # so "once" below is the internal call's rule and not the harness's.
        self.assertGreater(await self._attempts({}), 1)

    async def test_an_internal_search_call_is_asked_once(self) -> None:
        self.assertEqual(1, await self._attempts({"external_web_search_continuation": True}))
        self.assertEqual(1, await self._attempts({"external_web_search_synthesis": True}))
