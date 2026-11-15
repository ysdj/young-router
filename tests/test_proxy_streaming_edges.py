from __future__ import annotations

from hook_test_utils import *


class ProxyStreamingEdgeTests(HookTestCase):

    def test_context_length_text_is_not_read_as_an_output_limit(self) -> None:
        hooks, _ = load_hook_module()

        for text in (
            "context_length_exceeded",
            "This model's maximum context length is 128000 tokens.",
        ):
            self.assertIsNone(
                hooks._output_limit_incomplete_reason_from_text(text), text
            )
        self.assertEqual(
            hooks._output_limit_incomplete_reason_from_text(
                "stream disconnected: Incomplete response returned, reason: max_output_tokens"
            ),
            "max_output_tokens",
        )

    def test_context_length_incomplete_is_not_dressed_as_completed(self) -> None:
        hooks, _ = load_hook_module()
        chunk = {
            "type": "response.incomplete",
            "response": {
                "id": "resp-cut",
                "status": "incomplete",
                "incomplete_details": {
                    "message": "context_length_exceeded: input exceeds the context window"
                },
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "half"}],
                    }
                ],
            },
        }

        self.assertIsNone(
            hooks._responses_output_limit_incomplete_as_completed_chunk(chunk, {})
        )

    def test_encoded_responses_frames_keep_the_announced_response_id(self) -> None:
        hooks, _ = load_hook_module()
        request_data = {
            "call_type": "aresponses",
            "input": "say hello",
            hooks._ANNOUNCED_RESPONSE_ID_KEY: "resp-announced",
        }
        completed = (
            'data: {"type":"response.completed",'
            '"response":{"id":"resp-upstream","status":"completed"}}\n\n'
        )

        delivered = hooks._responses_stream_chunk_for_delivery(completed, request_data)

        self.assertIn("resp-announced", delivered)
        self.assertNotIn("resp-upstream", delivered)

    def test_encoded_upstream_opening_is_dropped_under_the_announced_id(self) -> None:
        hooks, _ = load_hook_module()
        request_data = {
            "call_type": "aresponses",
            "input": "say hello",
            hooks._ANNOUNCED_RESPONSE_ID_KEY: "resp-announced",
        }
        opening = 'data: {"type":"response.created","response":{"id":"resp-upstream"}}\n\n'

        self.assertIsNone(
            hooks._responses_stream_chunk_for_delivery(opening, request_data)
        )
