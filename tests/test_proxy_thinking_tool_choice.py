"""A route that refuses ``tool_choice`` while it thinks.

DeepSeek answers ``400 thinking mode does not support this tool_choice`` to a
Codex turn.  It is *not* the field: the identical replay with ``tool_choice``
removed is refused the same way (measured live 2026-11-04), and the same payload
signature is served outright about half the time.  So the refusal is the route
answering about this turn, and what it must not do is sit first in every request
while a sibling could answer: one Codex task paid it on every turn -- ~10 s and a
full upload of a 60 MB replay, 178 times in three hours.

These tests pin the two halves: the classifier reads that wording and nothing
else, and the policy parks the route after the ordinary failure threshold instead
of ending the turn.
"""

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

    def test_a_refusal_parks_the_route_instead_of_ending_the_turn(self) -> None:
        hooks, _ = load_hook_module()
        routing = hooks._routing_module

        self.assertEqual(
            routing._recovery_policy_for_exception(_tool_choice_error()),
            routing._RECOVERY_POLICY_COOLDOWN,
        )

    def test_two_refusals_park_the_route_for_the_cooldown_window(self) -> None:
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
        self.assertTrue(states, routing._DEPLOYMENT_COOLDOWNS)
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

    def test_the_refusal_earns_one_more_attempt_on_the_same_route(self) -> None:
        """A coin flip is asked again, on the route that flipped it.

        Measured through this proxy: the request DeepSeek refused carried
        ``tool_choice: null``, so there was nothing to relax and the bytes were
        not the problem -- the same payload signature is served outright about
        half the time.  With the configured budget at its default of zero the
        only outcomes were a sibling (which a single-route group does not have)
        or a failed turn, and the cooldown that parked the route had nothing to
        wait for.
        """

        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        self.set_env(routing._SAME_DEPLOYMENT_RETRIES_ENV, "0")
        self.assertEqual(0, routing._same_deployment_retries())

        self.assertGreaterEqual(
            routing._same_deployment_retry_budget(_tool_choice_error()), 1
        )
        # Everything else keeps the configured budget: this allowance is about
        # one class, not a global change of retry behaviour.
        self.assertEqual(
            0,
            routing._same_deployment_retry_budget(
                _tool_choice_error(
                    "litellm.BadRequestError: OpenAIException - tool_choice "
                    "references a tool that does not exist"
                )
            ),
        )

    def test_the_extra_attempt_is_asked_again_immediately(self) -> None:
        """Waiting the recovery interval would make the flake cost seconds.

        Nothing has to recover for this class: the route is up and answering,
        it just answered the other way this time.
        """

        hooks, _ = load_hook_module()
        routing = hooks._routing_module
        recovery_interval = routing._stream_route_exhaustion_retry_delay_seconds()
        self.assertGreater(recovery_interval, 0.2)

        self.assertLessEqual(
            routing._route_exhaustion_retry_delay_for_exception(
                _tool_choice_error(), recovery_interval
            ),
            0.2,
        )
        # A 429 still honours its own backoff, and an ordinary failure keeps the
        # configured delay.
        self.assertEqual(
            recovery_interval,
            routing._route_exhaustion_retry_delay_for_exception(
                _tool_choice_error(
                    "litellm.RateLimitError: OpenAIException - slow down"
                ),
                recovery_interval,
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

    async def test_a_served_retry_ends_the_turn_and_parks_nothing(self) -> None:
        """The retry is the whole remedy: no cooldown, no second refusal.

        The upstream serves this payload signature about half the time, so one
        route asked twice usually answers.  Running it through the generic
        helper is what proves the attempt is spent on the same route instead of
        advancing to a peer that does not exist.
        """

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

        self.assertEqual({"served": True}, response)
        self.assertEqual(2, len(attempts))
        self.assertFalse(
            [
                state
                for state in routing._DEPLOYMENT_COOLDOWNS.values()
                if isinstance(state, dict) and state.get("cooldown_until")
            ]
        )
