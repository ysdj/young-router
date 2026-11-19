from __future__ import annotations

import copy

from hook_test_utils import *


class _ThinkingRequiredRefusal(Exception):
    """The endpoint's own words for a history whose tool turns lost thinking.

    Measured against ``api.deepseek.com/anthropic/v1/messages`` on 2026-11-19:
    one assistant tool turn without a ``thinking`` block answers exactly this,
    and the same body with an empty placeholder block answers 200.
    """

    status_code = 400

    def __init__(self) -> None:
        super().__init__(
            "litellm.BadRequestError: AnthropicException - "
            '{"type":"error","error":{"type":"invalid_request_error",'
            '"message":"The `content[].thinking` in the thinking mode must be '
            'passed back to the API. (request_id: 7958730c)"}}'
        )


def _mixed_history() -> dict:
    """A thread whose turns were answered by two different routes."""

    return {
        "model": "deepseek-v4.1-flash",
        "max_tokens": 64,
        "messages": [
            {"role": "user", "content": [{"type": "text", "text": "list files"}]},
            {
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "hmm", "signature": ""},
                    {"type": "tool_use", "id": "t1", "name": "shell", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t1", "content": "a.txt"}
                ],
            },
            # Answered by a route that has no thinking block on its wire.
            {
                "role": "assistant",
                "content": [
                    {"type": "tool_use", "id": "t2", "name": "shell", "input": {}},
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "tool_result", "tool_use_id": "t2", "content": "b.txt"}
                ],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "done"}],
            },
        ],
        "litellm_params": {"litellm_call_id": "call-thinking-block"},
        "model_info": {"id": "b1673a97"},
        "client_metadata": {"session_id": "d5d38978"},
    }


class ThinkingBlockReplayTests(HookTestCase):
    def setUp(self) -> None:
        super().setUp()
        module = importlib.import_module("young_router.proxy.thinking_block_replay")
        module._REQUIREMENTS.clear()
        self.addCleanup(module._REQUIREMENTS.clear)
        self.module = module

    def test_the_endpoints_sentence_is_the_classifier(self) -> None:
        self.assertTrue(
            self.module.is_thinking_required_error(_ThinkingRequiredRefusal())
        )
        # The same status for another reason is not this rule.
        other = Exception(
            'litellm.BadRequestError: AnthropicException - {"error":'
            '{"message":"This model\'s maximum context length is 128000 tokens"}}'
        )
        other.status_code = 400
        self.assertFalse(self.module.is_thinking_required_error(other))
        # And a refusal about something else entirely is untouched.
        self.assertFalse(
            self.module.is_thinking_required_error(RuntimeError("upstream went away"))
        )

    def test_only_the_tool_turn_without_thinking_gets_the_placeholder(self) -> None:
        body = _mixed_history()

        repaired = self.module.with_inserted_thinking_blocks(body)

        messages = repaired["messages"]
        self.assertEqual(
            self.module.PLACEHOLDER_THINKING_BLOCK, messages[3]["content"][0]
        )
        # The turn that already carried thinking is untouched, in order.
        self.assertEqual(body["messages"][1], messages[1])
        # A text-only assistant turn is accepted without one (measured).
        self.assertEqual(body["messages"][5], messages[5])
        self.assertEqual(len(body["messages"]), len(messages))
        # The original body is not mutated: several callers hold it.
        self.assertEqual(1, len(body["messages"][3]["content"]))

    def test_a_history_that_needs_nothing_is_left_alone(self) -> None:
        body = _mixed_history()
        body["messages"].pop(3)

        self.assertIsNone(self.module.with_inserted_thinking_blocks(body))
        plain = {"messages": [{"role": "user", "content": "hi"}]}
        self.assertIsNone(self.module.with_inserted_thinking_blocks(plain))

    async def test_a_refused_history_is_resent_once_with_the_block(self) -> None:
        body = _mixed_history()
        sent: list = []

        async def call() -> str:
            sent.append(copy.deepcopy(body))
            if len(sent) == 1:
                raise _ThinkingRequiredRefusal()
            return "answered"

        result = await self.module.call_with_thinking_block_repair(call, body)

        self.assertEqual("answered", result)
        self.assertEqual(2, len(sent))
        self.assertEqual(
            self.module.PLACEHOLDER_THINKING_BLOCK,
            sent[1]["messages"][3]["content"][0],
        )
        # Learned for this session on this route.
        self.assertEqual("required", self.module._recorded_requirement(body))

    async def test_the_learned_repair_is_applied_before_the_next_turn(self) -> None:
        first = _mixed_history()
        failed: list = []

        async def failing() -> str:
            failed.append(1)
            raise _ThinkingRequiredRefusal()

        with self.assertRaises(_ThinkingRequiredRefusal):
            await self.module.call_with_thinking_block_repair(failing, first)

        # A second refusal on the repaired body records a decline, so this
        # test marks the route as requiring it the way a success would.
        self.module._record_requirement(first, "required", 60)
        body = _mixed_history()
        sent: list = []

        async def call() -> str:
            sent.append(copy.deepcopy(body))
            return "answered"

        await self.module.call_with_thinking_block_repair(call, body)

        self.assertEqual(1, len(sent))
        self.assertEqual(
            self.module.PLACEHOLDER_THINKING_BLOCK,
            sent[0]["messages"][3]["content"][0],
        )

    async def test_a_second_refusal_is_not_paid_for_again(self) -> None:
        body = _mixed_history()
        attempts: list = []

        async def call() -> str:
            attempts.append(1)
            raise _ThinkingRequiredRefusal()

        with self.assertRaises(_ThinkingRequiredRefusal):
            await self.module.call_with_thinking_block_repair(call, body)
        self.assertEqual(2, len(attempts))

        # The decline is remembered: this turn's next attempt does not resend.
        again = _mixed_history()
        with self.assertRaises(_ThinkingRequiredRefusal):
            await self.module.call_with_thinking_block_repair(call, again)
        self.assertEqual(3, len(attempts))
