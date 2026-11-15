from __future__ import annotations

from hook_test_utils import *


class ProxyResponsesToolsEdgeTests(HookTestCase):
    def test_incomplete_parallel_tool_group_keeps_collected_outputs(self) -> None:
        hooks, _ = load_hook_module()
        calls = [
            {"type": "function_call", "call_id": call_id, "name": "view_image"}
            for call_id in ("call_first", "call_second")
        ]
        output = {
            "type": "function_call_output",
            "call_id": "call_first",
            "output": "first",
        }
        canonical = [*calls, output]

        rewritten, moved_barriers = hooks._with_contiguous_responses_chat_tool_records(
            canonical
        )

        self.assertEqual(rewritten, canonical)
        self.assertEqual(moved_barriers, 0)


if __name__ == "__main__":
    unittest.main()
