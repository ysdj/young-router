"""A picture inside a tool result costs 300x more than the same picture beside it.

Regression ground: a Claude Code thread read one paper's figures through
``Read``, which answers with an ``image`` block *inside* a ``tool_result``.  The
Chat bridge serialises that block as a data URL, and the upstream tokenizes the
base64 as prose -- 327,716 prompt tokens for one 473 KB JPEG, and the model
reported the image had not rendered.  The same JPEG as an ordinary ``image``
part cost 1,053 tokens and the model described the four panels correctly.

Five figures were enough to put a 300k-token replay over a 1M-token window, so
the context pass elided history on every step and the model answered by reading
the same file again: 100 offset reads for 24.9 KB of unique text.

The pass moves the picture into the turn that carries the result and changes
nothing else -- same turn, same ``tool_use_id``, result still first.
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from young_router.proxy import tool_result_images as tri  # noqa: E402


def _image(data: str = "QUJD") -> dict:
    return {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/jpeg", "data": data},
    }


def _call(tool_id: str = "call_1") -> dict:
    return {
        "role": "assistant",
        "content": [
            {"type": "tool_use", "id": tool_id, "name": "Read", "input": {"file_path": "/tmp/a.jpg"}}
        ],
    }


def _result(tool_id: str = "call_1", content=None) -> dict:
    return {
        "role": "user",
        "content": [
            {
                "type": "tool_result",
                "tool_use_id": tool_id,
                "content": content if content is not None else [_image()],
            }
        ],
    }


class PromotionShapeTests(unittest.TestCase):
    def test_a_picture_leaves_the_result_and_joins_its_turn(self) -> None:
        body = {"model": "m", "messages": [_call(), _result()]}

        out = tri.promoted_tool_result_images(body)

        self.assertIsNotNone(out)
        turn = out["messages"][1]
        kinds = [block["type"] for block in turn["content"]]
        self.assertEqual(["tool_result", "text", "image"], kinds)
        # The result still answers the same call, and still opens the turn.
        self.assertEqual("call_1", turn["content"][0]["tool_use_id"])
        # The picture itself is the same bytes.
        self.assertEqual(_image()["source"], turn["content"][2]["source"])

    def test_the_result_says_what_left_it_without_instructing(self) -> None:
        body = {"model": "m", "messages": [_call(), _result()]}

        out = tri.promoted_tool_result_images(body)

        note = out["messages"][1]["content"][0]["content"][-1]
        self.assertEqual("text", note["type"])
        self.assertIn("1 image", note["text"])
        for instruction in ("re-read", "again", "ask the user", "retry"):
            self.assertNotIn(instruction, note["text"].lower())

    def test_text_the_tool_wrote_stays_in_front_of_the_note(self) -> None:
        body = {
            "model": "m",
            "messages": [
                _call(),
                _result(content=[{"type": "text", "text": "figure 3"}, _image()]),
            ],
        }

        out = tri.promoted_tool_result_images(body)

        kept = out["messages"][1]["content"][0]["content"]
        self.assertEqual("figure 3", kept[0]["text"])
        self.assertIn("1 image", kept[-1]["text"])

    def test_several_results_in_one_turn_keep_their_order(self) -> None:
        body = {
            "model": "m",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "tool_use", "id": "a", "name": "Read", "input": {}},
                        {"type": "tool_use", "id": "b", "name": "Read", "input": {}},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "a", "content": [_image("QQ==")]},
                        {"type": "tool_result", "tool_use_id": "b", "content": [_image("Qg==")]},
                    ],
                },
            ],
        }

        out = tri.promoted_tool_result_images(body)

        turn = out["messages"][1]["content"]
        self.assertEqual(
            ["tool_result", "tool_result", "text", "image", "image"],
            [block["type"] for block in turn],
        )
        self.assertEqual(["a", "b"], [turn[0]["tool_use_id"], turn[1]["tool_use_id"]])

    def test_a_picture_already_beside_the_result_is_left_alone(self) -> None:
        body = {
            "model": "m",
            "messages": [
                _call(),
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "done"},
                        _image(),
                    ],
                },
            ],
        }

        self.assertIsNone(tri.promoted_tool_result_images(body))

    def test_a_result_with_only_text_is_left_alone(self) -> None:
        body = {"model": "m", "messages": [_call(), _result(content="all text")]}

        self.assertIsNone(tri.promoted_tool_result_images(body))

    def test_a_body_without_messages_is_left_alone(self) -> None:
        self.assertIsNone(tri.promoted_tool_result_images({"input": []}))
        self.assertIsNone(tri.promoted_tool_result_images({}))
        self.assertIsNone(tri.promoted_tool_result_images(None))

    def test_running_twice_changes_nothing_the_second_time(self) -> None:
        body = {"model": "m", "messages": [_call(), _result()]}

        once = tri.promoted_tool_result_images(body)

        self.assertIsNotNone(once)
        self.assertIsNone(tri.promoted_tool_result_images(once))

    def test_the_original_body_is_not_mutated(self) -> None:
        body = {"model": "m", "messages": [_call(), _result()]}
        before = json.dumps(body, sort_keys=True)

        tri.promoted_tool_result_images(body)

        self.assertEqual(before, json.dumps(body, sort_keys=True))

    def test_a_picture_inside_a_result_is_priced_at_the_bytes_the_upstream_reads(self) -> None:
        """The upstream tokenizes the data URL, so that is what it costs.

        Measured on the live relay: one 473 KB JPEG inside a result was billed
        at 327,716 prompt tokens, against 1,053 for the same bytes as an
        ordinary image part.  A pixel price there would understate the body by
        two orders of magnitude, and the window guard would let through a
        request the upstream refuses.
        """

        from young_router.proxy import context_window as cw

        # A real JPEG header (1999x1589, the thread's own figure) plus filler:
        # the pixel price is readable, so the two shapes are priced from the
        # same bytes and only the shape differs.
        import base64
        import struct

        sof = (
            b"\xff\xc0"
            + struct.pack(">H", 17)
            + b"\x08"
            + struct.pack(">HH", 1589, 1999)
            + b"\x03"
            + b"\x01\x11\x00\x02\x11\x01\x03\x11\x01"
        )
        raw = (
            b"\xff\xd8"
            + b"\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
            + sof
            + b"\x00" * 300_000
            + b"\xff\xd9"
        )
        body = base64.b64encode(raw).decode("ascii")
        nested = {"model": "m", "messages": [_call(), _result(content=[_image(body)])]}
        beside = {
            "model": "m",
            "messages": [
                _call(),
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": "call_1", "content": "done"},
                        _image(body),
                    ],
                },
            ],
        }

        nested_price = cw._messages_wire_image_tokens(nested["messages"])
        beside_price = cw._messages_wire_image_tokens(beside["messages"])

        # Nested: the base64 is prose to the upstream, one token per character.
        self.assertGreaterEqual(nested_price, len(body))
        # Beside: the same bytes priced as a picture -- 1999x1589 at the
        # window's own pixel rate.
        self.assertGreater(beside_price, 4_000)
        self.assertLess(beside_price, 5_000)
        self.assertGreater(nested_price / max(beside_price, 1), 50)


if __name__ == "__main__":
    unittest.main()
