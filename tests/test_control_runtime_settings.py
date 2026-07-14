from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"
RUNTIME_SETTINGS_SOURCE = ROOT / "service" / "runtime_settings.sh"
RUNTIME_SETTINGS_CONFIGURE_SOURCE = ROOT / "service" / "runtime_settings_configure.sh"
ENVIRONMENT_SOURCE = ROOT / "service" / "environment.sh"
PROCESS_SOURCE = ROOT / "service" / "process.sh"
LAUNCHD_SOURCE = ROOT / "service" / "launchd_watch.sh"


class ControlRuntimeSettingsTests(unittest.TestCase):
    def control_env(
        self,
        temp: Path,
        env_overrides: dict[str, str] | None = None,
    ) -> dict[str, str]:
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
        return env

    def run_control(
        self,
        temp: Path,
        action: str,
        *,
        input_text: str | None = None,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(CONTROL), action],
            cwd=ROOT,
            env=self.control_env(temp, env_overrides),
            input=input_text,
            text=True,
            capture_output=True,
            check=False,
        )

    def run_runtime_settings(self, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            return self.run_control(
                temp,
                "runtime-settings",
                env_overrides=env_overrides,
            )

    @staticmethod
    def embedded_specs(path: Path, end_marker: str) -> list[object]:
        source = path.read_text(encoding="utf-8")
        body = source.split("SPECS = [", 1)[1].split(end_marker, 1)[0]
        return ast.literal_eval("[" + body + "]")

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

    def test_runtime_settings_and_configure_specs_match_exactly(self) -> None:
        displayed = self.embedded_specs(
            RUNTIME_SETTINGS_SOURCE,
            "]\n\n\ndef read_configured",
        )
        configurable = self.embedded_specs(
            RUNTIME_SETTINGS_CONFIGURE_SOURCE,
            "]\nSPEC_BY_KEY",
        )
        displayed_by_key = {item["key"]: item for item in displayed}
        configurable_by_key = {item[0]: item for item in configurable}

        self.assertEqual(set(displayed_by_key), set(configurable_by_key))
        for key, item in displayed_by_key.items():
            configured = configurable_by_key[key]
            self.assertEqual(item["kind"], configured[1], key)
            self.assertEqual(str(item["default"]), str(configured[2]), key)
            self.assertEqual(item.get("minimum"), configured[3], key)
            self.assertEqual(item.get("maximum"), configured[4], key)

    def test_model_runtime_settings_reach_loader_direct_process_and_launchd(self) -> None:
        propagated_keys = {
            "LITELLM_MENU_STREAM_START_TIMEOUT_SECONDS",
            "LITELLM_MENU_CODEX_COMPACTION_START_TIMEOUT_SECONDS",
            "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS",
            "LITELLM_MENU_WEB_SEARCH_READ_RESULTS",
            "LITELLM_MENU_WEB_SEARCH_READ_CHARS",
            "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND",
            "LITELLM_MENU_WEB_SEARCH_REGION",
            "LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS",
            "LITELLM_MENU_WEB_SEARCH_MAX_QUERIES",
            "LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES",
            "LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE",
            "LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES",
            "LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS",
            "LITELLM_MENU_IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS",
            "LITELLM_MENU_ROUTE_TRACE_PREVIEW_CHARS",
            "LITELLM_USE_SYSTEM_PROXIES",
        }
        sources = {
            "loader": ENVIRONMENT_SOURCE.read_text(encoding="utf-8"),
            "direct process": PROCESS_SOURCE.read_text(encoding="utf-8"),
            "launchd": LAUNCHD_SOURCE.read_text(encoding="utf-8"),
        }
        for key in sorted(propagated_keys):
            for source_name, source in sources.items():
                with self.subTest(key=key, source=source_name):
                    self.assertIn(key, source)

    def test_web_search_settings_save_and_reload_with_internal_backend_space(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            values = {
                "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "11",
                "LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "5",
                "LITELLM_MENU_WEB_SEARCH_READ_CHARS": "1800",
                "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND": "brave bing",
                "LITELLM_MENU_WEB_SEARCH_REGION": "cn-zh",
                "LITELLM_MENU_WEB_SEARCH_MAX_ROUNDS": "7",
                "LITELLM_MENU_WEB_SEARCH_MAX_QUERIES": "20",
                "LITELLM_MENU_WEB_SEARCH_MAX_OPEN_PAGES": "9",
                "LITELLM_MENU_WEB_SEARCH_MAX_FIND_IN_PAGE": "13",
                "LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRIES": "3",
                "LITELLM_MENU_EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS": "2.5",
            }
            configured = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps({"values": values}),
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)

            settings_file = temp / "runtime" / "runtime-settings.env"
            saved_text = settings_file.read_text(encoding="utf-8")
            self.assertIn(
                "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND=brave bing\n",
                saved_text,
            )

            reloaded = self.run_control(temp, "runtime-settings")
            self.assertEqual(reloaded.returncode, 0, reloaded.stderr)
            payload = json.loads(reloaded.stdout)
            reloaded_values = {
                item["key"]: str(item["value"])
                for item in payload["settings"]
                if item["key"] in values
            }
            self.assertEqual(reloaded_values, values)

    def test_runtime_setting_strings_reject_hash_and_line_breaks(self) -> None:
        for dangerous in ("hello#world", "hello\nworld", "hello\rworld"):
            with self.subTest(dangerous=repr(dangerous)), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                configured = self.run_control(
                    temp,
                    "runtime-settings-configure",
                    input_text=json.dumps(
                        {
                            "values": {
                                "LITELLM_MENU_VISION_BRIDGE_PROMPT": dangerous,
                            }
                        }
                    ),
                )
                self.assertNotEqual(configured.returncode, 0)
                self.assertFalse(
                    (temp / "runtime" / "runtime-settings.env").exists()
                )

    def test_runtime_settings_loader_preserves_spaces_and_rejects_hash_values(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            settings_file = runtime_root / "runtime-settings.env"
            settings_file.write_text(
                "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND=brave bing\n"
                "LITELLM_MENU_VISION_BRIDGE_PROMPT=unsafe#truncated\n",
                encoding="utf-8",
            )

            result = self.run_control(temp, "runtime-settings")
            self.assertEqual(result.returncode, 0, result.stderr)
            payload = json.loads(result.stdout)
            by_key = {item["key"]: item for item in payload["settings"]}
            self.assertEqual(
                by_key["LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND"]["value"],
                "brave bing",
            )
            self.assertEqual(
                by_key["LITELLM_MENU_VISION_BRIDGE_PROMPT"]["value"],
                by_key["LITELLM_MENU_VISION_BRIDGE_PROMPT"]["default"],
            )
            self.assertFalse(
                by_key["LITELLM_MENU_VISION_BRIDGE_PROMPT"]["configured"]
            )


if __name__ == "__main__":
    unittest.main()
