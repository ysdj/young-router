from __future__ import annotations

import json
import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"


class ControlAutoStartTests(unittest.TestCase):
    def make_env(self, temp: Path) -> dict[str, str]:
        home = temp / "home"
        runtime = temp / "runtime"
        fake_bin = temp / "bin"
        for path in (home, runtime, fake_bin):
            path.mkdir(parents=True, exist_ok=True)

        for name in ("launchctl", "plutil"):
            command = fake_bin / name
            command.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            command.chmod(command.stat().st_mode | stat.S_IXUSR)

        env = os.environ.copy()
        env.update(
            {
                "HOME": str(home),
                "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                "LITELLM_RUNTIME_ROOT": str(runtime),
                "LITELLM_TEMPLATE_ROOT": str(ROOT),
                "LITELLM_APP_PATH": "/Applications/LiteLLM Menu.app",
                "LITELLM_PORT": "49231",
                "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.test",
                "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.test",
            }
        )
        return env

    def run_control(
        self,
        action: str,
        env: dict[str, str],
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["/bin/bash", str(CONTROL), action],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )

    def write_command(self, directory: Path, name: str, body: str) -> None:
        command = directory / name
        command.write_text(textwrap.dedent(body).lstrip(), encoding="utf-8")
        command.chmod(command.stat().st_mode | stat.S_IXUSR)

    def test_enable_autostart_writes_menu_launch_agent_only(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)

            result = self.run_control("autostart-enable", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("menu:", result.stdout)

            menu_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_APP_LAUNCH_AGENT_LABEL']}.plist"
            self.assertTrue(menu_plist.exists())

            menu_text = menu_plist.read_text(encoding="utf-8")
            self.assertIn("/usr/bin/open", menu_text)
            self.assertIn("-gj", menu_text)
            self.assertIn("/Applications/LiteLLM Menu.app", menu_text)

            status = self.run_control("autostart-status", env)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(status.stdout.strip(), "enabled")

    def test_status_reports_missing_menu_launch_agent_without_mutating_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            state_file = temp / "runtime/.litellm-runtime/autostart.enabled"
            menu_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_APP_LAUNCH_AGENT_LABEL']}.plist"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("1\n", encoding="utf-8")

            status = self.run_control("autostart-status", env)
            self.assertEqual(status.returncode, 1, status.stderr)
            self.assertIn("enabled but missing", status.stdout)
            self.assertFalse(menu_plist.exists())

            menu_status = self.run_control("menu-status", env)
            self.assertEqual(menu_status.returncode, 0, menu_status.stderr)
            self.assertEqual(
                json.loads(menu_status.stdout)["auto_start_state"],
                "incomplete",
            )
            self.assertFalse(menu_plist.exists())

    def test_disable_autostart_removes_menu_launch_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            self.assertEqual(self.run_control("autostart-enable", env).returncode, 0)

            result = self.run_control("autostart-disable", env)
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertFalse((temp / "runtime/.litellm-runtime/autostart.enabled").exists())
            self.assertFalse((temp / f"home/Library/LaunchAgents/{env['LITELLM_APP_LAUNCH_AGENT_LABEL']}.plist").exists())

    def test_config_watch_ensure_does_not_reload_an_unchanged_running_agent(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            launchctl_log = temp / "launchctl.log"
            state_file = temp / "config-watch-loaded"
            self.write_command(
                temp / "bin",
                "launchctl",
                f"""
                #!/bin/sh
                printf '%s\\n' "$*" >> {shlex.quote(str(launchctl_log))}
                case "$1" in
                  print) test -f {shlex.quote(str(state_file))} ;;
                  bootstrap) : > {shlex.quote(str(state_file))} ;;
                  bootout) rm -f {shlex.quote(str(state_file))} ;;
                  *) exit 0 ;;
                esac
                """,
            )

            first = self.run_control("config-watch-ensure", env)
            self.assertEqual(first.returncode, 0, first.stderr)
            first_calls = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(call.startswith("bootstrap ") for call in first_calls), 1)
            self.assertFalse(any(call.startswith("kickstart ") for call in first_calls))

            watch_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_CONFIG_WATCH_LABEL']}.plist"
            watch_text = watch_plist.read_text(encoding="utf-8")
            self.assertNotIn("<key>RunAtLoad</key>", watch_text)
            self.assertIn("<key>LITELLM_CONFIG_WATCH_REVISION</key>", watch_text)
            self.assertIn("timestamp_log_runner.py", watch_text)
            self.assertIn("<string>--</string>", watch_text)

            second = self.run_control("config-watch-ensure", env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertIn("already enabled", second.stdout)
            all_calls = launchctl_log.read_text(encoding="utf-8").splitlines()
            second_calls = all_calls[len(first_calls) :]
            self.assertEqual(
                second_calls,
                [f"print gui/{os.getuid()}/{env['LITELLM_CONFIG_WATCH_LABEL']}"],
            )

    def test_config_watch_ensure_reloads_when_watcher_revision_changes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            launchctl_log = temp / "launchctl.log"
            state_file = temp / "config-watch-loaded"
            template = temp / "template"
            template.mkdir()
            watcher = template / "watch_config.sh"
            watcher.write_text(
                (ROOT / "watch_config.sh").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            runner_dir = template / "service"
            runner_dir.mkdir()
            shutil.copy2(
                ROOT / "service" / "timestamp_log_runner.py",
                runner_dir / "timestamp_log_runner.py",
            )
            env["LITELLM_TEMPLATE_ROOT"] = str(template)
            self.write_command(
                temp / "bin",
                "launchctl",
                f"""
                #!/bin/sh
                printf '%s\\n' "$*" >> {shlex.quote(str(launchctl_log))}
                case "$1" in
                  print) test -f {shlex.quote(str(state_file))} ;;
                  bootstrap) : > {shlex.quote(str(state_file))} ;;
                  bootout) rm -f {shlex.quote(str(state_file))} ;;
                  *) exit 0 ;;
                esac
                """,
            )

            first = self.run_control("config-watch-ensure", env)
            self.assertEqual(first.returncode, 0, first.stderr)
            watcher.write_text(watcher.read_text(encoding="utf-8") + "\n# revision change\n", encoding="utf-8")

            second = self.run_control("config-watch-ensure", env)
            self.assertEqual(second.returncode, 0, second.stderr)
            self.assertNotIn("already enabled", second.stdout)
            calls = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(sum(call.startswith("bootstrap ") for call in calls), 2)
            self.assertEqual(sum(call.startswith("bootout ") for call in calls), 1)

    def test_config_watch_disable_unloads_the_loaded_agent_before_removing_its_plist(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            state_file = temp / "config-watch-loaded"
            self.write_command(
                temp / "bin",
                "launchctl",
                f"""
                #!/bin/sh
                case "$1" in
                  print) test -f {shlex.quote(str(state_file))} ;;
                  bootstrap) : > {shlex.quote(str(state_file))} ;;
                  bootout) rm -f {shlex.quote(str(state_file))} ;;
                  *) exit 0 ;;
                esac
                """,
            )

            self.assertEqual(self.run_control("config-watch-ensure", env).returncode, 0)
            result = self.run_control("config-watch-disable", env)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertFalse(state_file.exists())
            self.assertFalse(
                (temp / f"home/Library/LaunchAgents/{env['LITELLM_CONFIG_WATCH_LABEL']}.plist").exists()
            )

    def test_config_watch_disable_preserves_the_plist_when_unload_fails(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            watch_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_CONFIG_WATCH_LABEL']}.plist"
            watch_plist.parent.mkdir(parents=True)
            watch_plist.write_text("<plist/>\n", encoding="utf-8")
            self.write_command(
                temp / "bin",
                "launchctl",
                """
                #!/bin/sh
                case "$1" in
                  print) exit 0 ;;
                  bootout) exit 9 ;;
                  *) exit 0 ;;
                esac
                """,
            )

            result = self.run_control("config-watch-disable", env)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not unload config watcher", result.stderr)
            self.assertTrue(watch_plist.exists())

    def test_checkout_lifecycle_actions_refuse_default_real_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(temp / "home"),
                    "LITELLM_RUNTIME_ROOT": str(temp / "runtime"),
                    "LITELLM_TEMPLATE_ROOT": str(ROOT),
                }
            )

            result = self.run_control("stop", env, timeout=3)

            self.assertEqual(result.returncode, 64)
            self.assertIn("Refusing to run 'stop'", result.stderr)
            self.assertIn("LITELLM_PORT=4000", result.stderr)

    def test_checkout_target_sensitive_actions_refuse_default_real_target(self) -> None:
        actions = (
            "status",
            "tail",
            "recent-requests",
            "logs-summary",
            "menu-actions-tail",
            "route-trace",
            "computer-facade-smoke",
            "runtime-settings-apply",
            "runtime-settings-save",
            "configuration-package-export",
            "configuration-package-import",
            "external-provider-import",
            "webdav-settings",
            "webdav-configure",
            "webdav-enable",
            "webdav-disable",
            "webdav-status",
            "webdav-sync-interval-seconds",
            "webdav-probe",
            "webdav-sync",
            "webdav-push",
            "webdav-pull",
            "stage-config",
            "codex-config-status",
            "codex-config-apply",
            "provider-billing",
            "validate",
            "verify-runtime-config",
            "config-watch-tail",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(temp / "home"),
                    "LITELLM_TEMPLATE_ROOT": str(ROOT),
                }
            )
            default_runtime = temp / "home/.litellm-menu"

            for action in actions:
                with self.subTest(action=action):
                    result = self.run_control(action, env, timeout=3)
                    self.assertEqual(result.returncode, 64, result.stderr)
                    self.assertIn(f"Refusing to run '{action}'", result.stderr)
                    self.assertFalse(default_runtime.exists())

    def test_explicit_isolated_target_allows_guarded_diagnostic_actions(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)

            status = self.run_control("status", env, timeout=3)
            tail = self.run_control("tail", env, timeout=3)

            self.assertNotEqual(status.returncode, 64, status.stderr)
            self.assertEqual(tail.returncode, 0, tail.stderr)
            self.assertIn("No service log file yet", tail.stdout)

    def test_isolated_target_rejects_runtime_paths_outside_its_root(self) -> None:
        path_variables = (
            "LITELLM_CONFIG_FILE",
            "LITELLM_RUNTIME_DIR",
            "LITELLM_MENU_RUNTIME_SETTINGS_FILE",
            "LITELLM_WEBDAV_SYNC_SETTINGS",
            "LITELLM_WEBDAV_SYNC_STATE",
            "LITELLM_MENU_LOG",
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            for variable in path_variables:
                with self.subTest(variable=variable):
                    env = self.make_env(temp / variable.lower())
                    outside = temp / "outside" / variable.lower()
                    outside.parent.mkdir(parents=True, exist_ok=True)
                    env[variable] = str(outside)

                    result = self.run_control("tail", env, timeout=3)

                    self.assertEqual(result.returncode, 64, result.stderr)
                    self.assertIn("outside LITELLM_RUNTIME_ROOT", result.stderr)
                    self.assertIn(variable, result.stderr)

    def test_isolated_target_rejects_symlink_escape(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            outside = temp / "outside"
            outside.mkdir()
            linked = Path(env["LITELLM_RUNTIME_ROOT"]) / "linked"
            linked.symlink_to(outside, target_is_directory=True)
            env["LITELLM_CONFIG_FILE"] = str(linked / "config.yaml")

            result = self.run_control("validate", env, timeout=3)

            self.assertEqual(result.returncode, 64, result.stderr)
            self.assertIn("LITELLM_CONFIG_FILE", result.stderr)
            self.assertIn("outside LITELLM_RUNTIME_ROOT", result.stderr)

    def test_isolated_target_accepts_normalized_paths_inside_its_root(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            runtime = Path(env["LITELLM_RUNTIME_ROOT"])
            env["LITELLM_CONFIG_FILE"] = str(runtime / "nested" / ".." / "config.yaml")

            result = self.run_control("tail", env, timeout=3)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("No service log file yet", result.stdout)

    def test_alternate_installed_app_requires_its_running_menu_owner(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            app = temp / "Applications/LiteLLM Menu Alternate.app"
            resources = app / "Contents/Resources/App"
            binary = app / "Contents/MacOS/LiteLLMMenu"
            resources.mkdir(parents=True)
            binary.parent.mkdir(parents=True)
            shutil.copy2(CONTROL, resources / "service.sh")
            shutil.copytree(ROOT / "service", resources / "service")
            shutil.copyfile("/bin/sleep", binary)
            binary.chmod(0o755)
            (resources / "service.sh").chmod(0o755)

            home = temp / "home"
            home.mkdir()
            env = os.environ.copy()
            env["HOME"] = str(home)

            without_owner = subprocess.run(
                ["/bin/bash", str(resources / "service.sh"), "tail"],
                cwd=resources,
                env=env,
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )
            self.assertEqual(without_owner.returncode, 64, without_owner.stderr)

            owner = subprocess.Popen([str(binary), "10"])
            try:
                owned_env = dict(env, LITELLM_MENU_OWNER_PID=str(owner.pid))
                with_owner = subprocess.run(
                    ["/bin/bash", str(resources / "service.sh"), "tail"],
                    cwd=resources,
                    env=owned_env,
                    text=True,
                    capture_output=True,
                    timeout=3,
                    check=False,
                )
            finally:
                owner.terminate()
                owner.wait(timeout=3)

            self.assertEqual(with_owner.returncode, 0, with_owner.stderr)
            self.assertIn("No service log file yet", with_owner.stdout)

    def test_restart_refuses_without_menu_app_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            env["LITELLM_APP_PATH"] = str(temp / "Missing LiteLLM Menu.app")
            result = self.run_control("restart", env, timeout=3)

            self.assertEqual(result.returncode, 64)
            self.assertIn("LiteLLM Menu app is not running", result.stderr)
            self.assertIn("required service owner", result.stderr)

    def test_restart_starts_native_service_only_when_menu_app_owns_it(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_root = temp / "runtime"
            runtime_dir = runtime_root / ".litellm-runtime"
            fake_bin = temp / "bin"
            runtime_dir.mkdir(parents=True)
            fake_bin.mkdir()

            (runtime_root / "config.yaml").write_text(
                textwrap.dedent(
                    """
                    model_list:
                      - model_name: default-chat
                        litellm_params:
                          model: openai/default-chat
                          api_base: https://example.test/v1
                          order: 1
                        model_info:
                          id: a1b2c3d4
                          provider: example
                          upstream_url_surface: openai/responses
                          supported_upstream_url_surfaces: [openai/responses]
                          route_key: example / openai/default-chat / order=1
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            model_info_path = temp / "model-info.json"
            model_info_path.write_text(
                json.dumps(
                    {
                        "data": [
                            {
                                "model_name": "default-chat",
                                "litellm_params": {
                                    "model": "openai/default-chat",
                                    "api_base": "https://example.test/v1",
                                    "order": 1,
                                },
                                "model_info": {
                                    "id": "a1b2c3d4",
                                    "provider": "example",
                                    "route_key": "example / openai/default-chat / order=1",
                                },
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )

            native_run_log = temp / "native-run.log"
            pid_file = runtime_dir / "litellm.pid"
            owner_file = runtime_dir / "litellm.owner"
            self.write_command(fake_bin, "launchctl", "#!/bin/sh\nexit 99\n")
            service_script = temp / "app/service.sh"
            service_script.parent.mkdir()
            service_script.write_text(
                textwrap.dedent(
                    f"""
                    #!/bin/sh
                    if [ "$1" = "run-native" ]; then
                      printf 'run-native\\n' >> {native_run_log}
                      /bin/sleep 60 &
                      printf '%s\\n' "$!" > {pid_file}
                      printf '%s %s\\n' "$LITELLM_MENU_OWNER_PID" "$!" > {owner_file}
                      wait "$!"
                    fi
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            service_script.chmod(service_script.stat().st_mode | stat.S_IXUSR)
            runner_dir = service_script.parent / "service"
            runner_dir.mkdir()
            shutil.copy2(
                ROOT / "service" / "timestamp_log_runner.py",
                runner_dir / "timestamp_log_runner.py",
            )
            (service_script.parent / "LITELLM_VERSION").write_text(
                (ROOT / "LITELLM_VERSION").read_text(encoding="utf-8"),
                encoding="utf-8",
            )
            callback_package = service_script.parent / "litellm_menu"
            callback_package.mkdir()
            (callback_package / "__init__.py").write_text("# test callback package\n", encoding="utf-8")
            (callback_package / "callbacks.py").write_text("# test callback\n", encoding="utf-8")
            for name, body in {
                "curl": "#!/bin/sh\nexit 0\n",
                "lsof": "#!/bin/sh\nexit 0\n",
                "plutil": "#!/bin/sh\nexit 0\n",
            }.items():
                self.write_command(fake_bin, name, body)
            self.write_command(
                fake_bin,
                "ps",
                """
                #!/bin/sh
                if [ "$1" = "axww" ]; then
                  printf '4242 /Applications/LiteLLM Menu.app/Contents/MacOS/LiteLLMMenu\\n'
                  exit 0
                fi
                if [ "$1" = "-p" ] && [ "$3" = "-o" ]; then
                  if [ "$2" = "4242" ]; then
                    printf '/Applications/LiteLLM Menu.app/Contents/MacOS/LiteLLMMenu\\n'
                  else
                    printf '/bin/sleep 60\\n'
                  fi
                  exit 0
                fi
                exit 1
                """,
            )

            def cleanup_processes() -> None:
                try:
                    pid = int(pid_file.read_text(encoding="utf-8").strip())
                except Exception:
                    return
                subprocess.run(["/bin/kill", str(pid)], check=False)

            self.addCleanup(cleanup_processes)

            env = os.environ.copy()
            env.update(
                {
                    "PATH": f"{fake_bin}:{env.get('PATH', '')}",
                    "PYTHON": sys.executable,
                    "LITELLM_RUNTIME_ROOT": str(runtime_root),
                    "LITELLM_TEMPLATE_ROOT": str(service_script.parent),
                    "LITELLM_MODEL_INFO_FILE": str(model_info_path),
                    "LITELLM_BIN": "/bin/sleep",
                    "LITELLM_NATIVE_PYTHON": sys.executable,
                    "LITELLM_NATIVE_PID_FILE": str(pid_file),
                    "LITELLM_NATIVE_OWNER_FILE": str(owner_file),
                    "LITELLM_PORT": "49232",
                    "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.restart-test",
                    "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.restart-test",
                    "LITELLM_MENU_OWNER_PID": "4242",
                    "LITELLM_HEALTH_WAIT_SECONDS": "1",
                    "LITELLM_RUNTIME_VERIFY_WAIT_SECONDS": "1",
                }
            )

            result = self.run_control("restart", env)

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("LiteLLM restarted", result.stdout)
            self.assertEqual(native_run_log.read_text(encoding="utf-8").splitlines(), ["run-native"])

    def test_restart_uses_short_port_release_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            argument_file = temp / "wait-argument"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {ROOT / 'service/process.sh'}
                require_menu_app_owner() {{ printf '4242\\n'; }}
                ensure_native_environment() {{ :; }}
                sync_runtime_config() {{ :; }}
                write_state() {{ :; }}
                request_native_processes_to_stop() {{ :; }}
                wait_for_native_port_released() {{ printf '%s\\n' "${{1:-}}" > {argument_file}; }}
                start_service_process() {{ :; }}
                wait_for_managed_health() {{ :; }}
                wait_for_runtime_config() {{ printf 'verified\\n'; }}
                write_runtime_reload_fingerprint() {{ :; }}
                clear_state() {{ :; }}
                PORT=49232
                NATIVE_WORKERS=1
                restart_server >/dev/null
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(argument_file.read_text(encoding="utf-8").strip(), "5")

    def test_service_owner_record_must_match_the_current_menu_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            owner_file = temp / "litellm.owner"
            pid_file = temp / "litellm.pid"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                NATIVE_OWNER_FILE={q(str(owner_file))}
                NATIVE_PID_FILE={q(str(pid_file))}
                process_is_menu_app_pid() {{ [[ "$1" == 4242 ]]; }}
                native_pid_from_file() {{ cat "$NATIVE_PID_FILE"; }}
                write_native_lifecycle_records 1111 9999
                if native_owned_by_menu_pid 4242; then
                  exit 1
                fi
                write_native_lifecycle_records 4242 9999
                native_owned_by_menu_pid 4242
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(owner_file.read_text(encoding="utf-8").strip(), "4242 9999")
            self.assertEqual(pid_file.read_text(encoding="utf-8").strip(), "9999")

    def test_owner_watchdog_retains_native_service_when_menu_process_disappears(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            log_file = temp / "menu-server.log"
            calls = temp / "kill-calls"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                LOG_FILE={q(str(log_file))}
                checks=0
                kill() {{
                  if [[ "$1" == -0 ]]; then
                    checks=$((checks + 1))
                    (( checks <= 1 ))
                    return
                  fi
                  printf '%s\\n' "$*" >> {q(str(calls))}
                }}
                process_is_menu_app_pid() {{ return 1; }}
                native_owner_for_pid() {{ printf '1111\\n'; }}
                rotate_log_if_needed() {{ :; }}
                sleep() {{ :; }}
                watch_native_owner 9999 1111
                [[ ! -e {q(str(calls))} ]]
                grep -q 'retaining native LiteLLM service pid 9999' {q(str(log_file))}
                ! grep -q 'stopping native LiteLLM service' {q(str(log_file))}
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_owner_watchdog_stops_unreplaced_native_service_after_grace(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            log_file = temp / "menu-server.log"
            calls = temp / "kill-calls"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                LOG_FILE={q(str(log_file))}
                OWNER_GRACE_SECONDS=2
                SECONDS=0
                checks=0
                kill() {{
                  if [[ "$1" == -0 ]]; then
                    checks=$((checks + 1))
                    (( checks <= 4 ))
                    return
                  fi
                  printf '%s\n' "$*" >> {q(str(calls))}
                }}
                process_is_menu_app_pid() {{ return 1; }}
                native_owner_for_pid() {{ return 1; }}
                rotate_log_if_needed() {{ :; }}
                sleep() {{ SECONDS=$((SECONDS + 1)); :; }}
                watch_native_owner 9999 1111
                grep -qx '9999' {q(str(calls))}
                grep -qx -- '-KILL 9999' {q(str(calls))}
                grep -q 'was not replaced within 2s; stopping native LiteLLM service pid 9999' {q(str(log_file))}
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_owner_watchdog_hands_retained_service_to_replacement_menu(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            log_file = temp / "menu-server.log"
            calls = temp / "kill-calls"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                LOG_FILE={q(str(log_file))}
                OWNER_GRACE_SECONDS=2
                SECONDS=0
                checks=0
                kill() {{
                  if [[ "$1" == -0 ]]; then
                    checks=$((checks + 1))
                    (( checks <= 3 ))
                    return
                  fi
                  printf '%s\n' "$*" >> {q(str(calls))}
                }}
                process_is_menu_app_pid() {{ [[ "$1" == 2222 ]]; }}
                native_owner_for_pid() {{
                  if (( SECONDS == 0 )); then
                    printf '1111\n'
                  else
                    printf '2222\n'
                  fi
                }}
                rotate_log_if_needed() {{ :; }}
                sleep() {{ SECONDS=$((SECONDS + 1)); :; }}
                watch_native_owner 9999 1111
                [[ ! -e {q(str(calls))} ]]
                grep -q 'owner handoff: pid 1111 -> pid 2222 for native LiteLLM service pid 9999' {q(str(log_file))}
                ! grep -q 'stopping native LiteLLM service' {q(str(log_file))}
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_status_does_not_stop_a_healthy_service_when_menu_process_is_missing(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            fake_bin = Path(env["PATH"].split(":", 1)[0])
            pid_file = temp / "runtime/.litellm-runtime/litellm.pid"
            pid_file.parent.mkdir(parents=True, exist_ok=True)
            self.write_command(fake_bin, "curl", "#!/bin/sh\nexit 0\n")

            native = subprocess.Popen(["/bin/sleep", "30"])
            try:
                pid_file.write_text(f"{native.pid}\\n", encoding="utf-8")
                env["LITELLM_BIN"] = "/bin/sleep"

                result = self.run_control("status", env, timeout=3)

                self.assertEqual(result.returncode, 3, result.stderr + result.stdout)
                self.assertEqual(result.stdout.strip(), "unmanaged")
                self.assertIsNone(native.poll(), "status must not terminate a retained native service")
            finally:
                if native.poll() is None:
                    native.terminate()
                    native.wait(timeout=3)

    def test_stale_owner_record_is_removed_when_the_native_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            owner_file = temp / "litellm.owner"
            pid_file = temp / "litellm.pid"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                NATIVE_OWNER_FILE={q(str(owner_file))}
                NATIVE_PID_FILE={q(str(pid_file))}
                native_pid_alive() {{ return 1; }}
                write_native_lifecycle_records 4242 9999
                clear_native_pid_file_if_stale_or_targeted ""
                [[ ! -e "$NATIVE_OWNER_FILE" && ! -e "$NATIVE_PID_FILE" ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_owner_record_survives_the_handoff_from_an_old_native_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            owner_file = temp / "litellm.owner"
            pid_file = temp / "litellm.pid"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                NATIVE_OWNER_FILE={q(str(owner_file))}
                NATIVE_PID_FILE={q(str(pid_file))}
                native_pid_alive() {{ [[ "$1" == 2222 ]]; }}
                printf '1111\\n' > "$NATIVE_PID_FILE"
                printf '4242 2222\\n' > "$NATIVE_OWNER_FILE"
                clear_native_pid_file_if_stale_or_targeted 1111
                [[ ! -e "$NATIVE_PID_FILE" ]]
                [[ "$(cat "$NATIVE_OWNER_FILE")" == "4242 2222" ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_start_adopts_healthy_service_after_previous_menu_process_exits(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            owner_file = temp / "litellm.owner"
            pid_file = temp / "litellm.pid"
            calls = temp / "calls"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                source {q(str(ROOT / 'service/runtime_settings_configure.sh'))}
                NATIVE_OWNER_FILE={q(str(owner_file))}
                NATIVE_PID_FILE={q(str(pid_file))}
                printf '1111 9999\\n' > "$NATIVE_OWNER_FILE"
                printf '9999\\n' > "$NATIVE_PID_FILE"
                require_menu_app_owner() {{ printf '4242\\n'; }}
                process_is_menu_app_pid() {{ [[ "$1" == 4242 ]]; }}
                native_pid_from_file() {{ printf '9999\\n'; }}
                ensure_runtime_layout() {{ :; }}
                webdav_sync_enabled() {{ return 1; }}
                runtime_config_matches_source() {{ :; }}
                ensure_native_environment() {{ :; }}
                sync_runtime_config() {{ :; }}
                health_ok() {{ :; }}
                native_port_pids() {{ printf '9999\\n'; }}
                request_native_processes_to_stop() {{ printf 'stop\\n' >> {q(str(calls))}; }}
                wait_for_native_port_released() {{ printf 'wait\\n' >> {q(str(calls))}; }}
                clear_transient_routing_state() {{ printf 'clear\\n' >> {q(str(calls))}; }}
                write_state() {{ printf 'state\\n' >> {q(str(calls))}; }}
                start_service_process() {{ printf 'start:%s\\n' "$1" >> {q(str(calls))}; }}
                wait_for_managed_health() {{ [[ "$1" == 4242 ]]; }}
                clear_state() {{ :; }}
                PORT=49232
                NATIVE_WORKERS=1
                start_server >/dev/null
                [[ "$(cat "$NATIVE_OWNER_FILE")" == '4242 9999' ]]
                [[ ! -e {q(str(calls))} ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_start_adopts_retained_service_when_runtime_config_changed(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            owner_file = temp / "litellm.owner"
            pid_file = temp / "litellm.pid"
            calls = temp / "calls"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                source {q(str(ROOT / 'service/runtime_settings_configure.sh'))}
                NATIVE_OWNER_FILE={q(str(owner_file))}
                NATIVE_PID_FILE={q(str(pid_file))}
                printf '1111 9999\\n' > "$NATIVE_OWNER_FILE"
                printf '9999\\n' > "$NATIVE_PID_FILE"
                require_menu_app_owner() {{ printf '4242\\n'; }}
                ensure_runtime_layout() {{ :; }}
                webdav_sync_enabled() {{ return 1; }}
                runtime_config_matches_source() {{ return 1; }}
                ensure_native_environment() {{ :; }}
                sync_runtime_config() {{ :; }}
                health_ok() {{ :; }}
                native_running() {{ :; }}
                native_port_pids() {{ printf '9999\\n'; }}
                native_pid_from_file() {{ printf '9999\\n'; }}
                native_pid_alive() {{ [[ "$1" == 9999 ]]; }}
                process_is_menu_app_pid() {{ [[ "$1" == 4242 ]]; }}
                request_native_processes_to_stop() {{ printf 'stop\\n' >> {q(str(calls))}; }}
                wait_for_native_port_released() {{ :; }}
                clear_transient_routing_state() {{ :; }}
                write_state() {{ :; }}
                start_service_process() {{ printf 'start:%s\\n' "$1" >> {q(str(calls))}; }}
                wait_for_managed_health() {{ [[ "$1" == 4242 ]]; }}
                clear_state() {{ :; }}
                PORT=49232
                NATIVE_WORKERS=1
                start_server >/dev/null
                [[ "$(cat "$NATIVE_OWNER_FILE")" == '4242 9999' ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse(calls.exists(), "a staged config must not restart a recovered service")

    def test_start_preserves_service_owned_by_a_live_previous_menu_process(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            calls = temp / "calls"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                source {q(str(ROOT / 'service/runtime_settings_configure.sh'))}
                require_menu_app_owner() {{ printf '4242\\n'; }}
                ensure_runtime_layout() {{ :; }}
                webdav_sync_enabled() {{ return 1; }}
                runtime_config_matches_source() {{ return 0; }}
                ensure_native_environment() {{ :; }}
                sync_runtime_config() {{ :; }}
                health_ok() {{ :; }}
                native_running() {{ :; }}
                native_port_pids() {{ printf '9999\\n'; }}
                native_pid_from_file() {{ printf '9999\\n'; }}
                native_pid_alive() {{ [[ "$1" == 9999 ]]; }}
                native_owner_record() {{ printf '1111 9999\\n'; }}
                process_is_menu_app_pid() {{ [[ "$1" == 1111 || "$1" == 4242 ]]; }}
                request_native_processes_to_stop() {{ printf 'stop\\n' >> {q(str(calls))}; }}
                wait_for_native_port_released() {{ :; }}
                clear_transient_routing_state() {{ :; }}
                write_state() {{ :; }}
                start_service_process() {{ printf 'start:%s\\n' "$1" >> {q(str(calls))}; }}
                wait_for_managed_health() {{ [[ "$1" == 4242 ]]; }}
                clear_state() {{ :; }}
                PORT=49232
                NATIVE_WORKERS=1
                start_server >/dev/null
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertFalse(calls.exists(), "normal start must not interrupt another live menu owner")

    def test_restart_clears_transient_routing_state_after_port_release(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cooldown_file = temp / "deployment-cooldowns.json"
            cooldown_lock = temp / "deployment-cooldowns.json.lock"
            recovery_file = temp / "route-recovery-state.json"
            recovery_lock = temp / "route-recovery-state.json.lock"
            cooldown_file.write_text(
                json.dumps(
                    {
                        "cooldowns": {
                            "id:route-a|surface:responses": {
                                "failures": 2,
                                "cooldown_until": 9999999999,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            cooldown_lock.write_text("", encoding="utf-8")
            recovery_file.write_text(
                json.dumps(
                    {
                        "recoveries": {
                            "request:old": {
                                "status": "polling",
                                "attempt": 12,
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )
            recovery_lock.write_text("", encoding="utf-8")
            observed_file = temp / "observed-cooldown-state"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {ROOT / 'service/process.sh'}
                require_menu_app_owner() {{ printf '4242\n'; }}
                ensure_native_environment() {{ :; }}
                sync_runtime_config() {{ :; }}
                write_state() {{ :; }}
                request_native_processes_to_stop() {{ :; }}
                wait_for_native_port_released() {{ :; }}
                start_service_process() {{
                  if grep -q '"cooldowns": {{}}' {cooldown_file} \
                    && grep -q '"recoveries": {{}}' {recovery_file}; then
                    printf 'cleared\n' > {observed_file}
                  fi
                }}
                wait_for_managed_health() {{ :; }}
                wait_for_runtime_config() {{ :; }}
                write_runtime_reload_fingerprint() {{ :; }}
                clear_state() {{ :; }}
                RUNTIME_DIR={temp}
                DEPLOYMENT_COOLDOWN_FILE={cooldown_file}
                ROUTE_RECOVERY_STATE_FILE={recovery_file}
                PYTHON={sys.executable}
                PORT=49232
                NATIVE_WORKERS=1
                restart_server >/dev/null
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(observed_file.read_text(encoding="utf-8"), "cleared\n")
            self.assertEqual(
                json.loads(cooldown_file.read_text(encoding="utf-8"))["cooldowns"],
                {},
            )
            self.assertEqual(
                json.loads(recovery_file.read_text(encoding="utf-8"))["recoveries"],
                {},
            )
            self.assertTrue(cooldown_lock.exists())
            self.assertTrue(recovery_lock.exists())

    def test_reload_clears_transient_routing_state_after_routes_verified(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            cooldown_file = temp / "deployment-cooldowns.json"
            recovery_file = temp / "route-recovery-state.json"
            cooldown_file.write_text(
                json.dumps({"cooldowns": {"id:old|surface:chat": {"failures": 2}}}),
                encoding="utf-8",
            )
            recovery_file.write_text(
                json.dumps({"recoveries": {"request:old": {"status": "polling"}}}),
                encoding="utf-8",
            )
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {ROOT / 'service/process.sh'}
                require_menu_app_owner() {{ :; }}
                ensure_python_tools() {{ :; }}
                native_master_pid() {{ printf '4242\n'; }}
                kill() {{ :; }}
                write_state() {{ :; }}
                wait_for_managed_health() {{ :; }}
                wait_for_runtime_config() {{ :; }}
                write_runtime_reload_fingerprint() {{ :; }}
                clear_state() {{ :; }}
                DEPLOYMENT_COOLDOWN_FILE={cooldown_file}
                ROUTE_RECOVERY_STATE_FILE={recovery_file}
                PYTHON={sys.executable}
                PORT=49232
                NATIVE_WORKERS=1
                reload_server >/dev/null
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(
                json.loads(cooldown_file.read_text(encoding="utf-8"))["cooldowns"],
                {},
            )
            self.assertEqual(
                json.loads(recovery_file.read_text(encoding="utf-8"))["recoveries"],
                {},
            )

    def test_apply_config_prefers_graceful_reload(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            calls = temp / "calls"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {ROOT / 'service/process.sh'}
                ensure_python_tools() {{ :; }}
                sync_runtime_config() {{ :; }}
                health_ok() {{ :; }}
                require_menu_app_owner() {{ :; }}
                runtime_reload_fingerprint_changed() {{ return 1; }}
                reload_server() {{ printf 'reload\n' >> {calls}; }}
                restart_server() {{ printf 'restart\n' >> {calls}; }}
                apply_config
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(calls.read_text(encoding="utf-8").splitlines(), ["reload"])

    def test_run_native_process_passes_worker_recycle_argument(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime_dir = temp / "runtime"
            callback_dir = temp / "litellm_menu"
            runtime_config = temp / "config.yaml"
            callback_source = callback_dir / "callbacks.py"
            log_file = temp / "menu-server.log"
            args_file = temp / "litellm-args.txt"
            proxy_flag_file = temp / "proxy-process-flag.txt"
            fake_litellm = temp / "litellm"
            runtime_dir.mkdir()
            callback_dir.mkdir()
            (callback_dir / "__init__.py").write_text("# test callback package\n", encoding="utf-8")
            runtime_config.write_text("model_list: []\n", encoding="utf-8")
            callback_source.write_text("# test callback\n", encoding="utf-8")
            fake_litellm.write_text(
                f"#!/bin/sh\nprintf '%s\\n' \"$@\" > {shlex.quote(str(args_file))}\n"
                f"printf '%s\\n' \"${{LITELLM_MENU_PROXY_PROCESS:-}}\" > {shlex.quote(str(proxy_flag_file))}\n",
                encoding="utf-8",
            )
            fake_litellm.chmod(fake_litellm.stat().st_mode | stat.S_IXUSR)

            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                require_menu_app_owner() {{ printf '4242\\n'; }}
                use_system_proxies_value() {{ printf '0\\n'; }}
                ensure_native_environment() {{ :; }}
                sync_runtime_config() {{ :; }}
                apply_system_proxy_guard() {{ :; }}
                process_is_menu_app_pid() {{ return 0; }}
                rotate_log_if_needed() {{ :; }}
                ROOT={q(str(temp))}
                TEMPLATE_ROOT={q(str(temp))}
                RUNTIME_DIR={q(str(runtime_dir))}
                RUNTIME_CONFIG={q(str(runtime_config))}
                CALLBACK_SOURCE={q(str(callback_source))}
                CALLBACK_PACKAGE_DIR={q(str(callback_dir))}
                LOG_FILE={q(str(log_file))}
                NATIVE_PID_FILE={q(str(temp / 'litellm.pid'))}
                LITELLM_BIN={q(str(fake_litellm))}
                MASTER_KEY=sk-test
                RECENT_REQUESTS_LOG={q(str(temp / 'recent.jsonl'))}
                LOCAL_LOG_MAX_BYTES=1024
                REQUEST_TIMEOUT_SECONDS=7200
                STALL_TIMEOUT_SECONDS=120
                STREAM_START_TIMEOUT_SECONDS=120
                CODEX_COMPACTION_START_TIMEOUT_SECONDS=300
                RECOVERY_MAX_SECONDS=43200
                RECOVERY_INTERVAL_SECONDS=5
                WEB_FETCH_TIMEOUT_SECONDS=30
                WEB_SEARCH_MAX_RESULTS=8
                WEB_SEARCH_READ_RESULTS=4
                WEB_SEARCH_READ_CHARS=1400
                WEB_SEARCH_DDGS_BACKEND=auto
                WEB_SEARCH_REGION=us-en
                WEB_SEARCH_MAX_ROUNDS=6
                WEB_SEARCH_MAX_QUERIES=16
                WEB_SEARCH_MAX_OPEN_PAGES=8
                WEB_SEARCH_MAX_FIND_IN_PAGE=12
                EXTERNAL_WEB_SEARCH_MODEL_RETRIES=2
                EXTERNAL_WEB_SEARCH_MODEL_RETRY_DELAY_SECONDS=1
                IMAGE_TOOL_FALLBACK_MAX_ATTEMPTS=3
                DEPLOYMENT_COOLDOWN_FAILURES=0
                DEPLOYMENT_COOLDOWN_SECONDS=0
                COMPUTER_FACADE_BACKEND=auto
                COMPUTER_FACADE_MODEL=
                COMPUTER_FACADE_MAX_STEPS=20
                COMPUTER_FACADE_TRACE=0
                COMPUTER_FACADE_TRACE_SCREENSHOTS=0
                COMPUTER_FACADE_ACTION_DENYLIST=
                COMPUTER_FACADE_REQUIRE_OBSERVATION=1
                LOCAL_MODEL_COST_MAP=True
                ROUTE_TRACE_PREVIEW_CHARS=2000
                HOST=127.0.0.1
                PORT=49232
                NATIVE_WORKERS=3
                NATIVE_MAX_REQUESTS_BEFORE_RESTART=37
                PROXY_TELEMETRY=False
                run_native_process
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            args = args_file.read_text(encoding="utf-8").splitlines()
            self.assertIn("--max_requests_before_restart", args)
            index = args.index("--max_requests_before_restart")
            self.assertEqual(args[index + 1], "37")
            self.assertEqual(proxy_flag_file.read_text(encoding="utf-8").strip(), "1")

    def test_run_native_skips_duplicate_when_a_healthy_service_is_retained(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            started = temp / "started"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                require_menu_app_owner() {{ printf '4242\\n'; }}
                health_ok() {{ :; }}
                adopt_native_service_for_menu_pid() {{ return 1; }}
                native_port_pids() {{ printf '9999\\n'; }}
                ensure_native_environment() {{ : > {q(str(started))}; }}
                PORT=49232
                run_native_process >/dev/null
                [[ ! -e {q(str(started))} ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_start_recovers_missing_lifecycle_records_without_restarting(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            owner_file = temp / "litellm.owner"
            pid_file = temp / "litellm.pid"
            started = temp / "started"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                source {q(str(ROOT / 'service/runtime_settings_configure.sh'))}
                NATIVE_OWNER_FILE={q(str(owner_file))}
                NATIVE_PID_FILE={q(str(pid_file))}
                require_menu_app_owner() {{ printf '4242\\n'; }}
                process_is_menu_app_pid() {{ [[ "$1" == 4242 ]]; }}
                native_pid_from_file() {{ printf '9999\\n'; }}
                native_pid_alive() {{ [[ "$1" == 9999 ]]; }}
                ensure_runtime_layout() {{ :; }}
                health_ok() {{ :; }}
                native_port_pids() {{ printf '9999\\n'; }}
                start_service_process() {{ : > {q(str(started))}; }}
                clear_state() {{ :; }}
                PORT=49232
                start_server >/dev/null
                [[ "$(cat "$NATIVE_OWNER_FILE")" == '4242 9999' ]]
                [[ "$(cat "$NATIVE_PID_FILE")" == '9999' ]]
                [[ ! -e {q(str(started))} ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)

    def test_port_release_wait_forces_after_configured_attempt(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            forced_file = temp / "forced"
            attempts_file = temp / "attempts"
            attempts_file.write_text("0\n", encoding="utf-8")
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {ROOT / 'service/process.sh'}
                HEALTH_WAIT_SECONDS=2
                native_port_pids() {{
                  python3 - <<'PY'
from pathlib import Path
path = Path({str(attempts_file)!r})
value = int(path.read_text(encoding='utf-8').strip() or '0') + 1
path.write_text(f"{{value}}\\n", encoding='utf-8')
PY
                  if [ ! -f {forced_file} ]; then
                    printf '12345\\n'
                  fi
                }}
                request_native_process_stop_list() {{ :; }}
                force_native_process_stop_list() {{ printf '1\\n' > {forced_file}; }}
                wait_for_native_port_released 5
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertTrue(forced_file.exists())
            self.assertLessEqual(int(attempts_file.read_text(encoding="utf-8")), 6)

    def test_stop_waits_for_direct_process_exit_without_launchctl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            launchctl_called = temp / "launchctl-called"
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                launchctl() {{ printf 'called\\n' > {q(str(launchctl_called))}; }}
                native_live_pid_candidates() {{
                  if [[ ! -f {q(str(temp / 'stopped'))} ]]; then
                    printf '12345\\n'
                  fi
                }}
                request_native_process_stop_list() {{ printf 'requested\\n' > {q(str(temp / 'stopped'))}; }}
                force_native_process_stop_list() {{ exit 1; }}
                clear_native_pid_file_if_stale_or_targeted() {{ :; }}
                clear_state() {{ :; }}
                stop_server
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=3,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertEqual(result.stdout.strip(), "LiteLLM stopped")
            self.assertEqual((temp / "stopped").read_text(encoding="utf-8"), "requested\n")
            self.assertFalse(launchctl_called.exists())

    def test_stop_fails_when_a_managed_process_survives_confirmation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            q = shlex.quote
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                source {q(str(ROOT / 'service/process.sh'))}
                native_live_pid_candidates() {{ printf '12345\\n'; }}
                request_native_process_stop_list() {{ :; }}
                force_native_process_stop_list() {{ :; }}
                write_state() {{ printf '%s\\n' "$1" > {q(str(temp / 'state'))}; }}
                print_native_health_failure() {{ printf '%s\\n' "$1" >&2; }}
                HEALTH_WAIT_SECONDS=1
                if stop_server; then
                  exit 1
                fi
                [[ "$(cat {q(str(temp / 'state'))})" == unhealthy ]]
                """
            ).lstrip()

            result = subprocess.run(
                ["/bin/bash", "-c", script],
                cwd=ROOT,
                text=True,
                capture_output=True,
                timeout=15,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr + result.stdout)
            self.assertIn("Timed out waiting for native LiteLLM processes to stop.", result.stderr)


if __name__ == "__main__":
    unittest.main()
