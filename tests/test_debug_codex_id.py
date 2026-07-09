from __future__ import annotations

import importlib.util
import json
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT_PATH = ROOT / "scripts" / "debug_codex_id.py"


def load_debug_module():
    spec = importlib.util.spec_from_file_location(
        "debug_codex_id_under_test",
        SCRIPT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class DebugCodexIdTests(unittest.TestCase):
    def test_structured_matching_ignores_preview_mentions_by_default(self) -> None:
        mod = load_debug_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recent = tmp_path / "recent-requests.jsonl"
            route = tmp_path / "menu-server.log"
            target = "thread-target"

            recent.write_text(
                "\n".join(
                    [
                        json.dumps(
                            {
                                "ts": "2026-06-25T00:00:00Z",
                                "request_id": "req-target",
                                "session": {"id": target},
                                "status": "success",
                            }
                        ),
                        json.dumps(
                            {
                                "ts": "2026-06-25T00:00:01Z",
                                "request_id": "req-mention",
                                "session": {"id": "other-thread"},
                                "status": "success",
                                "request_preview": {"preview": f"debug mentions {target}"},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            route.write_text(
                "\n".join(
                    [
                        "litellm_route_trace "
                        + json.dumps(
                            {
                                "timestamp": "2026-06-25T00:00:00Z",
                                "event": "selected_deployment",
                                "request_id": "req-target",
                                "session": {"id": target},
                            }
                        ),
                        "litellm_route_trace "
                        + json.dumps(
                            {
                                "timestamp": "2026-06-25T00:00:01Z",
                                "event": "selected_deployment",
                                "request_id": "req-mention",
                                "session": {"id": "other-thread"},
                                "request": {"preview": {"preview": f"debug mentions {target}"}},
                            }
                        ),
                    ]
                )
                + "\n",
                encoding="utf-8",
            )

            result = mod.build_debug_result(
                [target],
                recent_requests_path=recent,
                route_log_path=route,
            )

            self.assertEqual(result["summary"]["recent_request_count"], 1)
            self.assertEqual(result["summary"]["route_trace_event_count"], 1)
            self.assertEqual(result["related_ids"]["request_ids"], ["req-target"])
            self.assertEqual(result["related_ids"]["session_ids"], [target])

    def test_text_search_can_intentionally_match_preview_mentions(self) -> None:
        mod = load_debug_module()
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            recent = tmp_path / "recent-requests.jsonl"
            route = tmp_path / "menu-server.log"
            target = "thread-target"
            recent.write_text(
                json.dumps(
                    {
                        "ts": "2026-06-25T00:00:01Z",
                        "request_id": "req-mention",
                        "session": {"id": "other-thread"},
                        "status": "success",
                        "request_preview": {"preview": f"debug mentions {target}"},
                    }
                )
                + "\n",
                encoding="utf-8",
            )
            route.write_text("", encoding="utf-8")

            result = mod.build_debug_result(
                [target],
                recent_requests_path=recent,
                route_log_path=route,
                text_search=True,
            )

            self.assertEqual(result["summary"]["recent_request_count"], 1)
            self.assertEqual(result["related_ids"]["request_ids"], ["req-mention"])
            self.assertEqual(
                result["related_ids"]["session_ids"],
                ["other-thread"],
            )


if __name__ == "__main__":
    unittest.main()
