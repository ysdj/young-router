"""The context-window guard shortens a replay instead of letting it be refused.

Regression ground: a real Codex thread grew past the model group's declared
window and the turn died with "ran out of room" *before* any deployment was
selected, so nothing downstream could recover it.  The guard runs earlier and
elides the oldest, largest bodies in place.
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from young_router.proxy import context_window as cw  # noqa: E402


class _Router:
    """Just enough router for ``window_for`` and the exact counter."""

    def __init__(self, window: int) -> None:
        self.window = window

    def get_model_list(self, model_name=None):
        return [
            {"model_info": {"max_input_tokens": self.window}},
            {"model_info": {"max_input_tokens": self.window * 2}},
        ]

    def _count_pre_call_check_tokens(self, messages=None, input=None, request_kwargs=None):
        # ~1 token per 4 characters of serialized input.
        import json

        return int(len(json.dumps(input or [])) / 4)


def _tool_pair(index: int, chars: int) -> list:
    return [
        {
            "type": "function_call",
            "call_id": f"call_{index}",
            "name": "shell",
            "arguments": '{"command":["cat","big.log"]}',
        },
        {
            "type": "function_call_output",
            "call_id": f"call_{index}",
            "output": "x" * chars,
        },
    ]


def _input() -> list:
    items: list = [
        {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "be brief"}]},
    ]
    items.extend(_tool_pair(1, 4000))
    items.extend(_tool_pair(2, 8000))
    items.append(
        {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "what changed?"}],
        }
    )
    return items


class WindowTests(unittest.TestCase):
    def test_the_window_is_the_smallest_declared_sibling(self) -> None:
        # The router refuses when *no* deployment fits, so fitting the largest
        # sibling would still leave the request routable to a smaller one.
        router = _Router(1000)
        self.assertEqual(cw.window_for("m", router), 1000)

    def test_a_request_under_the_window_is_untouched(self) -> None:
        data = {"model": "m", "input": _input()}
        router = _Router(100000)
        count = cw.count_tokens(data, router)
        with mock.patch.object(cw, "_router", return_value=router):
            self.assertIsNone(cw.apply(data))

    def test_an_oversized_replay_keeps_its_shape_but_loses_its_bodies(self) -> None:
        data = {"model": "m", "input": _input()}
        router = _Router(600)
        with mock.patch.object(cw, "_router", return_value=router):
            fitted = cw.apply(data)
        self.assertIsNotNone(fitted)
        after = fitted["input"]
        self.assertEqual(len(after), len(data["input"]))
        # The tool call keeps its output item; the output is now a marker.
        outputs = [item for item in after if item.get("type") == "function_call_output"]
        self.assertEqual(len(outputs), 2)
        for item in outputs:
            text = cw._text_of(item.get("output"))
            self.assertIn("elided", text)
        # The current turn's own message survives verbatim.
        self.assertEqual(after[-1], data["input"][-1])
        # And the result fits the window it is sent against.
        self.assertLessEqual(cw.count_tokens(fitted, router), router.window)

    def test_phase_two_drops_a_marker_and_its_call_together(self) -> None:
        data = {"model": "m", "input": _input()}
        router = _Router(200)
        with mock.patch.object(cw, "_router", return_value=router):
            fitted = cw.apply(data)
        self.assertIsNotNone(fitted)
        after = fitted["input"]
        # Pairs leave as units: no orphan call, no orphan output.
        call_ids = {item.get("call_id") for item in after if item.get("type") == "function_call"}
        output_ids = {item.get("call_id") for item in after if item.get("type") == "function_call_output"}
        self.assertEqual(call_ids, output_ids)
        self.assertEqual(after[-1], data["input"][-1])

    def test_the_current_turn_is_never_elided_even_when_it_alone_is_over(self) -> None:
        last = {
            "type": "message",
            "role": "user",
            "content": [{"type": "input_text", "text": "y" * 9000}],
        }
        data = {"model": "m", "input": _input()[:-1] + [last]}
        router = _Router(1000)
        with mock.patch.object(cw, "_router", return_value=router):
            fitted = cw.apply(data)
        self.assertIsNotNone(fitted)
        self.assertEqual(fitted["input"][-1], last)

    def test_a_message_body_is_replaced_not_duplicated(self) -> None:
        # The list branch of the text replacement once wrote the marker under
        # the content-part's *type* key instead of its ``text`` key, leaving the
        # original body in place and silently doubling the request.
        item = {
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "z" * 5000}],
        }
        updated, replaced = cw._replace_text(item["content"], "MARKER")
        self.assertTrue(replaced)
        self.assertEqual(updated[0]["text"], "MARKER")
        self.assertEqual(updated[0]["type"], "output_text")
        self.assertNotIn("output_text", updated[0])

    def test_the_measured_count_is_published_for_usage_reporting(self) -> None:
        # Codex renders input_tokens as "context used" and compacts against it;
        # a relay that undercounts leaves that gauge near zero while the proxy
        # is already refusing the request.
        data = {"model": "m", "input": _input()}
        router = _Router(600)
        with mock.patch.object(cw, "_router", return_value=router):
            cw._MEASURED_INPUT_TOKENS.set(None)
            cw.apply(data)
        measured = cw.measured_input_tokens()
        self.assertIsNotNone(measured)
        self.assertLessEqual(measured, router.window)

    def test_an_upstream_refusal_shrinks_the_payload_in_place(self) -> None:
        # A relay holds the model to a window it never declared: the router
        # sends the prompt, upstream answers "prompt is too long", and the
        # turn dies.  Shrinking in place lets the same deployment answer it.
        data = {"model": "m", "input": _input()}
        before = cw.count_tokens(data, _Router(10**9))
        cw._MEASURED_INPUT_TOKENS.set(before)
        exc = RuntimeError("prompt is too long: 900000 tokens > 128000 maximum")
        self.assertTrue(cw.retry_for_refusal(data, exc, attempt=0))
        after = cw.count_tokens(data, _Router(10**9))
        self.assertLess(after, before)
        self.assertLessEqual(after, int(before * 0.55) + 1)
        # The current turn still arrives intact.
        self.assertEqual(data["input"][-1]["content"][0]["text"], "what changed?")

    def test_a_non_context_error_is_left_alone(self) -> None:
        data = {"model": "m", "input": _input()}
        cw._MEASURED_INPUT_TOKENS.set(cw.count_tokens(data, _Router(10**9)))
        self.assertFalse(cw.retry_for_refusal(data, RuntimeError("401 invalid api key"), attempt=0))

    def test_the_shrink_ladder_is_bounded(self) -> None:
        data = {"model": "m", "input": _input()}
        cw._MEASURED_INPUT_TOKENS.set(cw.count_tokens(data, _Router(10**9)))
        exc = RuntimeError("prompt is too long")
        self.assertTrue(cw.retry_for_refusal(data, exc, attempt=0))
        self.assertTrue(cw.retry_for_refusal(data, exc, attempt=1))
        self.assertFalse(cw.retry_for_refusal(data, exc, attempt=2))

    def test_a_size_accepted_after_a_shrink_bounds_the_next_turn(self) -> None:
        model = "deepseek-v4.1-flash"
        data = {"model": model, "input": _input()}
        router = _Router(10**9)  # the declared window is never the problem
        cw._ACCEPTED_SIZE.pop(model, None)
        try:
            cw._MEASURED_INPUT_TOKENS.set(cw.count_tokens(data, router))
            with mock.patch.object(cw, "_router", return_value=router):
                cw.record_accepted_size(data)
            self.assertIn(model, cw._ACCEPTED_SIZE)
            # The declared window would have let this through untouched; the
            # learned ceiling is what makes the next turn fit.
            cw._ACCEPTED_SIZE[model] = 200
            with mock.patch.object(cw, "_router", return_value=router):
                fitted = cw.apply({"model": model, "input": _input()})
            self.assertIsNotNone(fitted)
            self.assertLess(cw.count_tokens(fitted, router), cw.count_tokens(data, router))
        finally:
            cw._ACCEPTED_SIZE.pop(model, None)

    def test_a_usage_report_never_claims_less_than_the_measured_request(self) -> None:
        from young_router.proxy import base as base_module

        usage = base_module._responses_usage_for_codex(
            {"input_tokens": 5, "output_tokens": 7, "input_tokens_details": {"cached_tokens": 0}},
            input_token_floor=1200,
        )
        self.assertEqual(usage["input_tokens"], 1200)
        self.assertEqual(usage["total_tokens"], 1207)
        self.assertLessEqual(usage["input_tokens_details"]["cached_tokens"], usage["input_tokens"])


def _tool_image_item(chars: int = 5000) -> dict:
    """A ``view_image`` result: one text part and the picture itself."""

    from test_image_context import _data_url, _png

    return {
        "type": "function_call_output",
        "call_id": "call_view",
        "output": [
            {"type": "input_text", "text": "x" * chars},
            {"type": "input_image", "image_url": _data_url(_png(64, 64, noisy=True))},
        ],
    }


class CurrentTurnTests(unittest.TestCase):
    """The turn being answered arrives intact, images included.

    Real thread with a single user message: 24 ``view_image`` results all belong
    to that one turn.  Replacing them (they are the same turn as the question)
    handed the model a placeholder for the picture it had just asked to see.
    """

    def test_a_tool_result_with_an_image_is_never_elided(self) -> None:
        item = _tool_image_item()
        self.assertIsNone(cw._elide_item(item))

    def test_the_whole_current_turn_is_protected(self) -> None:
        items = [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "be brief"}]},
            {"type": "function_call", "call_id": "old", "name": "shell", "arguments": "{}"},
            {"type": "function_call_output", "call_id": "old", "output": "y" * 9000},
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "look"}]},
            {"type": "function_call", "call_id": "call_view", "name": "view_image", "arguments": "{}"},
            _tool_image_item(),
            {"type": "message", "role": "assistant", "content": [{"type": "output_text", "text": "I see it."}]},
        ]
        data = {"model": "m", "input": items}
        router = _Router(300)
        with mock.patch.object(cw, "_router", return_value=router):
            fitted = cw.apply(data)
        self.assertIsNotNone(fitted)
        assert fitted is not None
        after = fitted["input"]
        # The current turn's own items are untouched, in order.
        self.assertEqual(after[-3:], items[-3:])
        # The earlier tool call paid for it: elided, or dropped as a call and
        # its output together -- never one half of the pair.
        old = [item for item in after if item.get("call_id") == "old"]
        self.assertTrue(
            not old or all("elided" in cw._item_text(item) for item in old)
        )
        # And the current turn's picture is still in the request.
        self.assertEqual(
            cw._inline_image_bytes(after), cw._inline_image_bytes(items)
        )


class ImageWeightTests(unittest.TestCase):
    """The token counter and the relay's byte counter must not disagree.

    Real thread: 93 inline images, 37.5 MB.  The token counter reported 298k of
    a 950k window (31%, so Codex never compacted); the upstream refused the same
    request for its size.  Charging image bytes at the pessimists' rate is what
    keeps the two verdicts from diverging.
    """

    def setUp(self) -> None:
        import tempfile

        from young_router.proxy import image_context as image_context_module
        from young_router.proxy import image_context_store as store_module

        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = mock.patch.dict(
            os.environ,
            {
                image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "0",
                image_context_module.IMAGE_CONTEXT_DATA_DIR_ENV: os.path.join(
                    self.directory.name, "data"
                ),
                image_context_module.IMAGE_CONTEXT_FETCH_ENV: "path",
                image_context_module.IMAGE_CONTEXT_MIN_BYTES_ENV: "1",
                store_module.REPLAY_CACHE_DIR_ENV: os.path.join(
                    self.directory.name, "replays"
                ),
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def _image(self, seed: int) -> dict:
        from test_image_context import _data_url, _png

        return {
            "type": "input_image",
            "image_url": _data_url(_png(64, 64, noisy=True, seed=seed)),
        }

    def _replay(self, images: int) -> dict:
        items: list = [
            {"type": "message", "role": "developer", "content": [{"type": "input_text", "text": "be brief"}]}
        ]
        for index in range(images):
            items.append(
                {
                    "type": "message",
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": f"look at this {index}"},
                        self._image(index + 1),
                    ],
                }
            )
            items.append(
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "output_text", "text": "I see it."}],
                }
            )
        items.append(
            {"type": "message", "role": "user", "content": [{"type": "input_text", "text": "and now?"}]}
        )
        return {
            "call_type": "aresponses",
            "model": "m",
            # A real replay always carries Codex's own identification.
            "headers": {"Originator": "codex_cli_rs"},
            "input": items,
            "metadata": {"thread_id": "11111111-2222-3333-4444-555555555555"},
        }

    def test_image_bytes_count_against_the_window(self) -> None:
        data = self._replay(8)
        router = _Router(10**9)  # the token count alone would never trip
        count = cw.count_tokens(data, router)
        self.assertGreater(cw.pessimistic_tokens(data, count), count)

    def test_images_are_replaced_when_the_bytes_do_not_fit(self) -> None:
        data = self._replay(8)
        router = _Router(10**9)
        image_bytes_before = cw._inline_image_bytes(data["input"])
        # A window the text alone fits and the images do not.
        window = cw.count_tokens(data, router) + 1000
        with mock.patch.object(cw, "_router", return_value=_Router(window)):
            fitted = cw.apply(data)
        self.assertIsNotNone(fitted)
        assert fitted is not None
        self.assertLess(cw._inline_image_bytes(fitted["input"]), image_bytes_before)
        # A placeholder names a path the client's own viewer can open, so the
        # model can still look at the image it was told about.
        text = json.dumps(fitted["input"], ensure_ascii=False)
        self.assertIn("IMG-", text)
        self.assertIn(".png", text)
        # The current turn survives verbatim.
        self.assertEqual(fitted["input"][-1], data["input"][-1])

    def test_an_image_refusal_is_answered_by_replacing_the_images(self) -> None:
        data = self._replay(6)
        cw._MEASURED_INPUT_TOKENS.set(900000)
        before = cw._inline_image_bytes(data["input"])
        exc = RuntimeError(
            "workbuddy upstream failed: prompt is too long (upstream code 11115)"
        )
        self.assertTrue(cw.retry_for_refusal(data, exc, attempt=0))
        self.assertLess(cw._inline_image_bytes(data["input"]), before)


if __name__ == "__main__":
    unittest.main()
