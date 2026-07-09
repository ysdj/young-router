from __future__ import annotations

import importlib.util
import json
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
REPORT_PATH = ROOT / "route_recovery_report.py"


def load_report_module():
    spec = importlib.util.spec_from_file_location(
        "route_recovery_report_under_test",
        REPORT_PATH,
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class RouteRecoveryReportTests(unittest.TestCase):
    def test_summary_counts_active_recoveries_and_deployment_cooldowns(self) -> None:
        report = load_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            recovery_path = Path(tmp) / "route-recovery-state.json"
            cooldown_path = Path(tmp) / "deployment-cooldowns.json"
            recovery_path.write_text(
                json.dumps(
                    {"recoveries": {}}
                ),
                encoding="utf-8",
            )
            cooldown_path.write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "id:route": {
                                "deployment_id": "route",
                                "cooldown_until": time.time() + 120,
                            },
                            "id:backup": {
                                "deployment_id": "backup",
                                "cooldown_until": time.time() + 180,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            self.assertEqual(
                report.summary(
                    recovery_state_path=str(recovery_path),
                    cooldown_state_path=str(cooldown_path),
                ),
                "0 recovering / 2 cooldown",
            )

    def test_render_shows_current_recovery_and_recent_timeout_details(self) -> None:
        report = load_report_module()
        with tempfile.TemporaryDirectory() as tmp:
            recovery_path = Path(tmp) / "route-recovery-state.json"
            cooldown_path = Path(tmp) / "deployment-cooldowns.json"
            recent_path = Path(tmp) / "recent-requests.jsonl"
            recovery_path.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "request:req-a": {
                                "status": "polling",
                                "pid": 0,
                                "session": {"id": "thread-a", "name": "Thread A"},
                                "request_id": "req-a",
                                "model_group": "default-chat",
                                "attempt": 2,
                                "exception": {"type": "TimeoutError", "reason": "stream_idle_timeout"},
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            cooldown_path.write_text(json.dumps({"cooldowns": {}}), encoding="utf-8")
            recent_path.write_text(
                json.dumps(
                    {
                        "status": "stuck",
                        "ts": "2026-07-08T12:00:00Z",
                        "session": {"id": "thread-a"},
                        "model_group": "default-chat",
                        "stuck": {"reason": "stream_idle_timeout"},
                        "request_id": "req-a",
                    }
                )
                + "\n",
                encoding="utf-8",
            )

            html = report.render(
                recovery_state_path=str(recovery_path),
                cooldown_state_path=str(cooldown_path),
                recent_requests_path=str(recent_path),
            )

            self.assertIn("LiteLLM Recovery", html)
            self.assertIn("Thread A", html)
            self.assertIn("stream_idle_timeout", html)
            self.assertIn("Recent Recovery Timeouts", html)
            self.assertIn("recovering threads", html)


if __name__ == "__main__":
    unittest.main()
