import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


@unittest.skipUnless(sys.platform == "darwin", "requires the macOS Swift SDK and bundle tools")
class MacOSAutoUpdateTests(unittest.TestCase):
    def test_release_selection_and_bundle_install(self) -> None:
        tests = Path(__file__).resolve().parent
        updater = tests.parent / "rn/apps/macos/src/native/macos/AutoUpdater.swift"
        with tempfile.TemporaryDirectory() as directory:
            executable = Path(directory) / "macos-auto-update-tests"
            subprocess.run(
                ["xcrun", "swiftc", str(updater), str(tests / "macos_auto_update.swift"), "-o", str(executable)],
                check=True,
                timeout=90,
            )
            subprocess.run([str(executable)], check=True, timeout=30)
