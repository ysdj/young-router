from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CONTROL = ROOT / "service.sh"


class ControlLiteLLMVersionTests(unittest.TestCase):
    def runtime_paths(self, temp: Path, bundled_version: str) -> list[str]:
        bundled = temp / "bundled-runtime"
        bin_dir = bundled / "bin"
        bin_dir.mkdir(parents=True)
        for name in ("python", "litellm"):
            executable = bin_dir / name
            executable.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            executable.chmod(executable.stat().st_mode | stat.S_IXUSR)
        (bundled / "LITELLM_VERSION").write_text(
            f"{bundled_version}\n",
            encoding="utf-8",
        )

        env = os.environ.copy()
        for key in ("LITELLM_VENV_DIR", "LITELLM_NATIVE_PYTHON", "LITELLM_BIN"):
            env.pop(key, None)
        env.update(
            {
                "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                "LITELLM_RUNTIME_ROOT": str(temp / "user-runtime"),
                "LITELLM_TEMPLATE_ROOT": str(ROOT),
                "LITELLM_BUNDLED_RUNTIME_DIR": str(bundled),
                "LITELLM_VERSION_FILE": str(ROOT / "LITELLM_VERSION"),
            }
        )
        result = subprocess.run(
            [
                "/bin/bash",
                "-c",
                'source "$1/service/environment.sh"; '
                'printf "%s\\n%s\\n%s\\n" "$VENV_DIR" "$NATIVE_PYTHON" "$LITELLM_BIN"',
                "runtime-path-test",
                str(ROOT),
            ],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        return result.stdout.splitlines()

    def test_matching_bundled_runtime_is_used_without_bootstrap(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            version = (ROOT / "LITELLM_VERSION").read_text(encoding="utf-8").strip()

            paths = self.runtime_paths(temp, version)

            bundled = temp / "bundled-runtime"
            self.assertEqual(
                paths,
                [
                    str(bundled),
                    str(bundled / "bin" / "python"),
                    str(bundled / "bin" / "litellm"),
                ],
            )

    def test_mismatched_bundled_runtime_falls_back_to_user_venv(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)

            paths = self.runtime_paths(temp, "0.0.0")

            user_venv = temp / "user-runtime" / ".venv"
            self.assertEqual(
                paths,
                [
                    str(user_venv),
                    str(user_venv / "bin" / "python"),
                    str(user_venv / "bin" / "litellm"),
                ],
            )

    def test_bootstrap_installs_exact_locked_litellm_version(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            runtime = temp / "runtime"
            venv = runtime / ".venv"
            bin_dir = venv / "bin"
            bin_dir.mkdir(parents=True)
            install_log = temp / "uv-install.log"
            locked_version = (ROOT / "LITELLM_VERSION").read_text(encoding="utf-8").strip()

            uv = temp / "uv"
            uv.write_text(
                textwrap.dedent(
                    f"""
                    #!/bin/sh
                    printf '%s\n' "$*" > {install_log}
                    exit 0
                    """
                ).lstrip(),
                encoding="utf-8",
            )
            uv.chmod(uv.stat().st_mode | stat.S_IXUSR)

            env = os.environ.copy()
            env.update(
                {
                    "LITELLM_ALLOW_CHECKOUT_SERVICE": "1",
                    "LITELLM_RUNTIME_ROOT": str(runtime),
                    "LITELLM_TEMPLATE_ROOT": str(ROOT),
                    "LITELLM_VENV_DIR": str(venv),
                    "LITELLM_NATIVE_PYTHON": sys.executable,
                    "LITELLM_BIN": str(bin_dir / "litellm"),
                    "LITELLM_UV_BIN": str(uv),
                    "LITELLM_PORT": "49321",
                    "LITELLM_LAUNCH_AGENT_LABEL": "menu.litellm.service.version-test",
                    "LITELLM_APP_LAUNCH_AGENT_LABEL": "menu.litellm.menu-login.version-test",
                    "LITELLM_CONFIG_WATCH_LABEL": "menu.litellm.config-watch.version-test",
                }
            )

            result = subprocess.run(
                ["/bin/bash", str(CONTROL), "bootstrap"],
                cwd=ROOT,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            install_args = install_log.read_text(encoding="utf-8")
            self.assertIn(f"litellm[proxy]=={locked_version}", install_args)
            self.assertNotIn("litellm[proxy] Pillow", install_args)

    def test_release_lock_is_bundled_by_build_scripts(self) -> None:
        app_script = (ROOT / "app.sh").read_text(encoding="utf-8")
        build_script = (ROOT / "mac_menu" / "build.sh").read_text(encoding="utf-8")
        package_script = (ROOT / "scripts" / "package-release.sh").read_text(
            encoding="utf-8"
        )

        self.assertIn("LITELLM_VERSION", app_script)
        self.assertIn('! -d "$ROOT/mac_menu/Sources"', app_script)
        self.assertIn("LITELLM_VERSION", build_script)
        self.assertIn('-file-prefix-map "$ROOT=."', build_script)
        self.assertIn('-debug-prefix-map "$ROOT=."', build_script)
        self.assertIn('"litellm==$LITELLM_VERSION"', package_script)
        self.assertIn("runtime-requirements.txt", package_script)


if __name__ == "__main__":
    unittest.main()
