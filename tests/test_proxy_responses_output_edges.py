from __future__ import annotations

from hook_test_utils import *


class ResponsesOutputEdgeTests(HookTestCase):
    def test_released_search_stays_ahead_of_reasoning_text_frames(self):
        hooks, _ = load_hook_module()
        pending = [
            {"type": "response.output_item.added", "item": {"type": "reasoning"}},
            {"type": "response.reasoning_text.delta", "delta": "thinking"},
            {"type": "response.output_text.delta", "delta": "answer"},
        ]
        self.assertEqual(hooks._first_answer_frame_index(pending), 2)

    def test_custom_tool_input_deltas_accumulate_across_queue_patches(self):
        hooks, _ = load_hook_module()

        class Iterator:
            request_input = []
            responses_api_request = {
                "tools": [
                    {"type": "function", "name": "apply_patch", "parameters": {"type": "object"}, hooks._RESPONSES_BRIDGE_CUSTOM_TOOL_KEY: True}
                ]
            }

            def __init__(self):
                self._pending_tool_events = [
                    {"type": "response.output_item.added", "item": {"type": "function_call", "id": "fc_patch", "name": "apply_patch", "arguments": ""}},
                    {"type": "response.function_call_arguments.delta", "item_id": "fc_patch", "delta": '{"input":"*** Begin'},
                ]

        iterator = Iterator()
        hooks._normalize_pending_tool_search_events(iterator)
        iterator._pending_tool_events.append({"type": "response.function_call_arguments.delta", "item_id": "fc_patch", "delta": ' Patch\\n*** End Patch"}'})
        hooks._normalize_pending_tool_search_events(iterator)

        events = [hooks._jsonable(event) for event in iterator._pending_tool_events]
        self.assertEqual(events[-1]["type"], "response.custom_tool_call_input.delta")
        self.assertEqual(events[-1]["delta"], " Patch\n*** End Patch")
