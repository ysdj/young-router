from __future__ import annotations

import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = ROOT / "scripts" / "version.py"


class VersionScriptTests(unittest.TestCase):
    def test_bump_keeps_cask_version_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "mac_menu").mkdir()
            (root / "Casks").mkdir()
            (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
            (root / "BUILD_NUMBER").write_text("1\n", encoding="utf-8")
            with (root / "mac_menu" / "Info.plist").open("wb") as handle:
                plistlib.dump(
                    {
                        "CFBundleShortVersionString": "1.0.0",
                        "CFBundleVersion": "1",
                    },
                    handle,
                )
            cask = root / "Casks" / "litellm-menu.rb"
            cask.write_text(
                'cask "litellm-menu" do\n'
                '  version "1.0.0"\n'
                '  url "https://github.com/example/litellm-menu/releases/download/'
                'v#{version}/litellm-menu-#{version}-macos-arm64.tar.zst"\n'
                'end\n',
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(VERSION_SCRIPT), "--root", str(root), "bump"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertEqual(result.stdout.strip(), "1.0.1 (2)")
            self.assertEqual((root / "VERSION").read_text(encoding="utf-8"), "1.0.1\n")
            self.assertEqual((root / "BUILD_NUMBER").read_text(encoding="utf-8"), "2\n")
            with (root / "mac_menu" / "Info.plist").open("rb") as handle:
                info = plistlib.load(handle)
            self.assertEqual(info["CFBundleShortVersionString"], "1.0.1")
            self.assertEqual(info["CFBundleVersion"], "2")
            cask_text = cask.read_text(encoding="utf-8")
            self.assertIn('version "1.0.1"', cask_text)
            self.assertIn("releases/download/v#{version}/", cask_text)


if __name__ == "__main__":
    unittest.main()
