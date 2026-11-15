from __future__ import annotations

from hook_test_utils import *


class ProxyTraceEdgeTests(HookTestCase):
    def test_p95_text_length_uses_nearest_rank(self) -> None:
        hooks, _ = load_hook_module()
        histories = [
            {"role": "user", "content": "x" * index}
            for index in range(1, 21)
        ]

        shape = hooks._trace_responses_input_shape({"messages": histories})

        self.assertEqual(shape["longest_text_len"], 20)
        self.assertEqual(shape["p95_text_len"], 19)

    def test_trace_limited_value_marks_a_truncated_list(self) -> None:
        hooks, _ = load_hook_module()

        limited = hooks._trace_limited_value(
            [f"item-{index}" for index in range(25)]
        )

        self.assertEqual(len(limited), 21)
        self.assertEqual(limited[:-1], [f"item-{index}" for index in range(20)])
        self.assertEqual(limited[-1], {"_truncated": True})
        self.assertEqual(hooks._trace_limited_value(["a", "b"]), ["a", "b"])

    def test_route_trace_session_falls_back_to_source_request(self) -> None:
        hooks, _ = load_hook_module()

        with self.assertLogs("young_router.route_trace", level="WARNING") as captured:
            hooks._request_route_trace(
                "retry_start",
                {"request_id": "req-source", "thread_id": "thread-source"},
                trace_request={"request_id": "req-attempt"},
            )

        record = json.loads(captured.output[0].split("litellm_route_trace ", 1)[1])
        self.assertEqual(record["request_id"], "req-attempt")
        self.assertEqual(record["session"]["id"], "thread-source")
