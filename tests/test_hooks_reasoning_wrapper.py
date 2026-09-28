from __future__ import annotations

import json

from hook_test_utils import *


LEAKED_TEXT = "<thinking>**Searching official sources**\n**Planning label review**"
LEAKED_TAIL = "**Searching official sources**\n**Planning label review**"


class HookReasoningWrapperTests(HookTestCase):
    def test_strip_removes_a_closed_wrapper_and_keeps_its_summary(self) -> None:
        hooks, _proxy_server = load_hook_module()

        self.assertEqual(
            hooks._strip_reasoning_wrapper(
                "<thinking>**Weighing options**</thinking>\nThe answer is 4."
            ),
            "**Weighing options**\nThe answer is 4.",
        )
        self.assertEqual(
            hooks._strip_reasoning_wrapper("<think>draft</think>answer"),
            "draftanswer",
        )
        self.assertEqual(
            hooks._strip_reasoning_wrapper("<budget:thinking>draft</budget:thinking>x"),
            "draftx",
        )

    def test_strip_removes_an_unclosed_opener_and_keeps_its_summary(self) -> None:
        hooks, _proxy_server = load_hook_module()

        self.assertEqual(
            hooks._strip_reasoning_wrapper(LEAKED_TEXT),
            LEAKED_TAIL,
        )

    def test_strip_keeps_leading_whitespace_and_plain_text(self) -> None:
        hooks, _proxy_server = load_hook_module()

        self.assertEqual(
            hooks._strip_reasoning_wrapper("\n  <thinking>summary"),
            "\n  summary",
        )
        self.assertEqual(
            hooks._strip_reasoning_wrapper("plain answer"),
            "plain answer",
        )
        self.assertEqual(
            hooks._strip_reasoning_wrapper("see the <thinking> tag"),
            "see the <thinking> tag",
        )
        self.assertEqual(
            hooks._strip_reasoning_wrapper("<thinking-out-loud>answer"),
            "<thinking-out-loud>answer",
        )

    def test_strip_is_idempotent(self) -> None:
        hooks, _proxy_server = load_hook_module()

        for text in (
            LEAKED_TEXT,
            "<thinking>**Weighing options**</thinking>\nThe answer is 4.",
            "plain answer",
        ):
            once = hooks._strip_reasoning_wrapper(text)
            self.assertEqual(hooks._strip_reasoning_wrapper(once), once)

    def test_stream_filter_matches_the_finished_text_for_every_split(self) -> None:
        hooks, _proxy_server = load_hook_module()

        for text in (
            LEAKED_TEXT,
            "<thinking>**Weighing options**</thinking>\nThe answer is 4.",
            "<think>a</think>b",
            "plain answer",
            "<thinking-out-loud>answer",
            "  <thinking>**Summary**",
            "<thinking>",
        ):
            expected = hooks._strip_reasoning_wrapper(text)
            for split in range(len(text) + 1):
                stream_state = hooks._ReasoningWrapperStreamState()
                delivered = stream_state.consume_delta("item_1", text[:split])
                delivered += stream_state.consume_delta("item_1", text[split:])
                self.assertEqual(
                    delivered,
                    expected,
                    msg=f"split={split} text={text!r}",
                )

    def test_delivery_strips_a_wrapper_split_across_deltas(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {"model": "default-chat", "stream": True, "input": "go"}
        item_id = "item_c7047b533f79442c12ca4c77"

        delivered = "".join(
            hooks._responses_stream_chunk_for_delivery(
                {"type": "response.output_text.delta", "item_id": item_id, "delta": delta},
                request_data,
            )["delta"]
            for delta in ("<thin", "king>**Searching", " official sources**")
        )

        self.assertEqual(delivered, "**Searching official sources**")

    def test_delivery_keeps_item_identity_and_untouched_events(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {"model": "default-chat", "stream": True, "input": "go"}

        added = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.output_item.added",
                "output_index": 3,
                "item": {
                    "type": "message",
                    "id": "item_a187c8829689d43c857685ce",
                    "role": "assistant",
                    "status": "in_progress",
                    "content": [{"type": "output_text", "text": "", "annotations": []}],
                },
            },
            request_data,
        )
        done = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.output_item.done",
                "output_index": 3,
                "item": {
                    "type": "message",
                    "id": "item_a187c8829689d43c857685ce",
                    "role": "assistant",
                    "status": "completed",
                    "content": [{"type": "output_text", "text": LEAKED_TEXT}],
                },
            },
            request_data,
        )
        reasoning = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.reasoning_summary_text.delta",
                "item_id": "item_318fad174018cec3980401ab",
                "delta": "<thinking>not message text</thinking>",
            },
            request_data,
        )
        part = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.content_part.done",
                "item_id": "item_reasoning",
                "content_index": 0,
                "part": {"type": "reasoning_text", "text": "<thinking>kept</thinking>"},
            },
            request_data,
        )

        self.assertEqual(added["item"]["id"], "item_a187c8829689d43c857685ce")
        self.assertEqual(added["output_index"], 3)
        self.assertEqual(done["item"]["id"], "item_a187c8829689d43c857685ce")
        self.assertEqual(done["item"]["type"], "message")
        self.assertEqual(done["output_index"], 3)
        self.assertEqual(done["item"]["content"][0]["text"], LEAKED_TAIL)
        self.assertEqual(
            reasoning["delta"],
            "<thinking>not message text</thinking>",
        )
        self.assertEqual(part["part"]["text"], "<thinking>kept</thinking>")

    def test_delivery_sanitizes_the_finished_payloads(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {"model": "default-chat", "stream": True, "input": "go"}
        item = {
            "type": "message",
            "id": "item_a187c8829689d43c857685ce",
            "role": "assistant",
            "status": "completed",
            "content": [{"type": "output_text", "text": LEAKED_TEXT, "annotations": []}],
        }

        text_done = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.output_text.done",
                "item_id": item["id"],
                "text": LEAKED_TEXT,
            },
            request_data,
        )
        part_done = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.content_part.done",
                "item_id": item["id"],
                "content_index": 0,
                "part": {"type": "output_text", "text": LEAKED_TEXT},
            },
            request_data,
        )
        completed = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.completed",
                "response": {
                    "id": "resp_1",
                    "object": "response",
                    "status": "completed",
                    "output_text": LEAKED_TEXT,
                    "output": [item],
                },
            },
            request_data,
        )

        self.assertEqual(text_done["text"], LEAKED_TAIL)
        self.assertEqual(part_done["part"]["text"], LEAKED_TAIL)
        self.assertEqual(completed["response"]["output"][0]["content"][0]["text"], LEAKED_TAIL)
        self.assertEqual(completed["response"]["output_text"], LEAKED_TAIL)
        self.assertEqual(completed["response"]["output"][0]["id"], item["id"])
        self.assertEqual(
            completed["response"]["output"][0]["type"],
            "message",
        )

    def test_delivery_leaves_a_plain_message_alone(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {"model": "default-chat", "stream": True, "input": "go"}
        chunk = {
            "type": "response.output_text.delta",
            "item_id": "item_plain",
            "delta": "The file is at src/main.py.",
        }

        delivered = hooks._responses_stream_chunk_for_delivery(chunk, request_data)

        self.assertEqual(delivered["delta"], chunk["delta"])

    def test_delivery_strips_without_a_request_scope(self) -> None:
        hooks, _proxy_server = load_hook_module()

        delivered = hooks._responses_stream_chunk_for_delivery(
            {
                "type": "response.output_text.delta",
                "item_id": "item_1",
                "delta": LEAKED_TEXT,
            }
        )

        self.assertEqual(delivered["delta"], LEAKED_TAIL)

    def test_sse_text_delivery_strips_the_wrapper(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {"model": "default-chat", "stream": True, "input": "go"}
        payload = json.dumps(
            {
                "type": "response.output_text.delta",
                "item_id": "item_1",
                "delta": LEAKED_TEXT,
            }
        )
        chunk = f"event: response.output_text.delta\ndata: {payload}\n\n".encode("utf-8")

        delivered = hooks._responses_stream_chunk_for_delivery(chunk, request_data)
        delivered_payload = json.loads(
            delivered.decode("utf-8").split("data: ", 1)[1].split("\n", 1)[0]
        )

        self.assertEqual(delivered_payload["delta"], LEAKED_TAIL)
        self.assertEqual(delivered_payload["item_id"], "item_1")

    def test_whole_response_sanitizer_preserves_the_item_identity(self) -> None:
        hooks, _proxy_server = load_hook_module()
        request_data = {"model": "default-chat", "input": "go"}
        payload = {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "reasoning",
                    "id": "item_reasoning",
                    "summary": [{"type": "summary_text", "text": "**Planning**"}],
                    "content": None,
                    "encrypted_content": None,
                },
                {
                    "type": "message",
                    "id": "item_message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": LEAKED_TEXT, "annotations": []}
                    ],
                },
            ],
        }

        sanitized = hooks._sanitize_reasoning_wrapper_response(payload, request_data)

        self.assertEqual(sanitized["output"][1]["content"][0]["text"], LEAKED_TAIL)
        self.assertEqual(sanitized["output"][1]["id"], "item_message")
        self.assertEqual(sanitized["output"][0], payload["output"][0])
        json.dumps(sanitized)

    async def test_post_call_delivery_strips_the_wrapper(self) -> None:
        hooks, _proxy_server = load_hook_module()
        hook = hooks.YoungRouterHook()
        request_data = {
            "call_type": "aresponses",
            "model": "default-chat",
            "input": "go",
        }
        response = {
            "id": "resp_1",
            "object": "response",
            "status": "completed",
            "output": [
                {
                    "type": "message",
                    "id": "item_message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {"type": "output_text", "text": LEAKED_TEXT, "annotations": []}
                    ],
                }
            ],
        }

        delivered = await hook.async_post_call_success_deployment_hook(
            request_data,
            response,
            "aresponses",
        )

        self.assertEqual(delivered["output"][0]["content"][0]["text"], LEAKED_TAIL)
        self.assertEqual(delivered["output"][0]["id"], "item_message")
        self.assertEqual(delivered["output"][0]["type"], "message")
