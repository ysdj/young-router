from __future__ import annotations

import subprocess
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
COMMON_MODELS = ROOT / "mac_menu" / "Sources" / "CommonModels.swift"


HARNESS = r"""
import Foundation

let root = CommandLine.arguments[1]
let environment = ["LITELLM_PORT": "4100"]
print(localServicePort(runtimeRoot: root, environment: environment))
"""


class RuntimeSettingsSwiftHelperTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = tempfile.TemporaryDirectory()
        temp = Path(cls.temp.name)
        harness = temp / "main.swift"
        harness.write_text(textwrap.dedent(HARNESS), encoding="utf-8")
        cls.binary = temp / "runtime-settings-swift-helper"
        result = subprocess.run(
            [
                "swiftc",
                str(COMMON_MODELS),
                str(harness),
                "-o",
                str(cls.binary),
                "-framework",
                "Cocoa",
            ],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            raise RuntimeError(result.stdout + result.stderr)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.temp.cleanup()

    def port_for(self, settings: str | None) -> str:
        with tempfile.TemporaryDirectory() as runtime:
            if settings is not None:
                Path(runtime, "runtime-settings.env").write_text(
                    settings, encoding="utf-8"
                )
            result = subprocess.run(
                [str(self.binary), runtime],
                cwd=ROOT,
                text=True,
                capture_output=True,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def test_saved_port_overrides_process_environment_like_service_loader(self) -> None:
        self.assertEqual(self.port_for("LITELLM_PORT=4200\n"), "4200")

    def test_environment_port_is_fallback_when_no_saved_value_exists(self) -> None:
        self.assertEqual(self.port_for(None), "4100")

    def test_invalid_saved_port_falls_back_to_valid_environment_value(self) -> None:
        self.assertEqual(self.port_for("LITELLM_PORT=invalid\n"), "4100")


if __name__ == "__main__":
    unittest.main()
