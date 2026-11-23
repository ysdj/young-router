from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from scripts import update_litellm


ROOT = Path(__file__).resolve().parents[1]
UPDATE_SCRIPT = ROOT / "scripts" / "update-litellm.sh"


class UpdateLiteLLMTests(unittest.TestCase):
    def run_update(self, *arguments: str, locked_version: str = "1.90.0"):
        temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        temp = Path(temp_dir.name)
        lock_file = temp / "LITELLM_VERSION"
        lock_file.write_text(f"{locked_version}\n", encoding="utf-8")
        payload_file = temp / "litellm.json"
        payload_file.write_text(
            json.dumps(
                {
                    "releases": {
                        "1.91.2": [
                            {
                                "packagetype": "bdist_wheel",
                                "filename": "litellm-1.91.2-py3-none-any.whl",
                                "yanked": False,
                            }
                        ],
                        "1.92.0": [
                            {
                                "packagetype": "bdist_wheel",
                                "filename": "litellm-1.92.0-cp312-cp312-manylinux_2_28_aarch64.whl",
                                "yanked": False,
                            },
                            {
                                "packagetype": "sdist",
                                "filename": "litellm-1.92.0.tar.gz",
                                "yanked": False,
                            },
                        ],
                        "1.92.1": [
                            {
                                "packagetype": "bdist_wheel",
                                "filename": "litellm-1.92.1-cp310-cp310-macosx_11_0_arm64.whl",
                                "yanked": False,
                            }
                        ],
                        "1.93.0rc1": [
                            {
                                "packagetype": "bdist_wheel",
                                "filename": "litellm-1.93.0rc1-py3-none-any.whl",
                                "yanked": False,
                            }
                        ],
                    }
                }
            ),
            encoding="utf-8",
        )
        env = os.environ.copy()
        env.update(
            {
                "LITELLM_UPDATE_PYTHON": sys.executable,
                "LITELLM_VERSION_FILE": str(lock_file),
                "LITELLM_PYPI_JSON_URL": payload_file.as_uri(),
            }
        )
        result = subprocess.run(
            ["/bin/bash", str(UPDATE_SCRIPT), *arguments],
            cwd=ROOT,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
        return result, lock_file

    def test_update_skips_newer_release_without_a_python_312_artifact(self) -> None:
        result, lock_file = self.run_update()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "1.92.0\n")
        self.assertIn("1.90.0 -> 1.92.0", result.stdout)

    def test_check_accepts_latest_stable_release_with_an_installable_artifact(self) -> None:
        result, lock_file = self.run_update("--check", locked_version="1.92.0")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "1.92.0\n")
        self.assertIn("lock is current: 1.92.0", result.stdout)

    def test_build_update_syncs_the_windows_venv_only_with_x64_python(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            checkout = Path(directory)
            lock_file = checkout / "LITELLM_VERSION"
            lock_file.write_text("1.92.0\n", encoding="utf-8")
            venv = checkout / ".venv"
            interpreter = venv / "Scripts" / "python.exe"
            interpreter.parent.mkdir(parents=True)
            interpreter.touch()
            (venv / "pyvenv.cfg").touch()

            for platform in ("win-amd64", "win-arm64"):
                with (
                    self.subTest(platform=platform),
                    mock.patch.object(update_litellm, "ROOT", checkout),
                    mock.patch.object(update_litellm, "LOCK_FILE", lock_file),
                    mock.patch.object(update_litellm.sys, "platform", "win32"),
                    mock.patch.object(update_litellm, "latest_stable_version", return_value="1.92.0"),
                    mock.patch.object(update_litellm.shutil, "which", return_value="uv"),
                    mock.patch.object(update_litellm.subprocess, "check_output", return_value=platform + "\n"),
                    mock.patch.object(update_litellm.subprocess, "run") as install,
                ):
                    self.assertEqual(update_litellm.main(["--sync-venv"]), 0)
                    if platform == "win-amd64":
                        install.assert_called_once_with(
                            ["uv", "pip", "install", "--python", str(venv), "litellm[proxy]==1.92.0"],
                            check=True,
                        )
                    else:
                        install.assert_not_called()


if __name__ == "__main__":
    unittest.main()
