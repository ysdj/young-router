from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace

from young_router.proxy import patches, responses_execution


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
        # The block's own delta is the stock iterator's: restating it here sent
        # the first fragment twice (live 2026-11-04, "WeWe need answer").
        self.assertNotIn("response.reasoning_summary_text.delta", types)
        # The fragment itself is still kept for the item the block completes
        # with, which is what the client stores alongside the live deltas.
        self.assertEqual(
            "step one", iterator._young_router_reasoning_summary_text["r1"]
        )

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
            "response.reasoning_summary_part.added",
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



    def test_content_part_done_without_a_streamed_summary_sends_nothing(self) -> None:
        texts: dict[str, str] = {}
        seen: set[str] = set()
        done: set[str] = set()
        chunk = _event(
            {
                "type": "response.content_part.done",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "reasoning_text", "text": "step one"},
            }
        )

        events = patches.reasoning_summary_chunks_for_delivery(
            chunk, texts, seen, done
        )

        self.assertEqual([], events)
        self.assertEqual({}, texts)
        self.assertNotIn("r1", seen)
        self.assertNotIn("r1", done)

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


    def test_raw_reasoning_deltas_stream_as_summary_text(self) -> None:
        texts: dict[str, str] = {}
        seen: set[str] = set()
        done: set[str] = set()
        request_data = {"reasoning": {"effort": "high", "summary": "auto"}}
        first = _event(
            {
                "type": "response.reasoning_text.delta",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "delta": "We",
            }
        )
        second = _event(
            {
                "type": "response.reasoning_text.delta",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "delta": " need",
            }
        )

        first_events = patches.reasoning_summary_chunks_for_delivery(
            first, texts, seen, done, request_data
        )
        self.assertEqual(
            [
                "response.reasoning_summary_part.added",
                "response.reasoning_summary_text.delta",
            ],
            [event["type"] for event in first_events],
        )
        self.assertEqual("We", first_events[-1]["delta"])
        self.assertTrue(
            patches.reasoning_summary_suppresses_chunk(first, request_data)
        )

        second_events = patches.reasoning_summary_chunks_for_delivery(
            second, texts, seen, done, request_data
        )
        self.assertEqual(
            ["response.reasoning_summary_text.delta"],
            [event["type"] for event in second_events],
        )
        self.assertEqual("We need", texts["r1"])

        final = _event(
            {
                "type": "response.content_part.done",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "reasoning_text", "text": "We need"},
            }
        )
        final_events = patches.reasoning_summary_chunks_for_delivery(
            final, texts, seen, done, request_data
        )
        self.assertEqual(
            [
                "response.reasoning_summary_text.done",
                "response.reasoning_summary_part.done",
            ],
            [event["type"] for event in final_events],
        )

        self.assertEqual(
            [],
            patches.reasoning_summary_chunks_for_delivery(
                first, {}, set(), set(), {}
            ),
        )
        self.assertFalse(patches.reasoning_summary_suppresses_chunk(first, {}))

    def test_a_real_summary_delta_wins_over_a_raw_reasoning_delta(self) -> None:
        raw = _event(
            {
                "type": "response.reasoning_text.delta",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "delta": "raw",
            }
        )
        texts: dict[str, str] = {}
        seen = {"r1"}
        self.assertEqual(
            [],
            patches.reasoning_summary_chunks_for_delivery(
                raw,
                texts,
                seen,
                set(),
                {"reasoning": {"effort": "high", "summary": "auto"}},
            ),
        )


    def test_a_streamed_summary_owns_its_terminal_frames(self) -> None:
        texts: dict[str, str] = {}
        seen = {"r1"}
        done: set[str] = set()
        final = _event(
            {
                "type": "response.content_part.done",
                "item_id": "r1",
                "output_index": 0,
                "content_index": 0,
                "part": {"type": "reasoning_text", "text": "the upstream summary"},
            }
        )
        self.assertEqual(
            [],
            patches.reasoning_summary_chunks_for_delivery(
                final, texts, seen, done
            ),
        )
        self.assertEqual({}, texts)

if __name__ == "__main__":
    unittest.main()


class ChatBridgeReasoningSummaryTests(unittest.IsolatedAsyncioTestCase):
    """The Responses chat bridge streams a thinking route's reasoning_content."""

    async def test_reasoning_content_becomes_summary_frames_before_the_answer(self) -> None:
        class ChatStream:
            def __init__(self) -> None:
                self.chunks = [
                    {"choices": [{"delta": {"reasoning_content": "step one "}}]},
                    {"choices": [{"delta": {"reasoning_content": "step two"}}]},
                    {"choices": [{"delta": {"content": "answer"}}]},
                ]

            def __aiter__(self):
                return self

            async def __anext__(self):
                if not self.chunks:
                    raise StopAsyncIteration
                return self.chunks.pop(0)

        events = [
            event
            async for event in responses_execution._responses_chat_bridge_text_stream_from_chat_stream(
                ChatStream(),
                {
                    "model": "deepseek-v4.1-flash",
                    "reasoning": {"effort": "max", "summary": "auto"},
                },
            )
        ]
        types = [event["type"] for event in events]
        self.assertIn("response.reasoning_summary_part.added", types)
        deltas = [
            event["delta"]
            for event in events
            if event["type"] == "response.reasoning_summary_text.delta"
        ]
        self.assertEqual(["step one ", "step two"], deltas)
        reasoning_done = next(
            event
            for event in events
            if event["type"] == "response.output_item.done"
            and event["item"].get("type") == "reasoning"
        )
        self.assertEqual(
            [{"type": "summary_text", "text": "step one step two"}],
            reasoning_done["item"]["summary"],
        )
        answer = next(
            event for event in events if event["type"] == "response.output_text.delta"
        )
        self.assertLess(
            types.index("response.reasoning_summary_text.delta"),
            types.index("response.output_text.delta"),
        )
        self.assertEqual("answer", answer["delta"])


def _json_event_payload(event: object) -> dict:
    """One iterator frame as a plain dict, whatever shape it was handed over."""

    if isinstance(event, str):
        return json.loads(event)
    dumped = getattr(event, "model_dump", None)
    if callable(dumped):
        return dumped()
    return dict(event)


class ReasoningPhaseStreamTests(unittest.TestCase):
    """A Chat stream keeps its thinking block open until the answer begins.

    The observed failure (live 2026-11-04, a `deepseek-v4.1-flash` turn on the
    WorkBuddy route): the model streamed its thinking, wrote a blank line, went
    back to thinking and only then answered.  LiteLLM closed the reasoning item
    on the blank line, so the second half of the thinking arrived as summary
    deltas for an item the client had already finished -- and Codex rendered
    them as the body of the answer, which then read as English planning
    followed by the Chinese conclusion.
    """

    def _iterator(self, chunks: list[tuple[dict, str]]) -> object:
        try:
            from litellm.responses.litellm_completion_transformation.streaming_iterator import (
                LiteLLMCompletionStreamingIterator,
            )
            from litellm.types.utils import ModelResponseStream
        except Exception:  # pragma: no cover - LiteLLM is a data-plane dependency
            self.skipTest("LiteLLM's Chat-to-Responses iterator is unavailable")

        patches._install_responses_completion_stream_patch()

        class _Wrapper:
            logging_obj = None

            def __init__(self, items: list[object]) -> None:
                self._items = list(items)
                self._index = 0

            def __aiter__(self) -> "_Wrapper":
                return self

            async def __anext__(self) -> object:
                if self._index >= len(self._items):
                    raise StopAsyncIteration
                item = self._items[self._index]
                self._index += 1
                return item

        stream_chunks = [
            ModelResponseStream(
                id="chatcmpl-test",
                created=1,
                model="deepseek-v4.1-flash",
                choices=[
                    {
                        "index": 0,
                        "delta": delta,
                        "finish_reason": finish_reason,
                    }
                ],
            )
            for delta, finish_reason in chunks
        ]
        return LiteLLMCompletionStreamingIterator(
            model="deepseek-v4.1-flash",
            litellm_custom_stream_wrapper=_Wrapper(stream_chunks),
            request_input=[],
            responses_api_request={"model": "deepseek-v4.1-flash", "tools": []},
        )

    def _drain(self, iterator: object) -> list[dict]:
        events: list[dict] = []
        loop = asyncio.new_event_loop()
        try:
            while True:
                try:
                    event = loop.run_until_complete(iterator.__anext__())
                except StopAsyncIteration:
                    break
                events.append(_json_event_payload(event))
        finally:
            loop.close()
        return events

    def test_a_blank_answer_line_does_not_close_the_thinking(self) -> None:
        iterator = self._iterator(
            [
                ({"reasoning_content": "think A "}, ""),
                ({"content": " "}, ""),
                ({"reasoning_content": "think B "}, ""),
                ({"content": "answer"}, ""),
                ({}, "stop"),
            ]
        )
        events = self._drain(iterator)
        types = [event["type"] for event in events]

        reasoning_done_index = next(
            index
            for index, event in enumerate(events)
            if event["type"] == "response.output_item.done"
            and (event.get("item") or {}).get("type") == "reasoning"
        )
        # Not one late summary delta after the block closed: that is the frame
        # the client appends under the answer.
        self.assertNotIn(
            "response.reasoning_summary_text.delta",
            types[reasoning_done_index:],
        )
        summary = (events[reasoning_done_index].get("item") or {}).get("summary")
        self.assertEqual(
            [{"type": "summary_text", "text": "think A think B "}], summary
        )
        self.assertEqual(
            "answer",
            "".join(
                event["delta"]
                for event in events
                if event["type"] == "response.output_text.delta"
            ),
        )

    def test_thinking_that_resumes_after_the_answer_began_is_not_delivered(self) -> None:
        iterator = self._iterator(
            [
                ({"reasoning_content": "think A "}, ""),
                ({"content": "partial"}, ""),
                ({"reasoning_content": "late plan"}, ""),
                ({"content": "answer"}, ""),
                ({}, "stop"),
            ]
        )
        events = self._drain(iterator)
        self.assertEqual(
            ["partial", "answer"],
            [
                event["delta"]
                for event in events
                if event["type"] == "response.output_text.delta"
            ],
        )
        self.assertNotIn(
            "late plan",
            "".join(
                str(event.get("delta") or "")
                for event in events
                if "reasoning_summary_text" in str(event.get("type"))
            ),
        )

    def test_the_first_thinking_fragment_is_delivered_once(self) -> None:
        iterator = self._iterator(
            [
                ({"reasoning_content": "We"}, ""),
                ({"reasoning_content": " need"}, ""),
                ({"content": "answer"}, ""),
                ({}, "stop"),
            ]
        )
        events = self._drain(iterator)
        self.assertEqual(
            ["We", " need"],
            [
                event["delta"]
                for event in events
                if event["type"] == "response.reasoning_summary_text.delta"
            ],
        )
