from __future__ import annotations

import json
import os
import shlex
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
                "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                "LITELLM_PORT": "49231",
                "LITELLM_LAUNCH_AGENT_LABEL": "menu.litellm.service.test",
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

            service_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_LAUNCH_AGENT_LABEL']}.plist"
            menu_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_APP_LAUNCH_AGENT_LABEL']}.plist"
            self.assertFalse(service_plist.exists())
            self.assertTrue(menu_plist.exists())

            menu_text = menu_plist.read_text(encoding="utf-8")
            self.assertIn("/usr/bin/open", menu_text)
            self.assertIn("-gj", menu_text)
            self.assertIn("/Applications/LiteLLM Menu.app", menu_text)

            status = self.run_control("autostart-status", env)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(status.stdout.strip(), "enabled")

    def test_status_repairs_missing_menu_launch_agent_when_autostart_is_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            state_file = temp / "runtime/.litellm-runtime/autostart.enabled"
            service_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_LAUNCH_AGENT_LABEL']}.plist"
            menu_plist = temp / f"home/Library/LaunchAgents/{env['LITELLM_APP_LAUNCH_AGENT_LABEL']}.plist"
            state_file.parent.mkdir(parents=True, exist_ok=True)
            state_file.write_text("1\n", encoding="utf-8")

            status = self.run_control("autostart-status", env)
            self.assertEqual(status.returncode, 0, status.stderr)
            self.assertEqual(status.stdout.strip(), "enabled")
            self.assertFalse(service_plist.exists())
            self.assertTrue(menu_plist.exists())

    def test_disable_autostart_removes_both_launch_agents(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            self.assertEqual(self.run_control("autostart-enable", env).returncode, 0)

            result = self.run_control("autostart-disable", env)
            self.assertEqual(result.returncode, 0, result.stderr)

            self.assertFalse((temp / "runtime/.litellm-runtime/autostart.enabled").exists())
            self.assertFalse((temp / f"home/Library/LaunchAgents/{env['LITELLM_LAUNCH_AGENT_LABEL']}.plist").exists())
            self.assertFalse((temp / f"home/Library/LaunchAgents/{env['LITELLM_APP_LAUNCH_AGENT_LABEL']}.plist").exists())

    def test_checkout_lifecycle_actions_refuse_default_real_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = os.environ.copy()
            env.update(
                {
                    "HOME": str(temp / "home"),
                    "LITELLM_RUNTIME_ROOT": str(temp / "runtime"),
                    "LITELLM_TEMPLATE_ROOT": str(ROOT),
                    "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                }
            )

            result = self.run_control("stop", env, timeout=3)

            self.assertEqual(result.returncode, 64)
            self.assertIn("Refusing to run 'stop'", result.stderr)
            self.assertIn("LITELLM_PORT=4000", result.stderr)

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

            launchctl_log = temp / "launchctl-events.log"
            native_run_log = temp / "native-run.log"
            pid_file = runtime_dir / "litellm.pid"
            self.write_command(
                fake_bin,
                "launchctl",
                f"""
                #!/bin/sh
                printf '%s\\n' "$*" >> {launchctl_log}
                exit 0
                """,
            )
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
                      wait "$!"
                    fi
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            service_script.chmod(service_script.stat().st_mode | stat.S_IXUSR)
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
                    "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                    "LITELLM_MODEL_INFO_FILE": str(model_info_path),
                    "LITELLM_BIN": "/bin/sleep",
                    "LITELLM_NATIVE_PYTHON": sys.executable,
                    "LITELLM_NATIVE_PID_FILE": str(pid_file),
                    "LITELLM_PORT": "49232",
                    "LITELLM_LAUNCH_AGENT_LABEL": "menu.litellm.service.restart-test",
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
            events = launchctl_log.read_text(encoding="utf-8").splitlines()
            self.assertFalse(any(line.startswith("bootstrap ") for line in events), events)
            self.assertFalse(any(line.startswith("kickstart ") for line in events), events)

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
                bootout_launch_agent() {{ :; }}
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
                bootout_launch_agent() {{ :; }}
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
                route_trace_effective_value() {{ printf '0\\n'; }}
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
                RECOVERY_MAX_SECONDS=43200
                RECOVERY_INTERVAL_SECONDS=5
                WEB_FETCH_TIMEOUT_SECONDS=30
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
                ROUTE_TRACE_STATE_FILE={q(str(temp / 'route-trace.enabled'))}
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

    def test_stop_kills_directly_without_launchctl(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            env = self.make_env(temp)
            fake_bin = temp / "bin"
            launchctl_called = temp / "launchctl-called"

            self.write_command(
                fake_bin,
                "lsof",
                """
                #!/bin/sh
                printf '11111\\n'
                """,
            )
            self.write_command(
                fake_bin,
                "ps",
                f"""
                #!/bin/sh
                printf '%s\\n' '{temp}/runtime/.venv/bin/python {temp}/runtime/.venv/bin/litellm --config config.yaml'
                """,
            )
            self.write_command(
                fake_bin,
                "launchctl",
                f"""
                #!/bin/sh
                printf '1\\n' > {launchctl_called}
                sleep 60
                exit 0
                """,
            )

            result = self.run_control("stop", env, timeout=3)

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.strip(), "LiteLLM stopped")
            self.assertFalse(launchctl_called.exists())


if __name__ == "__main__":
    unittest.main()
