"""A Messages turn served by Responses keeps a tool call's arguments.

The Responses wire is allowed to state a function call's complete
``arguments`` on the item itself (``response.output_item.added`` or
``response.output_item.done``) and never emit a
``response.function_call_arguments.delta``.  LiteLLM's Anthropic bridge opens
``tool_use`` from the item but forwards the input only from the delta events,
so such a call reached the client as ``input: {}``.

Observed 2026-11-10 with the local Claude Code on a Responses-pinned GPT route:
two parallel calls arrived in one turn, one streamed its arguments and stayed
intact, the other stated only the finished item and arrived empty.  Claude
Code's Bash tool then answered ``InputValidationError: the required parameter
command is missing`` on every attempt; the model retried the same empty call
until the client gave up.

The probe runs in its own interpreter: another suite replaces ``litellm`` in
``sys.modules`` with a stub for the length of the process, and the real bridge
cannot then be imported.  It drives ``patches.install_all`` so the production
install path is what puts the repair in front of the provider stream, not a
helper called only here.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]

PROBE = textwrap.dedent(
    r"""
    import asyncio, json

    from young_router.proxy import patches

    patches.install_all()

    from litellm.llms.anthropic.pass_through.responses_adapters.streaming_iterator import (
        AnthropicResponsesStreamWrapper,
    )

    ALPHA = '{"command": "echo alpha"}'
    BETA = '{"command": "echo beta"}'
    PARTIAL = '{"command": "echo'
    COMPLETE = '{"command": "echo partial"}'

    def item(item_id, call_id, arguments):
        return {
            "id": item_id,
            "type": "function_call",
            "status": "in_progress",
            "call_id": call_id,
            "name": "Bash",
            "arguments": arguments,
        }

    def events_for(second_arguments, first_delta, first_done_arguments):
        return [
            {
                "type": "response.created",
                "response": {"id": "resp_test", "status": "in_progress", "output": []},
            },
            {"type": "response.output_item.added", "item": item("fc_a", "call_a", "")},
            {
                "type": "response.output_item.added",
                "item": item("fc_b", "call_b", second_arguments),
            },
            {
                "type": "response.function_call_arguments.delta",
                "item_id": "fc_a",
                "delta": first_delta,
            },
            {
                "type": "response.output_item.done",
                "item": {**item("fc_a", "call_a", first_done_arguments), "status": "completed"},
            },
            {
                "type": "response.output_item.done",
                "item": {**item("fc_b", "call_b", second_arguments), "status": "completed"},
            },
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_test",
                    "status": "completed",
                    "output": [
                        {**item("fc_a", "call_a", first_done_arguments), "status": "completed"},
                        {**item("fc_b", "call_b", second_arguments), "status": "completed"},
                    ],
                },
            },
        ]

    async def chunks_for(events):
        wrapper = AnthropicResponsesStreamWrapper(
            responses_stream=iter(events),
            model="gpt-6.1-sol",
        )
        chunks = []
        async for chunk in wrapper:
            chunks.append(chunk)
        return chunks

    def tool_inputs(chunks):
        blocks = {}
        positions = {"delta": {}, "stop": {}}
        for position, chunk in enumerate(chunks):
            kind = chunk.get("type")
            if kind == "content_block_start":
                block = chunk.get("content_block") or {}
                if block.get("type") == "tool_use":
                    blocks[chunk.get("index")] = {
                        "id": block.get("id"),
                        "json": "",
                    }
            elif kind == "content_block_delta":
                delta = chunk.get("delta") or {}
                if delta.get("type") == "input_json_delta":
                    blocks[chunk.get("index")]["json"] += delta.get("partial_json") or ""
                    positions["delta"][chunk.get("index")] = position
            elif kind == "content_block_stop":
                positions["stop"][chunk.get("index")] = position
        parsed = {
            block["id"]: json.loads(block["json"]) if block["json"] else {}
            for block in blocks.values()
        }
        ordered = {
            index: positions["delta"].get(index, -1) < positions["stop"].get(index, -1)
            for index in blocks
        }
        return parsed, ordered

    async def main():
        streamed, streamed_order = tool_inputs(
            await chunks_for(events_for(BETA, ALPHA, ALPHA))
        )
        partial, partial_order = tool_inputs(
            await chunks_for(events_for(BETA, PARTIAL, COMPLETE))
        )
        print(json.dumps({
            "streamed": streamed,
            "streamed_order": streamed_order,
            "partial": partial,
            "partial_order": partial_order,
        }))

    asyncio.run(main())
    """
)


class AnthropicResponsesToolArgumentsTests(unittest.TestCase):
    def test_an_item_only_function_call_reaches_the_client_with_its_input(self) -> None:
        completed = subprocess.run(
            [sys.executable, "-c", PROBE],
            cwd=str(ROOT),
            capture_output=True,
            text=True,
            timeout=180,
            env={
                **os.environ,
                # The bundled runtime puts LiteLLM's site-packages on this path
                # in the launcher; ``sys.executable`` is the interpreter it
                # exec'd, so the child has to inherit that path rather than
                # rebuild it.
                "PYTHONPATH": str(ROOT)
                + (
                    os.pathsep + os.environ["PYTHONPATH"]
                    if os.environ.get("PYTHONPATH")
                    else ""
                ),
                "LITELLM_LOCAL_MODEL_COST_MAP": "true",
            },
        )
        self.assertEqual(0, completed.returncode, completed.stderr[-4000:])
        result = json.loads(completed.stdout.strip().splitlines()[-1])

        # The call that only stated its arguments on the item is the one this
        # patch exists for; the call that streamed deltas must not gain a
        # second copy of them.
        self.assertEqual(
            {"command": "echo alpha"},
            result["streamed"]["call_a"],
        )
        self.assertEqual(
            {"command": "echo beta"},
            result["streamed"]["call_b"],
        )
        self.assertEqual({"0": True, "1": True}, result["streamed_order"])

        # A relay that streams a truncated call and finishes with the whole one
        # gets the missing suffix, not a refusal over half a JSON document.
        self.assertEqual(
            {"command": "echo partial"},
            result["partial"]["call_a"],
        )
        self.assertEqual(
            {"command": "echo beta"},
            result["partial"]["call_b"],
        )
        self.assertEqual({"0": True, "1": True}, result["partial_order"])


if __name__ == "__main__":
    unittest.main()
