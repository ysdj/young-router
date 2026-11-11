from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from young_router.webdav import commands as webdav_commands  # noqa: E402
from young_router.webdav import core as webdav_core  # noqa: E402
from young_router.webdav import operations as webdav_operations  # noqa: E402


class WebDAVRemoteRenameTests(unittest.TestCase):
    def _settings(self, directory: Path, remote_name: str = webdav_core.DEFAULT_REMOTE_NAME):
        path = directory / "webdav.json"
        path.write_text(
            json.dumps({"url": "https://webdav.example.test/config/", "username": "person", "password": "secret", "remote_name": remote_name}),
            encoding="utf-8",
        )
        return webdav_core.load_settings(path)

    def test_a_renamed_setting_moves_the_file_it_pointed_at(self) -> None:
        """The bundle and its manifest move; nothing is deleted first."""
        calls: list[tuple[str, str]] = []

        class Client:
            files = {
                "https://webdav.example.test/config/litellm-config.json": b'{"app": "young-router"}',
                "https://webdav.example.test/config/litellm-config.manifest.json": b'{"app": "young-router"}',
            }

            def head(self, url: str) -> tuple[int, dict[str, str]]:
                calls.append(("head", url))
                if url in self.files:
                    return 200, {}
                raise webdav_core.WebDAVHTTPError("HEAD", url, 404, "Not Found", b"")

            def get(self, url: str, *, max_bytes: int = 0) -> bytes:
                calls.append(("get", url))
                return self.files[url]

            def put(self, url: str, data: bytes, content_type: str) -> None:
                calls.append(("put", url))
                self.files[url] = data

            def delete(self, url: str) -> None:
                calls.append(("delete", url))
                self.files.pop(url, None)

        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            client = Client()
            moved = webdav_operations.adopt_renamed_remote_bundle(client, settings, webdav_core.LEGACY_REMOTE_NAMES)

        self.assertEqual({"name": "litellm-config.json", "removed": True}, moved)
        writes = [url for kind, url in calls if kind == "put"]
        deletes = [url for kind, url in calls if kind == "delete"]
        self.assertEqual(
            [
                "https://webdav.example.test/config/young-router-config.json",
                "https://webdav.example.test/config/young-router-config.manifest.json",
            ],
            writes,
        )
        # The copy lands before the old files go, and in that order.
        self.assertLess(calls.index(("put", writes[0])), calls.index(("delete", "https://webdav.example.test/config/litellm-config.manifest.json")))
        self.assertEqual(
            [
                "https://webdav.example.test/config/litellm-config.manifest.json",
                "https://webdav.example.test/config/litellm-config.json",
            ],
            deletes,
        )

    def test_a_server_that_refuses_delete_keeps_the_copy_and_says_so(self) -> None:
        """PUT only: the copy lands, the old file stays, the reader is told."""
        class Client:
            files = {"https://webdav.example.test/config/litellm-config.json": b'{"app": "young-router"}'}

            def head(self, url: str) -> tuple[int, dict[str, str]]:
                if url in self.files:
                    return 200, {}
                raise webdav_core.WebDAVHTTPError("HEAD", url, 404, "Not Found", b"")

            def get(self, url: str, *, max_bytes: int = 0) -> bytes:
                return self.files[url]

            def put(self, url: str, data: bytes, content_type: str) -> None:
                self.files[url] = data

            def delete(self, url: str) -> None:
                raise webdav_core.WebDAVHTTPError("DELETE", url, 403, "Forbidden", b"")

        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory))
            client = Client()
            moved = webdav_operations.adopt_renamed_remote_bundle(client, settings, webdav_core.LEGACY_REMOTE_NAMES)
            copied = client.files["https://webdav.example.test/config/young-router-config.json"]

        self.assertEqual({"name": "litellm-config.json", "removed": False}, moved)
        self.assertEqual(b'{"app": "young-router"}', copied)

    def test_a_settings_name_that_is_not_the_old_default_stays_put(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            settings = self._settings(Path(directory), "my-own-config.json")
        self.assertEqual("my-own-config.json", settings.remote_name)


class WebDAVSmartSyncPlanTests(unittest.TestCase):
    """One comparison decides every sync, and it never stops to ask."""

    @staticmethod
    def _manifest(digest: str, modified_at: str, created_at: str) -> dict[str, object]:
        return {
            "app": webdav_core.APP_NAME,
            "version": webdav_core.ARCHIVE_VERSION,
            "format": webdav_core.CONFIG_BUNDLE_FORMAT,
            "created_at": created_at,
            "summary": {"providers": 1, "active_models": 1, "disabled_models": 0},
            "files": [
                {
                    "path": "config.yaml",
                    "present": True,
                    "bytes": 1,
                    "sha256": digest,
                    "mode": 0o600,
                    "modified_at": modified_at,
                },
                {"path": "config.disabled-models.yaml", "present": False},
                {"path": "relay-accounts.json", "present": False},
            ],
        }

    def test_a_remote_that_already_matches_writes_nothing(self) -> None:
        local = self._manifest("same", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
        remote = self._manifest("same", "2026-09-25T09:00:00Z", "2026-09-25T09:00:00Z")
        self.assertEqual("unchanged", webdav_core.smart_sync_plan(local, remote, None))

    def test_the_side_that_moved_alone_is_the_side_taken(self) -> None:
        base = self._manifest("base", "2026-09-25T09:00:00Z", "2026-09-25T09:00:00Z")
        local = self._manifest("local", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
        remote = self._manifest("remote", "2026-09-25T11:00:00Z", "2026-09-25T11:00:00Z")
        self.assertEqual("push", webdav_core.smart_sync_plan(local, base, base))
        self.assertEqual("pull", webdav_core.smart_sync_plan(base, remote, base))

    def test_two_sides_that_both_moved_stop_for_a_choice(self) -> None:
        """A real conflict is the reader's to settle, never a coin toss."""

        base = self._manifest("base", "2026-09-25T09:00:00Z", "2026-09-25T09:00:00Z")
        older = self._manifest("older", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
        newer = self._manifest("newer", "2026-09-25T12:00:00Z", "2026-09-25T12:00:00Z")
        self.assertEqual("conflict", webdav_core.smart_sync_plan(newer, older, base))
        self.assertEqual("conflict", webdav_core.smart_sync_plan(older, newer, base))

    def _two_file_snapshot(
        self,
        *,
        config: str,
        relay: str,
        disabled: str | None = None,
        stamp: str = "2026-09-25T10:00:00Z",
    ) -> webdav_core.SyncSnapshot:
        files: list[dict[str, object]] = [
            {
                "path": "config.yaml",
                "present": True,
                "bytes": 1,
                "sha256": config,
                "mode": 0o600,
                "modified_at": stamp,
            },
            {"path": "config.disabled-models.yaml", "present": disabled is not None},
            {
                "path": "relay-accounts.json",
                "present": True,
                "bytes": 1,
                "sha256": relay,
                "mode": 0o600,
                "modified_at": stamp,
            },
        ]
        if disabled is not None:
            files[1] = {
                "path": "config.disabled-models.yaml",
                "present": True,
                "bytes": 1,
                "sha256": disabled,
                "mode": 0o600,
                "modified_at": stamp,
            }
        manifest = {
            "app": webdav_core.APP_NAME,
            "version": webdav_core.ARCHIVE_VERSION,
            "format": webdav_core.CONFIG_BUNDLE_FORMAT,
            "created_at": stamp,
            "summary": {"providers": 1, "active_models": 1, "disabled_models": 0},
            "files": files,
        }
        digests = {
            "config.yaml": config,
            "config.disabled-models.yaml": disabled,
            "relay-accounts.json": relay,
        }
        return webdav_core.SyncSnapshot(manifest, digests)

    def test_a_relay_move_beside_the_peers_route_edit_is_not_a_choice(self) -> None:
        """The store a pull merges is not an edit competing with the peer's.

        A pull that keeps a station or a tombstone leaves the machine holding
        more than the bundle did, and its saved baseline still describes the
        bundle.  The next comparison then reads that machine as moved.  When the
        peer has since edited a route -- a different file -- nothing competes:
        a pull installs the route and merge-keeps the store.  Reading the pair
        as a conflict is what stopped a gateway from taking a desktop's route.
        """

        base = self._two_file_snapshot(config="route-0", relay="store-0")
        gateway = self._two_file_snapshot(config="route-0", relay="store-kept")
        desktop = self._two_file_snapshot(config="route-1", relay="store-0", stamp="2026-09-25T11:00:00Z")
        self.assertEqual("pull", webdav_core.smart_sync_plan(gateway, desktop, base))
        # The sides swapped: this Mac's route edit is carried, and the peer's
        # kept store survives on the peer until it merges again.
        self.assertEqual("push", webdav_core.smart_sync_plan(desktop, gateway, base))

    def test_two_stores_that_both_moved_are_still_a_choice(self) -> None:
        base = self._two_file_snapshot(config="route-0", relay="store-0")
        local = self._two_file_snapshot(config="route-0", relay="store-local")
        remote = self._two_file_snapshot(config="route-0", relay="store-remote")
        self.assertEqual("conflict", webdav_core.smart_sync_plan(local, remote, base))

    def test_two_files_that_are_not_merged_are_still_a_choice(self) -> None:
        """Direction is decided by the files, not by the number of them."""

        base = self._two_file_snapshot(config="route-0", relay="store-0", disabled="disabled-0")
        local = self._two_file_snapshot(config="route-1", relay="store-0", disabled="disabled-0")
        remote = self._two_file_snapshot(
            config="route-0", relay="store-0", disabled="disabled-1", stamp="2026-09-25T11:00:00Z"
        )
        self.assertEqual("conflict", webdav_core.smart_sync_plan(local, remote, base))
        self.assertEqual("conflict", webdav_core.smart_sync_plan(remote, local, base))

    def test_without_a_baseline_the_newer_snapshot_still_decides(self) -> None:
        """A lost sync state is not a question for the reader either."""

        local = self._manifest("local", "2026-09-25T12:00:00Z", "2026-09-25T12:00:00Z")
        remote = self._manifest("remote", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
        self.assertEqual("push", webdav_core.smart_sync_plan(local, remote, None))
        self.assertEqual("pull", webdav_core.smart_sync_plan(remote, local, None))

    def test_a_tie_or_an_unreadable_stamp_takes_the_copy_that_keeps_a_backup(self) -> None:
        """With no baseline, the newer side wins -- and a tie pulls.

        Nothing says either side moved, so this is a first sync between two
        machines rather than a conflict.  A pull is the answer for a tie and for
        a stamp that cannot be read: installing the remote copy backs up what it
        replaces, while a push leaves the remote's version nowhere.
        """

        local = self._manifest("local", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
        same_moment = self._manifest("remote", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z")
        unreadable = dict(same_moment, created_at="not a timestamp")
        self.assertEqual("pull", webdav_core.smart_sync_plan(local, same_moment, None))
        self.assertEqual("pull", webdav_core.smart_sync_plan(local, unreadable, None))

    def test_an_absent_remote_snapshot_is_a_push(self) -> None:
        self.assertEqual(
            "push",
            webdav_core.smart_sync_plan(self._manifest("local", "2026-09-25T10:00:00Z", "2026-09-25T10:00:00Z"), None, None),
        )

    def test_a_direction_an_earlier_version_stored_is_not_carried_forward(self) -> None:
        """The setting is gone; the file that still has it reads and rewrites clean."""

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "webdav.json"
            path.write_text(
                json.dumps({"url": "https://webdav.example.test/config/", "sync_direction": "pull", "direction": "push"}),
                encoding="utf-8",
            )
            settings = webdav_core.load_settings(path)
            self.assertNotIn("sync_direction", settings.sanitized())
            webdav_core.save_settings(path, settings)
            self.assertNotIn("sync_direction", json.loads(path.read_text(encoding="utf-8")))

    def test_the_commands_layer_accepts_a_payload_from_that_version_too(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "webdav.json"
            existing = webdav_core.load_settings(path)
            updated = webdav_commands._settings_from_payload({"sync_direction": "pull"}, existing)
            self.assertEqual(existing.url, updated.url)
            self.assertFalse(hasattr(updated, "sync_direction"))


class WebDAVConflictRecordTests(unittest.TestCase):
    """A stopped sync keeps the side the pane has to compare, and nothing else."""

    def test_the_remote_side_round_trips_and_is_cleared(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "webdav-conflict.json"
            webdav_core.save_conflict_record(
                path,
                {
                    "config.yaml": "providers: {}\n".encode("utf-8"),
                    # A binary or non-UTF-8 member is not text, so it is not
                    # offered for a text comparison.
                    "config.disabled-models.yaml": b"\xff\xfe",
                },
                "2026-09-25T09:59:00Z",
            )
            record = webdav_core.read_conflict_record(path)
            self.assertIsNotNone(record)
            self.assertEqual({"config.yaml": "providers: {}\n"}, record["files"])
            self.assertEqual("2026-09-25T09:59:00Z", record["remote_created_at"])

            webdav_core.clear_conflict_record(path)
            self.assertIsNone(webdav_core.read_conflict_record(path))
            # Clearing a record that is already gone is not an error: the next
            # successful sync clears it whether or not a conflict preceded it.
            webdav_core.clear_conflict_record(path)

    def test_a_record_this_app_did_not_write_is_not_a_comparison(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "webdav-conflict.json"
            path.write_text("not json", encoding="utf-8")
            self.assertIsNone(webdav_core.read_conflict_record(path))

            path.write_text(
                json.dumps({"version": 1, "recorded_at": "x", "files": {"config.yaml": "y"}}) + " " * 1024,
                encoding="utf-8",
            )
            self.assertEqual({"config.yaml": "y"}, webdav_core.read_conflict_record(path)["files"])

            # A file larger than the writer's own cap is refused whole, so a
            # damaged or hand-made record cannot be parsed into memory.
            path.write_bytes(b"{" + b" " * (webdav_core.CONFIG_BUNDLE_MAX_BYTES * 2 + 4096))
            self.assertIsNone(webdav_core.read_conflict_record(path))

    def test_the_command_line_keeps_the_comparison_and_names_the_choice(self) -> None:
        """The CLI has no reader to compare two sides, so it stops and says so."""

        import argparse

        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = root / "config.yaml"
            config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            settings_path = root / "webdav.json"
            settings_path.write_text(json.dumps({"url": "https://webdav.example.test/config/"}), encoding="utf-8")
            base = {"files": [{"path": "config.yaml", "present": True, "sha256": "base"}]}
            local = {"files": [{"path": "config.yaml", "present": True, "sha256": "local"}]}
            remote = {"files": [{"path": "config.yaml", "present": True, "sha256": "remote"}]}
            kept: list[Path] = []

            args = argparse.Namespace(
                config=config,
                settings=settings_path,
                state=root / "state.json",
                status=root / "status.json",
                command="sync",
            )
            with patch("young_router.webdav.core.WebDAVClient", return_value=object()), patch(
                "young_router.webdav.commands.local_snapshot", return_value=webdav_core.SyncSnapshot(local)
            ), patch("young_router.webdav.commands.read_remote_manifest", return_value=remote), patch(
                "young_router.webdav.commands.decide_sync",
                return_value=("conflict", webdav_core.SyncSnapshot(remote)),
            ), patch(
                "young_router.webdav.commands.baseline_snapshot", return_value=webdav_core.SyncSnapshot(base)
            ), patch(
                "young_router.webdav.commands.default_conflict_file", return_value=root / "conflict.json"
            ), patch(
                "young_router.webdav.commands.remember_remote_bundle",
                side_effect=lambda _client, _settings, path: kept.append(path),
            ), patch(
                "young_router.webdav.commands.push_bundle"
            ) as pushed, patch(
                "young_router.webdav.commands.pull_bundle"
            ) as pulled:
                with self.assertRaises(webdav_core.SyncError) as raised:
                    webdav_commands.command_sync(args)

            self.assertEqual("webdav_sync_conflict", raised.exception.code)
            self.assertIn("both ends changed", str(raised.exception))
            self.assertEqual([root / "conflict.json"], kept)
            pushed.assert_not_called()
            pulled.assert_not_called()


class WebDAVArchiveTests(unittest.TestCase):
    def test_a_kept_aside_bundle_carries_when_it_was_set_aside(self) -> None:
        """The copy keeps its extension and gains the local backup's stamp."""
        self.assertEqual(
            "menu-config.bak-20260924-195812.json",
            webdav_core.archived_bundle_name("menu-config.json", "20260924-195812"),
        )
        self.assertEqual(
            "config.bak-20260924-195812.tar.gz",
            webdav_core.archived_bundle_name("config.tar.gz", "20260924-195812"),
        )
        self.assertEqual(
            "config.bak-20260924-195812.json",
            webdav_core.archived_bundle_name("", "20260924-195812"),
        )

    def test_archiving_copies_the_remote_file_to_a_sibling(self) -> None:
        """The unreadable file is read and written back under its new name."""
        calls: list[tuple[str, str, bytes]] = []

        class Client:
            def get(self, url: str, max_bytes: int = 0) -> bytes:
                calls.append(("get", url, b""))
                return b'{"app": "litellm-menu"}'

            def put(self, url: str, data: bytes, content_type: str) -> None:
                calls.append(("put", url, data))

        with tempfile.TemporaryDirectory() as directory:
            settings_path = Path(directory) / "webdav.json"
            settings_path.write_text(
                json.dumps({"url": "https://webdav.example.test/config/", "username": "person", "password": "secret", "remote_name": "menu-config.json"}),
                encoding="utf-8",
            )
            settings = webdav_core.load_settings(settings_path)
            name = webdav_operations.archive_remote_bundle(Client(), settings)
        self.assertTrue(name.startswith("menu-config.bak-"))
        self.assertTrue(name.endswith(".json"))
        self.assertEqual("get", calls[0][0])
        self.assertEqual("https://webdav.example.test/config/menu-config.json", calls[0][1])
        self.assertEqual("put", calls[1][0])
        self.assertEqual(f"https://webdav.example.test/config/{name}", calls[1][1])
        self.assertEqual(b'{"app": "litellm-menu"}', calls[1][2])


class _MemoryWebDAV:
    """A WebDAV collection held in memory: PUT stores, GET answers or 404s."""

    def __init__(self) -> None:
        self.files: dict[str, bytes] = {}

    def try_mkcol(self, url: str) -> None:
        return None

    def put(self, url: str, data: bytes, content_type: str) -> None:
        self.files[url] = data

    def get(self, url: str, max_bytes: int | None = None) -> bytes:
        if url not in self.files:
            raise webdav_core.WebDAVHTTPError("GET", url, 404, "Not Found")
        return self.files[url]


class WebDAVSyncBundleTests(unittest.TestCase):
    def test_an_incompatible_remote_bundle_names_its_cause(self) -> None:
        """A bundle from another app identity or archive version states why.

        The probe beside the sync button only proves the connection works, so
        the sync itself has to say what it refused and how to get past it: the
        error carries ``webdav_sync_incompatible``, which the pane turns into
        "the remote file came from an older version; push to overwrite it".
        """
        legacy = {
            "app": "litellm-menu",
            "version": webdav_core.ARCHIVE_VERSION,
            "format": webdav_core.CONFIG_BUNDLE_FORMAT,
            "created_at": "2026-07-30T21:05:26Z",
            "summary": {},
            "files": [],
        }
        with self.assertRaises(webdav_core.SyncError) as raised:
            webdav_core._validate_bundle_header(legacy)
        self.assertEqual("webdav_sync_incompatible", raised.exception.code)
        self.assertIn("not created by this Young Router version", str(raised.exception))
        # Anything else keeps the generic cause, so a pane cannot mistake it.
        self.assertEqual("webdav_sync_failed", webdav_core.SyncError("WebDAV sync failed").code)

    def write_config(self, directory: Path) -> Path:
        path = directory / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  local:
                    api_base: "https://example.com/v1"
                    api_keys:
                      - name: default
                        value: "sk-test"
                model_list:
                  - model_name: default-chat
                    litellm_params:
                      model: openai/default-chat
                      api_base: "https://example.com/v1"
                      api_key: "sk-test"
                    model_info:
                      id: "00000001"
                      provider: local
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        return path

    def test_recorded_command_writes_a_private_status_file_for_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            status_path = Path(temp_dir) / "webdav-sync-status.json"
            args = type("Args", (), {"command": "probe", "status": status_path})()

            with patch("young_router.webdav.commands.command_probe", return_value=0):
                self.assertEqual(0, webdav_commands._run_recorded_command(args))
            success = json.loads(status_path.read_text(encoding="utf-8"))

            with patch("young_router.webdav.commands.command_probe", side_effect=webdav_core.SyncError("failed")):
                with self.assertRaises(webdav_core.SyncError):
                    webdav_commands._run_recorded_command(args)
            failure = json.loads(status_path.read_text(encoding="utf-8"))

            self.assertEqual("probe", success["action"])
            self.assertTrue(success["ok"])
            self.assertEqual("probe", failure["action"])
            self.assertFalse(failure["ok"])
            self.assertEqual(0o600, status_path.stat().st_mode & 0o777)

    def test_json_bundle_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            target = temp / "target"
            source.mkdir()
            target.mkdir()
            source_config = self.write_config(source)
            target_config = target / "config.yaml"

            bundle, manifest_snapshot = webdav_core.create_bundle(source_config)
            manifest = manifest_snapshot.manifest
            result = webdav_core.install_bundle(bundle, target_config)

            self.assertEqual(manifest["app"], "young-router")
            self.assertTrue(target_config.exists())
            self.assertEqual(result["manifest"]["summary"]["active_models"], 1)

    def test_a_repeated_pull_of_the_same_bundle_writes_nothing(self) -> None:
        """An interval pull converges instead of accumulating backups.

        ``pull`` installs the remote bundle whatever its content, and every
        install backed the target up first.  A host that pulls on a timer
        therefore left one ``.bak-webdav-*`` per tick beside a configuration
        nothing had changed, and rewrote the file (and its mtime) each time.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            target = temp / "target"
            source.mkdir()
            target.mkdir()
            bundle, _manifest_snapshot = webdav_core.create_bundle(self.write_config(source))
            _manifest = _manifest_snapshot.manifest
            config_path = target / "config.yaml"

            first = webdav_core.install_bundle(bundle, config_path)
            self.assertEqual([str(config_path)], first["installed"])
            self.assertEqual([], first["backups"])
            installed_at = config_path.stat().st_mtime_ns

            # Long enough that a rewrite would be visible even on a coarse mtime.
            time.sleep(0.02)
            second = webdav_core.install_bundle(bundle, config_path)

            self.assertEqual([], second["installed"])
            self.assertEqual([], second["backups"])
            self.assertEqual([], list(target.glob("*.bak-webdav-*")))
            self.assertEqual(installed_at, config_path.stat().st_mtime_ns)

    def test_a_pull_that_matches_the_remote_manifest_skips_the_bundle(self) -> None:
        """``pull`` reads the sidecar manifest first and stops when it matches.

        An unchanged remote is the ordinary case for a keep-in-sync host, and
        downloading the whole bundle to discover that is the transfer this
        comparison exists to avoid.  A remote that does differ still installs.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            target = temp / "target"
            source.mkdir()
            target.mkdir()
            bundle, manifest_snapshot = webdav_core.create_bundle(self.write_config(source))
            manifest = manifest_snapshot.manifest
            config_path = target / "config.yaml"
            webdav_core.install_bundle(bundle, config_path)
            manifest_data = json.dumps(manifest).encode("utf-8")

            class FakeClient:
                def __init__(self) -> None:
                    self.urls: list[str] = []

                def get(self, url: str, *, max_bytes: int) -> bytes:
                    self.urls.append(url)
                    if url.endswith(".manifest.json"):
                        return manifest_data
                    return bundle

            settings = webdav_core.Settings(
                url="https://example.com/dav/resource/", remote_name="config.json"
            )
            client = FakeClient()
            result = webdav_operations.pull_bundle(client, settings, config_path)

            self.assertEqual([], result["installed"])
            self.assertEqual(
                [
                    "https://example.com/dav/resource/config.manifest.json",
                    # The digest record is read with the manifest; a server
                    # that has none answers the bytes this test's fake returns,
                    # which parses as "no record" and falls back to the bytes.
                    "https://example.com/dav/resource/config.digests.json",
                ],
                client.urls,
            )

    def test_a_pull_installs_a_remote_that_differs_from_the_local_file(self) -> None:
        """The manifest shortcut must not swallow a real change."""

        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            target = temp / "target"
            source.mkdir()
            target.mkdir()
            config_path = target / "config.yaml"
            old_bundle, _old_manifest_snapshot = webdav_core.create_bundle(self.write_config(source))
            _old_manifest = _old_manifest_snapshot.manifest
            webdav_core.install_bundle(old_bundle, config_path)

            source_config = self.write_config(source)
            source_config.write_text(
                source_config.read_text(encoding="utf-8").replace(
                    "default-chat", "changed-chat"
                ),
                encoding="utf-8",
            )
            new_bundle, new_manifest_snapshot = webdav_core.create_bundle(source_config)
            new_manifest = new_manifest_snapshot.manifest
            manifest_data = json.dumps(new_manifest).encode("utf-8")

            class FakeClient:
                def __init__(self) -> None:
                    self.urls: list[str] = []

                def get(self, url: str, *, max_bytes: int) -> bytes:
                    self.urls.append(url)
                    if url.endswith(".manifest.json"):
                        return manifest_data
                    return new_bundle

            settings = webdav_core.Settings(
                url="https://example.com/dav/resource/", remote_name="config.json"
            )
            result = webdav_operations.pull_bundle(FakeClient(), settings, config_path)

            self.assertEqual([str(config_path)], result["installed"])
            self.assertIn("changed-chat", config_path.read_text(encoding="utf-8"))

    def test_relay_accounts_and_plaintext_password_round_trip_with_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            target = temp / "target"
            source.mkdir()
            target.mkdir()
            source_config = self.write_config(source)
            relay_path = source / ".litellm-runtime" / "relay-accounts.json"
            relay_path.parent.mkdir()
            relay_payload = {
                "version": 1,
                "accounts": [
                    {
                        "id": "relay-one",
                        "type": "sub2api",
                        "label": "Relay One",
                        "origin": "https://relay.example.test",
                        "username": "person@example.test",
                        "login_status": "unknown",
                        "remember_password": True,
                        "password": "replace-password",
                        "session": {
                            "cookie": "session=replace-cookie",
                            "access_token": "replace-access-token",
                            "refresh_token": "",
                        },
                        "balance": 12.5,
                        "last_updated_at": "",
                        "resource_status": "idle",
                        "resource_error": "none",
                        "resources": [],
                    }
                ],
                "pending_credential_cleanups": [],
            }
            relay_path.write_text(json.dumps(relay_payload), encoding="utf-8")
            relay_path.chmod(0o600)

            bundle, _manifest_snapshot = webdav_core.create_bundle(source_config)
            _manifest = _manifest_snapshot.manifest
            self.assertIn(b"replace-password", bundle)
            self.assertIn(b"replace-cookie", bundle)
            result = webdav_core.install_bundle(bundle, target / "config.yaml")

            installed_relay = target / ".litellm-runtime" / "relay-accounts.json"
            self.assertIn(str(installed_relay), result["installed"])
            self.assertEqual(relay_payload, json.loads(installed_relay.read_text(encoding="utf-8")))
            self.assertEqual(0o600, installed_relay.stat().st_mode & 0o777)

    def test_a_pull_never_drops_a_relay_account_this_mac_holds(self) -> None:
        """The relay store is not replaceable by the remote copy.

        It is the one synced file whose content exists nowhere else: the
        stations, their saved sessions, and the plaintext passwords that make
        every bound key readable again.  A station added on this Mac while the
        remote still holds an older bundle was silently dropped by the next
        pull, with no tombstone and nothing in the UI naming it — the account
        simply vanished.
        """

        nimbus_station = {"id": "station-nimbus", "name": "nimbus", "origin": "https://relay.example.test", "type": "newapi"}
        atlas_station = {
            "id": "station-atlas",
            "name": "atlas",
            "origin": "https://peer.example.test",
            "type": "sub2api",
        }

        def account(account_id: str, station_id: str, origin: str) -> dict[str, object]:
            return {
                "id": account_id,
                "station_id": station_id,
                "type": "newapi",
                "label": account_id,
                "origin": origin,
                "username": "person@example.test",
                "login_status": "unknown",
                "remember_password": True,
                "password": "replace-password",
                "session": {"cookie": "session=c", "access_token": "replace-token", "refresh_token": ""},
                "balance": 1.0,
                "last_updated_at": "",
                "login_observed_at": "",
                "resource_status": "ready",
                "resource_error": "none",
                "resources": [],
                "groups": [],
            }

        def document(stations: list[dict[str, str]], accounts: list[dict[str, object]]) -> dict[str, object]:
            return {"version": 3, "stations": stations, "accounts": accounts, "pending_credential_cleanups": []}

        local_document = document(
            [nimbus_station, atlas_station],
            [
                account("account-nimbus", "station-nimbus", "https://relay.example.test"),
                account("account-atlas", "station-atlas", "https://peer.example.test"),
            ],
        )

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            local_config = self.write_config(local)
            remote_config = self.write_config(remote)

            local_relay = webdav_core.relay_accounts_path(local_config)
            local_relay.parent.mkdir(parents=True, exist_ok=True)
            local_relay.write_text(json.dumps(local_document), encoding="utf-8")

            # The remote side predates the station this Mac added, and still
            # carries the other account under a name it has since been given.
            remote_document = document(
                [atlas_station], [account("account-atlas", "station-atlas", "https://peer.example.test")]
            )
            remote_document["accounts"][0]["label"] = "atlas-renamed"  # type: ignore[index]
            remote_relay = webdav_core.relay_accounts_path(remote_config)
            remote_relay.parent.mkdir(parents=True, exist_ok=True)
            remote_relay.write_text(json.dumps(remote_document), encoding="utf-8")

            bundle, _manifest_snapshot = webdav_core.create_bundle(remote_config)
            _manifest = _manifest_snapshot.manifest
            webdav_core.install_bundle(bundle, local_config)

            merged = json.loads(local_relay.read_text(encoding="utf-8"))
            self.assertEqual(
                {"atlas", "nimbus"},
                {station["name"] for station in merged["stations"]},
            )
            # The incoming copy still lands for the accounts it describes, so
            # this can only ever keep an account, never pin the old version.
            self.assertEqual(
                ["atlas-renamed"],
                [entry["label"] for entry in merged["accounts"] if entry["id"] == "account-atlas"],
            )
            # The kept account carries its credential, not a hollow row.
            self.assertEqual(
                ["replace-password"],
                [entry["password"] for entry in merged["accounts"] if entry["id"] == "account-nimbus"],
            )

    def test_a_pull_keeps_the_fresher_local_session_for_a_shared_account(self) -> None:
        """A client that just signed in keeps its session against an older bundle.

        The relay store is whole-file replaced on pull, so a session this Mac
        minted moments ago was overwritten by another client's older copy of the
        same account — and the next read then signed in again, which is exactly
        the repeated post-login that fills a station's concurrent-session cap.
        The side with the later ``login_observed_at`` keeps its session; an
        older or unordered observation leaves the incoming copy in place.
        """

        nimbus_station = {"id": "station-nimbus", "name": "nimbus", "origin": "https://relay.example.test", "type": "newapi"}

        def account(observed_at: str, cookie: str) -> dict[str, object]:
            return {
                "id": "account-nimbus",
                "station_id": "station-nimbus",
                "type": "newapi",
                "label": "nimbus",
                "origin": "https://relay.example.test",
                "username": "person@example.test",
                "login_status": "signed_in",
                "remember_password": True,
                "password": "replace-password",
                "session": {"cookie": cookie, "access_token": "", "refresh_token": ""},
                "balance": 1.0,
                "last_updated_at": observed_at,
                "login_observed_at": observed_at,
                "resource_status": "ready",
                "resource_error": "none",
                "resources": [],
                "groups": [],
            }

        def document(accounts: list[dict[str, object]]) -> dict[str, object]:
            return {"version": 3, "stations": [nimbus_station], "accounts": accounts, "pending_credential_cleanups": []}

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            local_config = self.write_config(local)
            remote_config = self.write_config(remote)

            # This Mac signed in later than the bundle the other client built.
            local_relay = webdav_core.relay_accounts_path(local_config)
            local_relay.parent.mkdir(parents=True, exist_ok=True)
            local_relay.write_text(
                json.dumps(document([account("2026-01-02T00:00:00Z", "session=this-mac")])), encoding="utf-8"
            )
            remote_relay = webdav_core.relay_accounts_path(remote_config)
            remote_relay.parent.mkdir(parents=True, exist_ok=True)
            remote_relay.write_text(
                json.dumps(document([account("2026-01-01T00:00:00Z", "session=other-mac")])), encoding="utf-8"
            )

            bundle, _manifest_snapshot = webdav_core.create_bundle(remote_config)
            _manifest = _manifest_snapshot.manifest
            webdav_core.install_bundle(bundle, local_config)

            merged = json.loads(local_relay.read_text(encoding="utf-8"))
            self.assertEqual(["session=this-mac"], [entry["session"]["cookie"] for entry in merged["accounts"]])

            # An older local observation yields to the incoming session, so a
            # client that logged in elsewhere still propagates through sync.
            local_relay.write_text(
                json.dumps(document([account("2025-12-31T00:00:00Z", "session=this-mac")])), encoding="utf-8"
            )
            webdav_core.install_bundle(bundle, local_config)
            merged = json.loads(local_relay.read_text(encoding="utf-8"))
            self.assertEqual(["session=other-mac"], [entry["session"]["cookie"] for entry in merged["accounts"]])

    def _relay_store(self, read_at: str, session: str) -> dict[str, object]:
        """One relay account whose observations differ from the other machine's."""

        return {
            "version": 3,
            "stations": [
                {"id": "station-nimbus", "name": "nimbus", "origin": "https://relay.example.test", "type": "newapi"}
            ],
            "accounts": [
                {
                    "id": "account-nimbus",
                    "station_id": "station-nimbus",
                    "type": "newapi",
                    "label": "nimbus",
                    "origin": "https://relay.example.test",
                    "username": "person@example.test",
                    "remember_password": True,
                    "password": "replace-password",
                    "auto_grouping": True,
                    "login_status": "signed_in",
                    "session": {"cookie": session, "access_token": "", "refresh_token": ""},
                    "balance": 12.5,
                    "last_updated_at": read_at,
                    "login_observed_at": read_at,
                    "resource_status": "ready",
                    "resource_error": "none",
                    "resources": [{"id": "sub2api-1", "api_name": "nimbus"}],
                    "groups": [{"id": "21", "name": "mixed", "multiplier": 0.15}],
                }
            ],
            "pending_credential_cleanups": [],
        }

    def _two_station_store(
        self,
        *,
        removed: bool,
    ) -> dict[str, object]:
        """Two relay stations, one of them optionally deleted with its account.

        The deleted half is the shape the wizard's own delete leaves behind: the
        station and its account are gone, and the store names both ids in
        ``removed_stations`` / ``removed_accounts`` so a sync cannot hand them
        back.  The surviving half is the control -- it must arrive untouched.
        """

        document: dict[str, object] = {
            "version": 3,
            "stations": [
                {"id": "station-nimbus", "name": "nimbus", "origin": "https://relay.example.test", "type": "newapi"},
                {"id": "station-wawapii", "name": "wawapii", "origin": "https://wawapii.com", "type": "newapi"},
            ],
            "accounts": [
                {
                    "id": "account-nimbus",
                    "station_id": "station-nimbus",
                    "type": "newapi",
                    "label": "nimbus",
                    "origin": "https://relay.example.test",
                    "username": "person@example.test",
                    "login_status": "unknown",
                    "remember_password": False,
                    "password": "",
                    "session": {"cookie": "", "access_token": "", "refresh_token": ""},
                    "balance": 0.0,
                    "last_updated_at": "",
                    "login_observed_at": "",
                    "resource_status": "ready",
                    "resource_error": "none",
                    "resources": [],
                    "groups": [],
                },
                {
                    "id": "account-wawapii",
                    "station_id": "station-wawapii",
                    "type": "newapi",
                    "label": "wawapii",
                    "origin": "https://wawapii.com",
                    "username": "person@example.test",
                    "login_status": "signed_in",
                    "remember_password": True,
                    "password": "replace-password",
                    "session": {"cookie": "session=wawapii", "access_token": "", "refresh_token": ""},
                    "balance": 3.5,
                    "last_updated_at": "2026-11-10T03:00:00Z",
                    "login_observed_at": "2026-11-10T03:00:00Z",
                    "resource_status": "ready",
                    "resource_error": "none",
                    "resources": [{"id": "sub2api-1", "api_name": "GLM"}],
                    "groups": [],
                },
            ],
            "pending_credential_cleanups": [],
        }
        if removed:
            document["stations"] = [document["stations"][0]]  # type: ignore[index]
            document["accounts"] = [document["accounts"][0]]  # type: ignore[index]
            document["removed_stations"] = [{"id": "station-wawapii", "removed_at": "2026-11-10T03:05:00Z"}]
            document["removed_accounts"] = [{"id": "account-wawapii", "removed_at": "2026-11-10T03:05:00Z"}]
            document["pending_credential_cleanups"] = [
                {"account_id": "account-wawapii", "label": "wawapii", "kind": "credentials"}
            ]
        return document

    def _write_relay(self, config_path: Path, document: dict[str, object]) -> Path:
        path = webdav_core.relay_accounts_path(config_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(document), encoding="utf-8")
        return path

    def test_a_deleted_station_stays_deleted_when_the_bundle_still_lists_it(self) -> None:
        """A deletion is content; an absence is not.

        The relay store is compared as a whole, so "this Mac never had it" and
        "this Mac deleted it" are the same bytes -- which is how a station
        deleted here, with its twelve keys and its dashboard session, came back
        from another machine's older bundle minutes after it was removed.  The
        store now names the ids it removed, and a pull that still lists them
        installs everything else and drops those.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            local_config = self.write_config(local)
            remote_config = self.write_config(remote)

            local_relay = self._write_relay(local_config, self._two_station_store(removed=True))
            # The other machine has not synced since the delete: its bundle
            # still carries the station, the account, its password and session.
            self._write_relay(remote_config, self._two_station_store(removed=False))

            bundle, _manifest_snapshot = webdav_core.create_bundle(remote_config)
            webdav_core.install_bundle(bundle, local_config)

            merged = json.loads(local_relay.read_text(encoding="utf-8"))
            self.assertEqual(["station-nimbus"], [station["id"] for station in merged["stations"]])
            self.assertEqual(["account-nimbus"], [account["id"] for account in merged["accounts"]])
            # The tombstone is what keeps doing the work: the next stale bundle
            # is answered by the same rule.
            self.assertEqual(["station-wawapii"], [row["id"] for row in merged["removed_stations"]])
            self.assertEqual(["account-wawapii"], [row["id"] for row in merged["removed_accounts"]])
            self.assertEqual(
                [{"account_id": "account-wawapii", "label": "wawapii", "kind": "credentials"}],
                merged["pending_credential_cleanups"],
            )

    def test_a_deletion_made_on_the_other_machine_lands_here(self) -> None:
        """The rule is symmetric: the peer's tombstone removes the local row.

        Without this half, "deleted here" would win but "deleted there" would
        be undone by this machine's next push -- the same bug with the sides
        swapped.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            local_config = self.write_config(local)
            remote_config = self.write_config(remote)

            local_relay = self._write_relay(local_config, self._two_station_store(removed=False))
            self._write_relay(remote_config, self._two_station_store(removed=True))

            bundle, _manifest_snapshot = webdav_core.create_bundle(remote_config)
            webdav_core.install_bundle(bundle, local_config)

            installed = json.loads(local_relay.read_text(encoding="utf-8"))
            self.assertEqual(["station-nimbus"], [station["id"] for station in installed["stations"]])
            self.assertEqual(["account-nimbus"], [account["id"] for account in installed["accounts"]])
            self.assertEqual(["station-wawapii"], [row["id"] for row in installed["removed_stations"]])

    def test_a_removal_tombstone_outlives_the_copy_that_agrees(self) -> None:
        """The tombstone is kept, not spent when the two copies agree.

        Pruning it the moment neither copy lists the id is what let a stale
        peer -- agreeing today, offering the row back tomorrow -- reinstate a
        station that had already been deleted twice.  The list is bounded by
        the store's own cap rather than by an agreement that cannot be proven.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            local_config = self.write_config(local)
            remote_config = self.write_config(remote)

            local_relay = self._write_relay(local_config, self._two_station_store(removed=True))
            agreed = self._two_station_store(removed=True)
            agreed["removed_stations"] = []  # type: ignore[index]
            agreed["removed_accounts"] = []  # type: ignore[index]
            agreed["pending_credential_cleanups"] = []  # type: ignore[index]
            self._write_relay(remote_config, agreed)

            bundle, _manifest_snapshot = webdav_core.create_bundle(remote_config)
            webdav_core.install_bundle(bundle, local_config)

            installed = json.loads(local_relay.read_text(encoding="utf-8"))
            self.assertEqual(["station-wawapii"], [row["id"] for row in installed["removed_stations"]])
            self.assertEqual(["account-wawapii"], [row["id"] for row in installed["removed_accounts"]])
            # The credential tombstone travels with the removal it belongs to.
            self.assertEqual(
                [{"account_id": "account-wawapii", "label": "wawapii", "kind": "credentials"}],
                installed["pending_credential_cleanups"],
            )

    def test_a_stale_peer_push_does_not_undo_this_machines_delete(self) -> None:
        """The whole failure, end to end, through the real sync entry points.

        Two machines, one in-memory WebDAV collection, the same push/pull/plan
        calls the app makes.  This Mac deletes a station, syncs, and the peer
        pulls -- then the peer, still holding its pre-delete copy, pushes it
        back.  That last pull is the one that brought the station back with a
        full key list; with the tombstones it installs the peer's other work and
        leaves the deletion alone.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mac = root / "mac"
            peer = root / "peer"
            mac.mkdir()
            peer.mkdir()
            mac_config = self.write_config(mac)
            peer_config = self.write_config(peer)
            mac_state = mac / "sync-state.json"
            peer_state = peer / "sync-state.json"
            settings = webdav_core.Settings(url="https://dav.example.test/young-router/", remote_name="config.json")
            server = _MemoryWebDAV()

            def sync(config_path: Path, state_path: Path) -> str:
                local_snapshot = webdav_core.local_snapshot(config_path)
                remote_snapshot = webdav_operations.remote_snapshot(
                    server, settings, webdav_operations.read_remote_manifest(server, settings)
                )
                if remote_snapshot.manifest is None:
                    webdav_operations.push_bundle(server, settings, config_path, state_path, "sync-push")
                    return "push"
                plan, remote_snapshot = webdav_operations.decide_sync(
                    server, settings, local_snapshot, remote_snapshot, webdav_core.baseline_snapshot(state_path)
                )
                if plan == "unchanged":
                    agreed = webdav_core.SyncSnapshot(
                        remote_snapshot.manifest, remote_snapshot.digests or local_snapshot.digests
                    )
                    webdav_core.save_sync_state(state_path, settings, agreed, "sync")
                    return "unchanged"
                if plan == "push":
                    webdav_operations.push_bundle(server, settings, config_path, state_path, "sync-push")
                    return "push"
                if plan == "conflict":
                    return "conflict"
                webdav_operations.pull_bundle(server, settings, config_path, state_path, "sync-pull", skip_when_unchanged=False)
                return "pull"

            mac_relay = self._write_relay(mac_config, self._two_station_store(removed=False))
            self._write_relay(peer_config, self._two_station_store(removed=False))
            self.assertEqual("push", sync(mac_config, mac_state))
            # Both machines start from the same document, so the peer's first
            # run only records the baseline it compares against later.
            self.assertIn(sync(peer_config, peer_state), {"unchanged", "pull"})

            # This Mac deletes the station the way Core does: the row goes, the
            # tombstone stays.
            self._write_relay(mac_config, self._two_station_store(removed=True))
            peer_before = (webdav_core.relay_accounts_path(peer_config)).read_bytes()
            self.assertEqual("push", sync(mac_config, mac_state))
            self.assertEqual("pull", sync(peer_config, peer_state))
            peer_after = json.loads((webdav_core.relay_accounts_path(peer_config)).read_text(encoding="utf-8"))
            self.assertEqual(["station-nimbus"], [station["id"] for station in peer_after["stations"]])
            self.assertNotEqual(peer_before, (webdav_core.relay_accounts_path(peer_config)).read_bytes())

            # The peer rewinds to the copy it had before the delete and pushes
            # it: the stale bundle that used to win.
            self._write_relay(peer_config, self._two_station_store(removed=False))
            self.assertEqual("push", sync(peer_config, peer_state))

            self.assertEqual("pull", sync(mac_config, mac_state))
            kept = json.loads(mac_relay.read_text(encoding="utf-8"))
            self.assertEqual(["station-nimbus"], [station["id"] for station in kept["stations"]])
            self.assertEqual(["account-nimbus"], [account["id"] for account in kept["accounts"]])
            self.assertEqual(["station-wawapii"], [row["id"] for row in kept["removed_stations"]])
            self.assertFalse([account for account in kept["accounts"] if account["label"] == "wawapii"])

    def test_a_route_added_while_the_peer_kept_a_tombstone_still_syncs(self) -> None:
        """A kept relay row is not an edit competing with the peer's route.

        A pull that preserves a tombstone writes a store its saved baseline
        does not describe, so the pulling machine reads as moved on the next
        run.  A route added on the other machine lives in a different file:
        taking it installs the route and merge-keeps the store, and the kept
        store is carried back on a later run.  This is the pair that stopped a
        gateway from taking a desktop's new model and asked the reader which
        unrelated change to keep.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            mac = root / "mac"
            peer = root / "peer"
            mac.mkdir()
            peer.mkdir()
            mac_config = self.write_config(mac)
            peer_config = self.write_config(peer)
            mac_state = mac / "sync-state.json"
            peer_state = peer / "sync-state.json"
            settings = webdav_core.Settings(url="https://dav.example.test/young-router/", remote_name="config.json")
            server = _MemoryWebDAV()

            def sync(config_path: Path, state_path: Path) -> str:
                local_snapshot = webdav_core.local_snapshot(config_path)
                remote_snapshot = webdav_operations.remote_snapshot(
                    server, settings, webdav_operations.read_remote_manifest(server, settings)
                )
                if remote_snapshot.manifest is None:
                    webdav_operations.push_bundle(server, settings, config_path, state_path, "sync-push")
                    return "push"
                plan, remote_snapshot = webdav_operations.decide_sync(
                    server, settings, local_snapshot, remote_snapshot, webdav_core.baseline_snapshot(state_path)
                )
                if plan == "unchanged":
                    agreed = webdav_core.SyncSnapshot(
                        remote_snapshot.manifest, remote_snapshot.digests or local_snapshot.digests
                    )
                    webdav_core.save_sync_state(state_path, settings, agreed, "sync")
                    return "unchanged"
                if plan == "push":
                    webdav_operations.push_bundle(server, settings, config_path, state_path, "sync-push")
                    return "push"
                if plan == "conflict":
                    return "conflict"
                webdav_operations.pull_bundle(server, settings, config_path, state_path, "sync-pull", skip_when_unchanged=False)
                return "pull"

            # The peer holds a tombstone the Mac's store never had.
            self._write_relay(mac_config, self._two_station_store(removed=False))
            self._write_relay(peer_config, self._two_station_store(removed=True))
            self.assertEqual("push", sync(mac_config, mac_state))
            self.assertEqual("pull", sync(peer_config, peer_state))
            # The merge kept the tombstone, so this machine no longer matches
            # the baseline its own pull saved.
            self.assertFalse(
                webdav_core.local_snapshot(peer_config).matches(webdav_core.baseline_snapshot(peer_state))
            )

            # The Mac adds a route and syncs it up.
            mac_config.write_text(
                mac_config.read_text(encoding="utf-8").replace("default-chat", "added-chat"),
                encoding="utf-8",
            )
            self.assertEqual("push", sync(mac_config, mac_state))
            # The peer takes the route; nothing about the two files competes.
            self.assertEqual("pull", sync(peer_config, peer_state))
            self.assertIn("added-chat", peer_config.read_text(encoding="utf-8"))
            kept = json.loads(webdav_core.relay_accounts_path(peer_config).read_text(encoding="utf-8"))
            self.assertIn("station-wawapii", [row["id"] for row in kept["removed_stations"]])

            # The kept store reaches the Mac on the peer's next run, and the
            # Mac merges it: both sides end up holding the tombstone.
            self.assertEqual("push", sync(peer_config, peer_state))
            self.assertEqual("pull", sync(mac_config, mac_state))
            mac_kept = json.loads(webdav_core.relay_accounts_path(mac_config).read_text(encoding="utf-8"))
            self.assertIn("station-wawapii", [row["id"] for row in mac_kept["removed_stations"]])

    def test_a_balance_read_is_not_a_change_between_machines(self) -> None:
        """A station read on one machine must not read as an edit on the other.

        Both machines run their own relay poller, so ``balance``, the balance's
        timestamp, the session and the station's catalog were rewritten every
        few minutes.  The bytes differed on every tick, the three-way
        comparison saw both ends move, and a reader who had just edited one
        route was told the two sides had both changed -- the other side was a
        balance nobody had edited.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            desktop = root / "desktop"
            host = root / "host"
            desktop.mkdir()
            host.mkdir()
            desktop_config = self.write_config(desktop)
            host_config = self.write_config(host)
            desktop_relay = webdav_core.relay_accounts_path(desktop_config)
            desktop_relay.parent.mkdir(parents=True, exist_ok=True)
            desktop_relay.write_text(
                json.dumps(self._relay_store("2026-11-08T03:11:37Z", "session=desktop")), encoding="utf-8"
            )
            host_relay = webdav_core.relay_accounts_path(host_config)
            host_relay.parent.mkdir(parents=True, exist_ok=True)
            host_relay.write_text(
                json.dumps(self._relay_store("2026-11-08T03:07:33Z", "session=host")), encoding="utf-8"
            )

            desktop_snapshot = webdav_core.local_snapshot(desktop_config)
            desktop_manifest, desktop_digests = desktop_snapshot.manifest, desktop_snapshot.digests
            host_snapshot = webdav_core.local_snapshot(host_config)
            host_manifest, host_digests = host_snapshot.manifest, host_snapshot.digests

            # The bytes really do differ -- that is the whole bug.
            self.assertNotEqual(
                [entry.get("sha256") for entry in desktop_manifest["files"]],
                [entry.get("sha256") for entry in host_manifest["files"]],
            )
            # The content a reader is responsible for does not.
            self.assertTrue(
                webdav_core.manifests_match(desktop_manifest, host_manifest, desktop_digests, host_digests)
            )
            self.assertEqual(
                "unchanged",
                webdav_core.smart_sync_plan(
                    webdav_core.SyncSnapshot(desktop_manifest, desktop_digests),
                    webdav_core.SyncSnapshot(host_manifest, host_digests),
                    webdav_core.SyncSnapshot(desktop_manifest, desktop_digests),
                ),
            )

    def test_a_route_edit_beside_observation_churn_is_pushed(self) -> None:
        """The reported bug, end to end at the comparison level.

        The reader edits one route on the desktop while the host only re-read
        its station.  Without the compared digests both sides look moved and
        the run stops; with them the route edit is the one move worth carrying.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            desktop = root / "desktop"
            host = root / "host"
            desktop.mkdir()
            host.mkdir()
            desktop_config = self.write_config(desktop)
            host_config = self.write_config(host)
            base_dir = root / "base"
            base_dir.mkdir()
            base_config = self.write_config(base_dir)
            for config_path, session, read_at in (
                (desktop_config, "session=desktop", "2026-11-08T03:11:37Z"),
                (host_config, "session=host", "2026-11-08T03:07:33Z"),
                (base_config, "session=base", "2026-11-08T03:00:00Z"),
            ):
                relay_path = webdav_core.relay_accounts_path(config_path)
                relay_path.parent.mkdir(parents=True, exist_ok=True)
                relay_path.write_text(json.dumps(self._relay_store(read_at, session)), encoding="utf-8")

            base_snapshot = webdav_core.local_snapshot(base_config)
            base_manifest, base_digests = base_snapshot.manifest, base_snapshot.digests
            host_snapshot = webdav_core.local_snapshot(host_config)
            host_manifest, host_digests = host_snapshot.manifest, host_snapshot.digests
            desktop_config.write_text(
                desktop_config.read_text(encoding="utf-8").replace("default-chat", "edited-chat"),
                encoding="utf-8",
            )
            desktop_snapshot = webdav_core.local_snapshot(desktop_config)
            desktop_manifest, desktop_digests = desktop_snapshot.manifest, desktop_snapshot.digests

            # Byte comparison: the relay files moved on both sides since the
            # baseline, so the run would stop and ask which side to keep.
            self.assertEqual(
                "conflict",
                webdav_core.smart_sync_plan(desktop_manifest, host_manifest, base_manifest),
            )
            # Compared content: only the route edit moved.
            self.assertEqual(
                "push",
                webdav_core.smart_sync_plan(
                    webdav_core.SyncSnapshot(desktop_manifest, desktop_digests),
                    webdav_core.SyncSnapshot(host_manifest, host_digests),
                    webdav_core.SyncSnapshot(base_manifest, base_digests),
                ),
            )

    def test_a_real_relay_edit_on_both_sides_still_stops(self) -> None:
        """Observations are ignored; an account change is still a question."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            desktop = root / "desktop"
            host = root / "host"
            desktop.mkdir()
            host.mkdir()
            desktop_config = self.write_config(desktop)
            host_config = self.write_config(host)
            base_store = self._relay_store("2026-11-08T03:00:00Z", "session=base")
            desktop_store = self._relay_store("2026-11-08T03:11:37Z", "session=desktop")
            host_store = self._relay_store("2026-11-08T03:07:33Z", "session=host")
            desktop_store["accounts"][0]["label"] = "desktop-renamed"  # type: ignore[index]
            host_store["accounts"][0]["label"] = "host-renamed"  # type: ignore[index]
            for config_path, store in (
                (desktop_config, desktop_store),
                (host_config, host_store),
            ):
                relay_path = webdav_core.relay_accounts_path(config_path)
                relay_path.parent.mkdir(parents=True, exist_ok=True)
                relay_path.write_text(json.dumps(store), encoding="utf-8")

            base_path = root / "base"
            base_path.mkdir()
            base_config = self.write_config(base_path)
            base_relay = webdav_core.relay_accounts_path(base_config)
            base_relay.parent.mkdir(parents=True, exist_ok=True)
            base_relay.write_text(json.dumps(base_store), encoding="utf-8")

            desktop_snapshot = webdav_core.local_snapshot(desktop_config)
            desktop_manifest, desktop_digests = desktop_snapshot.manifest, desktop_snapshot.digests
            host_snapshot = webdav_core.local_snapshot(host_config)
            host_manifest, host_digests = host_snapshot.manifest, host_snapshot.digests
            base_snapshot = webdav_core.local_snapshot(base_config)
            base_manifest, base_digests = base_snapshot.manifest, base_snapshot.digests
            self.assertEqual(
                "conflict",
                webdav_core.smart_sync_plan(
                    webdav_core.SyncSnapshot(desktop_manifest, desktop_digests),
                    webdav_core.SyncSnapshot(host_manifest, host_digests),
                    webdav_core.SyncSnapshot(base_manifest, base_digests),
                ),
            )

    def test_a_baseline_saved_without_digests_borrows_the_side_it_holds(self) -> None:
        """An upgrade must not read the relay store's observations as edits.

        A state file written before digests existed has raw hashes only.  The
        side whose bytes still equal the baseline's holds exactly the baseline
        content, so its compared digest is the baseline's -- and that is what
        lets the first run after an upgrade carry a route edit instead of
        stopping over a station read that happened on the other machine.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_dir = root / "base"
            desktop = root / "desktop"
            host = root / "host"
            for directory in (base_dir, desktop, host):
                directory.mkdir()
            config_paths = {
                "base": self.write_config(base_dir),
                "desktop": self.write_config(desktop),
                "host": self.write_config(host),
            }
            stores = {
                "base": self._relay_store("2026-11-08T03:00:00Z", "session=base"),
                "desktop": self._relay_store("2026-11-08T03:00:00Z", "session=base"),
                "host": self._relay_store("2026-11-08T03:07:33Z", "session=host"),
            }
            for name, config_path in config_paths.items():
                relay_path = webdav_core.relay_accounts_path(config_path)
                relay_path.parent.mkdir(parents=True, exist_ok=True)
                relay_path.write_text(json.dumps(stores[name]), encoding="utf-8")

            # The baseline was saved by the version before digests: raw only.
            base_manifest = webdav_core.local_snapshot(config_paths["base"]).manifest
            host_snapshot = webdav_core.local_snapshot(config_paths["host"])
            host_manifest, host_digests = host_snapshot.manifest, host_snapshot.digests

            # The desktop edits a route; its relay bytes are still the baseline's.
            config_paths["desktop"].write_text(
                config_paths["desktop"].read_text(encoding="utf-8").replace("default-chat", "edited-chat"),
                encoding="utf-8",
            )
            desktop_snapshot = webdav_core.local_snapshot(config_paths["desktop"])
            desktop_manifest, desktop_digests = desktop_snapshot.manifest, desktop_snapshot.digests

            # Raw comparison: the host's balance read moved the relay store,
            # but a relay move is the one a pull merges, so the route edit is
            # still the side to carry rather than a choice for the reader.
            self.assertEqual(
                "push",
                webdav_core.smart_sync_plan(desktop_manifest, host_manifest, base_manifest),
            )
            # With the baseline's digests filled in from the side that still
            # holds its bytes, the relay store did not move at all: only the
            # route edit is left, and it is the side to carry.
            self.assertEqual(
                "push",
                webdav_core.smart_sync_plan(
                    webdav_core.SyncSnapshot(desktop_manifest, desktop_digests),
                    webdav_core.SyncSnapshot(host_manifest, host_digests),
                    webdav_core.SyncSnapshot(base_manifest, None),
                ),
            )

    def test_both_sides_reaching_the_same_route_is_not_a_conflict(self) -> None:
        """A file both sides converged on is not a question.

        Two machines can both apply the same route edit since the baseline.  A
        snapshot-level comparison counted that as both ends moving and stopped;
        the file itself carries nothing to choose, so only the files the two
        sides disagree on may decide the direction.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_dir = root / "base"
            desktop = root / "desktop"
            host = root / "host"
            for directory in (base_dir, desktop, host):
                directory.mkdir()
            base_config = self.write_config(base_dir)
            desktop_config = self.write_config(desktop)
            host_config = self.write_config(host)
            base_relay = webdav_core.relay_accounts_path(base_config)
            base_relay.parent.mkdir(parents=True, exist_ok=True)
            base_relay.write_text(
                json.dumps(self._relay_store("2026-11-08T03:00:00Z", "session=base")), encoding="utf-8"
            )
            desktop_relay = webdav_core.relay_accounts_path(desktop_config)
            desktop_relay.parent.mkdir(parents=True, exist_ok=True)
            desktop_relay.write_text(
                json.dumps(self._relay_store("2026-11-08T03:00:00Z", "session=base")), encoding="utf-8"
            )
            host_relay = webdav_core.relay_accounts_path(host_config)
            host_relay.parent.mkdir(parents=True, exist_ok=True)
            host_store = self._relay_store("2026-11-08T03:07:33Z", "session=host")
            host_store["accounts"][0]["label"] = "renamed-on-the-host"  # type: ignore[index]
            host_relay.write_text(json.dumps(host_store), encoding="utf-8")

            # The same route edit landed on both machines.
            for config_path in (desktop_config, host_config):
                config_path.write_text(
                    config_path.read_text(encoding="utf-8").replace("default-chat", "shared-chat"),
                    encoding="utf-8",
                )

            base_snapshot = webdav_core.local_snapshot(base_config)
            base_manifest, base_digests = base_snapshot.manifest, base_snapshot.digests
            desktop_snapshot = webdav_core.local_snapshot(desktop_config)
            desktop_manifest, desktop_digests = desktop_snapshot.manifest, desktop_snapshot.digests
            host_snapshot = webdav_core.local_snapshot(host_config)
            host_manifest, host_digests = host_snapshot.manifest, host_snapshot.digests

            # Snapshot-level attribution would have called this a conflict
            # (both sides differ from the baseline); per file, the route both
            # sides already hold is skipped and only the host's account rename
            # is a move -- so either comparison carries it.
            self.assertEqual(
                "pull",
                webdav_core.smart_sync_plan(desktop_manifest, host_manifest, base_manifest),
            )
            self.assertEqual(
                "pull",
                webdav_core.smart_sync_plan(
                    webdav_core.SyncSnapshot(desktop_manifest, desktop_digests),
                    webdav_core.SyncSnapshot(host_manifest, host_digests),
                    webdav_core.SyncSnapshot(base_manifest, base_digests),
                ),
            )

    def test_the_digest_record_answers_only_for_its_own_manifest(self) -> None:
        """A record for another snapshot is not evidence about this one."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            config_path = self.write_config(source)
            snapshot = webdav_core.local_snapshot(config_path)
            manifest, digests = snapshot.manifest, snapshot.digests
            record = webdav_core.build_digest_record(manifest, digests)

            self.assertEqual(digests, webdav_core.read_digest_record(record, manifest))

            # A manifest that has since been replaced, malformed JSON, and an
            # unexpected file set all fall back to comparing bytes.
            replaced = json.loads(json.dumps(manifest))
            replaced["created_at"] = "2026-11-08T00:00:00Z"
            self.assertIsNone(webdav_core.read_digest_record(record, replaced))
            self.assertIsNone(webdav_core.read_digest_record(b"{not json", manifest))
            partial = json.loads(record)
            partial["files"].pop("relay-accounts.json")
            self.assertIsNone(
                webdav_core.read_digest_record(json.dumps(partial).encode("utf-8"), manifest)
            )

    def test_a_push_publishes_the_digests_and_the_baseline_keeps_them(self) -> None:
        """The remote side and the baseline carry the same comparison."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config_path = self.write_config(root)
            relay_path = webdav_core.relay_accounts_path(config_path)
            relay_path.parent.mkdir(parents=True, exist_ok=True)
            relay_path.write_text(
                json.dumps(self._relay_store("2026-11-08T03:11:37Z", "session=desktop")),
                encoding="utf-8",
            )
            settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")
            state_path = root / "state.json"

            class Client:
                def __init__(self) -> None:
                    self.puts: dict[str, bytes] = {}

                def try_mkcol(self, _url: str) -> None:
                    return None

                def put(self, url: str, data: bytes, _content_type: str) -> None:
                    self.puts[url] = data

            client = Client()
            webdav_operations.push_bundle(client, settings, config_path, state_path, "push")

            manifest_data = client.puts[webdav_core.manifest_url(settings)]
            record_data = client.puts[webdav_core.digests_url(settings)]
            manifest = json.loads(manifest_data)
            digests = webdav_core.baseline_snapshot(state_path).digests

            self.assertIsNotNone(digests)
            self.assertEqual(digests, webdav_core.read_digest_record(record_data, manifest))
            # The record describes content, while the bundle's own manifest
            # still describes the bytes it shipped.
            _manifest, files = webdav_core.read_config_bundle(client.puts[webdav_core.bundle_url(settings)])
            for name, data in files.items():
                self.assertEqual(webdav_core.content_digest(name, data), digests[name])

    def test_a_crlf_configuration_keeps_the_bytes_its_manifest_hashed(self) -> None:
        """The bundle carries what the manifest hashed, not newline-folded text."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            config_path = self.write_config(source)
            config_path.write_bytes(config_path.read_bytes().replace(b"\n", b"\r\n"))

            bundle, _manifest_snapshot = webdav_core.create_bundle(config_path)
            _manifest = _manifest_snapshot.manifest
            _manifest, files = webdav_core.read_config_bundle(bundle)
            self.assertEqual(config_path.read_bytes(), files["config.yaml"])

    def test_a_station_read_is_not_content_and_an_account_edit_is(self) -> None:
        """Only what a reader set is compared; the poll's own answers are not.

        Both machines rewrite the relay store on their own poll: a balance, the
        moment it was read, the session a login proved, the station's key list
        and its group catalog.  Those reads are nobody's edit, so they must not
        reach the digest -- and an account's own fields must, or a rename on one
        machine would be dropped instead of carried.
        """

        def digest(**changes: object) -> str:
            store = self._relay_store("2026-11-08T03:11:37Z", "session=desktop")
            store["accounts"][0].update(changes)  # type: ignore[index]
            return webdav_core.content_digest(
                "relay-accounts.json", json.dumps(store).encode("utf-8")
            )

        baseline = digest()
        # Every field the poll rewrites, one at a time.
        for name, value in (
            ("balance", 99.0),
            ("last_updated_at", "2026-11-08T09:00:00Z"),
            ("login_observed_at", "2026-11-08T09:00:00Z"),
            ("login_status", "signed_out"),
            ("resource_status", "unavailable"),
            ("resource_error", "login_expired"),
            ("resources", [{"id": "k-renamed", "name": "renamed", "enabled": False}]),
            ("groups", [{"id": "22", "name": "other", "multiplier": 1.0}]),
            ("station_resource_names", {"k1": "station-name"}),
            ("session", {"cookie": "session=other", "access_token": "", "refresh_token": ""}),
            ("user_id", "42"),
        ):
            self.assertEqual(baseline, digest(**{name: value}), name)
        # The fields a reader sets do reach it.  ``auto_grouping`` is already
        # true in this store, so flipping it to true would prove nothing.
        for name, value in (
            ("label", "renamed-account"),
            ("auto_grouping", False),
            ("remember_password", False),
            ("password", "a-new-password"),
            ("username", "someone-else@example.test"),
        ):
            self.assertNotEqual(baseline, digest(**{name: value}), name)

    def test_a_plan_that_stops_asks_a_remote_without_a_record_once(self) -> None:
        """The bundle answers a byte comparison the server published no record for.

        A conflict is the plan that costs a download anyway, so asking the
        bundle there is free -- and it is the only place the answer changes what
        the run does: a record the server does hold, or a plan that already
        knows its direction, fetches nothing.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            base_dir, desktop, host = root / "base", root / "desktop", root / "host"
            for directory in (base_dir, desktop, host):
                directory.mkdir()
            configs = {
                "base": self.write_config(base_dir),
                "desktop": self.write_config(desktop),
                "host": self.write_config(host),
            }
            stores = {
                "base": self._relay_store("2026-11-08T03:00:00Z", "session=base"),
                "desktop": self._relay_store("2026-11-08T03:00:00Z", "session=base"),
                "host": self._relay_store("2026-11-08T03:07:33Z", "session=host"),
            }
            for name, config_path in configs.items():
                relay_path = webdav_core.relay_accounts_path(config_path)
                relay_path.parent.mkdir(parents=True, exist_ok=True)
                relay_path.write_text(json.dumps(stores[name]), encoding="utf-8")

            base = webdav_core.local_snapshot(configs["base"])
            # Both machines edit the same route differently: one file moved on
            # both sides, which is the question no direction can answer and the
            # one plan whose download buys a digest record it does not have.
            configs["desktop"].write_text(
                configs["desktop"].read_text(encoding="utf-8").replace("default-chat", "edited-chat"),
                encoding="utf-8",
            )
            configs["host"].write_text(
                configs["host"].read_text(encoding="utf-8").replace("default-chat", "host-chat"),
                encoding="utf-8",
            )
            local = webdav_core.local_snapshot(configs["desktop"])
            bundle, published = webdav_core.create_bundle(configs["host"])
            settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

            # The host's bundle is what the server holds, and no digest record
            # was ever published beside it.
            class Client:
                def __init__(self) -> None:
                    self.gets = 0

                def get(self, _url: str, *, max_bytes: int) -> bytes:
                    self.gets += 1
                    return bundle

            remote = webdav_core.SyncSnapshot(published.manifest)
            self.assertEqual("conflict", webdav_core.smart_sync_plan(local, remote, base))

            client = Client()
            plan, resolved = webdav_operations.decide_sync(client, settings, local, remote, base)
            self.assertEqual(1, client.gets)
            self.assertEqual("conflict", plan)
            self.assertEqual(published.digests, resolved.digests)

            # A record the server does hold is never asked for again.
            client = Client()
            plan, _resolved = webdav_operations.decide_sync(
                client, settings, local, webdav_core.SyncSnapshot(published.manifest, published.digests), base
            )
            self.assertEqual(0, client.gets)
            self.assertEqual("conflict", plan)

            # A plan with a direction needs no bundle at all.
            client = Client()
            webdav_operations.decide_sync(
                client, settings, webdav_core.SyncSnapshot(base.manifest, base.digests), remote, base
            )
            self.assertEqual(0, client.gets)

            # A bundle that is not the manifest just compared is not mixed in:
            # its digests would describe content the caller is not looking at.
            replaced = json.loads(json.dumps(published.manifest))
            for entry in replaced["files"]:
                if entry.get("path") == "config.yaml":
                    entry["sha256"] = "0" * 64
            client = Client()
            plan, resolved = webdav_operations.decide_sync(
                client, settings, local, webdav_core.SyncSnapshot(replaced), base
            )
            self.assertEqual(1, client.gets)
            self.assertEqual("conflict", plan)
            self.assertIsNone(resolved.digests)

    def test_a_bundle_answers_for_its_manifest_when_no_record_exists(self) -> None:
        """A remote published before digests existed is still compared fairly."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            source = root / "source"
            source.mkdir()
            config_path = self.write_config(source)
            relay_path = webdav_core.relay_accounts_path(config_path)
            relay_path.parent.mkdir(parents=True, exist_ok=True)
            relay_path.write_text(
                json.dumps(self._relay_store("2026-11-08T03:11:37Z", "session=desktop")),
                encoding="utf-8",
            )
            bundle, snapshot = webdav_core.create_bundle(config_path)
            manifest, digests = snapshot.manifest, snapshot.digests
            settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

            class Client:
                def __init__(self, data: bytes) -> None:
                    self.data = data

                def get(self, _url: str, *, max_bytes: int) -> bytes:
                    return self.data

            from_bundle = webdav_operations.remote_snapshot_from_bundle(
                Client(bundle), settings, manifest
            )
            self.assertIsNotNone(from_bundle)
            self.assertEqual(digests, from_bundle.digests)

            # A sidecar manifest that does not describe this bundle is not
            # answered from it: the digests would name content the caller is
            # not comparing.
            other = json.loads(json.dumps(manifest))
            for entry in other["files"]:
                if entry.get("path") == "config.yaml":
                    entry["sha256"] = "0" * 64
            self.assertIsNone(
                webdav_operations.remote_snapshot_from_bundle(Client(bundle), settings, other)
            )

            # Publishing is best effort: a collection that refuses the file
            # leaves the byte comparison it always had.
            class ReadyClient:
                def __init__(self) -> None:
                    self.puts: dict[str, bytes] = {}

                def put(self, url: str, data: bytes, _content_type: str) -> None:
                    self.puts[url] = data

            ready = ReadyClient()
            self.assertTrue(
                webdav_operations.publish_remote_digests(
                    ready, settings, webdav_core.SyncSnapshot(manifest, digests)
                )
            )
            self.assertEqual(
                digests,
                webdav_core.read_digest_record(ready.puts[webdav_core.digests_url(settings)], manifest),
            )

            class RefusingClient:
                def put(self, _url: str, _data: bytes, _content_type: str) -> None:
                    raise webdav_core.WebDAVHTTPError("PUT", _url, 403, "Forbidden")

            self.assertFalse(
                webdav_operations.publish_remote_digests(
                    RefusingClient(), settings, webdav_core.SyncSnapshot(manifest, digests)
                )
            )

    def test_a_bundle_without_a_relay_store_does_not_delete_the_local_one(self) -> None:
        """An absent file in the bundle is not an instruction to delete."""

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            local = root / "local"
            remote = root / "remote"
            local.mkdir()
            remote.mkdir()
            local_config = self.write_config(local)
            remote_config = self.write_config(remote)

            local_relay = webdav_core.relay_accounts_path(local_config)
            local_relay.parent.mkdir(parents=True, exist_ok=True)
            local_relay.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "stations": [
                            {
                                "id": "station-nimbus",
                                "name": "nimbus",
                                "origin": "https://relay.example.test",
                                "type": "newapi",
                            }
                        ],
                        "accounts": [],
                        "pending_credential_cleanups": [],
                    }
                ),
                encoding="utf-8",
            )

            # The remote tree never had a relay store at all.
            bundle, manifest_snapshot = webdav_core.create_bundle(remote_config)
            manifest = manifest_snapshot.manifest
            self.assertFalse(
                [entry for entry in manifest["files"] if entry["path"] == "relay-accounts.json"][0]["present"]
            )
            result = webdav_core.install_bundle(bundle, local_config)

            self.assertNotIn(str(local_relay), result["removed"])
            self.assertTrue(local_relay.exists())
            self.assertEqual(
                "nimbus",
                json.loads(local_relay.read_text(encoding="utf-8"))["stations"][0]["name"],
            )

    def test_relay_document_sits_beside_a_runtime_directory_configuration(self) -> None:
        """The configuration already inside the runtime directory keeps one level.

        ``<root>/.litellm-runtime/config.yaml`` names the runtime directory
        itself, so appending another ``.litellm-runtime`` under its parent
        answered with ``.../.litellm-runtime/.litellm-runtime/relay-accounts.json``
        — a path an older layout left behind as a stale empty document.  Every
        reader that followed it saw no relay accounts while the real document
        sat next to the configuration, which is exactly how the relay panes
        came up empty.
        """

        with tempfile.TemporaryDirectory() as temp_dir:
            runtime = Path(temp_dir) / ".litellm-runtime"
            runtime.mkdir()
            config = runtime / "config.yaml"
            config.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")

            relay_path = webdav_core.relay_accounts_path(config)

            self.assertEqual(runtime / "relay-accounts.json", relay_path)
            # A configuration outside the runtime directory keeps the directory
            # this app creates beside it.
            other = Path(temp_dir) / "custom.yaml"
            other.write_text("providers: {}\nmodel_list: []\n", encoding="utf-8")
            self.assertEqual(
                Path(temp_dir) / ".litellm-runtime" / "relay-accounts.json",
                webdav_core.relay_accounts_path(other),
            )

    def test_current_version_three_relay_document_is_accepted_by_the_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            config = self.write_config(root)
            relay_path = root / ".litellm-runtime" / "relay-accounts.json"
            relay_path.parent.mkdir()
            relay_path.write_text(
                json.dumps(
                    {
                        "version": 3,
                        "stations": [],
                        "accounts": [],
                        "pending_credential_cleanups": [],
                    }
                ),
                encoding="utf-8",
            )

            bundle, manifest_snapshot = webdav_core.create_bundle(config)
            manifest = manifest_snapshot.manifest

            self.assertGreater(len(bundle), 0)
            relay_entry = next(item for item in manifest["files"] if item["path"] == "relay-accounts.json")
            self.assertTrue(relay_entry["present"])

    def test_tar_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_config = Path(temp_dir) / "config.yaml"
            tar_like = gzip.compress(b"not a supported json bundle")

            with self.assertRaisesRegex(webdav_core.SyncError, "must be JSON"):
                webdav_core.install_bundle(tar_like, target_config)

    def test_bundle_rejects_tampered_content_digest_without_writing_target(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = self.write_config(temp)
            target = temp / "target.yaml"
            target.write_text("untouched\n", encoding="utf-8")
            bundle, _manifest_snapshot = webdav_core.create_bundle(source)
            _manifest = _manifest_snapshot.manifest
            payload = json.loads(bundle)
            config_entry = next(
                entry for entry in payload["files"] if entry["path"] == "config.yaml"
            )
            config_entry["sha256"] = "0" * 64

            with self.assertRaisesRegex(webdav_core.SyncError, "invalid sha256"):
                webdav_core.install_bundle(
                    json.dumps(payload).encode("utf-8"), target
                )

            self.assertEqual("untouched\n", target.read_text(encoding="utf-8"))

    def test_bundle_rejects_duplicate_or_unexpected_file_entries(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = self.write_config(Path(temp_dir))
            bundle, _manifest_snapshot = webdav_core.create_bundle(source)
            _manifest = _manifest_snapshot.manifest
            payload = json.loads(bundle)
            payload["files"].append(dict(payload["files"][0]))

            with self.assertRaisesRegex(webdav_core.SyncError, "exactly the current"):
                webdav_core.read_config_bundle(json.dumps(payload).encode("utf-8"))

            payload = json.loads(bundle)
            payload["files"][1]["path"] = "unexpected.yaml"
            with self.assertRaisesRegex(webdav_core.SyncError, "unexpected file"):
                webdav_core.read_config_bundle(json.dumps(payload).encode("utf-8"))

    def test_bundle_rejects_duplicate_json_keys(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = self.write_config(Path(temp_dir))
            bundle, _manifest_snapshot = webdav_core.create_bundle(source)
            _manifest = _manifest_snapshot.manifest
            text = bundle.decode("utf-8")
            duplicate_key_bundle = text.replace(
                '"app": "young-router",',
                '"app": "young-router", "app": "young-router",',
                1,
            ).encode("utf-8")

            with self.assertRaisesRegex(webdav_core.SyncError, "duplicate JSON key"):
                webdav_core.read_config_bundle(duplicate_key_bundle)

    def test_bundle_file_reader_checks_size_before_reading(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "oversized.json"
            path.write_bytes(b"{}")

            with patch.object(webdav_core, "CONFIG_BUNDLE_MAX_BYTES", 1):
                with self.assertRaisesRegex(webdav_core.SyncError, "exceeds"):
                    webdav_core.read_config_bundle_file(path)

    def test_bundle_rejects_removed_schema_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            source = self.write_config(Path(temp_dir))
            bundle, _manifest_snapshot = webdav_core.create_bundle(source)
            _manifest = _manifest_snapshot.manifest
            payload = json.loads(bundle)
            config_entry = next(
                entry for entry in payload["files"] if entry["path"] == "config.yaml"
            )
            config_entry["content"] = config_entry["content"].replace(
                "upstream_url_surface: openai/responses",
                "supports_vision: true\n      upstream_url_surface: openai/responses",
            )
            encoded = config_entry["content"].encode("utf-8")
            config_entry["bytes"] = len(encoded)
            config_entry["sha256"] = webdav_core._sha256_bytes(encoded)

            with self.assertRaisesRegex(webdav_core.SyncError, "supports_vision"):
                webdav_core.read_config_bundle(json.dumps(payload).encode("utf-8"))

    def test_legacy_remote_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(webdav_core.SyncError, "must end with .json"):
            webdav_core._settings_from_raw(
                {
                    "url": "https://example.com/webdav/",
                    "remote_name": "young-router-config.tar.gz",
                }
            )

    def test_legacy_tar_url_is_rejected(self) -> None:
        settings = webdav_core.Settings(
            url="https://example.com/webdav/young-router-config.tar.gz"
        )

        with self.assertRaisesRegex(webdav_core.SyncError, "tar/tgz"):
            webdav_core.bundle_url(settings)

    def test_vercel_bypass_colon_query_is_normalized(self) -> None:
        settings = webdav_core._settings_from_raw(
            {
                "url": "https://example.com/dav/resource?x-vercel-protection-bypass:secret-value",
                # A stored earlier default is read as the current one: the
                # remote file is named after this app, not the proxy under it.
                "remote_name": "litellm-config.json",
            }
        )

        self.assertEqual(
            webdav_core.bundle_url(settings),
            "https://example.com/dav/resource/young-router-config.json?x-vercel-protection-bypass=secret-value",
        )
        self.assertEqual(webdav_core.DEFAULT_REMOTE_NAME, settings.remote_name)

    def test_a_folder_a_reader_types_is_percent_encoded(self) -> None:
        """A request line is ASCII; a folder name is not.

        The transport hands the target to ``http.client``, which encodes it as
        ASCII, so a collection named 我的坚果云 raised ``UnicodeEncodeError``
        before the request was sent and a space did the same.  The folder a
        server gives a reader is often exactly that kind, so the address is
        quoted where it is read.  An address pasted in its encoded form stays
        as it is: quoting twice would point at a different folder.
        """

        typed = webdav_core.Settings(url="https://dav.example.test/dav/我的坚果云/")
        encoded = "https://dav.example.test/dav/%E6%88%91%E7%9A%84%E5%9D%9A%E6%9E%9C%E4%BA%91"
        self.assertEqual(f"{encoded}/young-router-config.json", webdav_core.bundle_url(typed))
        self.assertEqual(f"{encoded}/young-router-config.manifest.json", webdav_core.manifest_url(typed))
        self.assertEqual(f"{encoded}/", webdav_core.collection_url(typed))

        pasted = webdav_core.Settings(url=f"{encoded}/")
        self.assertEqual(f"{encoded}/young-router-config.json", webdav_core.bundle_url(pasted))
        self.assertEqual(f"{encoded}/", webdav_core.collection_url(pasted))

        spaced = webdav_core.Settings(url="https://dav.example.test/dav/my folder/")
        self.assertEqual(
            "https://dav.example.test/dav/my%20folder/young-router-config.json",
            webdav_core.bundle_url(spaced),
        )

    def test_collection_access_is_read_from_the_privilege_set(self) -> None:
        """Asking whether the folder takes writes must not create anything."""

        def multistatus(*privileges: str) -> bytes:
            granted = "".join(f"<d:privilege><d:{name}/></d:privilege>" for name in privileges)
            return (
                '<?xml version="1.0" encoding="UTF-8"?>'
                '<d:multistatus xmlns:d="DAV:"><d:response><d:href>/dav/x/</d:href>'
                f"<d:propstat><d:prop><d:current-user-privilege-set>{granted}</d:current-user-privilege-set>"
                "</d:prop><d:status>HTTP/1.1 200 OK</d:status></d:propstat></d:response></d:multistatus>"
            ).encode()

        self.assertEqual("writable", webdav_core._collection_privilege_access(multistatus("read", "write")))
        self.assertEqual("writable", webdav_core._collection_privilege_access(multistatus("all")))
        self.assertEqual("read_only", webdav_core._collection_privilege_access(multistatus("read")))
        # No set, or nothing parseable, leaves the question open: guessing
        # "writable" here is what made a read-only mount point look usable.
        self.assertEqual("unknown", webdav_core._collection_privilege_access(b"<d:multistatus xmlns:d=\"DAV:\"/>"))
        self.assertEqual("unknown", webdav_core._collection_privilege_access(b"not xml at all"))

    def test_collection_access_asks_with_propfind_and_never_mkcol(self) -> None:
        requested: list[tuple[str, str]] = []

        def request(method: str, url: str, *args: object, **kwargs: object) -> tuple[int, dict[str, str], bytes]:
            requested.append((method, url))
            if method == "PROPFIND":
                return (
                    207,
                    {},
                    b'<d:multistatus xmlns:d="DAV:"><d:response><d:propstat><d:prop>'
                    b'<d:current-user-privilege-set><d:privilege><d:read/></d:privilege>'
                    b"</d:current-user-privilege-set></d:prop></d:propstat></d:response></d:multistatus>",
                )
            raise AssertionError(f"{method} is a write, and a probe must not write")

        settings = webdav_core.Settings(url="https://dav.example.test/dav/")
        client = webdav_core.WebDAVClient(settings)
        with patch.object(client, "request", side_effect=request):
            self.assertEqual("read_only", client.collection_access())

        self.assertEqual([("PROPFIND", "https://dav.example.test/dav/")], requested)

    def test_a_missing_collection_is_reported_as_missing(self) -> None:
        settings = webdav_core.Settings(url="https://dav.example.test/dav/new/")
        client = webdav_core.WebDAVClient(settings)
        missing = webdav_core.WebDAVHTTPError("PROPFIND", "https://dav.example.test/dav/new/", 404, "Not Found", b"")
        with patch.object(client, "request", side_effect=missing):
            self.assertEqual("missing", client.collection_access())

    def test_timeout_seconds_is_saved_with_webdav_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "webdav-sync.json"
            settings = webdav_core.Settings(
                url="https://example.com/dav/resource/",
                timeout_seconds=45.5,
            )

            webdav_core.save_settings(path, settings)
            loaded = webdav_core.load_settings(path)

        self.assertEqual(45.5, loaded.timeout_seconds)
        self.assertEqual(45.5, loaded.sanitized()["timeout_seconds"])

    def test_removed_timeout_environment_variable_does_not_override_current_default(self) -> None:
        with patch.dict("os.environ", {"LITELLM_WEBDAV_TIMEOUT": "99"}, clear=False):
            settings = webdav_core._settings_from_raw({})

        self.assertEqual(webdav_core.DEFAULT_TIMEOUT_SECONDS, settings.timeout_seconds)

    def test_removed_retry_environment_variables_do_not_change_internal_policy(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "LITELLM_WEBDAV_RETRY_ATTEMPTS": "9",
                "LITELLM_WEBDAV_RETRY_DELAY_SECONDS": "0",
            },
            clear=False,
        ):
            attempts = webdav_core._webdav_request_retry_attempts()
            delay = webdav_core._webdav_request_retry_delay(2)

        self.assertEqual(3, attempts)
        self.assertEqual(2.0, delay)

    def test_webdav_client_uses_settings_timeout(self) -> None:
        settings = webdav_core.Settings(
            url="https://example.com/dav/resource/",
            timeout_seconds=12.5,
        )

        client = webdav_core.WebDAVClient(settings)

        self.assertEqual(12.5, client.timeout)

    def test_vercel_security_checkpoint_error_explains_bypass(self) -> None:
        error = webdav_core.WebDAVHTTPError(
            "GET",
            "https://example.com/dav/resource/litellm-config.json?x-vercel-protection-bypass=secret-value",
            403,
            "Forbidden",
            b"<title>Vercel Security Checkpoint</title>",
        )

        message = str(error)
        self.assertIn("Vercel protection rejected the request", message)
        self.assertIn("x-vercel-protection-bypass=<secret>", message)
        self.assertNotIn("secret-value", message)

    def test_http_error_never_includes_arbitrary_response_body(self) -> None:
        marker = "sk-synthetic-webdav-leak-marker"
        error = webdav_core.WebDAVHTTPError(
            "PUT",
            "https://example.com/dav/resource/config.json",
            500,
            "Internal Server Error",
            f"upstream echoed Authorization: Bearer {marker}".encode(),
        )

        self.assertIn("HTTP 500 Internal Server Error", str(error))
        self.assertNotIn(marker, str(error))
        self.assertNotIn("Authorization", str(error))

    def test_vercel_checkpoint_retries_before_success(self) -> None:
        class FakeResponse:
            status = 200
            headers = {}

            def __init__(self) -> None:
                self.body = io.BytesIO(b"ok")

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self, size: int = -1) -> bytes:
                return self.body.read(size)

        settings = webdav_core.Settings(url="https://example.com/dav/resource/", username="webdav", password="token")
        client = webdav_core.WebDAVClient(settings)
        checkpoint = HTTPError(
            "https://example.com/dav/resource/file.json",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"<title>Vercel Security Checkpoint</title>"),
        )

        with patch("urllib.request.urlopen", side_effect=[checkpoint, FakeResponse()]) as urlopen, \
            patch("time.sleep"):
            status, _headers, body = client.request("GET", "https://example.com/dav/resource/file.json")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual(urlopen.call_count, 2)

    def test_success_response_is_read_within_explicit_limit(self) -> None:
        class FakeResponse(io.BytesIO):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        client = webdav_core.WebDAVClient(
            webdav_core.Settings(url="https://example.com/dav/resource/")
        )
        response = FakeResponse(b"okay")

        with patch("urllib.request.urlopen", return_value=response):
            status, _headers, body = client.request(
                "GET",
                "https://example.com/dav/resource/file.json",
                response_max_bytes=4,
            )

        self.assertEqual(status, 200)
        self.assertEqual(body, b"okay")

    def test_success_response_rejects_overflow_without_reading_the_rest(self) -> None:
        class FakeResponse(io.BytesIO):
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

        client = webdav_core.WebDAVClient(
            webdav_core.Settings(url="https://example.com/dav/resource/")
        )
        response = FakeResponse(b"x" * 1_000_000)

        with patch("urllib.request.urlopen", return_value=response):
            with self.assertRaisesRegex(webdav_core.SyncError, "4-byte limit"):
                client.request(
                    "GET",
                    "https://example.com/dav/resource/file.json",
                    response_max_bytes=4,
                )

        self.assertEqual(response.tell(), 5)

    def test_http_error_body_is_bounded_to_checkpoint_limit(self) -> None:
        body_stream = io.BytesIO(b"error" * 10_000)
        error = HTTPError(
            "https://example.com/dav/resource/file.json",
            500,
            "Internal Server Error",
            {},
            body_stream,
        )
        client = webdav_core.WebDAVClient(
            webdav_core.Settings(url="https://example.com/dav/resource/")
        )

        with patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(webdav_core.WebDAVHTTPError) as raised:
                client.request(
                    "GET",
                    "https://example.com/dav/resource/file.json",
                    response_max_bytes=webdav_core.CONFIG_BUNDLE_MAX_BYTES,
                )

        self.assertEqual(
            len(raised.exception.body), webdav_core.HTTP_ERROR_BODY_MAX_BYTES
        )
        self.assertEqual(body_stream.tell(), webdav_core.HTTP_ERROR_BODY_MAX_BYTES)

    def test_unauthorized_does_not_retry(self) -> None:
        settings = webdav_core.Settings(url="https://example.com/dav/resource/", username="webdav", password="bad")
        client = webdav_core.WebDAVClient(settings)
        unauthorized = HTTPError(
            "https://example.com/dav/resource/file.json",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b""),
        )

        with patch("urllib.request.urlopen", side_effect=unauthorized) as urlopen:
            with self.assertRaises(webdav_core.WebDAVHTTPError):
                client.request("GET", "https://example.com/dav/resource/file.json")

        self.assertEqual(urlopen.call_count, 1)

    def test_checkpointed_manifest_falls_back_to_bundle_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            bundle, manifest_snapshot = webdav_core.create_bundle(config_path)
            manifest = manifest_snapshot.manifest

        class FakeClient:
            def __init__(self) -> None:
                self.urls: list[str] = []
                self.limits: list[int] = []

            def get(self, url: str, *, max_bytes: int) -> bytes:
                self.urls.append(url)
                self.limits.append(max_bytes)
                if url.endswith(".manifest.json"):
                    raise webdav_core.WebDAVHTTPError(
                        "GET",
                        url,
                        403,
                        "Forbidden",
                        b"<title>Vercel Security Checkpoint</title>",
                    )
                return bundle

        client = FakeClient()
        settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        remote_manifest = webdav_operations.read_remote_manifest(client, settings)

        self.assertEqual(remote_manifest["summary"], manifest["summary"])
        self.assertEqual(len(client.urls), 2)
        self.assertEqual(
            client.limits,
            [webdav_core.MANIFEST_MAX_BYTES, webdav_core.CONFIG_BUNDLE_MAX_BYTES],
        )

    def test_missing_sidecar_manifest_falls_back_to_bundle_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            bundle, manifest_snapshot = webdav_core.create_bundle(config_path)
            manifest = manifest_snapshot.manifest

        class FakeClient:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url: str, *, max_bytes: int) -> bytes:
                self.urls.append(url)
                if url.endswith(".manifest.json"):
                    raise webdav_core.WebDAVHTTPError("GET", url, 404, "Not Found", b"")
                return bundle

        client = FakeClient()
        settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        remote_manifest = webdav_operations.read_remote_manifest(client, settings)

        self.assertEqual(remote_manifest["summary"], manifest["summary"])
        self.assertEqual(
            client.urls,
            [
                "https://example.com/dav/resource/config.manifest.json",
                "https://example.com/dav/resource/config.json",
            ],
        )

    def test_server_error_sidecar_manifest_falls_back_to_bundle_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            bundle, manifest_snapshot = webdav_core.create_bundle(config_path)
            manifest = manifest_snapshot.manifest

        class FakeClient:
            def get(self, url: str, *, max_bytes: int) -> bytes:
                if url.endswith(".manifest.json"):
                    raise webdav_core.WebDAVHTTPError("GET", url, 500, "Internal Server Error", b"")
                return bundle

        client = FakeClient()
        settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        remote_manifest = webdav_operations.read_remote_manifest(client, settings)

        self.assertEqual(remote_manifest["summary"], manifest["summary"])

    def test_push_succeeds_when_sidecar_manifest_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config_path = self.write_config(temp)
            state_path = temp / "state.json"

            class FakeClient:
                def __init__(self) -> None:
                    self.puts: list[tuple[str, bytes]] = []

                def try_mkcol(self, url: str) -> None:
                    pass

                def put(self, url: str, data: bytes, content_type: str) -> None:
                    self.puts.append((url, data))
                    if url.endswith(".manifest.json"):
                        raise webdav_core.WebDAVHTTPError("PUT", url, 404, "Not Found", b"")

            client = FakeClient()
            settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

            bundle_size, manifest = webdav_operations.push_bundle(client, settings, config_path, state_path, "push")

            self.assertGreater(bundle_size, 0)
            self.assertEqual(manifest["summary"]["active_models"], 1)
            self.assertEqual(
                [url for url, _data in client.puts],
                [
                    "https://example.com/dav/resource/config.json",
                    "https://example.com/dav/resource/config.manifest.json",
                ],
            )
            saved_state = json.loads(state_path.read_text(encoding="utf-8"))
            self.assertEqual(saved_state["remote_name"], "config.json")

    def test_push_succeeds_when_sidecar_manifest_has_server_error(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            config_path = self.write_config(temp)

            class FakeClient:
                def try_mkcol(self, url: str) -> None:
                    pass

                def put(self, url: str, data: bytes, content_type: str) -> None:
                    if url.endswith(".manifest.json"):
                        raise webdav_core.WebDAVHTTPError("PUT", url, 500, "Internal Server Error", b"")

            settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

            bundle_size, manifest = webdav_operations.push_bundle(FakeClient(), settings, config_path)

            self.assertGreater(bundle_size, 0)
            self.assertEqual(manifest["summary"]["active_models"], 1)

    def test_probe_uses_configured_remote_file_without_temporary_put(self) -> None:
        class Args:
            stdin_settings = False
            settings = Path("unused.json")

        class FakeClient:
            def __init__(self, settings: webdav_core.Settings) -> None:
                self.settings = settings

            def collection_access(self) -> str:
                return "writable"

            def try_mkcol(self, url: str) -> None:
                raise AssertionError("a probe must not create the collection")

            def head(self, url: str) -> tuple[int, dict[str, str]]:
                self.head_url = url
                return 200, {"Content-Length": "123"}

            def put(self, url: str, data: bytes, content_type: str) -> None:
                raise AssertionError("probe must not upload temporary files")

        settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        with patch("young_router.webdav.commands.load_settings", return_value=settings), \
            patch("young_router.webdav.commands.WebDAVClient", FakeClient), \
            patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = webdav_commands.command_probe(Args())

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("configured remote file", output)
        self.assertIn("https://example.com/dav/resource/config.json", output)
        self.assertNotIn(".young-router-probe", output)

    def test_checkpoint_after_retries_uses_curl_fallback(self) -> None:
        settings = webdav_core.Settings(url="https://example.com/dav/resource/", username="webdav", password="token")
        client = webdav_core.WebDAVClient(settings)
        checkpoint = HTTPError(
            "https://example.com/dav/resource/file.json",
            403,
            "Forbidden",
            {},
            io.BytesIO(b"<title>Vercel Security Checkpoint</title>"),
        )

        with patch("urllib.request.urlopen", side_effect=checkpoint) as urlopen, \
            patch("young_router.webdav.core._webdav_request_retry_attempts", return_value=1), \
            patch.object(client, "_curl_request", return_value=(200, {}, b"ok")) as curl_request:
            status, _headers, body = client.request("GET", "https://example.com/dav/resource/file.json")

        self.assertEqual(status, 200)
        self.assertEqual(body, b"ok")
        self.assertEqual(urlopen.call_count, 1)
        self.assertEqual(curl_request.call_count, 1)

    def test_curl_fallback_rejects_response_limit_exit(self) -> None:
        client = webdav_core.WebDAVClient(
            webdav_core.Settings(url="https://example.com/dav/resource/")
        )
        seen_config = ""

        def fake_run(arguments, capture_output, check):
            nonlocal seen_config
            seen_config = Path(arguments[2]).read_text(encoding="utf-8")
            return type(
                "CurlResult",
                (),
                {"stdout": b"200", "stderr": b"maximum file size exceeded", "returncode": 63},
            )()

        with patch("young_router.webdav.core._curl_binary", return_value="/usr/bin/curl"), \
            patch("young_router.webdav.core.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(webdav_core.SyncError, "4-byte limit"):
                client._curl_request(
                    "GET",
                    "https://example.com/dav/resource/file.json",
                    None,
                    None,
                    webdav_core.SyncError("urllib failed"),
                    4,
                )

        self.assertIn('max-filesize = "5"', seen_config)

    def test_retry_fallback_rejects_oversized_curl_body(self) -> None:
        url = "https://example.com/dav/resource/file.json"
        checkpoint = HTTPError(
            url,
            403,
            "Forbidden",
            {},
            io.BytesIO(b"<title>Vercel Security Checkpoint</title>"),
        )
        client = webdav_core.WebDAVClient(
            webdav_core.Settings(url="https://example.com/dav/resource/")
        )

        def config_value(config: str, key: str) -> str:
            prefix = f'{key} = "'
            line = next(line for line in config.splitlines() if line.startswith(prefix))
            return line[len(prefix):-1]

        def fake_run(arguments, capture_output, check):
            config = Path(arguments[2]).read_text(encoding="utf-8")
            Path(config_value(config, "dump-header")).write_bytes(
                b"HTTP/1.1 200 OK\r\nContent-Type: application/json\r\n\r\n"
            )
            Path(config_value(config, "output")).write_bytes(b"12345")
            return type(
                "CurlResult",
                (),
                {"stdout": b"200", "stderr": b"", "returncode": 0},
            )()

        with patch("urllib.request.urlopen", side_effect=checkpoint), \
            patch("young_router.webdav.core._webdav_request_retry_attempts", return_value=1), \
            patch("young_router.webdav.core._curl_binary", return_value="/usr/bin/curl"), \
            patch("young_router.webdav.core.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(webdav_core.SyncError, "4-byte limit"):
                client.request("GET", url, response_max_bytes=4)


if __name__ == "__main__":
    unittest.main()
