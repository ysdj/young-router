from __future__ import annotations

import unittest
from types import SimpleNamespace

from young_router.proxy import patches


def _event(payload: dict) -> object:
    return patches._streaming_module._json_stream_event(payload)


def _reasoning_item(item_id: str = "r1", **extra: object) -> dict:
    item = {"type": "reasoning", "id": item_id, "status": "in_progress", "summary": []}
    item.update(extra)
    return item


class ReasoningSummaryStreamTests(unittest.TestCase):
    """A Chat delta carrying reasoning_content is rendered from the summary."""

    def test_reasoning_content_is_streamed_as_summary_text(self) -> None:
        iterator = SimpleNamespace()
        pending = [
            _event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": _reasoning_item(),
                }
            ),
            _event(
                {
                    "type": "response.content_part.added",
                    "item_id": "r1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "reasoning_text", "text": ""},
                }
            ),
        ]

        patches._open_reasoning_summary_items(
            iterator,
            pending,
            0,
            {"choices": [{"delta": {"reasoning_content": "step one"}}]},
        )

        types = [event["type"] for event in pending]
        self.assertIn("response.reasoning_summary_part.added", types)
        self.assertIn("response.reasoning_summary_text.delta", types)
        delta = next(
            event
            for event in pending
            if event["type"] == "response.reasoning_summary_text.delta"
        )
        self.assertEqual("step one", delta["delta"])
        self.assertEqual(0, delta["summary_index"])

    def test_the_final_content_part_closes_the_summary_and_fills_the_item(self) -> None:
        iterator = SimpleNamespace()
        first = [
            _event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": _reasoning_item(),
                }
            )
        ]
        patches._open_reasoning_summary_items(
            iterator,
            first,
            0,
            {"choices": [{"delta": {"reasoning_content": "step one"}}]},
        )

        final = [
            _event(
                {
                    "type": "response.content_part.done",
                    "item_id": "r1",
                    "output_index": 0,
                    "content_index": 0,
                    "part": {"type": "reasoning_text", "text": "step one step two"},
                }
            ),
            _event(
                {
                    "type": "response.output_item.done",
                    "output_index": 0,
                    "item": _reasoning_item(status="completed"),
                }
            ),
        ]
        patches._open_reasoning_summary_items(
            iterator,
            final,
            0,
            {"choices": [{"delta": {"reasoning_content": " step two"}}]},
        )

        types = [event["type"] for event in final]
        self.assertIn("response.reasoning_summary_text.done", types)
        done = next(
            event
            for event in final
            if event["type"] == "response.reasoning_summary_text.done"
        )
        self.assertEqual("step one step two", done["text"])
        item_done = next(
            event for event in final if event["type"] == "response.output_item.done"
        )
        self.assertEqual(
            [{"type": "summary_text", "text": "step one step two"}],
            item_done["item"]["summary"],
        )

    def test_a_dict_item_still_gets_the_summary_frames(self) -> None:
        iterator = SimpleNamespace()
        pending = [
            _event(
                {
                    "type": "response.output_item.added",
                    "output_index": 0,
                    "item": {"type": "reasoning", "id": "r1", "status": "in_progress"},
                }
            )
        ]

        patches._open_reasoning_summary_items(
            iterator,
            pending,
            0,
            {"choices": [{"delta": {"reasoning_content": "x"}}]},
        )

        self.assertIn(
            "response.reasoning_summary_text.delta",
            [event["type"] for event in pending],
        )

    def test_completed_response_keeps_the_summary_on_the_reasoning_item(self) -> None:
        iterator = SimpleNamespace(
            _young_router_reasoning_summary_text={"r1": "the whole thought"}
        )
        response = {
            "output": [
                {"type": "reasoning", "id": "r1", "summary": [], "status": "completed"},
                {"type": "message", "id": "m1", "status": "completed"},
            ]
        }

        patches._apply_reasoning_summary_to_response(iterator, response)

        self.assertEqual(
            [{"type": "summary_text", "text": "the whole thought"}],
            response["output"][0]["summary"],
        )
        self.assertEqual([], response["output"][1].get("summary", []))



    def test_content_part_done_is_mirrored_into_summary_frames(self) -> None:
        texts: dict[str, str] = {}
        seen: set[str] = set()
        chunk = _event(
            {
                "type": "response.content_part.done",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "reasoning_text", "text": "step one"},
            }
        )

        events = patches.reasoning_summary_chunks_for_delivery(chunk, texts, seen)

        self.assertEqual(
            [
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_text.delta",
                "response.reasoning_summary_text.done",
                "response.reasoning_summary_part.done",
            ],
            [event["type"] for event in events],
        )
        delta = next(
            event for event in events if event["type"].endswith(".delta")
        )
        self.assertEqual("step one", delta["delta"])
        self.assertEqual("step one", texts["r1"])
        self.assertIn("r1", seen)
        self.assertEqual([], patches.reasoning_summary_chunks_for_delivery(chunk, texts, seen))

    def test_output_item_done_and_completed_carry_the_summary(self) -> None:
        texts = {"r1": "the whole thought"}
        done_item = _event(
            {
                "type": "response.output_item.done",
                "item": {"type": "reasoning", "id": "r1", "status": "completed", "summary": []},
            }
        )
        patches.reasoning_summary_chunks_for_delivery(done_item, texts, set())
        self.assertEqual(
            [{"type": "summary_text", "text": "the whole thought"}],
            done_item["item"]["summary"],
        )

        response = {
            "output": [{"type": "reasoning", "id": "r1", "summary": [], "status": "completed"}]
        }
        completed = _event({"type": "response.completed", "response": response})
        patches.reasoning_summary_chunks_for_delivery(completed, texts, set())
        self.assertEqual(
            [{"type": "summary_text", "text": "the whole thought"}],
            response["output"][0]["summary"],
        )

if __name__ == "__main__":
    unittest.main()
