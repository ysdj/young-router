from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"
RETAIN_EXISTING_VALUE = "__LITELLM_MENU_RETAIN_EXISTING__"
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
                "LITELLM_PORT": "49239",
                "LITELLM_LAUNCH_AGENT_LABEL": "menu.litellm.service.runtime-settings-test",
                "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.runtime-settings-test",
                "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.runtime-settings-test",
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

    def run_runtime_settings_transaction(
        self,
        temp: Path,
        function: str,
        *,
        input_text: str,
        env_overrides: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return self.run_control(
            temp,
            function.replace("_", "-"),
            input_text=input_text,
            env_overrides=env_overrides,
        )

    @staticmethod
    def write_transaction_control(
        path: Path,
        calls_path: Path,
        *,
        fail_watch_attempts: tuple[int, ...] = (),
        fail_restart_attempts: tuple[int, ...] = (),
    ) -> None:
        path.write_text(
            textwrap.dedent(
                f"""
                #!/bin/bash
                set -euo pipefail
                action="${{1:-}}"
                printf '%s|%s|%s|%s\n' \
                  "$action" \
                  "${{LITELLM_PORT:-}}" \
                  "${{LITELLM_MENU_VISION_BRIDGE_API_KEY:-}}" \
                  "${{LITELLM_CONFIG_WATCH_INTERVAL:-}}" >> {calls_path}
                count="$(awk -F'|' -v action="$action" '$1 == action {{ count++ }} END {{ print count + 0 }}' {calls_path})"
                case "$action:$count" in
                  {"|".join(f"config-watch-ensure:{attempt}" for attempt in fail_watch_attempts) or "never"}) exit 1 ;;
                  {"|".join(f"restart:{attempt}" for attempt in fail_restart_attempts) or "never"}) exit 1 ;;
                esac
                """
            ).lstrip(),
            encoding="utf-8",
        )
        path.chmod(0o700)

    def run_runtime_settings(self, env_overrides: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            return self.run_control(
                temp,
                "runtime-settings",
                env_overrides=env_overrides,
            )

    @staticmethod
    def setting(payload: dict[str, object], key: str) -> dict[str, object]:
        return next(
            item
            for item in payload["settings"]  # type: ignore[index]
            if item["key"] == key
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

    def test_runtime_settings_never_returns_vision_api_key_value(self) -> None:
        secret = "synthetic-secret-token"
        result = self.run_runtime_settings(
            {"LITELLM_MENU_VISION_BRIDGE_API_KEY": secret}
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn(secret, result.stdout)
        payload = json.loads(result.stdout)
        api_key = self.setting(payload, "LITELLM_MENU_VISION_BRIDGE_API_KEY")
        self.assertEqual(api_key["value"], RETAIN_EXISTING_VALUE)
        self.assertEqual(api_key["default"], "")
        self.assertFalse(api_key["configured"])
        self.assertTrue(api_key["secret"])
        self.assertEqual(api_key["retain_existing"], RETAIN_EXISTING_VALUE)

    def test_runtime_settings_redacts_saved_vision_api_key(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            secret = "synthetic-saved-secret"
            (runtime_root / "runtime-settings.env").write_text(
                f"LITELLM_MENU_VISION_BRIDGE_API_KEY={secret}\n",
                encoding="utf-8",
            )

            result = self.run_control(temp, "runtime-settings")

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn(secret, result.stdout)
            payload = json.loads(result.stdout)
            api_key = self.setting(payload, "LITELLM_MENU_VISION_BRIDGE_API_KEY")
            self.assertEqual(api_key["value"], RETAIN_EXISTING_VALUE)
            self.assertTrue(api_key["configured"])

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

    def test_runtime_settings_patch_preserves_saved_values_and_never_copies_env_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            settings_file = runtime_root / "runtime-settings.env"
            saved_secret = "synthetic-saved-secret"
            environment_secret = "synthetic-environment-secret"
            settings_file.write_text(
                "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS=11\n"
                f"LITELLM_MENU_VISION_BRIDGE_API_KEY={saved_secret}\n",
                encoding="utf-8",
            )

            configured = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps({"values": {"LITELLM_PORT": "49240"}}),
                env_overrides={
                    "LITELLM_MENU_VISION_BRIDGE_API_KEY": environment_secret,
                },
            )

            self.assertEqual(configured.returncode, 0, configured.stderr)
            self.assertNotIn(saved_secret, configured.stdout)
            self.assertNotIn(environment_secret, configured.stdout)
            saved_text = settings_file.read_text(encoding="utf-8")
            self.assertIn("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS=11\n", saved_text)
            self.assertIn(f"LITELLM_MENU_VISION_BRIDGE_API_KEY={saved_secret}\n", saved_text)
            self.assertIn("LITELLM_PORT=49240\n", saved_text)
            self.assertNotIn(environment_secret, saved_text)
            response = json.loads(configured.stdout)
            self.assertEqual(
                response,
                {
                    "path": str(settings_file),
                    "saved_keys": [
                        "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS",
                        "LITELLM_MENU_VISION_BRIDGE_API_KEY",
                        "LITELLM_PORT",
                    ],
                },
            )

    def test_runtime_settings_retain_marker_preserves_only_saved_secret(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            settings_file = runtime_root / "runtime-settings.env"
            secret = "synthetic-saved-secret"
            settings_file.write_text(
                f"LITELLM_MENU_VISION_BRIDGE_API_KEY={secret}\n",
                encoding="utf-8",
            )

            retained = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_MENU_VISION_BRIDGE_API_KEY": RETAIN_EXISTING_VALUE,
                            "LITELLM_PORT": "49240",
                        }
                    }
                ),
            )

            self.assertEqual(retained.returncode, 0, retained.stderr)
            self.assertNotIn(secret, retained.stdout)
            saved_text = settings_file.read_text(encoding="utf-8")
            self.assertIn(f"LITELLM_MENU_VISION_BRIDGE_API_KEY={secret}\n", saved_text)

            settings_file.unlink()
            environment_secret = "synthetic-environment-only-secret"
            not_copied = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_MENU_VISION_BRIDGE_API_KEY": RETAIN_EXISTING_VALUE,
                            "LITELLM_PORT": "49240",
                        }
                    }
                ),
                env_overrides={
                    "LITELLM_MENU_VISION_BRIDGE_API_KEY": environment_secret,
                },
            )
            self.assertEqual(not_copied.returncode, 0, not_copied.stderr)
            self.assertNotIn(environment_secret, not_copied.stdout)
            self.assertNotIn(environment_secret, settings_file.read_text(encoding="utf-8"))

    def test_runtime_settings_empty_secret_and_default_value_clear_saved_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            settings_file = runtime_root / "runtime-settings.env"
            settings_file.write_text(
                "LITELLM_MENU_VISION_BRIDGE_API_KEY=synthetic-secret\n"
                "LITELLM_PORT=49240\n"
                "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS=11\n",
                encoding="utf-8",
            )

            configured = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_MENU_VISION_BRIDGE_API_KEY": "",
                            "LITELLM_PORT": "4000",
                        }
                    }
                ),
            )

            self.assertEqual(configured.returncode, 0, configured.stderr)
            saved_text = settings_file.read_text(encoding="utf-8")
            self.assertNotIn("VISION_BRIDGE_API_KEY", saved_text)
            self.assertNotIn("LITELLM_PORT=", saved_text)
            self.assertIn("LITELLM_MENU_WEB_SEARCH_MAX_RESULTS=11\n", saved_text)
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)

            cleared_last_value = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps(
                    {"values": {"LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "8"}}
                ),
            )
            self.assertEqual(
                cleared_last_value.returncode,
                0,
                cleared_last_value.stderr,
            )
            self.assertFalse(settings_file.exists())
            self.assertEqual(json.loads(cleared_last_value.stdout)["saved_keys"], [])

    def test_runtime_settings_patch_normalizes_all_supported_kinds(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            configured = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_PORT": "049240",
                            "LITELLM_CONFIG_WATCH_SETTLE_INTERVAL": "2.500000",
                            "LITELLM_MENU_LOG_MAX_BYTES": "0.5",
                            "LITELLM_USE_SYSTEM_PROXIES": "yes",
                            "LITELLM_MENU_VISION_BRIDGE_BACKEND": "LOCAL",
                            "LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND": "brave bing",
                        }
                    }
                ),
            )

            self.assertEqual(configured.returncode, 0, configured.stderr)
            saved_text = (temp / "runtime" / "runtime-settings.env").read_text(
                encoding="utf-8"
            )
            self.assertIn("LITELLM_PORT=49240\n", saved_text)
            self.assertIn("LITELLM_CONFIG_WATCH_SETTLE_INTERVAL=2.5\n", saved_text)
            self.assertIn("LITELLM_MENU_LOG_MAX_BYTES=524288\n", saved_text)
            self.assertIn("LITELLM_USE_SYSTEM_PROXIES=1\n", saved_text)
            self.assertIn("LITELLM_MENU_VISION_BRIDGE_BACKEND=local\n", saved_text)
            self.assertIn("LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND=brave bing\n", saved_text)

    def test_runtime_settings_patch_rejects_invalid_values_without_replacing_file(self) -> None:
        invalid_values = (
            {"LITELLM_PORT": "1.0"},
            {"LITELLM_CONFIG_WATCH_INTERVAL": "1e2"},
            {"LITELLM_MENU_LOG_MAX_BYTES": ".5"},
            {"LITELLM_USE_SYSTEM_PROXIES": "maybe"},
            {"LITELLM_MENU_VISION_BRIDGE_BACKEND": "unknown"},
            {"LITELLM_MENU_WEB_SEARCH_REGION": "us en"},
            {"LITELLM_MENU_WEB_SEARCH_DDGS_BACKEND": RETAIN_EXISTING_VALUE},
            {"UNKNOWN_RUNTIME_SETTING": "1"},
        )
        for values in invalid_values:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                runtime_root = temp / "runtime"
                runtime_root.mkdir()
                settings_file = runtime_root / "runtime-settings.env"
                original = "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS=11\n"
                settings_file.write_text(original, encoding="utf-8")

                configured = self.run_control(
                    temp,
                    "runtime-settings-configure",
                    input_text=json.dumps({"values": values}),
                )

                self.assertNotEqual(configured.returncode, 0)
                self.assertEqual(settings_file.read_text(encoding="utf-8"), original)

    def test_runtime_settings_patch_validates_web_search_read_results_against_final_max(self) -> None:
        invalid_values = (
            {"LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "3"},
            {"LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "9"},
            {
                "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "5",
                "LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "6",
            },
        )
        for values in invalid_values:
            with self.subTest(values=values), tempfile.TemporaryDirectory() as temp_dir:
                temp = Path(temp_dir)
                configured = self.run_control(
                    temp,
                    "runtime-settings-configure",
                    input_text=json.dumps({"values": values}),
                )
                self.assertNotEqual(configured.returncode, 0)
                self.assertIn("cannot exceed", configured.stderr)
                self.assertFalse((temp / "runtime" / "runtime-settings.env").exists())

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            configured = self.run_control(
                temp,
                "runtime-settings-configure",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS": "5",
                            "LITELLM_MENU_WEB_SEARCH_READ_RESULTS": "5",
                        }
                    }
                ),
            )
            self.assertEqual(configured.returncode, 0, configured.stderr)

    def test_runtime_settings_save_reloads_new_settings_without_restart(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            calls = temp / "calls.log"
            calls.touch()
            fake_control = temp / "control.sh"
            self.write_transaction_control(fake_control, calls)

            result = self.run_runtime_settings_transaction(
                temp,
                "runtime_settings_save",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_PORT": "49240",
                            "LITELLM_CONFIG_WATCH_INTERVAL": "7",
                        }
                    }
                ),
                env_overrides={
                    "LITELLM_RUNTIME_SETTINGS_CONTROL_PATH": str(fake_control),
                    "LITELLM_PORT": "49999",
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "Runtime settings saved.")
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                ["config-watch-ensure|49240||7"],
            )
            self.assertEqual(
                (runtime_root / "runtime-settings.env").stat().st_mode & 0o777,
                0o600,
            )

    def test_runtime_settings_apply_reloads_new_settings_and_restarts(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            calls = temp / "calls.log"
            calls.touch()
            fake_control = temp / "control.sh"
            self.write_transaction_control(fake_control, calls)

            result = self.run_runtime_settings_transaction(
                temp,
                "runtime_settings_apply",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_PORT": "49240",
                            "LITELLM_MENU_VISION_BRIDGE_API_KEY": "new-secret",
                        }
                    }
                ),
                env_overrides={
                    "LITELLM_RUNTIME_SETTINGS_CONTROL_PATH": str(fake_control),
                },
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("new-secret", result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "Runtime settings saved and applied.")
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    "config-watch-ensure|49240|new-secret|",
                    "restart|49240|new-secret|",
                ],
            )
            backups = list(runtime_root.glob(".runtime-settings.env.backup.*"))
            self.assertEqual(backups, [])

    def test_runtime_settings_apply_restart_failure_rolls_back_and_restarts_old_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            settings_file = runtime_root / "runtime-settings.env"
            old_secret = "old-secret"
            original = (
                "LITELLM_PORT=49231\n"
                f"LITELLM_MENU_VISION_BRIDGE_API_KEY={old_secret}\n"
            )
            settings_file.write_text(original, encoding="utf-8")
            settings_file.chmod(0o600)
            calls = temp / "calls.log"
            calls.touch()
            fake_control = temp / "control.sh"
            self.write_transaction_control(
                fake_control,
                calls,
                fail_restart_attempts=(1,),
            )

            result = self.run_runtime_settings_transaction(
                temp,
                "runtime_settings_apply",
                input_text=json.dumps(
                    {
                        "values": {
                            "LITELLM_PORT": "49240",
                            "LITELLM_MENU_VISION_BRIDGE_API_KEY": "new-secret",
                        }
                    }
                ),
                env_overrides={
                    "LITELLM_RUNTIME_SETTINGS_CONTROL_PATH": str(fake_control),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("old-secret", result.stdout + result.stderr)
            self.assertNotIn("new-secret", result.stdout + result.stderr)
            self.assertIn("rolled back", result.stderr)
            self.assertEqual(settings_file.read_text(encoding="utf-8"), original)
            self.assertEqual(settings_file.stat().st_mode & 0o777, 0o600)
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    "config-watch-ensure|49240|new-secret|",
                    "restart|49240|new-secret|",
                    "config-watch-ensure|49231|old-secret|",
                    "restart|49231|old-secret|",
                ],
            )
            self.assertEqual(
                list(runtime_root.glob(".runtime-settings.env.backup.*")),
                [],
            )

    def test_runtime_settings_save_watch_failure_restores_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_root.mkdir()
            settings_file = runtime_root / "runtime-settings.env"
            calls = temp / "calls.log"
            calls.touch()
            fake_control = temp / "control.sh"
            self.write_transaction_control(
                fake_control,
                calls,
                fail_watch_attempts=(1,),
            )

            result = self.run_runtime_settings_transaction(
                temp,
                "runtime_settings_save",
                input_text=json.dumps({"values": {"LITELLM_PORT": "49240"}}),
                env_overrides={
                    "LITELLM_RUNTIME_SETTINGS_CONTROL_PATH": str(fake_control),
                },
            )

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("rolled back", result.stderr)
            self.assertFalse(settings_file.exists())
            self.assertEqual(
                calls.read_text(encoding="utf-8").splitlines(),
                [
                    "config-watch-ensure|49240||",
                    "config-watch-ensure|||",
                ],
            )
            self.assertEqual(
                list(runtime_root.glob(".runtime-settings.env.backup.*")),
                [],
            )

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
                "LITELLM_MENU_VISION_BRIDGE_PROMPT=unsafe#truncated\n"
                f"LITELLM_MENU_VISION_BRIDGE_API_KEY={RETAIN_EXISTING_VALUE}\n",
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
            self.assertEqual(
                by_key["LITELLM_MENU_VISION_BRIDGE_API_KEY"]["value"],
                "",
            )
            self.assertFalse(
                by_key["LITELLM_MENU_VISION_BRIDGE_API_KEY"]["configured"]
            )


if __name__ == "__main__":
    unittest.main()
