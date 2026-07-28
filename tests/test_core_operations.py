from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock

from litellm_menu.core import CoreStore
from litellm_menu.core.domains.legacy import ProvidersModelsDomain, RuntimeSettingsDomain
from litellm_menu.core.operations import CoreServiceController


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
            ), mock.patch.object(controller, "_write_state"), mock.patch.object(
                controller, "_write_owner_record"
            ), mock.patch.object(controller, "_health", return_value=True), mock.patch(
                "litellm_menu.core.operations.atomic_write_text"
            ), mock.patch("litellm_menu.core.operations.subprocess.Popen", return_value=process) as popen:
                controller.start()

            command = popen.call_args.args[0]
            self.assertEqual(controller.python, command[0])
            self.assertEqual("-c", command[1])
            self.assertIn("from litellm import run_server", command[2])
            self.assertNotIn(controller.litellm_bin, command)

    def test_service_dispatch_projects_real_status_and_autostart(self) -> None:
        calls: list[str] = []

        def handler(operation: str) -> dict[str, object]:
            calls.append(operation)
            return {
                "state": "running",
                "pid": 123,
                "auto_start_state": "enabled",
                "route_recovery": {"recovering": 1},
            }

        core = CoreStore(service_handlers={"health": handler})
        core.dispatch({"type": "service.health"})
        service = core.snapshot()["service"]

        self.assertEqual(["health"], calls)
        self.assertEqual("running", service["state"])
        self.assertEqual("enabled", service["auto_start_state"])
        self.assertEqual(1, service["route_recovery"]["recovering"])

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

    def test_proxy_environment_loads_bundled_callback_fallback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            controller = CoreServiceController(directory)
            environment = controller._runtime_env()
            core_root = Path(__file__).resolve().parents[1]

            self.assertEqual("1", environment["LITELLM_MENU_PROXY_PROCESS"])
            self.assertEqual(str(core_root), environment["LITELLM_TEMPLATE_ROOT"])
            self.assertEqual(str(core_root), environment["PYTHONPATH"].split(os.pathsep)[0])
            self.assertTrue((core_root / "sitecustomize.py").is_file())

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

    def test_owner_record_is_independent_of_the_core_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = CoreServiceController(directory)
            first.paths.pid.parent.mkdir(parents=True, exist_ok=True)
            first.paths.pid.write_text("4242\n", encoding="utf-8")
            first.paths.owner.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": 4242,
                        "identity": "synthetic-process-identity",
                        "token": "synthetic-service-owner-token-1234567890",
                    }
                ),
                encoding="utf-8",
            )

            replacement = CoreServiceController(directory)
            with mock.patch.object(
                replacement, "_process_identity", return_value="synthetic-process-identity"
            ), mock.patch.object(replacement, "_posix_process_has_token", return_value=True):
                self.assertEqual(4242, replacement._pid())

    @unittest.skipIf(os.name == "nt", "the fixture uses POSIX signals")
    def test_controller_recognizes_owned_service_after_core_restart(self) -> None:
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

            self.assertEqual(process.pid, replacement._pid())
            self.assertEqual(0o600, stat.S_IMODE(replacement.paths.owner.stat().st_mode))
            with mock.patch.object(replacement, "_health", return_value=True):
                self.assertEqual("running", replacement.status()["state"])

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
                        "version": 1,
                        "pid": process.pid,
                        "identity": identity,
                        "token": "different-synthetic-owner-token-12345",
                    }
                ),
                encoding="utf-8",
            )

            self.assertIsNone(controller._pid())

            controller.paths.owner.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "pid": process.pid,
                        "identity": "posix-lstart-sha256:" + "0" * 64,
                        "token": token,
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


if __name__ == "__main__":
    unittest.main()
