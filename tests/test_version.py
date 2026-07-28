from __future__ import annotations

import codecs
import plistlib
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = ROOT / "scripts" / "version.py"


class VersionScriptTests(unittest.TestCase):
    @staticmethod
    def _write_fixture(root: Path) -> None:
        (root / "Casks").mkdir()
        (root / "rn" / "apps" / "macos" / "macos" / "LiteLLMMenu-macOS").mkdir(
            parents=True
        )
        (root / "rn" / "apps" / "macos" / "macos" / "LiteLLMMenu.xcodeproj").mkdir(
            parents=True
        )
        (root / "rn" / "apps" / "windows" / "windows" / "LiteLLMMenu").mkdir(
            parents=True
        )
        (
            root
            / "rn"
            / "apps"
            / "windows"
            / "windows"
            / "LiteLLMMenu.Package"
        ).mkdir(parents=True)
        (root / "VERSION").write_text("1.0.0\n", encoding="utf-8")
        (root / "BUILD_NUMBER").write_text("1\n", encoding="utf-8")
        info_path = (
            root
            / "rn"
            / "apps"
            / "macos"
            / "macos"
            / "LiteLLMMenu-macOS"
            / "Info.plist"
        )
        with info_path.open("wb") as handle:
            plistlib.dump(
                {
                    "CFBundleShortVersionString": "1.0.0",
                    "CFBundleVersion": "1",
                },
                handle,
            )
        (
            root
            / "rn"
            / "apps"
            / "macos"
            / "macos"
            / "LiteLLMMenu.xcodeproj"
            / "project.pbxproj"
        ).write_text(
            "\t\t\t\tCURRENT_PROJECT_VERSION = 1;\n",
            encoding="utf-8",
        )
        manifest_paths = (
            root
            / "rn"
            / "apps"
            / "windows"
            / "windows"
            / "LiteLLMMenu"
            / "Package.appxmanifest",
            root
            / "rn"
            / "apps"
            / "windows"
            / "windows"
            / "LiteLLMMenu.Package"
            / "Package.appxmanifest",
        )
        manifest_text = (
            '<?xml version="1.0" encoding="utf-8"?>\r\n'
            '<Package><Identity Name="LiteLLMMenu" Publisher="CN=Development" '
            'Version="1.0.0.0" /></Package>\r\n'
        ).encode("utf-8")
        manifest_paths[0].write_bytes(codecs.BOM_UTF8 + manifest_text)
        manifest_paths[1].write_bytes(manifest_text)

    def test_bump_keeps_cask_version_in_sync(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_fixture(root)
            cask = root / "Casks" / "litellm-menu.rb"
            cask.write_text(
                'cask "litellm-menu" do\n'
                '  version "1.0.0,1"\n'
                '  url "https://github.com/example/litellm-menu/releases/download/'
                'v#{version.csv.first}/litellm-menu-#{version.csv.first}-'
                '#{version.csv.second}-macos-arm64.tar.zst"\n'
                'end\n',
                encoding="utf-8",
            )
            rn_plist_path = (
                root
                / "rn"
                / "apps"
                / "macos"
                / "macos"
                / "LiteLLMMenu-macOS"
                / "Info.plist"
            )
            rn_plist_before = rn_plist_path.read_bytes()

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
            with rn_plist_path.open("rb") as handle:
                rn_info = plistlib.load(handle)
            self.assertEqual(rn_info["CFBundleShortVersionString"], "1.0.1")
            self.assertEqual(rn_info["CFBundleVersion"], "2")
            self.assertEqual(
                rn_plist_path.read_bytes(),
                rn_plist_before.replace(b"<string>1.0.0</string>", b"<string>1.0.1</string>")
                .replace(b"<string>1</string>", b"<string>2</string>"),
            )
            xcode_project = (
                root
                / "rn"
                / "apps"
                / "macos"
                / "macos"
                / "LiteLLMMenu.xcodeproj"
                / "project.pbxproj"
            ).read_text(encoding="utf-8")
            self.assertIn("CURRENT_PROJECT_VERSION = 2;", xcode_project)
            self.assertIn("MARKETING_VERSION = 1.0.1;", xcode_project)
            self.assertEqual(xcode_project.count("CURRENT_PROJECT_VERSION"), 1)
            manifest_paths = (
                root
                / "rn"
                / "apps"
                / "windows"
                / "windows"
                / "LiteLLMMenu"
                / "Package.appxmanifest",
                root
                / "rn"
                / "apps"
                / "windows"
                / "windows"
                / "LiteLLMMenu.Package"
                / "Package.appxmanifest",
            )
            for manifest_path in manifest_paths:
                self.assertIn('Version="1.0.1.2"', manifest_path.read_text(encoding="utf-8-sig"))
                self.assertIn(b"\r\n", manifest_path.read_bytes())
            self.assertTrue(manifest_paths[0].read_bytes().startswith(codecs.BOM_UTF8))
            self.assertFalse(manifest_paths[1].read_bytes().startswith(codecs.BOM_UTF8))
            cask_text = cask.read_text(encoding="utf-8")
            self.assertIn('version "1.0.1,2"', cask_text)
            self.assertIn("releases/download/v#{version.csv.first}/", cask_text)

    def test_sync_rejects_windows_version_components_over_65535(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self._write_fixture(root)
            (root / "BUILD_NUMBER").write_text("65536\n", encoding="utf-8")
            (root / "Casks" / "litellm-menu.rb").write_text(
                'cask "litellm-menu" do\n  version "1.0.0,1"\nend\n',
                encoding="utf-8",
            )

            result = subprocess.run(
                [sys.executable, str(VERSION_SCRIPT), "--root", str(root), "sync"],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )

            self.assertEqual(result.returncode, 1)
            self.assertIn("must not exceed 65535", result.stderr)
            rn_plist_path = (
                root
                / "rn"
                / "apps"
                / "macos"
                / "macos"
                / "LiteLLMMenu-macOS"
                / "Info.plist"
            )
            with rn_plist_path.open("rb") as handle:
                info = plistlib.load(handle)
            self.assertEqual(info["CFBundleVersion"], "1")


if __name__ == "__main__":
    unittest.main()
