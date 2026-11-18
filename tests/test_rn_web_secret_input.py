"""The browser secret input keeps its auto-commit and save handshake."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DRIVER = Path(__file__).resolve().with_name("test_rn_web_secret_input.mjs")


def _browser() -> str | None:
    candidates = [
        os.environ.get("CHROME_PATH", ""),
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
        "/usr/bin/google-chrome",
        "/usr/bin/chromium",
        "/usr/bin/chromium-browser",
    ]
    return next((candidate for candidate in candidates if candidate and Path(candidate).exists()), None)


@unittest.skipUnless(shutil.which("node"), "node is required to build and drive the page")
@unittest.skipUnless(_browser(), "a Chrome or Chromium binary is required (set CHROME_PATH)")
@unittest.skipUnless(
    (ROOT / "rn" / "apps" / "web" / "node_modules" / "react-native-web").exists()
    and (ROOT / "rn" / "node_modules" / "esbuild").exists(),
    "the web host's own dependencies are not installed",
)
class WebSecretInputTests(unittest.TestCase):
    """A blur before the quiet period cannot lose a password-only edit."""

    def test_blur_keeps_auto_commit_and_reports_saved(self) -> None:
        completed = subprocess.run(
            ["node", str(DRIVER)],
            capture_output=True,
            text=True,
            timeout=300,
        )
        payload = completed.stdout.strip().splitlines()[-1] if completed.stdout.strip() else "{}"
        try:
            result = json.loads(payload)
        except ValueError:
            self.fail(f"the secret-input driver answered no result:\n{completed.stdout}\n{completed.stderr}")
        self.assertEqual(0, completed.returncode, completed.stderr or completed.stdout)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual({"status": "saved", "saved": True, "writes": 1}, result.get("state"))


if __name__ == "__main__":
    unittest.main()
