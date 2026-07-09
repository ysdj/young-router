from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "route_trace_report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location(
        "route_trace_report_under_test",
        REPORT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RouteTraceReportTests(unittest.TestCase):
    def test_render_shows_interface_reasoning_and_tool_calls(self) -> None:
        report = load_report_module()
        events = [
            {
                "timestamp": "2026-06-20T01:00:00Z",
                "event": "filter_deployments",
                "request_id": "req-trace",
                "model_group": "balanced-chat",
                "request": {
                    "preview": {
                        "source": "input",
                        "latest_user": "今天天气如何",
                        "latest_user_kind": "user_request",
                    },
                    "interface": {
                        "client_surface": "responses",
                        "effective_upstream_surface": "responses",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                        "upstream_url_surface": "openai/responses",
                    },
                    "reasoning": {
                        "present": True,
                        "effort": "xhigh",
                        "reasoning": {"effort": "xhigh"},
                    },
                    "tools": {
                        "count": 2,
                        "types": ["web_search", "function"],
                        "names": ["lookup_order"],
                        "exposed": [
                            {"type": "web_search"},
                            {"type": "function", "name": "lookup_order"},
                        ],
                        "has_web_search_tool": True,
                    },
                    "metadata_flags": {
                        "external_web_search_bridge": True,
                    },
                },
                "healthy": [],
                "selected_candidates": [],
            },
            {
                "timestamp": "2026-06-20T01:00:01Z",
                "event": "responses_external_web_search_bridge_retry_start",
                "request_id": "req-trace",
                "model_group": "balanced-chat",
                "request": {
                    "interface": {
                        "client_surface": "responses",
                        "effective_upstream_surface": "responses",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                        "upstream_url_surface": "openai/responses",
                    },
                },
                "retry_request": {
                    "interface": {
                        "client_surface": "chat",
                        "effective_upstream_surface": "chat",
                        "requested_endpoint": "/v1/chat/completions",
                        "stream": True,
                        "upstream_url_surface": "openai/chat",
                    },
                    "tools": {
                        "count": 1,
                        "types": ["function"],
                        "names": ["_litellm_web_search"],
                    },
                },
                "retry_tool_types": ["function"],
                "retry_tool_names": ["_litellm_web_search"],
            },
            {
                "timestamp": "2026-06-20T01:00:02Z",
                "event": "external_web_search_bridge_post_call_start",
                "request_id": "req-trace",
                "model_group": "balanced-chat",
                "request": {
                    "interface": {
                        "client_surface": "responses",
                        "effective_upstream_surface": "responses",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                        "upstream_url_surface": "openai/responses",
                    },
                },
                "response": {
                    "tool_calls": {
                        "count": 1,
                        "types": ["function_call"],
                        "names": ["_litellm_web_search"],
                        "calls": [
                            {
                                "type": "function_call",
                                "name": "_litellm_web_search",
                                "call_id": "call_1",
                                "arguments_preview": {"query": "Sample City weather"},
                            }
                        ],
                    },
                    "web_search_actions": [
                        {"type": "search", "query": "Sample City weather"}
                    ],
                },
                "actions": [{"type": "search", "query": "Sample City weather"}],
            },
        ]

        html = report.render(events, scan_lines="2", max_requests=10)

        self.assertIn("Interface", html)
        self.assertIn("Reasoning", html)
        self.assertIn("Actual Tool Calls", html)
        self.assertIn("openai/responses", html)
        self.assertIn("responses -&gt; chat", html)
        self.assertIn("retry client=chat upstream=chat", html)
        self.assertIn("Responses External Web Search Bridge Retry Start", html)
        route_chain = html.split("Request Preview", 1)[0]
        timeline = html.split("Timeline events", 1)[1]
        self.assertIn("responses -&gt; chat", route_chain)
        self.assertIn("responses -&gt; chat", timeline)
        self.assertIn("xhigh", html)
        self.assertIn("lookup_order", html)
        self.assertIn("_litellm_web_search", html)
        self.assertIn("Sample City weather", html)

    def test_preemptive_chat_bridge_reports_responses_to_chat_not_retry_chat(self) -> None:
        report = load_report_module()
        events = [
            {
                "timestamp": "2026-06-28T17:25:00Z",
                "event": "selected_deployment",
                "request_id": "req-bridge",
                "model_group": "legacy-chat",
                "deployment": {
                    "id": "d8c19f52",
                    "provider": "provider_chat",
                    "model": "openai/vendor-chat",
                    "route_key": "provider_chat / openai/vendor-chat / key=default / order=1",
                    "upstream_url_surface": "openai/chat",
                    "supports_responses_endpoint": False,
                },
                "request": {
                    "interface": {
                        "client_surface": "responses",
                        "effective_upstream_surface": "chat",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                        "upstream_url_surface": "openai/chat",
                        "supported_upstream_url_surfaces": ["openai/chat"],
                        "supports_responses_endpoint": False,
                    },
                },
            },
            {
                "timestamp": "2026-06-28T17:25:01Z",
                "event": "responses_chat_bridge_preemptive_start",
                "request_id": "req-bridge",
                "model_group": "legacy-chat",
                "request": {
                    "interface": {
                        "client_surface": "responses",
                        "effective_upstream_surface": "chat",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                        "upstream_url_surface": "openai/chat",
                        "supported_upstream_url_surfaces": ["openai/chat"],
                        "supports_responses_endpoint": False,
                    },
                },
                "retry_request": {
                    "interface": {
                        "client_surface": "responses",
                        "effective_upstream_surface": "chat",
                        "requested_endpoint": "/v1/responses",
                        "stream": True,
                        "upstream_url_surface": "openai/chat",
                        "supported_upstream_url_surfaces": ["openai/chat"],
                        "supports_responses_endpoint": False,
                        "use_chat_completions_api": True,
                    },
                },
                "preemptive_reason": "responses_endpoint_unsupported",
            },
        ]

        html = report.render(events, scan_lines="2", max_requests=10)
        route_chain = html.split("Request Preview", 1)[0]
        timeline = html.split("Timeline events", 1)[1]

        self.assertIn("Responses Chat Bridge Preemptive Start", html)
        self.assertIn("responses -&gt; chat", route_chain)
        self.assertIn("responses -&gt; chat", timeline)
        self.assertIn("responses_endpoint_unsupported", html)
        self.assertNotIn("retry chat", html)


if __name__ == "__main__":
    unittest.main()
