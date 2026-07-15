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
    def test_bump_keeps_formula_tag_and_version_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "mac_menu").mkdir()
            (root / "Formula").mkdir()
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
            formula = root / "Formula" / "litellm-menu.rb"
            formula.write_text(
                'class LitellmMenu < Formula\n'
                '  url "https://github.com/example/litellm-menu/releases/download/'
                'v1.0.0/litellm-menu-1.0.0-macos-arm64.tar.zst"\n'
                '  version "1.0.0"\n'
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
            formula_text = formula.read_text(encoding="utf-8")
            self.assertIn("releases/download/v1.0.1/", formula_text)
            self.assertIn("litellm-menu-1.0.1-macos-arm64.tar.zst", formula_text)
            self.assertIn('version "1.0.1"', formula_text)


if __name__ == "__main__":
    unittest.main()
