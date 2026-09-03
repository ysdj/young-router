from __future__ import annotations

import base64
import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from litellm_menu.core import CoreStore
from litellm_menu.core.domains.providers_models import ProvidersModelsDomain
from litellm_menu.core.domains.relay_accounts import RelayAccountsDomain
from litellm_menu.core.domains.runtime import RuntimeSettingsDomain
from litellm_menu.core.domains.webdav import WebDAVSettingsDomain
from litellm_menu.core.domains._shared import DomainError
from litellm_menu.core.operations import CoreServiceController
from litellm_menu.core.persistence import PersistenceError


PROVIDER_CONFIG = """
providers:
  primary:
    api_base: "https://example.test/v1"
    api_keys:
      - name: default
        value: "replace-me-secret"
model_list: []
"""


class CoreOperationsTests(unittest.TestCase):
    @staticmethod
    def _configured_webdav_domain(root: Path, config: Path) -> WebDAVSettingsDomain:
        domain = WebDAVSettingsDomain(
            root / "webdav.json",
            enabled_path=root / "webdav.enabled",
            status_path=root / "webdav-status.json",
            config_path=config,
            state_path=root / "webdav-state.json",
        )
        domain.stage_secret("password", None, "replace-webdav-password")
        domain.dispatch(
            "patch",
            {
                "url": "https://webdav.example.test/config/",
                "username": "person",
                "remote_name": "menu-config.json",
            },
        )
        domain.apply()
        return domain

    def test_reload_restarts_the_managed_service(self) -> None:
        controller = CoreServiceController("/tmp/unused-runtime")
        expected = {"state": "running"}
        with mock.patch.object(controller, "_pid", return_value=4811), mock.patch.object(
            controller, "restart", return_value=expected
        ) as restart:
            self.assertEqual(expected, controller.reload())

        restart.assert_called_once_with()

    @unittest.skipIf(os.name == "nt", "POSIX process states are unavailable on Windows")
    def test_process_alive_rejects_zombies(self) -> None:
        zombie = subprocess.CompletedProcess(
            ["ps", "-o", "stat=", "-p", "4811"],
            returncode=0,
            stdout="Z+\n",
            stderr="",
        )
        sleeping = subprocess.CompletedProcess(
            ["ps", "-o", "stat=", "-p", "4811"],
            returncode=0,
            stdout="S+\n",
            stderr="",
        )
        with mock.patch("litellm_menu.core.operations.subprocess.run", return_value=zombie):
            self.assertFalse(CoreServiceController._process_alive(4811))
        with mock.patch("litellm_menu.core.operations.subprocess.run", return_value=sleeping):
            self.assertTrue(CoreServiceController._process_alive(4811))

    def test_windows_batch_launcher_uses_the_bundled_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(
                directory,
                python=r"C:\\LiteLLM Menu\\Core\\runtime\\python.exe",
                litellm_bin=r"C:\\LiteLLM Menu\\Core\\runtime\\bin\\litellm.cmd",
            )
            process = mock.Mock(pid=4812)
            process.poll.return_value = None
            with mock.patch("litellm_menu.core.operations.os.name", "nt"), mock.patch.object(
                controller, "status", return_value={"state": "stopped"}
            ), mock.patch.object(controller, "_stage_runtime_config"), mock.patch.object(
                controller, "_runtime_env", return_value={"LITELLM_PORT": "4000", "LITELLM_NUM_WORKERS": "1"}
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", return_value=process) as popen:
                controller.start()

            command = popen.call_args.args[0]
            self.assertEqual(controller.python, command[0])
            self.assertEqual("-c", command[1])
            self.assertIn("from litellm import run_server", command[2])
            self.assertNotIn(controller.litellm_bin, command)

    def test_macos_uses_configured_forkserver_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            process = mock.Mock(pid=4813)
            process.poll.return_value = None
            with mock.patch("litellm_menu.core.operations.os.name", "posix"), mock.patch(
                "litellm_menu.core.operations.sys.platform", "darwin"
            ), mock.patch.object(controller, "status", return_value={"state": "stopped"}), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller,
                "_runtime_env",
                return_value={"LITELLM_PORT": "4000", "LITELLM_NUM_WORKERS": "16"},
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", return_value=process) as popen:
                controller.start()

            command = popen.call_args.args[0]
            self.assertEqual([controller.python, "-m", "litellm_menu.macos_proxy"], command[:3])
            workers_index = command.index("--workers")
            self.assertEqual("16", command[workers_index + 1])
            self.assertNotIn("--run_gunicorn", command)
            self.assertNotIn(controller.litellm_bin, command)

    def test_macos_defaults_to_sixteen_forkserver_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            process = mock.Mock(pid=4815)
            process.poll.return_value = None
            with mock.patch("litellm_menu.core.operations.os.name", "posix"), mock.patch(
                "litellm_menu.core.operations.sys.platform", "darwin"
            ), mock.patch.object(controller, "status", return_value={"state": "stopped"}), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller,
                "_runtime_env",
                return_value={"LITELLM_PORT": "4000"},
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", return_value=process) as popen:
                controller.start()

            command = popen.call_args.args[0]
            workers_index = command.index("--workers")
            self.assertEqual("16", command[workers_index + 1])
            self.assertNotIn("--run_gunicorn", command)

    def test_non_macos_posix_retains_gunicorn_workers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            process = mock.Mock(pid=4816)
            process.poll.return_value = None
            with mock.patch("litellm_menu.core.operations.os.name", "posix"), mock.patch(
                "litellm_menu.core.operations.sys.platform", "linux"
            ), mock.patch.object(controller, "status", return_value={"state": "stopped"}), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller,
                "_runtime_env",
                return_value={"LITELLM_PORT": "4000", "LITELLM_NUM_WORKERS": "16"},
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", return_value=process) as popen:
                controller.start()

            self.assertIn("--run_gunicorn", popen.call_args.args[0])

    def test_service_dispatch_projects_real_status_and_autostart(self) -> None:
        calls: list[str] = []

        def handler(operation: str) -> dict[str, object]:
            calls.append(operation)
            return {
                "state": "running",
                "pid": 123,
                "port": 49173,
                "auto_start_state": "enabled",
                "route_recovery": {"recovering": 1},
            }

        core = CoreStore(service_handlers={"health": handler})
        core.dispatch({"type": "service.health"})
        service = core.snapshot()["service"]

        self.assertEqual(["health"], calls)
        self.assertEqual("running", service["state"])
        self.assertEqual(49173, service["port"])
        self.assertEqual("enabled", service["auto_start_state"])
        self.assertEqual(1, service["route_recovery"]["recovering"])

    def test_recovery_summary_counts_only_live_recoveries_and_cooldowns(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.recovery.parent.mkdir(parents=True)
            now = datetime.now(timezone.utc)
            controller.paths.recovery.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "live": {"heartbeat_at": (now - timedelta(seconds=5)).isoformat()},
                            "stale": {"heartbeat_at": (now - timedelta(minutes=5)).isoformat()},
                        }
                    }
                ),
                encoding="utf-8",
            )
            controller.paths.cooldowns.write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "active": {"cooldown_until": now.timestamp() + 60},
                            "expired": {"cooldown_until": now.timestamp() - 60},
                        }
                    }
                ),
                encoding="utf-8",
            )

            summary = controller._recovery_summary()

            self.assertEqual(1, summary["recovering"])
            self.assertEqual(1, summary["cooldown"])

    def test_new_proxy_start_clears_previous_recovery_and_cooldown_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.recovery.parent.mkdir(parents=True)
            controller.paths.recovery.write_text('{"recoveries":{"old":{}}}', encoding="utf-8")
            controller.paths.cooldowns.write_text('{"cooldowns":{"old":{}}}', encoding="utf-8")
            process = mock.Mock(pid=4817)
            process.poll.return_value = None

            def spawn(*_args: object, **_kwargs: object) -> mock.Mock:
                self.assertFalse(controller.paths.recovery.exists())
                self.assertFalse(controller.paths.cooldowns.exists())
                return process

            with mock.patch.object(controller, "status", return_value={"state": "stopped"}), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller,
                "_runtime_env",
                return_value={"LITELLM_PORT": "4000", "LITELLM_NUM_WORKERS": "16"},
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", side_effect=spawn):
                controller.start()

            self.assertFalse(controller.paths.recovery.exists())
            self.assertFalse(controller.paths.cooldowns.exists())

    def test_repeated_start_of_running_proxy_keeps_current_routing_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.recovery.parent.mkdir(parents=True)
            controller.paths.recovery.write_text('{"recoveries":{"live":{}}}', encoding="utf-8")
            controller.paths.cooldowns.write_text('{"cooldowns":{"live":{}}}', encoding="utf-8")
            running = {"state": "running", "pid": 4818, "port": 4000}

            with mock.patch.object(controller, "status", return_value=running), mock.patch(
                "litellm_menu.core.operations.subprocess.Popen"
            ) as popen:
                self.assertEqual(running, controller.start())

            popen.assert_not_called()
            self.assertTrue(controller.paths.recovery.exists())
            self.assertTrue(controller.paths.cooldowns.exists())

    def test_unchanged_service_health_does_not_publish_a_new_revision(self) -> None:
        status = {
            "state": "running",
            "pid": 123,
            "port": 49173,
            "auto_start_state": "enabled",
            "route_recovery": {"recovering": 0},
        }
        core = CoreStore(service_handlers={"health": lambda _operation: dict(status)})

        core.dispatch({"type": "service.health"})
        revision = core.revision
        events: list[dict[str, object]] = []
        unsubscribe = core.subscribe(events.append)
        self.addCleanup(unsubscribe)

        core.dispatch({"type": "service.health"})

        self.assertEqual(revision, core.revision)
        self.assertEqual([], events)

    def test_snapshot_projects_live_service_status_without_persisting_a_transition(self) -> None:
        status = {
            "state": "running",
            "pid": 123,
            "port": 49173,
            "auto_start_state": "enabled",
        }
        calls: list[str] = []

        def handler(operation: str) -> dict[str, object]:
            calls.append(operation)
            return dict(status)

        with tempfile.TemporaryDirectory() as directory:
            metadata = Path(directory) / "core-state.json"
            prior_core = CoreStore(metadata_path=metadata)
            prior_core.set_service_status("starting")
            core = CoreStore(metadata_path=metadata, service_handlers={"status": handler})
            revision = core.revision
            persisted = metadata.read_bytes()
            events: list[dict[str, object]] = []
            unsubscribe = core.subscribe(events.append)
            self.addCleanup(unsubscribe)

            snapshot = core.snapshot()

            self.assertEqual(["status"], calls)
            self.assertEqual("running", snapshot["service"]["state"])
            self.assertEqual(49173, snapshot["service"]["port"])
            self.assertEqual(revision, core.revision)
            self.assertEqual(persisted, metadata.read_bytes())
            self.assertEqual([], events)

    def test_default_core_exposes_stopped_service_before_starting_the_proxy(self) -> None:
        with mock.patch("litellm_menu.core.operations.CoreServiceController") as controller_type:
            controller = controller_type.return_value
            controller.status.return_value = {"state": "stopped"}
            controller.dispatch.return_value = {"state": "stopped"}

            core = CoreStore.with_default_domains(runtime_root="/tmp/litellm-menu-core-start")

        controller.start.assert_not_called()
        self.assertEqual("stopped", core.snapshot()["service"]["state"])

    def test_new_app_core_resets_transient_routing_state(self) -> None:
        with mock.patch("litellm_menu.core.operations.CoreServiceController") as controller_type:
            controller = controller_type.return_value
            controller.status.return_value = {"state": "stopped"}
            controller.dispatch.return_value = {"state": "stopped"}

            CoreStore.with_default_domains(
                runtime_root="/tmp/litellm-menu-core-new-app",
                reset_transient_routing_state=True,
            )

        controller.reset_transient_routing_state.assert_called_once_with()

    def test_default_core_exposes_a_non_running_service_for_diagnostics(self) -> None:
        with mock.patch("litellm_menu.core.operations.CoreServiceController") as controller_type:
            controller = controller_type.return_value
            controller.status.return_value = {"state": "unknown"}
            controller.dispatch.return_value = {"state": "unknown"}

            core = CoreStore.with_default_domains(runtime_root="/tmp/litellm-menu-core-unavailable")

        controller.start.assert_not_called()
        self.assertEqual("unknown", core.snapshot()["service"]["state"])

    def test_controller_status_and_autostart_use_private_runtime_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            with mock.patch.object(controller, "_health", return_value=False):
                self.assertEqual("stopped", controller.status()["state"])
            self.assertEqual("disabled", controller.autostart_status())
            controller.autostart_enable()
            with mock.patch.object(controller, "_health", return_value=False):
                self.assertEqual("enabled", controller.status()["auto_start_state"])
            self.assertEqual(0o600, controller.paths.autostart.stat().st_mode & 0o777)
            controller.autostart_disable()
            self.assertEqual("disabled", controller.autostart_status())

    def test_snapshot_status_reuses_recent_probe_but_health_forces_refresh(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            with mock.patch.object(controller, "_pid", side_effect=[4819, None]) as pid, mock.patch.object(
                controller, "_health", side_effect=[True, False]
            ) as health, mock.patch.object(
                controller, "_recorded_proxy_is_orphaned", return_value=None
            ):
                first = controller.dispatch("status")
                cached = controller.dispatch("status")
                refreshed = controller.dispatch("health")

            self.assertEqual("running", first["state"])
            self.assertEqual(first, cached)
            self.assertEqual("stopped", refreshed["state"])
            self.assertEqual(2, pid.call_count)
            self.assertEqual(2, health.call_count)

    def test_controller_status_exposes_the_configured_port_only_while_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime-settings.env").write_text("LITELLM_PORT=49173\n", encoding="utf-8")
            controller = CoreServiceController(root)

            with mock.patch.object(controller, "_pid", return_value=1234), mock.patch.object(
                controller, "_health", return_value=True
            ):
                running = controller.status()
            self.assertEqual("running", running["state"])
            self.assertEqual(49173, running["port"])

            with mock.patch.object(controller, "_pid", return_value=None), mock.patch.object(
                controller, "_health", return_value=False
            ):
                stopped = controller.status()
            self.assertEqual("stopped", stopped["state"])
            self.assertNotIn("port", stopped)

            with mock.patch.object(controller, "_pid", return_value=None), mock.patch.object(
                controller, "_health", return_value=True
            ):
                unknown = controller.status()
            self.assertEqual("unknown", unknown["state"])
            self.assertNotIn("port", unknown)

    def test_controller_projects_the_latest_webdav_result_without_overwriting_a_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.webdav_enabled.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.webdav_enabled.write_text("1\n", encoding="utf-8")
            controller.paths.webdav_sync_state.write_text(
                json.dumps({"updated_at": "2026-07-29T04:42:00Z", "action": "sync"}),
                encoding="utf-8",
            )
            controller.paths.webdav_status.write_text(
                json.dumps({"checked_at": "2026-07-29T04:43:00Z", "action": "probe", "ok": False}),
                encoding="utf-8",
            )

            summary = controller._webdav_summary()

            self.assertEqual(
                {"enabled": True, "ok": False, "checked_at": "2026-07-29T04:43:00Z", "action": "probe"},
                summary,
            )

    def test_controller_uses_the_successful_baseline_only_when_no_status_exists(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.webdav_sync_state.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.webdav_sync_state.write_text(
                json.dumps({"updated_at": "2026-07-29T04:42:00Z", "action": "sync"}),
                encoding="utf-8",
            )

            summary = controller._webdav_summary()

            self.assertEqual(
                {"enabled": False, "ok": True, "checked_at": "2026-07-29T04:42:00Z", "action": "sync"},
                summary,
            )

    def test_webdav_probe_refreshes_the_service_menu_projection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / ".litellm-runtime" / "webdav-sync-status.json"
            domain = WebDAVSettingsDomain(root / "webdav.json", enabled_path=root / "enabled", status_path=status_path)
            domain.dispatch("patch", {"url": "https://example.test/webdav/", "remote_name": "config.json"})
            controller = CoreServiceController(root)
            core = CoreStore(
                domains=[domain],
                service_handlers={"status": lambda _operation: {"state": "stopped", "webdav": controller._webdav_summary()}},
            )

            with mock.patch("webdav.core.WebDAVClient.head", return_value=(200, {})), mock.patch(
                "webdav.core.WebDAVClient.try_mkcol"
            ):
                core.probe(domain="webdav")

            menu_webdav = core.snapshot()["service"]["webdav"]
            self.assertTrue(menu_webdav["ok"])
            self.assertEqual("probe", menu_webdav["action"])
            self.assertIsInstance(menu_webdav["checked_at"], str)

    def test_controller_uses_validated_saved_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime-settings.env").write_text(
                "LITELLM_PORT=49173\n"
                "LITELLM_NUM_WORKERS=3\n"
                "LITELLM_CONFIG_WATCH_INTERVAL=5\n"
                "LITELLM_CONFIG_WATCH_SETTLE_INTERVAL=2\n",
                encoding="utf-8",
            )
            controller = CoreServiceController(
                root,
                environment={
                    "LITELLM_MENU_VISION_BRIDGE_API_KEY": "test-inherited-secret",
                    "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON": '{"enabled":false}',
                    "LITELLM_MENU_VISION_ROUTER_CONFIG_JSON": '{"enabled":false}',
                },
            )
            environment = controller._runtime_env()

            self.assertEqual("49173", environment["LITELLM_PORT"])
            self.assertEqual("3", environment["LITELLM_NUM_WORKERS"])
            self.assertNotIn("LITELLM_CONFIG_WATCH_INTERVAL", environment)
            self.assertNotIn("LITELLM_CONFIG_WATCH_SETTLE_INTERVAL", environment)
            self.assertNotIn("LITELLM_MENU_VISION_BRIDGE_API_KEY", environment)
            self.assertEqual(
                json.loads(environment["LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"]),
                {
                    "enabled": True,
                    "backend": "auto",
                    "freeFallback": True,
                    "timeoutSeconds": 45,
                    "maxTokens": 4096,
                    "providers": [],
                    "httpProviders": [],
                    "localOllama": {"enabled": False},
                    "localLmStudio": {"enabled": False},
                },
            )
            self.assertNotIn("LITELLM_MENU_VISION_ROUTER_CONFIG_JSON", environment)
            with mock.patch.object(controller, "_health", return_value=True):
                self.assertEqual("unknown", controller.status()["state"])

    def test_dsh_quick_runtime_settings_merge_into_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RuntimeSettingsDomain(root / "runtime-settings.env")
            json_key = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
            domain.dispatch(
                "set_setting",
                {
                    "key": json_key,
                    "value": json.dumps(
                        {
                            "enabled": True,
                            "backend": "api",
                            "freeFallback": True,
                            "timeoutSeconds": 9,
                            "maxTokens": 321,
                            "localOllama": {"enabled": False, "model": "custom-local"},
                        }
                    ),
                },
            )
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED", "value": "off"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND", "value": "local"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK", "value": "off"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", "value": "27"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS", "value": "2048"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED", "value": "off"})
            domain.apply()

            environment = CoreServiceController(root)._runtime_env()
            merged = json.loads(environment[json_key])
            self.assertEqual(
                {
                    "enabled": False,
                    "backend": "local",
                    "freeFallback": False,
                    "timeoutSeconds": 27,
                    "maxTokens": 2048,
                    "localOllama": {"enabled": False, "model": "custom-local"},
                },
                merged,
            )
            self.assertNotIn("LITELLM_MENU_DSH_VISION_ROUTER_ENABLED", environment)
            self.assertNotIn("LITELLM_MENU_DSH_VISION_ROUTER_BACKEND", environment)

    def test_dsh_json_values_remain_when_quick_settings_are_unconfigured(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RuntimeSettingsDomain(root / "runtime-settings.env")
            json_key = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
            custom = {
                "enabled": False,
                "backend": "local",
                "freeFallback": False,
                "timeoutSeconds": 9,
                "maxTokens": 321,
                "localOllama": {"enabled": True, "model": "custom-local"},
            }
            domain.dispatch("set_setting", {"key": json_key, "value": json.dumps(custom)})
            domain.apply()

            environment = CoreServiceController(root)._runtime_env()
            self.assertEqual(custom, json.loads(environment[json_key]))

    def test_dsh_quick_inherit_values_restore_advanced_json(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RuntimeSettingsDomain(root / "runtime-settings.env")
            json_key = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
            custom = {
                "enabled": False,
                "backend": "api",
                "freeFallback": False,
                "timeoutSeconds": 13,
                "maxTokens": 513,
                "localOllama": {"enabled": True, "model": "custom-local"},
            }
            domain.dispatch("set_setting", {"key": json_key, "value": json.dumps(custom)})
            # Explicit values override JSON, including values that previously
            # collided with schema defaults.
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED", "value": "on"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", "value": "45"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS", "value": "4096"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED", "value": "off"})
            domain.apply()

            overridden = json.loads(CoreServiceController(root)._runtime_env()[json_key])
            self.assertTrue(overridden["enabled"])
            self.assertEqual(45, overridden["timeoutSeconds"])
            self.assertEqual(4096, overridden["maxTokens"])
            self.assertFalse(overridden["localOllama"]["enabled"])

            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED", "value": "inherit"})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", "value": ""})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS", "value": ""})
            domain.dispatch("set_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED", "value": "inherit"})
            domain.apply()

            inherited = json.loads(CoreServiceController(root)._runtime_env()[json_key])
            self.assertEqual(custom, inherited)

    def test_dsh_inherit_and_empty_quick_values_do_not_reach_proxy_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            json_key = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
            config = '{"enabled":false,"timeoutSeconds":13}'
            encoded = base64.urlsafe_b64encode(config.encode("utf-8")).decode("ascii").rstrip("=")
            (root / "runtime-settings.env").write_text(
                f"{json_key}=base64:{encoded}\n"
                "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED=inherit\n"
                "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS=\n",
                encoding="utf-8",
            )

            environment = CoreServiceController(root)._runtime_env()
            self.assertEqual(json.loads(config), json.loads(environment[json_key]))
            self.assertNotIn("LITELLM_MENU_DSH_VISION_ROUTER_ENABLED", environment)
            self.assertNotIn("LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", environment)

    def test_dsh_quick_controls_and_json_are_one_bidirectional_draft(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RuntimeSettingsDomain(root / "runtime-settings.env")
            config_key = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
            enabled_key = "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED"
            backend_key = "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND"

            fields = {item["key"]: item for item in domain.snapshot()["fields"]}
            self.assertEqual("on", fields[enabled_key]["value"])
            self.assertEqual("on", fields[enabled_key]["default"])
            self.assertNotIn("inherit", fields[enabled_key]["options"])

            domain.dispatch("set_setting", {"key": enabled_key, "value": "off"})
            quick_updated = json.loads(domain.trusted_secret_value("setting", config_key))
            self.assertFalse(quick_updated["enabled"])
            with self.assertRaisesRegex(DomainError, "Runtime settings are invalid"):
                domain.dispatch(
                    "set_setting",
                    {"key": "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", "value": "0"},
                )

            domain.stage_secret(
                "setting",
                config_key,
                json.dumps({"enabled": True, "backend": "local", "timeoutSeconds": 12}),
            )
            fields = {item["key"]: item for item in domain.snapshot()["fields"]}
            self.assertEqual("on", fields[enabled_key]["value"])
            self.assertEqual("local", fields[backend_key]["value"])
            self.assertEqual("12", fields["LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS"]["value"])

            domain.dispatch("clear_setting", {"key": "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS"})
            cleared = json.loads(domain.trusted_secret_value("setting", config_key))
            self.assertNotIn("timeoutSeconds", cleared)

    def test_invalid_dsh_json_stops_runtime_environment_materialization(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime-settings.env").write_text(
                "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON=base64:not-valid-json\n"
                "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED=0\n",
                encoding="utf-8",
            )
            controller = CoreServiceController(root)

            with self.assertRaisesRegex(RuntimeError, "Runtime settings are invalid"):
                controller._runtime_env()

            fallback = controller._runtime_env(strict=False)
            self.assertNotIn("LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON", fallback)
            self.assertNotIn("LITELLM_MENU_DSH_VISION_ROUTER_ENABLED", fallback)

    def test_codex_descendant_cleanup_uses_runtime_settings_value(self) -> None:
        key = "LITELLM_MENU_CODEX_DESCENDANT_CLEANUP"
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = RuntimeSettingsDomain(root / "runtime-settings.env")
            domain.dispatch("set_setting", {"key": key, "value": "0"})
            domain.apply()

            saved_off = CoreServiceController(root, environment={key: "1"})
            self.assertEqual("0", saved_off._runtime_env()[key])

            domain.dispatch("set_setting", {"key": key, "value": "1"})
            domain.apply()

            restored_default = CoreServiceController(root, environment={key: "0"})
            self.assertEqual("1", restored_default._runtime_env()[key])
            self.assertNotIn(key, (root / "runtime-settings.env").read_text(encoding="utf-8"))

    def test_proxy_environment_loads_bundled_callback_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            environment = controller._runtime_env()
            core_root = Path(__file__).resolve().parents[1]

            self.assertEqual("1", environment["LITELLM_MENU_PROXY_PROCESS"])
            self.assertEqual("1", environment["LITELLM_MENU_TIMESTAMP_OUTPUT"])
            self.assertEqual(str(Path(directory) / "menu-server.log"), environment["LITELLM_MENU_SERVICE_LOG"])
            self.assertEqual(str(Path(directory) / "recent-requests.jsonl"), environment["LITELLM_RECENT_REQUESTS_LOG"])
            self.assertEqual("true", environment["LITELLM_LOCAL_MODEL_COST_MAP"])
            self.assertEqual(str(controller.paths.recovery), environment["LITELLM_MENU_ROUTE_RECOVERY_STATE_FILE"])
            self.assertEqual(str(controller.paths.cooldowns), environment["LITELLM_MENU_DEPLOYMENT_COOLDOWN_FILE"])
            self.assertEqual(
                "litellm_menu.search_endpoint:register",
                environment["LITELLM_WORKER_STARTUP_HOOKS"],
            )
            self.assertEqual(str(core_root), environment["LITELLM_TEMPLATE_ROOT"])
            self.assertEqual(str(core_root), environment["PYTHONPATH"].split(os.pathsep)[0])
            self.assertTrue((core_root / "sitecustomize.py").is_file())

    def test_explicit_controller_port_overrides_persisted_runtime_port(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = Path(directory) / "runtime-settings.env"
            settings.write_text("LITELLM_PORT=4000\n", encoding="utf-8")
            controller = CoreServiceController(
                directory,
                environment={"LITELLM_PORT": "44001"},
            )

            self.assertEqual("44001", controller._runtime_env()["LITELLM_PORT"])

    def test_service_start_appends_proxy_output_to_service_log(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            process = mock.Mock(pid=4814)
            process.poll.return_value = None
            with mock.patch.object(controller, "status", return_value={"state": "stopped"}), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller, "_runtime_env", return_value={"LITELLM_PORT": "4000", "LITELLM_NUM_WORKERS": "1"}
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", return_value=process) as popen:
                controller.start()

            service_log = Path(directory) / "menu-server.log"
            self.assertTrue(service_log.exists())
            self.assertIs(popen.call_args.kwargs["stderr"], subprocess.STDOUT)
            self.assertEqual(str(service_log), popen.call_args.kwargs["stdout"].name)

    def test_relocated_core_resolves_the_owned_callback_from_pythonpath(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            package = root / "litellm_menu"
            package.mkdir()
            (package / "__init__.py").write_text("", encoding="utf-8")
            (package / "callbacks.py").write_text("value = 'portable'\n", encoding="utf-8")
            (root / "sitecustomize.py").write_text(
                (Path(__file__).resolve().parents[1] / "sitecustomize.py").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            runtime = root / "runtime"
            runtime.mkdir()
            (runtime / "config.yaml").write_text("model_list: []\n", encoding="utf-8")
            environment = os.environ.copy()
            environment.update(
                {
                    "LITELLM_MENU_PROXY_PROCESS": "1",
                    "PYTHONPATH": str(root),
                }
            )
            completed = subprocess.run(
                [
                    sys.executable,
                    "-c",
                    (
                        "from litellm.proxy.types_utils.utils import get_instance_fn; "
                        "print(get_instance_fn('litellm_menu.callbacks.value', "
                        "config_file_path='config.yaml'))"
                    ),
                ],
                cwd=runtime,
                env=environment,
                text=True,
                capture_output=True,
                check=False,
            )

            if completed.returncode != 0 and "No module named 'litellm'" in completed.stderr:
                self.skipTest("litellm is not installed for this Python")
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual("portable", completed.stdout.strip())

    def test_owner_record_requires_the_current_core_identity(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text("4242\n", encoding="utf-8")
            controller.paths.owner.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "pid": 4242,
                        "identity": "synthetic-process-identity",
                        "token": "synthetic-service-owner-token-1234567890",
                        "core_pid": 4343,
                        "core_identity": "synthetic-core-identity",
                    }
                ),
                encoding="utf-8",
            )

            with mock.patch.object(
                controller, "_recorded_pid", return_value=4242
            ), mock.patch.object(controller, "_core_identity", return_value=(4343, "synthetic-core-identity")):
                self.assertEqual(4242, controller._pid())
            with mock.patch.object(
                controller, "_recorded_pid", return_value=4242
            ), mock.patch.object(controller, "_core_identity", return_value=(4444, "replacement-core-identity")):
                self.assertIsNone(controller._pid())

    @unittest.skipIf(os.name == "nt", "the fixture uses POSIX signals")
    def test_controller_reclaims_only_a_verified_orphan_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = CoreServiceController(directory)
            token = "synthetic-service-owner-token-1234567890"
            environment = os.environ.copy()
            environment["LITELLM_MENU_SERVICE_OWNER_TOKEN"] = token
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )

            def cleanup() -> None:
                if process.poll() is None:
                    try:
                        os.killpg(process.pid, 9)
                    except OSError:
                        process.kill()
                process.wait(timeout=2)

            self.addCleanup(cleanup)
            first._write_owner_record(process, token)
            first.paths.pid.write_text(f"{process.pid}\n", encoding="utf-8")

            replacement = CoreServiceController(directory)
            record = first._read_owner_record()
            self.assertIsNotNone(record)
            assert record is not None

            self.assertEqual(0o600, stat.S_IMODE(replacement.paths.owner.stat().st_mode))
            with mock.patch.object(
                replacement, "_core_identity", return_value=(record.core_pid + 1, "replacement-core-identity")
            ), mock.patch.object(replacement, "_recorded_pid", return_value=process.pid), mock.patch.object(
                replacement, "_process_identity", side_effect=lambda pid: (
                    record.identity if pid == process.pid else record.core_identity if pid == record.core_pid else None
                )
            ):
                self.assertIsNone(replacement._pid())
                self.assertIsNone(replacement._recorded_proxy_is_orphaned())
            with mock.patch.object(
                replacement, "_core_identity", return_value=(record.core_pid + 1, "replacement-core-identity")
            ), mock.patch.object(replacement, "_process_identity", side_effect=lambda pid: (
                record.identity if pid == process.pid else None
            )), mock.patch.object(replacement, "_posix_process_has_token", return_value=True):
                self.assertEqual(process.pid, replacement._recorded_proxy_is_orphaned())

    def test_stop_preserves_another_core_ownership_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text("4242\n", encoding="utf-8")
            controller.paths.owner.write_text("owner\n", encoding="utf-8")

            with mock.patch.object(controller, "_pid", return_value=None), mock.patch.object(
                controller, "status", return_value={"state": "unknown"}
            ) as status:
                result = controller.stop()

            self.assertEqual({"state": "unknown"}, result)
            status.assert_called_once_with()
            self.assertEqual("4242\n", controller.paths.pid.read_text(encoding="utf-8"))
            self.assertEqual("owner\n", controller.paths.owner.read_text(encoding="utf-8"))

    def test_verified_orphan_projects_as_stopped_for_normal_recovery(self) -> None:
        controller = CoreServiceController("/tmp/unused-runtime")

        with mock.patch.object(controller, "_pid", return_value=None), mock.patch.object(
            controller, "_health", return_value=True
        ), mock.patch.object(controller, "_recorded_proxy_is_orphaned", return_value=4242):
            self.assertEqual("stopped", controller.status()["state"])

    def test_unhealthy_start_cleans_the_owned_process_group(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            process = mock.Mock()
            process.pid = 4242
            process.poll.return_value = None
            controller.paths.pid.write_text("4242\n", encoding="utf-8")
            controller.paths.owner.write_text("owner\n", encoding="utf-8")

            with mock.patch.object(controller, "status", side_effect=[{"state": "stopped"}]), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(controller, "_runtime_env", return_value={"LITELLM_HEALTH_WAIT_SECONDS": "1"}), mock.patch.object(
                controller, "_configured_port", return_value=4000
            ), mock.patch(
                "litellm_menu.core.operations.subprocess.Popen", return_value=process
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=False
            ), mock.patch.object(controller, "_stop_process_group") as stop_group, mock.patch(
                "litellm_menu.core.operations.time.sleep"
            ), mock.patch(
                "litellm_menu.core.operations.time.monotonic", side_effect=[0, 0, 2]
            ):
                with self.assertRaisesRegex(RuntimeError, "did not become healthy"):
                    controller.start()

            stop_group.assert_called_once_with(4242)
            self.assertFalse(controller.paths.pid.exists())
            self.assertFalse(controller.paths.owner.exists())

    def test_start_recovers_a_recorded_zombie_proxy_without_a_listener(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            replacement = mock.Mock()
            replacement.pid = 9999
            replacement.poll.return_value = None
            controller.paths.pid.write_text("4242\n", encoding="utf-8")
            controller.paths.owner.write_text("owner\n", encoding="utf-8")

            with mock.patch.object(
                controller,
                "status",
                side_effect=[{"state": "unhealthy"}, {"state": "stopped"}, {"state": "running"}],
            ), mock.patch.object(controller, "_recorded_pid", return_value=4242), mock.patch.object(
                controller, "_health_refused", return_value=True
            ), mock.patch.object(
                controller, "_stop_process_group"
            ) as stop_group, mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(
                controller, "_runtime_env", return_value={"LITELLM_HEALTH_WAIT_SECONDS": "1"}
            ), mock.patch.object(controller, "_configured_port", return_value=4000), mock.patch(
                "litellm_menu.core.operations.subprocess.Popen", return_value=replacement
            ), mock.patch.object(controller, "_write_owner_record"), mock.patch.object(
                controller, "_health", return_value=True
            ):
                controller.start()

            stop_group.assert_called_once_with(4242)
            self.assertEqual("9999\n", controller.paths.pid.read_text(encoding="utf-8"))

    def test_start_does_not_kill_a_busy_unhealthy_proxy(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            with mock.patch.object(
                controller,
                "status",
                side_effect=[{"state": "unhealthy"}],
            ), mock.patch.object(controller, "_recorded_pid", return_value=4242), mock.patch.object(
                controller, "_health_refused", return_value=False
            ), mock.patch.object(controller, "_stop_process_group") as stop_group:
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    controller.start()

            stop_group.assert_not_called()

    def test_start_failure_clears_partial_ownership_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text("4242\n", encoding="utf-8")
            controller.paths.owner.write_text("owner\n", encoding="utf-8")
            process = mock.Mock(pid=4242)

            with mock.patch.object(controller, "status", return_value={"state": "stopped"}), mock.patch.object(
                controller, "_stage_runtime_config"
            ), mock.patch.object(controller, "_runtime_env", return_value={}), mock.patch.object(
                controller, "_configured_port", return_value=4000
            ), mock.patch(
                "litellm_menu.core.operations.subprocess.Popen", return_value=process
            ), mock.patch.object(
                controller, "_write_owner_record", side_effect=PersistenceError("write failed")
            ), mock.patch.object(controller, "_stop_process_group") as stop_group:
                with self.assertRaisesRegex(RuntimeError, "could not start"):
                    controller.start()

            stop_group.assert_called_once_with(4242)
            self.assertFalse(controller.paths.pid.exists())
            self.assertFalse(controller.paths.owner.exists())

    @unittest.skipIf(os.name == "nt", "the fixture uses POSIX process metadata")
    def test_controller_rejects_forged_or_reused_pid_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            token = "synthetic-service-owner-token-1234567890"
            environment = os.environ.copy()
            environment["LITELLM_MENU_SERVICE_OWNER_TOKEN"] = token
            process = subprocess.Popen(
                [sys.executable, "-c", "import time; time.sleep(30)"],
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

            def cleanup() -> None:
                if process.poll() is None:
                    process.kill()
                process.wait(timeout=2)

            self.addCleanup(cleanup)
            identity = controller._process_identity(process.pid)
            self.assertIsNotNone(identity)
            controller.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            controller.paths.pid.write_text(f"{process.pid}\n", encoding="utf-8")
            controller.paths.owner.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "pid": process.pid,
                        "identity": identity,
                        "token": "different-synthetic-owner-token-12345",
                        "core_pid": os.getpid(),
                        "core_identity": controller._process_identity(os.getpid()),
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(controller._pid())

            controller.paths.owner.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "pid": process.pid,
                        "identity": "posix-lstart-sha256:" + "0" * 64,
                        "token": token,
                        "core_pid": os.getpid(),
                        "core_identity": controller._process_identity(os.getpid()),
                    }
                ),
                encoding="utf-8",
            )
            self.assertIsNone(controller._pid())

    def test_core_file_package_uses_existing_format_and_stages_import(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            settings = root / "runtime-settings.env"
            package = root / "package.json"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            settings.write_text("LITELLM_PORT=4100\n", encoding="utf-8")
            core = CoreStore(domains=[ProvidersModelsDomain(config), RuntimeSettingsDomain(settings)])

            export_token = core.file_capabilities.register(package, "export")
            exported = core.export(["providers_models", "runtime"], destination_token=export_token)
            payload = json.loads(package.read_text(encoding="utf-8"))

            self.assertEqual("litellm-menu-configuration-package", payload["format"])
            self.assertEqual(2, exported["section_count"])
            self.assertNotIn("replace-me-secret", json.dumps(exported))
            import_token = core.file_capabilities.register(package, "import")
            prepared = core.prepare_import(source_token=import_token, revision=core.revision)
            imported = core.import_package(
                package=prepared.package,
                sections=["providers_models", "runtime"],
                revision=prepared.revision,
            )
            self.assertEqual(["providers_models", "runtime"], imported["draft_domains"])
            self.assertFalse(imported["preview"]["providers_models"]["will_replace_draft"])
            self.assertFalse(imported["preview"]["runtime"]["will_replace_draft"])

    def test_preview_detects_a_single_domain_file_before_staging(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            settings = root / "runtime-settings.env"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            settings.write_text("LITELLM_PORT=4100\n", encoding="utf-8")
            core = CoreStore(domains=[ProvidersModelsDomain(config), RuntimeSettingsDomain(settings)])
            provider_file = root / "providers.json"
            core.export(
                ["providers_models"],
                destination_token=core.file_capabilities.register(provider_file, "export"),
            )

            import_token = core.file_capabilities.register(provider_file, "import")
            prepared = core.prepare_import(source_token=import_token, revision=core.revision)
            result = core.import_package(
                package=prepared.package,
                sections=["providers_models"],
                revision=prepared.revision,
            )

            self.assertEqual(["providers_models"], result["draft_domains"])

    def test_manual_webdav_push_uses_selected_bundle_sections_and_records_status(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            providers = ProvidersModelsDomain(config)
            relay = RelayAccountsDomain(storage_path=root / ".litellm-runtime" / "relay-accounts.json")
            webdav = self._configured_webdav_domain(root, config)
            core = CoreStore(domains=[providers, relay, webdav])

            with mock.patch("webdav.core.WebDAVClient", return_value=object()), mock.patch(
                "webdav.operations.push_bundle",
                return_value=(32, {"files": []}),
            ) as pushed:
                result = core.dispatch(
                    {
                        "domain": "webdav",
                        "type": "push",
                        "payload": {"sections": ["providers_models", "relay_accounts"]},
                    },
                    expected_revision=core.revision,
                )

            self.assertEqual({"revision"}, set(result))
            pushed.assert_called_once()
            status = json.loads((root / "webdav-status.json").read_text(encoding="utf-8"))
            self.assertEqual({"action": "push", "ok": True}, {key: status[key] for key in ("action", "ok")})
            summary = core.snapshot()["action_summaries"]["webdav"]
            self.assertEqual(["providers_models", "relay_accounts"], summary["sections"])
            self.assertNotIn("replace-webdav-password", json.dumps(core.snapshot()))

    def test_manual_webdav_sync_rejects_partial_sections_and_dirty_sources(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            providers = ProvidersModelsDomain(config)
            relay = RelayAccountsDomain(storage_path=root / ".litellm-runtime" / "relay-accounts.json")
            webdav = self._configured_webdav_domain(root, config)
            core = CoreStore(domains=[providers, relay, webdav])

            with self.assertRaises(Exception) as partial:
                core.dispatch(
                    {
                        "domain": "webdav",
                        "type": "sync",
                        "payload": {"sections": ["providers_models"]},
                    },
                    expected_revision=core.revision,
                )
            self.assertEqual("invalid_sections", partial.exception.code)

            core.dispatch(
                {
                    "domain": "webdav",
                    "type": "patch",
                    "payload": {"remote_name": "other-config.json"},
                },
                expected_revision=core.revision,
            )
            with self.assertRaises(Exception) as dirty_webdav:
                core.dispatch(
                    {"domain": "webdav", "type": "push", "payload": {}},
                    expected_revision=core.revision,
                )
            self.assertEqual("webdav_sync_conflict", dirty_webdav.exception.code)

            core.reload("webdav", revision=core.revision)
            core.dispatch(
                {
                    "domain": "providers_models",
                    "type": "provider.patch",
                    "payload": {"provider_id": "primary", "changes": {"enabled": False}},
                },
                expected_revision=core.revision,
            )
            with self.assertRaises(Exception) as dirty_providers:
                core.dispatch(
                    {"domain": "webdav", "type": "pull", "payload": {}},
                    expected_revision=core.revision,
                )
            self.assertEqual("webdav_sync_conflict", dirty_providers.exception.code)

    def test_manual_webdav_failure_records_failed_status_without_leaking_details(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            providers = ProvidersModelsDomain(config)
            relay = RelayAccountsDomain(storage_path=root / ".litellm-runtime" / "relay-accounts.json")
            webdav = self._configured_webdav_domain(root, config)
            core = CoreStore(domains=[providers, relay, webdav])

            with mock.patch("webdav.core.WebDAVClient", return_value=object()), mock.patch(
                "webdav.operations.push_bundle",
                side_effect=RuntimeError("replace-webdav-password /private/config.yaml"),
            ), self.assertRaises(Exception) as raised:
                core.dispatch(
                    {"domain": "webdav", "type": "push", "payload": {}},
                    expected_revision=core.revision,
                )

            self.assertEqual("webdav_sync_failed", raised.exception.code)
            self.assertEqual("WebDAV sync failed", str(raised.exception))
            status = json.loads((root / "webdav-status.json").read_text(encoding="utf-8"))
            self.assertEqual({"action": "push", "ok": False}, {key: status[key] for key in ("action", "ok")})

    def test_single_domain_files_are_json_and_provider_yaml_remains_importable(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            settings = root / "runtime-settings.env"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            settings.write_text("LITELLM_PORT=4100\n", encoding="utf-8")
            core = CoreStore(domains=[ProvidersModelsDomain(config), RuntimeSettingsDomain(settings)])

            provider_json = root / "providers.json"
            result = core.export(
                ["providers_models"],
                destination_token=core.file_capabilities.register(provider_json, "export"),
            )
            payload = json.loads(provider_json.read_text(encoding="utf-8"))
            self.assertEqual("litellm-menu-domain-settings", payload["format"])
            self.assertEqual("providers_models", payload["domain"])
            self.assertEqual(1, result["section_count"])
            self.assertNotIn("replace-me-secret", json.dumps(result))
            self.assertIn("replace-me-secret", json.dumps(payload))
            provider_token = core.file_capabilities.register(provider_json, "import")
            provider_plan = core.prepare_import(source_token=provider_token, revision=core.revision)
            imported = core.import_package(
                package=provider_plan.package,
                sections=["providers_models"],
                revision=provider_plan.revision,
            )
            self.assertEqual(["providers_models"], imported["draft_domains"])

            runtime_json = root / "runtime.json"
            core.export(
                ["runtime"],
                destination_token=core.file_capabilities.register(runtime_json, "export"),
            )
            runtime_payload = json.loads(runtime_json.read_text(encoding="utf-8"))
            self.assertEqual("runtime", runtime_payload["domain"])
            self.assertEqual("4100", runtime_payload["settings"]["values"]["LITELLM_PORT"])
            runtime_token = core.file_capabilities.register(runtime_json, "import")
            runtime_plan = core.prepare_import(source_token=runtime_token, revision=core.revision)
            core.import_package(
                package=runtime_plan.package,
                sections=["runtime"],
                revision=runtime_plan.revision,
            )

            yaml_token = core.file_capabilities.register(config, "import")
            yaml_plan = core.prepare_import(source_token=yaml_token, revision=core.revision)
            yaml_import = core.import_package(
                package=yaml_plan.package,
                sections=["providers_models"],
                revision=yaml_plan.revision,
            )
            self.assertEqual(["providers_models"], yaml_import["draft_domains"])

    def test_single_domain_export_cannot_replace_the_active_settings_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text(textwrap.dedent(PROVIDER_CONFIG).lstrip(), encoding="utf-8")
            core = CoreStore(domains=[ProvidersModelsDomain(config)])

            with self.assertRaisesRegex(Exception, "outside the active settings files"):
                core.export(
                    ["providers_models"],
                    destination_token=core.file_capabilities.register(config, "export"),
                )


if __name__ == "__main__":
    unittest.main()
