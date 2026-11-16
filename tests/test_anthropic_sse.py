from __future__ import annotations

import json
import re
import unittest

from young_router.proxy import anthropic_sse as sse

CALL = (
    "<\uff5c\uff5cDSML\uff5c\uff5c calls>\n"
    '<\uff5c\uff5cDSML\uff5c\uff5c invoke name="Bash">\n'
    '<\uff5c\uff5cDSML\uff5c\uff5c parameter name="command" string="true">echo hi'
    "</\uff5c\uff5cDSML\uff5c\uff5c parameter>\n"
    "</\uff5c\uff5cDSML\uff5c\uff5c invoke>\n"
    "</\uff5c\uff5cDSML\uff5c\uff5c calls>"
)


def event(payload: dict) -> bytes:
    return (
        "event: "
        + payload["type"]
        + "\ndata: "
        + json.dumps(payload, ensure_ascii=False)
        + "\n\n"
    ).encode()


def frames(**overrides):
    text = overrides.get("text", CALL)
    stop = overrides.get("stop_reason", "end_turn")
    return [
        event({"type": "message_start", "message": {"id": "m", "role": "assistant", "content": []}}),
        event({"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}}),
        event({"type": "content_block_delta", "index": 0, "delta": {"type": "text_delta", "text": text}}),
        event({"type": "content_block_stop", "index": 0}),
        event({"type": "message_delta", "delta": {"stop_reason": stop}}),
        event({"type": "message_stop"}),
    ]


def run(body: bytes, chunk_size: int | None = None) -> bytes:
    rewriter = sse.MessagesSseRewriter()
    out = b""
    if chunk_size is None:
        out += b"".join(rewriter.feed(body.decode()))
    else:
        text = body.decode()
        for index in range(0, len(text), chunk_size):
            out += b"".join(rewriter.feed(text[index : index + chunk_size]))
    out += b"".join(rewriter.finish())
    return out


def run_with_lead(body: bytes, blocks: list[dict]) -> bytes:
    rewriter = sse.MessagesSseRewriter(lead_blocks=blocks)
    out = b"".join(rewriter.feed(body.decode()))
    out += b"".join(rewriter.finish())
    return out


def _without_ids(body: bytes) -> bytes:
    return re.sub(rb"toolu_dsml_[0-9]+_[0-9]+", b"toolu_dsml_ID", body)


def parsed(body: bytes) -> list[dict]:
    events = []
    for block in body.decode().split("\n\n"):
        for line in block.splitlines():
            if line.startswith("data:"):
                try:
                    events.append(json.loads(line[5:].strip()))
                except Exception:
                    pass
    return events


class RewriteTests(unittest.TestCase):
    def test_markup_becomes_a_tool_use_block(self) -> None:
        out = run(b"".join(frames()))

        events = parsed(out)
        self.assertNotIn("DSML", out.decode())
        tool_starts = [
            e
            for e in events
            if e.get("type") == "content_block_start"
            and (e.get("content_block") or {}).get("type") == "tool_use"
        ]
        self.assertEqual(1, len(tool_starts))
        self.assertEqual("Bash", tool_starts[0]["content_block"]["name"])
        self.assertEqual(900, tool_starts[0]["index"])
        inputs = "".join(
            e["delta"]["partial_json"]
            for e in events
            if e.get("type") == "content_block_delta"
            and (e.get("delta") or {}).get("type") == "input_json_delta"
        )
        self.assertEqual({"command": "echo hi"}, json.loads(inputs))

    def test_the_stop_reason_names_the_call(self) -> None:
        events = parsed(run(b"".join(frames())))

        tail = [e for e in events if e.get("type") == "message_delta"][-1]
        self.assertEqual("tool_use", tail["delta"]["stop_reason"])

    def test_the_text_block_is_closed_before_the_call_opens(self) -> None:
        events = parsed(run(b"".join(frames())))

        stops = [i for i, e in enumerate(events) if e.get("type") == "content_block_stop" and e.get("index") == 0]
        opens = [
            i
            for i, e in enumerate(events)
            if e.get("type") == "content_block_start"
            and (e.get("content_block") or {}).get("type") == "tool_use"
        ]
        self.assertEqual(1, len(stops))
        self.assertLess(stops[0], opens[0])

    def test_a_frame_boundary_may_fall_anywhere(self) -> None:
        # The synthesized id carries the moment it was made, so two runs of the
        # same body differ in that name and nowhere else.
        body = b"".join(frames())
        whole = _without_ids(run(body))
        for size in (1, 3, 7, 41, len(body.decode()) - 1):
            with self.subTest(chunk_size=size):
                self.assertEqual(whole, _without_ids(run(body, chunk_size=size)))

    def test_an_answer_without_markup_is_forwarded_byte_for_byte(self) -> None:
        body = b"".join(frames(text="Just prose, no call here."))

        self.assertEqual(body, run(body))

    def test_prose_around_the_call_survives(self) -> None:
        out = run(b"".join(frames(text="Checking that." + CALL))).decode()

        self.assertIn("Checking that.", out)
        self.assertNotIn("DSML", out)

    def test_a_fenced_example_is_left_as_text(self) -> None:
        body = b"".join(frames(text="```\n" + CALL + "\n```"))

        out = run(body).decode()
        self.assertNotIn("tool_use", out)
        self.assertIn("DSML", out)

    def test_an_unterminated_body_still_hands_over_the_call(self) -> None:
        body = b"".join(frames()[:-2])  # drop message_delta and message_stop

        out = run(body).decode()
        self.assertNotIn("DSML", out)
        self.assertIn("tool_use", out)

    def test_thinking_and_pings_are_untouched(self) -> None:
        thinking = event(
            {"type": "content_block_delta", "index": 0, "delta": {"type": "thinking_delta", "thinking": "hm"}}
        )
        ping = event({"type": "ping"})
        body = b"".join(frames(text="plain")) + thinking + ping

        self.assertEqual(body, run(body))


class LeadBlockTests(unittest.TestCase):
    """A search this proxy already ran is published before the answer."""

    LEAD = [
        {
            "type": "server_tool_use",
            "id": "srvtoolu_abc",
            "name": "web_search",
            "input": {"query": "ruby 3.4"},
        },
        {
            "type": "web_search_tool_result",
            "tool_use_id": "srvtoolu_abc",
            "content": [
                {
                    "type": "web_search_result",
                    "title": "Ruby 3.4 Released",
                    "url": "https://www.ruby-lang.org/en/news/2024/12/25/ruby-3-4-0-released/",
                    "encrypted_content": "",
                }
            ],
        },
    ]

    def test_the_search_blocks_open_before_the_answer_and_move_its_index(self) -> None:
        events = parsed(run_with_lead(b"".join(frames(text="plain prose")), self.LEAD))

        starts = [e for e in events if e.get("type") == "content_block_start"]
        self.assertEqual([0, 1, 2], [e["index"] for e in starts])
        self.assertEqual("server_tool_use", starts[0]["content_block"]["type"])
        self.assertEqual("web_search_tool_result", starts[1]["content_block"]["type"])
        self.assertEqual("text", starts[2]["content_block"]["type"])
        # Every frame the route wrote names the shifted index, not only its
        # opening one: a delta left at 0 would append the answer to the search
        # call the client is already holding there.
        deltas = [e for e in events if e.get("type") == "content_block_delta"]
        self.assertEqual([0, 2], [e["index"] for e in deltas])
        self.assertEqual("plain prose", deltas[1]["delta"]["text"])
        stops = [e["index"] for e in events if e.get("type") == "content_block_stop"]
        self.assertEqual([0, 1, 2], stops)

    def test_the_search_input_is_streamed_as_tool_json(self) -> None:
        events = parsed(run_with_lead(b"".join(frames(text="x")), self.LEAD))

        partials = [
            e["delta"]["partial_json"]
            for e in events
            if e.get("type") == "content_block_delta"
            and (e.get("delta") or {}).get("type") == "input_json_delta"
            and e.get("index") == 0
        ]
        self.assertEqual([{"query": "ruby 3.4"}], [json.loads(part) for part in partials])

    def test_a_message_that_already_opened_blocks_keeps_them(self) -> None:
        body = b"".join(
            [
                event(
                    {
                        "type": "message_start",
                        "message": {
                            "id": "m",
                            "role": "assistant",
                            "content": [{"type": "text", "text": "hi"}],
                        },
                    }
                ),
                event({"type": "message_delta", "delta": {"stop_reason": "end_turn"}}),
                event({"type": "message_stop"}),
            ]
        )

        # The client already holds index 0, so a lead written there would
        # replace it; the answer is forwarded instead.
        self.assertEqual(body, run_with_lead(body, self.LEAD))

    def test_the_search_count_is_stated_on_the_final_usage(self) -> None:
        # Claude Code reads ``usage.server_tool_use`` from the last
        # ``message_delta``; without it a search that answered reports zero.
        body = b"".join(
            [
                event(
                    {
                        "type": "message_start",
                        "message": {"id": "m", "role": "assistant", "content": []},
                    }
                ),
                event(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {"input_tokens": 3, "output_tokens": 4},
                    }
                ),
                event({"type": "message_stop"}),
            ]
        )

        events = parsed(run_with_lead(body, self.LEAD))

        delta = [e for e in events if e.get("type") == "message_delta"][-1]
        self.assertEqual({"web_search_requests": 1}, delta["usage"]["server_tool_use"])
        self.assertEqual(3, delta["usage"]["input_tokens"])

    def test_an_upstream_usage_is_not_restated(self) -> None:
        body = b"".join(
            [
                event(
                    {
                        "type": "message_start",
                        "message": {"id": "m", "role": "assistant", "content": []},
                    }
                ),
                event(
                    {
                        "type": "message_delta",
                        "delta": {"stop_reason": "end_turn"},
                        "usage": {
                            "input_tokens": 3,
                            "server_tool_use": {"web_search_requests": 2},
                        },
                    }
                ),
                event({"type": "message_stop"}),
            ]
        )

        delta = [
            e
            for e in parsed(run_with_lead(body, self.LEAD))
            if e.get("type") == "message_delta"
        ][-1]

        self.assertEqual({"web_search_requests": 2}, delta["usage"]["server_tool_use"])

    def test_markup_is_still_lifted_after_a_lead(self) -> None:
        out = run_with_lead(b"".join(frames()), self.LEAD).decode()

        self.assertNotIn("DSML", out)
        self.assertIn("srvtoolu_abc", out)
        self.assertIn("Bash", out)


if __name__ == "__main__":
    unittest.main()
