from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from young_router.core.claude_desktop import (
    ClaudeDesktopConfig,
    ClaudeDesktopConfigError,
    ClaudeDeveloperSettings,
)


class ClaudeDesktopEdgeCaseTests(unittest.TestCase):
    def test_non_string_gateway_auth_scheme_is_a_validation_error(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            desktop = ClaudeDesktopConfig(pathlib.Path(directory) / "configLibrary")

            with self.assertRaisesRegex(ClaudeDesktopConfigError, "bearer or x-api-key"):
                desktop.patch({"inferenceGatewayAuthScheme": {}})

    def test_crlf_desktop_library_is_not_reported_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory) / "configLibrary"
            root.mkdir()
            (root / "_meta.json").write_bytes(
                b'{\r\n  "appliedId": "crlf-config",\r\n'
                b'  "entries": [{"id": "crlf-config", "name": "CRLF"}]\r\n}\r\n'
            )
            config_path = root / "crlf-config.json"
            config_path.write_bytes(b'{\r\n  "inferenceProvider": "gateway"\r\n}\r\n')

            desktop = ClaudeDesktopConfig(root)

            self.assertFalse(desktop.external_disk_state()["changed"])
            desktop.patch({"inferenceGatewayBaseUrl": "https://gateway.example.test"})
            desktop.apply()
            self.assertEqual(
                "https://gateway.example.test",
                json.loads(config_path.read_text(encoding="utf-8"))["inferenceGatewayBaseUrl"],
            )

    def test_crlf_developer_settings_are_not_reported_as_changed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / "developer_settings.json"
            path.write_bytes(b'{\r\n  "allowDevTools": true\r\n}\r\n')

            settings = ClaudeDeveloperSettings(path)

            self.assertFalse(settings.external_disk_state()["changed"])
            settings.patch({"allowDevTools": False})
            settings.apply()
            self.assertFalse(json.loads(path.read_text(encoding="utf-8"))["allowDevTools"])
