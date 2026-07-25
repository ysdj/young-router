from __future__ import annotations

import re
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
RUNNER = ROOT / "service" / "timestamp_log_runner.py"


class TimestampLogRunnerTests(unittest.TestCase):
    def test_adds_one_utc_timestamp_to_unstamped_service_output(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            service = temp / "service.sh"
            log = temp / "menu-server.log"
            service.write_text(
                "#!/bin/bash\n"
                "printf 'plain stdout\\n'\n"
                "printf 'plain stderr\\n' >&2\n"
                "printf '[2026-07-20T20:13:44Z] existing timestamp\\n'\n",
                encoding="utf-8",
            )
            service.chmod(0o700)

            result = subprocess.run(
                [sys.executable, str(RUNNER), str(service), str(log), str(temp)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 3)
            self.assertTrue(all(re.match(r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\]", line) for line in lines[:2]))
            self.assertEqual(lines[2], "[2026-07-20T20:13:44Z] existing timestamp")

    def test_blank_output_does_not_become_a_timestamp_only_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            service = temp / "service.sh"
            log = temp / "menu-server.log"
            service.write_text("#!/bin/bash\nprintf '\\n'\nprintf 'record\\n'\n", encoding="utf-8")
            service.chmod(0o700)

            result = subprocess.run(
                [sys.executable, str(RUNNER), str(service), str(log), str(temp)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            lines = log.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertRegex(lines[0], r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] record$")

    def test_runs_a_no_argument_script_when_requested(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            script = temp / "watch.sh"
            log = temp / "config-watch.log"
            script.write_text("#!/bin/bash\nprintf 'watcher started\\n'\n", encoding="utf-8")
            script.chmod(0o700)

            result = subprocess.run(
                [sys.executable, str(RUNNER), str(script), str(log), str(temp), "--"],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertRegex(
                log.read_text(encoding="utf-8"),
                r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] watcher started\n$",
            )

    def test_start_failure_writes_a_canonical_safe_record(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            log = temp / "menu-server.log"

            result = subprocess.run(
                [sys.executable, str(RUNNER), str(temp / "missing.sh"), str(log), str(temp)],
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertRegex(
                log.read_text(encoding="utf-8"),
                r"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z\] timestamp log runner could not start the local script\n$",
            )


if __name__ == "__main__":
    unittest.main()
