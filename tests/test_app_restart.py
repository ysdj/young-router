from __future__ import annotations

import subprocess
import os
import stat
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
APP_SCRIPT = ROOT / "app.sh"
BUILD_SCRIPT = ROOT / "mac_menu" / "build.sh"


class AppRestartTests(unittest.TestCase):
    def test_app_path_is_canonicalized_before_pid_matching(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            set -- version
            LITELLM_APP_PATH=/tmp/LiteLLM-Test.app
            source {APP_SCRIPT!s} >/dev/null
            [[ "$APP" == /private/tmp/LiteLLM-Test.app ]]
            [[ "$LITELLM_APP_PATH" == "$APP" ]]
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

    def test_open_requires_a_current_non_previous_app_pid_and_checks_that_owner(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            set -- version
            source {APP_SCRIPT!s} >/dev/null
            app_pids() {{ printf '101\\n202\\n'; }}
            app_pid_matches_current_bundle() {{ [[ "$1" == 202 ]]; }}
            [[ "$(current_app_pid 101)" == 202 ]]
            app_running() {{ return 0; }}
            wait_for_current_app_pid() {{
              [[ "$1" == 101 ]]
              printf '202\\n'
            }}
            require_control() {{ :; }}
            service_running_for_owner() {{ [[ "$1" == 202 ]]; }}
            open_litellm_app 101
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
        self.assertIn("app pid 202 owns the healthy service", result.stdout)

    def test_script_never_force_kills_a_menu_process_after_graceful_quit_times_out(self) -> None:
        source = APP_SCRIPT.read_text(encoding="utf-8")
        close_body = source.split("close_litellm_app() {", 1)[1].split("\n}\n\nopen_litellm_app", 1)[0]
        self.assertIn("refusing to force-kill", close_body)
        self.assertNotIn('kill "$pid"', close_body)

    def test_restart_closes_the_old_owner_before_replacing_the_bundle(self) -> None:
        source = APP_SCRIPT.read_text(encoding="utf-8")
        restart_body = source.split("restart_litellm_app() {", 1)[1].split("\n}\n\ncase", 1)[0]
        self.assertLess(restart_body.index("close_litellm_app"), restart_body.index("build_app"))

    def test_failed_build_reopens_the_previous_app_instance(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            set -- version
            source {APP_SCRIPT!s} >/dev/null
            DISRUPTIVE=1
            calls=""
            app_pids() {{ printf '101\\n'; }}
            suspend_app_launch_agent() {{ calls="$calls suspend"; }}
            restore_app_launch_agent() {{ calls="$calls restore"; }}
            close_litellm_app() {{ calls="$calls close"; }}
            needs_build() {{ return 0; }}
            build_app() {{
              [[ "$1" == *restart-backup* ]]
              calls="$calls build"
              return 1
            }}
            wait_for_restart_quiescence() {{ calls="$calls quiet"; }}
            open_litellm_app() {{
              [[ "$1" == 101 ]]
              calls="$calls reopen"
            }}
            if restart_litellm_app; then
              exit 1
            fi
            [[ "$calls" == " suspend close quiet build reopen restore" ]]
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
        self.assertIn("previous LiteLLM Menu app was restored", result.stderr)

    def test_restart_requires_an_explicit_disruptive_flag(self) -> None:
        result = subprocess.run(
            ["/bin/bash", str(APP_SCRIPT), "restart"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 75, result.stderr + result.stdout)
        self.assertIn("--disruptive", result.stderr)

    def test_sourcing_app_script_never_executes_the_requested_action(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            set -- restart --disruptive
            source {APP_SCRIPT!s} >/dev/null
            called=""
            restart_litellm_app() {{ called=restart; }}
            [[ -z "$called" ]]
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

    def test_open_never_launches_the_app_as_a_shell_child(self) -> None:
        source = APP_SCRIPT.read_text(encoding="utf-8")
        open_body = source.split("open_litellm_app() {", 1)[1].split("\n}\n\nrestart_litellm_app", 1)[0]
        launch_body = source.split("launch_litellm_app() {", 1)[1].split(
            "\n}\n\nwait_for_app_stability", 1
        )[0]
        self.assertNotIn('"$BIN"', launch_body)
        self.assertNotIn("direct_launch_litellm_app", source)
        self.assertIn('/usr/bin/open -n "${launch_environment[@]}" "$APP"', launch_body)
        self.assertNotIn("direct_launch_litellm_app", open_body)
        self.assertIn("LITELLM_MENU_TEST_HEADLESS", launch_body)

    def test_open_falls_back_when_launch_services_accepts_but_never_creates_a_pid(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            marker = Path(temp_dir) / "launch-services"
            script = textwrap.dedent(
                f"""
                set -euo pipefail
                set -- version
                source {APP_SCRIPT!s} >/dev/null
                launch_litellm_app() {{
                  printf launched > {str(marker)!r}.started
                }}
                current_app_pid() {{
                  [[ -f {str(marker)!r}.started ]] && printf '202\\n' || return 1
                }}
                wait_for_current_app_pid() {{ current_app_pid "$1"; }}
                service_running_for_owner() {{ [[ "$1" == 202 ]]; }}
                wait_for_service_running() {{ [[ "$1" == 202 ]]; }}
                wait_for_app_stability() {{ [[ "$1" == 202 ]]; }}
                require_control() {{ :; }}
                open_litellm_app >/dev/null
                [[ -f {str(marker)!r}.started ]]
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
            self.assertTrue(Path(f"{marker}.started").exists())

    def test_restart_uses_launch_services_new_instance(self) -> None:
        source = APP_SCRIPT.read_text(encoding="utf-8")
        launch_body = source.split("launch_litellm_app() {", 1)[1].split(
            "\n}\n\nwait_for_app_stability", 1
        )[0]
        force_branch = launch_body.split('if [[ "$force_new" == "1" ]]', 1)[1].split(
            "fi", 1
        )[0]
        self.assertIn('/usr/bin/open -n "${launch_environment[@]}" "$APP"', force_branch)
        self.assertNotIn('"$BIN"', force_branch)

    def test_restarted_old_pid_does_not_suppress_a_new_launch(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            set -- version
            source {APP_SCRIPT!s} >/dev/null
            calls=""
            app_pids() {{ printf '101\\n'; }}
            current_app_pid() {{
              [[ "$calls" == *launch* ]] && printf '202\\n' || return 1
            }}
            launch_litellm_app() {{ calls="$calls launch"; }}
            wait_for_current_app_pid() {{ current_app_pid "$1"; }}
            service_running_for_owner() {{ [[ "$1" == 202 ]]; }}
            wait_for_service_running() {{ [[ "$1" == 202 ]]; }}
            wait_for_app_stability() {{ [[ "$1" == 202 ]]; }}
            require_control() {{ :; }}
            app_running() {{ return 0; }}
            open_litellm_app 101 >/dev/null
            [[ "$calls" == " launch" ]]
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

    def test_new_app_must_remain_stable_before_open_reports_success(self) -> None:
        script = textwrap.dedent(
            f"""
            set -euo pipefail
            set -- version
            source {APP_SCRIPT!s} >/dev/null
            calls=""
            current_app_pid() {{ [[ "$calls" == launched ]] && printf '202\n' || return 1; }}
            wait_for_current_app_pid() {{ current_app_pid; }}
            launch_litellm_app() {{ calls=launched; }}
            wait_for_service_running() {{ return 0; }}
            wait_for_app_stability() {{ return 1; }}
            require_control() {{ :; }}
            service_running_for_owner() {{ return 0; }}
            any_app_pid() {{ return 1; }}
            if open_litellm_app; then
              exit 1
            fi
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
        self.assertIn("did not remain alive", result.stderr)

    def test_restart_keeps_backup_until_new_owner_is_healthy(self) -> None:
        source = APP_SCRIPT.read_text(encoding="utf-8")
        restart_body = source.split("restart_litellm_app() {", 1)[1].split("\n}\n\ncase", 1)[0]
        self.assertIn('build_app "$backup_app"', restart_body)
        self.assertLess(
            restart_body.index('open_litellm_app "$previous_pids"'),
            restart_body.index('rm -rf "$backup_app"'),
        )

        build_source = BUILD_SCRIPT.read_text(encoding="utf-8")
        self.assertIn('LITELLM_KEEP_BACKUP', build_source)
        self.assertIn('LITELLM_BACKUP_APP_PATH', build_source)

    def test_direct_build_refuses_to_replace_a_running_target_before_compilation(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            app = temp / "LiteLLM Menu.app"
            binary = app / "Contents/MacOS/LiteLLMMenu"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\nsleep 30\n", encoding="utf-8")
            binary.chmod(binary.stat().st_mode | stat.S_IXUSR)
            running = subprocess.Popen([str(binary)])
            try:
                env = os.environ.copy()
                env["LITELLM_APP_PATH"] = str(app)
                result = subprocess.run(
                    ["/bin/bash", str(BUILD_SCRIPT)],
                    cwd=ROOT,
                    env=env,
                    text=True,
                    capture_output=True,
                    timeout=5,
                    check=False,
                )
            finally:
                running.terminate()
                running.wait(timeout=3)

            self.assertNotEqual(result.returncode, 0)
            self.assertIn("refusing to replace its bundle in place", result.stderr)
            self.assertIn("app.sh restart", result.stderr)

    def test_direct_build_rechecks_the_target_immediately_before_bundle_swap(self) -> None:
        source = BUILD_SCRIPT.read_text(encoding="utf-8")
        swap_body = source.split('codesign --verify --deep --strict "$BUILD_APP"', 1)[1]
        self.assertIn("guard_app_not_running || exit 1", swap_body)
        self.assertLess(
            swap_body.index("guard_app_not_running || exit 1"),
            swap_body.index('if [[ -e "$APP" ]]'),
        )


if __name__ == "__main__":
    unittest.main()
