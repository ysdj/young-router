from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


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

    def test_update_skips_newer_release_without_universal_wheel(self) -> None:
        result, lock_file = self.run_update()

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "1.91.2\n")
        self.assertIn("1.92.0 has no universal", result.stderr)
        self.assertIn("1.90.0 -> 1.91.2", result.stdout)

    def test_check_accepts_latest_universal_wheel_release(self) -> None:
        result, lock_file = self.run_update("--check", locked_version="1.91.2")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertEqual(lock_file.read_text(encoding="utf-8"), "1.91.2\n")
        self.assertIn("lock is current: 1.91.2", result.stdout)


if __name__ == "__main__":
    unittest.main()
