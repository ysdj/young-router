from __future__ import annotations

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
from litellm_menu.core.domains.legacy import ProvidersModelsDomain, RuntimeSettingsDomain, WebDAVSettingsDomain
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

    def test_macos_uses_configured_spawn_workers(self) -> None:
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
            workers_index = command.index("--num_workers")
            self.assertEqual("16", command[workers_index + 1])
            self.assertNotIn("--run_gunicorn", command)

    def test_macos_defaults_to_sixteen_spawn_workers(self) -> None:
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
            workers_index = command.index("--num_workers")
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
                environment={"LITELLM_MENU_VISION_BRIDGE_API_KEY": "test-inherited-secret"},
            )
            environment = controller._runtime_env()

            self.assertEqual("49173", environment["LITELLM_PORT"])
            self.assertEqual("3", environment["LITELLM_NUM_WORKERS"])
            self.assertNotIn("LITELLM_CONFIG_WATCH_INTERVAL", environment)
            self.assertNotIn("LITELLM_CONFIG_WATCH_SETTLE_INTERVAL", environment)
            self.assertNotIn("LITELLM_MENU_VISION_BRIDGE_API_KEY", environment)
            with mock.patch.object(controller, "_health", return_value=True):
                self.assertEqual("unknown", controller.status()["state"])

    def test_controller_ignores_removed_persisted_runtime_settings(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "runtime-settings.env").write_text(
                "LITELLM_PORT=49173\n"
                "LITELLM_MENU_WEB_SEARCH_READ_RESULTS=1\n"
                "LITELLM_MENU_BALANCE_REFRESH_MINUTES=5\n"
                "LITELLM_BROWSER_BILLING=1\n",
                encoding="utf-8",
            )

            environment = CoreServiceController(root)._runtime_env()

            self.assertEqual("49173", environment["LITELLM_PORT"])
            self.assertNotIn("LITELLM_MENU_WEB_SEARCH_READ_RESULTS", environment)
            self.assertNotIn("LITELLM_MENU_BALANCE_REFRESH_MINUTES", environment)
            self.assertNotIn("LITELLM_BROWSER_BILLING", environment)

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
            imported = core.import_package(
                source_token=import_token,
                sections=["providers_models", "runtime"],
                revision=core.revision,
            )
            self.assertEqual(["providers_models", "runtime"], imported["draft_domains"])
            self.assertFalse(imported["preview"]["providers_models"]["will_replace_draft"])
            self.assertFalse(imported["preview"]["runtime"]["will_replace_draft"])

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
            imported = core.import_package(
                source_token=core.file_capabilities.register(provider_json, "import"),
                sections=["providers_models"],
                revision=core.revision,
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
            core.import_package(
                source_token=core.file_capabilities.register(runtime_json, "import"),
                sections=["runtime"],
                revision=core.revision,
            )

            yaml_import = core.import_package(
                source_token=core.file_capabilities.register(config, "import"),
                sections=["providers_models"],
                revision=core.revision,
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
