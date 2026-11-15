"""Edge cases of the WebDAV settings payload's field allow-list."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from young_router.core.domains import DomainError
from young_router.core.domains.webdav import WebDAVSettingsDomain


class WebDAVSettingsFieldTests(unittest.TestCase):
    def test_an_unread_settings_field_is_refused_rather_than_dropped(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = WebDAVSettingsDomain(
                root / "webdav.json",
                enabled_path=root / "enabled",
                status_path=root / "status.json",
                config_path=root / "config.yaml",
                state_path=root / "state.json",
                conflict_path=root / "conflict.json",
            )

            with self.assertRaises(DomainError):
                domain.dispatch("patch", {"keep_password": True})


if __name__ == "__main__":
    unittest.main()
