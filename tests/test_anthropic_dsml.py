"""DeepSeek's native markup must not reach an Anthropic client as answer text.

Regression ground: replaying the PIK3CA thread's own body through
``POST /v1/messages`` on 2026-11-16 returned a 200 whose answer was

    \n\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="Bash">\n<｜｜DSML｜｜ parameter

The route had answered with DeepSeek's own tool-call markup, which the
Responses wire already parses (``web_search_native``) and the Anthropic
Messages wire did not: the client was handed XML as prose, so the tool call it
described never ran and the turn stopped.

Two properties matter, and they pull against each other: the markup must not be
visible even for one delta (a client renders a half tag), and a fenced code
example must stay prose (a thread discussing this format must not have its
example executed).
"""

from __future__ import annotations

import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))

from young_router.proxy import anthropic_dsml as dsml  # noqa: E402

#: One call, spelled the way the live route spelled it.
CALL = (
    '\n\n<｜｜DSML｜｜ calls>\n'
    '<｜｜DSML｜｜ invoke name="Bash">\n'
    '<｜｜DSML｜｜ parameter name="command" string="true">sed -n \'1,80p\' /tmp/x</｜｜DSML｜｜ parameter>\n'
    "</｜｜DSML｜｜ invoke>\n"
    "</｜｜DSML｜｜ calls>"
)


class ParseTests(unittest.TestCase):
    def test_a_call_becomes_a_tool_use_block(self) -> None:
        [call] = dsml.parse_tool_calls(CALL)

        self.assertEqual("tool_use", call["type"])
        self.assertEqual("Bash", call["name"])
        self.assertEqual({"command": "sed -n '1,80p' /tmp/x"}, call["input"])
        self.assertTrue(call["id"].startswith("toolu_dsml_"))

    def test_a_json_parameter_is_decoded(self) -> None:
        text = (
            "<｜｜DSML｜｜ calls>"
            '<｜｜DSML｜｜ invoke name="Read">'
            '<｜｜DSML｜｜ parameter name="limit" string="false">50</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>"
            "</｜｜DSML｜｜ calls>"
        )

        [call] = dsml.parse_tool_calls(text)

        self.assertEqual(50, call["input"]["limit"])

    def test_a_parameter_that_is_not_json_stays_text(self) -> None:
        # ``string="false"`` is the model's claim, not a promise; raising here
        # would end a stream the route is still writing.
        text = (
            "<｜｜DSML｜｜ calls>"
            '<｜｜DSML｜｜ invoke name="Bash">'
            '<｜｜DSML｜｜ parameter name="command" string="false">not json</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>"
            "</｜｜DSML｜｜ calls>"
        )

        [call] = dsml.parse_tool_calls(text)

        self.assertEqual("not json", call["input"]["command"])

    def test_several_calls_in_one_block(self) -> None:
        text = (
            "<｜｜DSML｜｜ calls>"
            '<｜｜DSML｜｜ invoke name="Read">'
            '<｜｜DSML｜｜ parameter name="file_path">/a</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>"
            '<｜｜DSML｜｜ invoke name="Read">'
            '<｜｜DSML｜｜ parameter name="file_path">/b</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>"
            "</｜｜DSML｜｜ calls>"
        )

        calls = dsml.parse_tool_calls(text)

        self.assertEqual(["/a", "/b"], [call["input"]["file_path"] for call in calls])

    def test_a_fenced_example_is_not_a_call(self) -> None:
        text = f"Here is the format:\n\n```\n{CALL}\n```\n"

        self.assertEqual([], dsml.parse_tool_calls(text))

    def test_an_inline_code_example_is_not_a_call(self) -> None:
        text = f"Write `{dsml.CALLS_START}` to open a block."

        self.assertEqual([], dsml.parse_tool_calls(text))

    def test_entities_are_unescaped(self) -> None:
        text = (
            "<｜｜DSML｜｜ calls>"
            '<｜｜DSML｜｜ invoke name="Bash">'
            '<｜｜DSML｜｜ parameter name="command">a &amp;&amp; b</｜｜DSML｜｜ parameter>'
            "</｜｜DSML｜｜ invoke>"
            "</｜｜DSML｜｜ calls>"
        )

        [call] = dsml.parse_tool_calls(text)

        self.assertEqual("a && b", call["input"]["command"])


class StripTests(unittest.TestCase):
    def test_the_markup_leaves_no_residue(self) -> None:
        # ``CALL`` carries the blank lines the live route wrote before the
        # block; those are prose and survive, the markup does not.
        cleaned = dsml.strip_tool_calls("Here you go:" + CALL)

        self.assertEqual("Here you go:\n\n", cleaned)
        self.assertNotIn("DSML", cleaned)

    def test_an_unterminated_block_is_still_removed(self) -> None:
        cleaned = dsml.strip_tool_calls("text\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name=")

        self.assertEqual("text\n", cleaned)

    def test_a_fenced_example_survives(self) -> None:
        text = f"```\n{CALL}\n```"

        self.assertEqual(text, dsml.strip_tool_calls(text))


class StreamFilterTests(unittest.TestCase):
    """The filter sees deltas, not messages: a tag can be split anywhere."""

    def _feed(self, text: str, step: int) -> tuple[str, list]:
        stream = dsml.MessageStreamFilter()
        visible = ""
        for index in range(0, len(text), step):
            visible += stream.feed(text[index : index + step])
        visible += stream.drain()
        return visible, stream.take_calls()

    def test_no_delta_ever_shows_a_partial_tag(self) -> None:
        # Every split point, because a route chooses them and not this proxy.
        # ``CALL`` opens with the blank lines the live route wrote before the
        # block; they are prose and stay, which is what the assertion keeps.
        for step in range(1, 24):
            with self.subTest(step=step):
                visible, calls = self._feed("Answer:" + CALL, step)
                self.assertEqual("Answer:\n\n", visible)
                self.assertEqual(1, len(calls))
                self.assertEqual("Bash", calls[0]["name"])

    def test_no_partial_tag_leaks_for_any_split_inside_the_markup(self) -> None:
        marker = dsml.CALLS_START
        for step in range(1, len(marker) + 1):
            with self.subTest(step=step):
                visible, _calls = self._feed("text\n" + CALL, step)
                self.assertNotIn("DSML", visible)
                self.assertNotIn("｜", visible)

    def test_text_around_the_call_survives(self) -> None:
        visible, calls = self._feed("before\n" + CALL + "\nafter", 5)

        # ``CALL`` opens with ``\n\n`` and the block's own trailing newline is
        # followed by the text's: the call leaves blank lines where it stood.
        self.assertEqual("before\n\n\n\nafter", visible)
        self.assertEqual(1, len(calls))

    def test_a_message_without_markup_is_passed_through(self) -> None:
        stream = dsml.MessageStreamFilter()
        text = "The paper reports a hazard ratio of 1.42."

        self.assertEqual(text, stream.feed(text))
        self.assertEqual("", stream.drain())
        self.assertEqual([], stream.take_calls())

    def test_a_fenced_example_streams_as_text(self) -> None:
        text = f"```\n{CALL}\n```"
        for step in (1, 2, 5, len(text)):
            with self.subTest(step=step):
                stream = dsml.MessageStreamFilter()
                visible = ""
                for index in range(0, len(text), step):
                    visible += stream.feed(text[index : index + step])
                visible += stream.drain()
                self.assertEqual(text, visible)
                self.assertEqual([], stream.take_calls())

    def test_an_unterminated_call_is_parsed_at_the_end(self) -> None:
        stream = dsml.MessageStreamFilter()
        stream.feed('<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="Bash">\n'
                    '<｜｜DSML｜｜ parameter name="command">ls</｜｜DSML｜｜ parameter>\n'
                    "</｜｜DSML｜｜ invoke>")
        visible = stream.drain()
        calls = stream.take_calls()

        self.assertEqual("", visible)
        self.assertEqual("Bash", calls[0]["name"])


class HasToolCallsTests(unittest.TestCase):
    def test_detects_a_call(self) -> None:
        self.assertTrue(dsml.has_tool_calls("x" + CALL))

    def test_a_plain_answer_has_none(self) -> None:
        self.assertFalse(dsml.has_tool_calls("the hazard ratio was 1.42"))

    def test_a_fenced_example_has_none(self) -> None:
        # ``has_tool_calls`` is the cheap pre-check; the fence rule lives in
        # ``parse_tool_calls`` and ``strip_tool_calls``, which are what act.
        self.assertTrue(dsml.has_tool_calls(f"```\n{CALL}\n```"))
        self.assertEqual([], dsml.parse_tool_calls(f"```\n{CALL}\n```"))


if __name__ == "__main__":
    unittest.main()


class OrphanTagTests(unittest.TestCase):
    """The shape the live route actually leaked.

    Session ``454f73ba``, one assistant message, 2026-11-16:

        <thinking> </thinking>
        |
        </｜｜DSML｜｜ calls>

    A closing tag with no block around it.  Everything keyed on the *opening*
    marker passes it straight through, which is why the first draft of this
    filter did: it held blocks and left the dangling closer alone.
    """

    LEAK = "<thinking> </thinking>\n\n</｜｜DSML｜｜ calls>"

    def test_a_dangling_closer_is_stripped(self) -> None:
        self.assertEqual("<thinking> </thinking>\n\n", dsml.strip_tool_calls(self.LEAK))

    def test_a_dangling_closer_never_reaches_the_stream(self) -> None:
        # Every split point: the closer is recognised from its first ``<``.
        for step in range(1, len(self.LEAK) + 1):
            with self.subTest(step=step):
                stream = dsml.MessageStreamFilter()
                visible = ""
                for index in range(0, len(self.LEAK), step):
                    visible += stream.feed(self.LEAK[index : index + step])
                visible += stream.drain()
                self.assertEqual("<thinking> </thinking>\n\n", visible)
                self.assertEqual([], stream.take_calls())

    def test_a_dangling_invoke_and_parameter_closer_are_stripped_too(self) -> None:
        text = (
            "answer\n"
            "</｜｜DSML｜｜ invoke>"
            "</｜｜DSML｜｜ parameter>"
        )

        self.assertEqual("answer\n", dsml.strip_tool_calls(text))

    def test_ordinary_angle_brackets_are_not_tags(self) -> None:
        text = "The HR was < 1 and p > 0.05, so <b>not</b> significant."

        self.assertEqual(text, dsml.strip_tool_calls(text))
        stream = dsml.MessageStreamFilter()
        self.assertEqual(text, stream.feed(text) + stream.drain())
