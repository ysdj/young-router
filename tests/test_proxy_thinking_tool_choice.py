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
