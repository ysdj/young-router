from __future__ import annotations

from hook_test_utils import *


class ProxyWebSearchNativeEdgeTests(HookTestCase):
    def test_unconvertible_hosted_search_item_is_dropped_from_response(self) -> None:
        hooks, _ = load_hook_module()
        hosted = {"id": "or_search_1", "type": "openrouter:web_search", "status": "completed"}
        response = {
            "id": "resp_hosted",
            "object": "response",
            "status": "completed",
            "output": [
                hosted,
                {"id": "msg_1", "type": "message", "status": "completed",
                 "role": "assistant",
                 "content": [{"type": "output_text", "text": "Done.", "annotations": []}]},
            ],
        }
        sanitized = hooks._sanitize_response_web_search_call_items(response)
        self.assertEqual(["message"], [item["type"] for item in sanitized["output"]])
        chunk = {"type": "response.output_item.done", "output_index": 0, "item": hosted}
        self.assertIsNone(hooks._sanitize_web_search_stream_chunk(chunk))

    async def test_bridge_stream_numbers_every_event_from_one_counter(self) -> None:
        hooks, _ = load_hook_module()
        response = {
            "id": "resp_bridge",
            "object": "response",
            "status": "completed",
            "output": [
                {"id": "ws_1", "type": "web_search_call", "status": "completed",
                 "action": {"type": "search", "query": "latest Python"}},
                {"id": "msg_1", "type": "message", "status": "completed",
                 "role": "assistant",
                 "content": [{"type": "output_text", "text": "Python 3.14.", "annotations": []}]},
            ],
        }
        events = [
            hooks._jsonable(chunk)
            async for chunk in hooks._external_web_search_bridge_stream(response)
        ]
        self.assertEqual(
            list(range(1, len(events) + 1)),
            [event.get("sequence_number") for event in events],
        )
        self.assertIn("response.web_search_call.completed", [e["type"] for e in events])
