from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "menu_status.py"


class MenuStatusTests(unittest.TestCase):
    def service_state(
        self,
        *,
        health: bool = False,
        native: bool = False,
        port_pid: bool = False,
        recent: str = "",
        expected_owner: str = "",
        owner_matches: bool = True,
    ) -> str:
        script = f"""
        source {ROOT / 'service/menu_status.sh'}
        health_ok() {{ {'return 0' if health else 'return 1'}; }}
        native_running() {{ {'return 0' if native else 'return 1'}; }}
        native_port_pids() {{ {'echo 4242' if port_pid else ':'}; }}
        native_owned_by_menu_pid() {{ {'return 0' if owner_matches else 'return 1'}; }}
        recent_state() {{ printf '%s' {recent!r}; }}
        LITELLM_MENU_OWNER_PID={expected_owner!r}
        menu_service_state
        """
        result = subprocess.run(
            ["/bin/bash", "-c", script],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_service_state_classifies_running_starting_unhealthy_and_stopped(self) -> None:
        self.assertEqual(self.service_state(health=True, native=True), "running")
        self.assertEqual(self.service_state(health=True, port_pid=True), "running")
        self.assertEqual(self.service_state(recent="starting"), "starting")
        self.assertEqual(self.service_state(native=True), "unhealthy")
        self.assertEqual(self.service_state(health=True), "unhealthy")
        self.assertEqual(self.service_state(), "stopped")

    def test_service_state_rejects_a_service_owned_by_another_menu_process(self) -> None:
        self.assertEqual(
            self.service_state(
                health=True,
                native=True,
                expected_owner="4242",
                owner_matches=False,
            ),
            "unhealthy",
        )

    def test_status_combines_recovery_and_safe_webdav_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            recovery = temp / "recovery.json"
            cooldown = temp / "cooldown.json"
            webdav = temp / "webdav.json"
            recovery.write_text(json.dumps({"recoveries": {}}), encoding="utf-8")
            cooldown.write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "route": {"cooldown_until": time.time() + 60}
                        }
                    }
                ),
                encoding="utf-8",
            )
            webdav.write_text("not json", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--service-state",
                    "running",
                    "--auto-start-state",
                    "incomplete",
                    "--webdav-enabled",
                    "true",
                    "--webdav-status-file",
                    str(webdav),
                    "--recovery-state-file",
                    str(recovery),
                    "--cooldown-state-file",
                    str(cooldown),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["service_state"], "running")
            self.assertEqual(payload["auto_start_state"], "incomplete")
            self.assertEqual(payload["route_recovery_summary"], "0 recovering / 1 cooldown")
            self.assertTrue(payload["webdav_sync_enabled"])
            self.assertEqual(payload["webdav_last_status"]["enabled"], True)
            self.assertEqual(payload["webdav_last_status"]["output"], "")

    def test_status_includes_structured_current_recovery_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            recovery = temp / "recovery.json"
            cooldown = temp / "cooldown.json"
            webdav = temp / "webdav.json"
            recovery.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "request:current": {
                                "pid": 0,
                                "status": "polling",
                                "heartbeat_at": time.time(),
                                "attempt": 3,
                                "cooldown_remaining_seconds": 41.5,
                                "diagnostic": {
                                    "kind": "billing",
                                    "title": "Billing or credit limit",
                                    "detail": "The upstream reported insufficient balance, quota, or credits.",
                                },
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            cooldown.write_text(json.dumps({"cooldowns": {}}), encoding="utf-8")
            webdav.write_text("{}", encoding="utf-8")

            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "--service-state", "running",
                    "--auto-start-state", "disabled",
                    "--webdav-enabled", "false",
                    "--webdav-status-file", str(webdav),
                    "--recovery-state-file", str(recovery),
                    "--cooldown-state-file", str(cooldown),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual(payload["route_recovery_summary"], "1 recovering / 0 cooldown")
            self.assertEqual(payload["route_recovery"]["current"]["kind"], "billing")
            self.assertEqual(payload["route_recovery"]["current"]["activity"], "active")
            self.assertEqual(
                payload["route_recovery"]["current"]["cooldown_remaining_seconds"],
                41.5,
            )

    def test_invalid_webdav_status_fields_are_sanitized_without_losing_menu_state(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            recovery = temp / "recovery.json"
            cooldown = temp / "cooldown.json"
            webdav = temp / "webdav.json"
            recovery.write_text(json.dumps({"recoveries": {}}), encoding="utf-8")
            cooldown.write_text(json.dumps({"cooldowns": {}}), encoding="utf-8")
            webdav.write_text(
                json.dumps(
                    {
                        "enabled": "yes",
                        "ok": "no",
                        "exit_code": "bad",
                        "action": 7,
                        "checked_at": ["bad"],
                        "output": {"bad": True},
                    }
                ),
                encoding="utf-8",
            )

            result = subprocess.run(
                [
                    sys.executable, str(SCRIPT),
                    "--service-state", "running",
                    "--auto-start-state", "enabled",
                    "--webdav-enabled", "false",
                    "--webdav-status-file", str(webdav),
                    "--recovery-state-file", str(recovery),
                    "--cooldown-state-file", str(cooldown),
                ],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(0, result.returncode, result.stderr)
            payload = json.loads(result.stdout)
            self.assertEqual("running", payload["service_state"])
            self.assertEqual("enabled", payload["auto_start_state"])
            self.assertEqual(
                {
                    "action": None,
                    "ok": None,
                    "exit_code": None,
                    "checked_at": None,
                    "enabled": False,
                    "output": "",
                },
                payload["webdav_last_status"],
            )


if __name__ == "__main__":
    unittest.main()
