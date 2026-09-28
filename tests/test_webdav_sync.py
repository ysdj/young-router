from __future__ import annotations

import gzip
import io
import json
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from webdav import commands as webdav_commands  # noqa: E402
from webdav import core as webdav_core  # noqa: E402
from webdav import operations as webdav_operations  # noqa: E402


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


class WebDAVSyncDirectionTests(unittest.TestCase):
    """The direction is a saved setting, so the interval loop obeys it too."""

    def _path(self, directory: Path) -> Path:
        return directory / "webdav.json"

    def test_the_direction_round_trips_and_defaults_to_smart(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(Path(directory))
            settings = webdav_core.load_settings(path)
            self.assertEqual("smart", settings.sync_direction)
            self.assertEqual("smart", settings.sanitized()["sync_direction"])

            saved = webdav_core.Settings(url="https://webdav.example.test/config/", sync_direction="pull")
            webdav_core.save_settings(path, saved)
            self.assertEqual("pull", webdav_core.load_settings(path).sync_direction)
            self.assertEqual("pull", json.loads(path.read_text(encoding="utf-8"))["sync_direction"])

    def test_the_ipc_spelling_and_legacy_keys_still_read_as_smart(self) -> None:
        cases = {"sync": "smart", "auto": "smart", "SMART": "smart", "": "smart", "sideways": "smart", "push": "push"}
        for stored, expected in cases.items():
            with self.subTest(stored):
                with tempfile.TemporaryDirectory() as directory:
                    path = self._path(Path(directory))
                    path.write_text(
                        json.dumps({"url": "https://webdav.example.test/config/", "sync_direction": stored}),
                        encoding="utf-8",
                    )
                    self.assertEqual(expected, webdav_core.load_settings(path).sync_direction)

    def test_a_direction_alias_from_an_older_file_is_honored(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(Path(directory))
            path.write_text(
                json.dumps({"url": "https://webdav.example.test/config/", "direction": "push"}),
                encoding="utf-8",
            )
            self.assertEqual("push", webdav_core.load_settings(path).sync_direction)

    def test_the_commands_layer_writes_the_direction_through(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = self._path(Path(directory))
            existing = webdav_core.load_settings(path)
            updated = webdav_commands._settings_from_payload({"sync_direction": "pull"}, existing)
            self.assertEqual("pull", updated.sync_direction)
            # A payload that says nothing about the direction keeps the one the
            # settings file already has.
            self.assertEqual("pull", webdav_commands._settings_from_payload({}, updated).sync_direction)
            self.assertEqual("smart", webdav_commands._settings_from_payload({}, existing).sync_direction)


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

            with patch("webdav.commands.command_probe", return_value=0):
                self.assertEqual(0, webdav_commands._run_recorded_command(args))
            success = json.loads(status_path.read_text(encoding="utf-8"))

            with patch("webdav.commands.command_probe", side_effect=webdav_core.SyncError("failed")):
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

            bundle, manifest = webdav_core.create_bundle(source_config)
            result = webdav_core.install_bundle(bundle, target_config)

            self.assertEqual(manifest["app"], "young-router")
            self.assertTrue(target_config.exists())
            self.assertEqual(result["manifest"]["summary"]["active_models"], 1)

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

            bundle, _manifest = webdav_core.create_bundle(source_config)
            self.assertIn(b"replace-password", bundle)
            self.assertIn(b"replace-cookie", bundle)
            result = webdav_core.install_bundle(bundle, target / "config.yaml")

            installed_relay = target / ".litellm-runtime" / "relay-accounts.json"
            self.assertIn(str(installed_relay), result["installed"])
            self.assertEqual(relay_payload, json.loads(installed_relay.read_text(encoding="utf-8")))
            self.assertEqual(0o600, installed_relay.stat().st_mode & 0o777)

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

            bundle, manifest = webdav_core.create_bundle(config)

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
            bundle, _manifest = webdav_core.create_bundle(source)
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
            bundle, _manifest = webdav_core.create_bundle(source)
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
            bundle, _manifest = webdav_core.create_bundle(source)
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
            bundle, _manifest = webdav_core.create_bundle(source)
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
            bundle, manifest = webdav_core.create_bundle(config_path)

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
            bundle, manifest = webdav_core.create_bundle(config_path)

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
            bundle, manifest = webdav_core.create_bundle(config_path)

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

            def try_mkcol(self, url: str) -> None:
                pass

            def head(self, url: str) -> tuple[int, dict[str, str]]:
                self.head_url = url
                return 200, {"Content-Length": "123"}

            def put(self, url: str, data: bytes, content_type: str) -> None:
                raise AssertionError("probe must not upload temporary files")

        settings = webdav_core.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        with patch("webdav.commands.load_settings", return_value=settings), \
            patch("webdav.commands.WebDAVClient", FakeClient), \
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
            patch("webdav.core._webdav_request_retry_attempts", return_value=1), \
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

        with patch("webdav.core._curl_binary", return_value="/usr/bin/curl"), \
            patch("webdav.core.subprocess.run", side_effect=fake_run):
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
            patch("webdav.core._webdav_request_retry_attempts", return_value=1), \
            patch("webdav.core._curl_binary", return_value="/usr/bin/curl"), \
            patch("webdav.core.subprocess.run", side_effect=fake_run):
            with self.assertRaisesRegex(webdav_core.SyncError, "4-byte limit"):
                client.request("GET", url, response_max_bytes=4)


if __name__ == "__main__":
    unittest.main()
