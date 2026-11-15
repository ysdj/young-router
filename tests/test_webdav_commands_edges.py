from __future__ import annotations

import argparse
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from young_router.webdav import commands as webdav_commands  # noqa: E402
from young_router.webdav import core as webdav_core  # noqa: E402


class WebDAVCommandConflictCleanupTests(unittest.TestCase):
    """A push or pull that settled the divergence drops the kept-aside side."""

    def _args(self, root: Path, command: str) -> argparse.Namespace:
        return argparse.Namespace(
            config=root / "config.yaml",
            settings=root / "webdav.json",
            state=root / "state.json",
            status=root / "status.json",
            command=command,
        )

    def _conflict(self, root: Path) -> Path:
        path = root / "webdav-conflict.json"
        webdav_core.save_conflict_record(path, {"config.yaml": b"providers: {}\n"}, "2026-09-25T09:59:00Z")
        return path

    def _settings(self) -> webdav_core.Settings:
        return webdav_core._settings_from_raw({"url": "https://webdav.example.test/config/"})

    def test_a_successful_push_drops_the_kept_aside_remote_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conflict = self._conflict(root)
            with patch("young_router.webdav.commands.load_settings", return_value=self._settings()), \
                patch("young_router.webdav.commands.WebDAVClient", return_value=object()), \
                patch("young_router.webdav.commands.push_bundle", return_value=(5, {})), \
                patch("young_router.webdav.commands.default_conflict_file", return_value=conflict):
                self.assertEqual(0, webdav_commands.command_push(self._args(root, "push")))

            self.assertFalse(conflict.exists())

    def test_a_successful_pull_drops_the_kept_aside_remote_side(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conflict = self._conflict(root)
            pulled = {"manifest": {}, "installed": [], "removed": [], "backups": []}
            with patch("young_router.webdav.commands.load_settings", return_value=self._settings()), \
                patch("young_router.webdav.commands.WebDAVClient", return_value=object()), \
                patch("young_router.webdav.commands.pull_bundle", return_value=pulled), \
                patch("young_router.webdav.commands.default_conflict_file", return_value=conflict):
                self.assertEqual(0, webdav_commands.command_pull(self._args(root, "pull")))

            self.assertFalse(conflict.exists())

    def test_a_sync_that_pushes_over_a_missing_remote_drops_the_record_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conflict = self._conflict(root)
            local = webdav_core.SyncSnapshot({"files": []})
            remote = webdav_core.SyncSnapshot(None)
            with patch("young_router.webdav.commands.load_settings", return_value=self._settings()), \
                patch("young_router.webdav.commands.WebDAVClient", return_value=object()), \
                patch("young_router.webdav.commands.local_snapshot", return_value=local), \
                patch("young_router.webdav.commands.read_remote_manifest", return_value=None), \
                patch("young_router.webdav.commands.remote_snapshot", return_value=remote), \
                patch("young_router.webdav.commands.baseline_snapshot", return_value=remote), \
                patch("young_router.webdav.commands.push_bundle", return_value=(5, {})), \
                patch("young_router.webdav.commands.default_conflict_file", return_value=conflict):
                self.assertEqual(0, webdav_commands.command_sync(self._args(root, "sync")))

            self.assertFalse(conflict.exists())

    def test_a_sync_archives_an_incompatible_remote_and_pushes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            local = webdav_core.SyncSnapshot({"files": []})
            remote = webdav_core.SyncSnapshot(None)
            incompatible = webdav_core.SyncError("not this version", webdav_core.INCOMPATIBLE_BUNDLE_CODE)
            with patch("young_router.webdav.commands.load_settings", return_value=self._settings()), \
                patch("young_router.webdav.commands.WebDAVClient", return_value=object()), \
                patch("young_router.webdav.commands.local_snapshot", return_value=local), \
                patch("young_router.webdav.commands.read_remote_manifest", side_effect=incompatible), \
                patch("young_router.webdav.commands.archive_remote_bundle", return_value="kept.json") as archived, \
                patch("young_router.webdav.commands.remote_snapshot", return_value=remote), \
                patch("young_router.webdav.commands.baseline_snapshot", return_value=remote), \
                patch("young_router.webdav.commands.push_bundle", return_value=(5, {})) as pushed, \
                patch("young_router.webdav.commands.default_conflict_file", return_value=root / "conflict.json"):
                self.assertEqual(0, webdav_commands.command_sync(self._args(root, "sync")))

            archived.assert_called_once()
            pushed.assert_called_once()


class WebDAVCommandStatusTests(unittest.TestCase):
    """A stopped run's cause survives into the file the pane reads."""

    def test_a_conflict_records_its_own_code(self) -> None:
        """``code`` is how the pane knows to offer a choice, not a retry.

        The recorded command used to write ``ok: false`` and nothing else, so a
        conflict on a host driven by this CLI read as an unreachable server.
        """

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "config.yaml").write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            args = argparse.Namespace(
                config=root / "config.yaml",
                settings=root / "webdav.json",
                state=root / "state.json",
                status=root / "status.json",
                command="sync",
            )
            settings = webdav_core._settings_from_raw({"url": "https://webdav.example.test/config/"})
            local = webdav_core.SyncSnapshot({"files": []})
            with patch("young_router.webdav.commands.load_settings", return_value=settings), \
                patch("young_router.webdav.commands.WebDAVClient", return_value=object()), \
                patch("young_router.webdav.commands.local_snapshot", return_value=local), \
                patch("young_router.webdav.commands.remote_snapshot", return_value=local), \
                patch("young_router.webdav.commands.read_remote_manifest", return_value=local.manifest), \
                patch("young_router.webdav.commands.baseline_snapshot", return_value=local), \
                patch("young_router.webdav.commands.decide_sync", return_value=("conflict", local)), \
                patch("young_router.webdav.commands.conflict_merge", return_value=None), \
                patch("young_router.webdav.commands.remember_remote_bundle"), \
                patch("young_router.webdav.commands.default_conflict_file", return_value=root / "conflict.json"):
                with self.assertRaises(webdav_core.SyncError):
                    webdav_commands._run_recorded_command(args)

            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertEqual("webdav_sync_conflict", status["code"])

    def test_a_transport_failure_states_no_cause_of_our_own(self) -> None:
        """The remote's HTTP status is its word, not a token this app states."""

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            args = argparse.Namespace(
                config=root / "config.yaml",
                settings=root / "webdav.json",
                state=root / "state.json",
                status=root / "status.json",
                command="sync",
            )
            failure = webdav_core.WebDAVHTTPError("GET", "https://webdav.example.test/x", 503, "Unavailable")
            with patch("young_router.webdav.commands.load_settings", side_effect=failure):
                with self.assertRaises(webdav_core.WebDAVHTTPError):
                    webdav_commands._run_recorded_command(args)

            status = json.loads((root / "status.json").read_text(encoding="utf-8"))
        self.assertFalse(status["ok"])
        self.assertNotIn("code", status)


if __name__ == "__main__":
    unittest.main()
