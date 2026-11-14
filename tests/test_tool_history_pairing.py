"""A replayed tool history is paired before any route is asked to accept it.

Ground: a thread whose turn was cut between a tool call and its output replays
one half of the pair, and every wire refuses the whole body -- ``400 orphaned
tool_result in conversation history`` on the Messages surface, ``工具记录不完整``
on a station's own validator, ``No tool call found for function call output`` on
the Responses one.  The refusal is about the replay, so a resend meets it again
and the turn ends on "start a new task", losing the work in the thread.

The pairing is repairable from the client's own data, and the repair has to
happen before a deployment is chosen: after the refusal it costs one refused
round trip per turn and cannot help a client that keeps the broken history.
"""

from __future__ import annotations

import json
import unittest

from hook_test_utils import HookTestCase, load_hook_module


class ToolHistoryPairingTests(unittest.TestCase):
    def _module(self):
        import sys

        sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[1]))
        from young_router.proxy import tool_history

        return tool_history

    def test_an_orphan_result_leaves_and_a_quiet_history_is_untouched(self) -> None:
        tool_history = self._module()
        paired = [
            {"type": "function_call", "call_id": "call_a", "name": "exec", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "call_a", "output": "ok"},
        ]

        self.assertIsNone(tool_history.paired_request({"input": paired}))
        # Nothing changed, so the caller is handed ``None`` rather than a copy.
        self.assertIsNone(
            tool_history.paired_request(
                {"input": [{"type": "message", "role": "user", "content": "hi"}]}
            )
        )

    def test_an_orphan_output_is_dropped_and_the_pair_is_kept(self) -> None:
        """A result whose call left the history is what the route refuses.

        Compaction that keeps a result but drops its call is the shape every
        client has produced at least once.  The result answers nothing the
        model can read, so it leaves -- and the call it belonged to, if it is
        still there, keeps its own answer.
        """

        tool_history = self._module()
        request = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "run it"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_keep", "name": "Bash", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_gone", "content": "old"},
                        {"type": "tool_result", "tool_use_id": "toolu_keep", "content": "ok"},
                    ],
                },
            ],
        }

        repaired = tool_history.paired_request(request)

        assert repaired is not None
        blocks = repaired["messages"][2]["content"]
        self.assertEqual(
            [block["tool_use_id"] for block in blocks],
            ["toolu_keep"],
        )
        # The caller's own dict is not mutated: a failed attempt may still need it.
        self.assertEqual(len(request["messages"][2]["content"]), 2)

    def test_an_unanswered_call_is_answered_where_the_wire_reads_it(self) -> None:
        """A call with no result is what ends the turn; it gets a stated answer.

        The placeholder is the fact, not an invented success, and it goes in the
        turn that follows the call -- Messages reads the result from there, so a
        notice placed anywhere else turns the answer into that notice's answer.
        """

        tool_history = self._module()
        request = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "go"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {}},
                        {"type": "tool_use", "id": "toolu_b", "name": "Read", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_a", "content": "ok"},
                        {"type": "text", "text": "and then?"},
                    ],
                },
            ],
        }

        repaired = tool_history.paired_request(request)

        assert repaired is not None
        blocks = repaired["messages"][2]["content"]
        self.assertEqual(blocks[0]["tool_use_id"], "toolu_a")
        self.assertEqual(blocks[1]["tool_use_id"], "toolu_b")
        self.assertTrue(blocks[1]["is_error"])
        self.assertIn("No result was recorded", blocks[1]["content"])
        # The user's own words stay after the results, where the wire reads them.
        self.assertEqual(blocks[-1]["type"], "text")
        self.assertEqual(blocks[-1]["text"], "and then?")

    def test_a_result_with_no_following_turn_gets_one(self) -> None:
        """The last turn is a call and the client's next request is the retry.

        There is no following user turn to hold the result, so the turn is
        created: without it the call is unanswered for the rest of the thread.
        """

        tool_history = self._module()
        request = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "go"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {}},
                    ],
                },
            ],
        }

        repaired = tool_history.paired_request(request)

        assert repaired is not None
        self.assertEqual(len(repaired["messages"]), 3)
        self.assertEqual(repaired["messages"][2]["role"], "user")
        self.assertEqual(
            repaired["messages"][2]["content"][0]["tool_use_id"], "toolu_a"
        )

    def test_a_responses_replay_is_paired_by_either_identifier(self) -> None:
        """A resumed thread replays the item id where the call used ``call_id``."""

        tool_history = self._module()
        request = {
            "input": [
                {"type": "message", "role": "user", "content": "go"},
                {
                    "type": "function_call",
                    "id": "fc_1",
                    "call_id": "call_1",
                    "name": "exec",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_1", "output": "ok"},
                {
                    "type": "function_call",
                    "id": "fc_2",
                    "call_id": "call_2",
                    "name": "exec",
                    "arguments": "{}",
                },
                {"type": "function_call_output", "call_id": "call_gone", "output": "stale"},
            ],
        }

        repaired = tool_history.paired_request(request)

        assert repaired is not None
        items = repaired["input"]
        self.assertEqual(
            [item["type"] for item in items],
            [
                "message",
                "function_call",
                "function_call_output",
                "function_call",
                "function_call_output",
            ],
        )
        # The unanswered call is answered right after itself, before the stale
        # result that followed it -- a Chat surface reads the answer from there.
        self.assertEqual(items[3]["call_id"], "call_2")
        self.assertEqual(items[4]["call_id"], "call_2")
        self.assertNotIn("call_gone", [item.get("call_id") for item in items])

    def test_a_thread_held_by_the_upstream_is_left_alone(self) -> None:
        """With ``previous_response_id`` the history is not in this body."""

        tool_history = self._module()

        self.assertIsNone(
            tool_history.paired_request(
                {
                    "previous_response_id": "resp_1",
                    "input": [
                        {"type": "function_call_output", "call_id": "call_x", "output": "?"}
                    ],
                }
            )
        )


class ToolHistoryPreCallTests(HookTestCase):
    async def test_the_pre_call_hook_pairs_before_a_deployment_is_chosen(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        request = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "go"}]},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "toolu_a", "name": "Bash", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "toolu_gone", "content": "x"}
                    ],
                },
            ],
        }

        modified = await hook.async_pre_call_hook({}, None, request, None)

        assert modified is not None
        blocks = modified["messages"][-1]["content"]
        self.assertEqual(
            [block["tool_use_id"] for block in blocks],
            ["toolu_a"],
        )
        # The refusal this prevents is the one that ends the turn on a new task.
        self.assertNotIn("toolu_gone", json.dumps(modified))

    async def test_a_paired_replay_is_not_rewritten(self) -> None:
        hooks, _ = load_hook_module()
        hook = hooks.YoungRouterHook()
        request = {
            "model": "default-chat",
            "messages": [{"role": "user", "content": "hi"}],
        }

        # The hook's only other change rides on the request it was handed, so a
        # paired history answers ``None`` and the caller keeps its own object.
        self.assertIsNone(await hook.async_pre_call_hook({}, None, request, None))
        self.assertNotIn("messages", json.dumps({k: v for k, v in request.items() if k != "messages"}))


if __name__ == "__main__":
    unittest.main()
