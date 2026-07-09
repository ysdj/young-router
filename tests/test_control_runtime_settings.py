from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"


class ControlRuntimeSettingsTests(unittest.TestCase):
    def run_runtime_settings(self, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = os.environ.copy()
            env.update(
                {
                    "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                    "LITELLM_RUNTIME_ROOT": str(temp / "runtime"),
                    "LITELLM_TEMPLATE_ROOT": str(ROOT),
                    "PYTHON": sys.executable,
                }
            )
            if env_overrides:
                env.update(env_overrides)
            return subprocess.run(
                ["/bin/bash", str(CONTROL), "runtime-settings"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

    def test_runtime_settings_reports_vision_bridge_backend(self) -> None:
        result = self.run_runtime_settings({"LITELLM_MENU_VISION_BRIDGE_BACKEND": "local"})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        backend = next(item for item in payload["settings"] if item["key"] == "LITELLM_MENU_VISION_BRIDGE_BACKEND")
        self.assertEqual("local", backend["value"])
        self.assertEqual(["auto", "local", "api", "off"], backend["options"])

    def test_runtime_settings_reports_local_format(self) -> None:
        result = self.run_runtime_settings({"LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT": "detailed"})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        setting = next(item for item in payload["settings"] if item["key"] == "LITELLM_MENU_VISION_BRIDGE_LOCAL_FORMAT")
        self.assertEqual("detailed", setting["value"])
        self.assertEqual(["compact", "detailed"], setting["options"])

    def test_runtime_settings_legacy_mode_maps_to_backend(self) -> None:
        result = self.run_runtime_settings({"LITELLM_MENU_VISION_BRIDGE_MODE": "off"})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        backend = next(item for item in payload["settings"] if item["key"] == "LITELLM_MENU_VISION_BRIDGE_BACKEND")
        self.assertEqual("off", backend["value"])

    def test_runtime_settings_reports_custom_local_port(self) -> None:
        result = self.run_runtime_settings({"LITELLM_PORT": "49240"})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        port = next(item for item in payload["settings"] if item["key"] == "LITELLM_PORT")
        self.assertEqual("49240", port["value"])
        self.assertEqual("4000", port["default"])
        self.assertEqual(1, port["minimum"])
        self.assertEqual(65535, port["maximum"])

    def test_runtime_settings_do_not_include_webdav_settings(self) -> None:
        result = self.run_runtime_settings({"LITELLM_WEBDAV_TIMEOUT": "45"})

        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        keys = {item["key"] for item in payload["settings"]}
        categories = {item["category"] for item in payload["settings"]}
        self.assertNotIn("LITELLM_WEBDAV_TIMEOUT", keys)
        self.assertNotIn("WebDAV", categories)


if __name__ == "__main__":
    unittest.main()
