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

import webdav_sync  # noqa: E402


class WebDAVSyncBundleTests(unittest.TestCase):
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
                """
            ).lstrip(),
            encoding="utf-8",
        )
        return path

    def test_json_bundle_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            temp = Path(temp_dir)
            source = temp / "source"
            target = temp / "target"
            source.mkdir()
            target.mkdir()
            source_config = self.write_config(source)
            target_config = target / "config.yaml"

            bundle, manifest = webdav_sync.create_bundle(source_config)
            result = webdav_sync.install_bundle(bundle, target_config)

            self.assertEqual(manifest["app"], "litellm-menu")
            self.assertTrue(target_config.exists())
            self.assertEqual(result["manifest"]["summary"]["active_models"], 1)

    def test_tar_bundle_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            target_config = Path(temp_dir) / "config.yaml"
            tar_like = gzip.compress(b"not a supported json bundle")

            with self.assertRaisesRegex(webdav_sync.SyncError, "must be JSON"):
                webdav_sync.install_bundle(tar_like, target_config)

    def test_legacy_remote_name_is_rejected(self) -> None:
        with self.assertRaisesRegex(webdav_sync.SyncError, "must end with .json"):
            webdav_sync._settings_from_raw(
                {
                    "url": "https://example.com/webdav/",
                    "remote_name": "litellm-menu-config.tar.gz",
                }
            )

    def test_legacy_tar_url_is_rejected(self) -> None:
        settings = webdav_sync.Settings(
            url="https://example.com/webdav/litellm-menu-config.tar.gz"
        )

        with self.assertRaisesRegex(webdav_sync.SyncError, "tar/tgz"):
            webdav_sync.bundle_url(settings)

    def test_vercel_bypass_colon_query_is_normalized(self) -> None:
        settings = webdav_sync._settings_from_raw(
            {
                "url": "https://example.com/dav/resource?x-vercel-protection-bypass:secret-value",
                "remote_name": "litellm-config.json",
            }
        )

        self.assertEqual(
            webdav_sync.bundle_url(settings),
            "https://example.com/dav/resource/litellm-config.json?x-vercel-protection-bypass=secret-value",
        )

    def test_timeout_seconds_is_saved_with_webdav_settings(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "webdav-sync.json"
            settings = webdav_sync.Settings(
                url="https://example.com/dav/resource/",
                timeout_seconds=45.5,
            )

            webdav_sync.save_settings(path, settings)
            loaded = webdav_sync.load_settings(path)

        self.assertEqual(45.5, loaded.timeout_seconds)
        self.assertEqual(45.5, loaded.sanitized()["timeout_seconds"])

    def test_webdav_client_uses_settings_timeout(self) -> None:
        settings = webdav_sync.Settings(
            url="https://example.com/dav/resource/",
            timeout_seconds=12.5,
        )

        client = webdav_sync.WebDAVClient(settings)

        self.assertEqual(12.5, client.timeout)

    def test_vercel_security_checkpoint_error_explains_bypass(self) -> None:
        error = webdav_sync.WebDAVHTTPError(
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

    def test_vercel_checkpoint_retries_before_success(self) -> None:
        class FakeResponse:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb) -> None:
                return None

            def read(self) -> bytes:
                return b"ok"

        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", username="webdav", password="token")
        client = webdav_sync.WebDAVClient(settings)
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

    def test_unauthorized_does_not_retry(self) -> None:
        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", username="webdav", password="bad")
        client = webdav_sync.WebDAVClient(settings)
        unauthorized = HTTPError(
            "https://example.com/dav/resource/file.json",
            401,
            "Unauthorized",
            {},
            io.BytesIO(b""),
        )

        with patch("urllib.request.urlopen", side_effect=unauthorized) as urlopen:
            with self.assertRaises(webdav_sync.WebDAVHTTPError):
                client.request("GET", "https://example.com/dav/resource/file.json")

        self.assertEqual(urlopen.call_count, 1)

    def test_checkpointed_manifest_falls_back_to_bundle_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            bundle, manifest = webdav_sync.create_bundle(config_path)

        class FakeClient:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url: str) -> bytes:
                self.urls.append(url)
                if url.endswith(".manifest.json"):
                    raise webdav_sync.WebDAVHTTPError(
                        "GET",
                        url,
                        403,
                        "Forbidden",
                        b"<title>Vercel Security Checkpoint</title>",
                    )
                return bundle

        client = FakeClient()
        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        remote_manifest = webdav_sync.read_remote_manifest(client, settings)

        self.assertEqual(remote_manifest["summary"], manifest["summary"])
        self.assertEqual(len(client.urls), 2)

    def test_missing_sidecar_manifest_falls_back_to_bundle_manifest(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = self.write_config(Path(temp_dir))
            bundle, manifest = webdav_sync.create_bundle(config_path)

        class FakeClient:
            def __init__(self) -> None:
                self.urls: list[str] = []

            def get(self, url: str) -> bytes:
                self.urls.append(url)
                if url.endswith(".manifest.json"):
                    raise webdav_sync.WebDAVHTTPError("GET", url, 404, "Not Found", b"")
                return bundle

        client = FakeClient()
        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        remote_manifest = webdav_sync.read_remote_manifest(client, settings)

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
            bundle, manifest = webdav_sync.create_bundle(config_path)

        class FakeClient:
            def get(self, url: str) -> bytes:
                if url.endswith(".manifest.json"):
                    raise webdav_sync.WebDAVHTTPError("GET", url, 500, "Internal Server Error", b"")
                return bundle

        client = FakeClient()
        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        remote_manifest = webdav_sync.read_remote_manifest(client, settings)

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
                        raise webdav_sync.WebDAVHTTPError("PUT", url, 404, "Not Found", b"")

            client = FakeClient()
            settings = webdav_sync.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

            bundle_size, manifest = webdav_sync.push_bundle(client, settings, config_path, state_path, "push")

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
                        raise webdav_sync.WebDAVHTTPError("PUT", url, 500, "Internal Server Error", b"")

            settings = webdav_sync.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

            bundle_size, manifest = webdav_sync.push_bundle(FakeClient(), settings, config_path)

            self.assertGreater(bundle_size, 0)
            self.assertEqual(manifest["summary"]["active_models"], 1)

    def test_probe_uses_configured_remote_file_without_temporary_put(self) -> None:
        class Args:
            stdin_settings = False
            settings = Path("unused.json")

        class FakeClient:
            def __init__(self, settings: webdav_sync.Settings) -> None:
                self.settings = settings

            def try_mkcol(self, url: str) -> None:
                pass

            def head(self, url: str) -> tuple[int, dict[str, str]]:
                self.head_url = url
                return 200, {"Content-Length": "123"}

            def put(self, url: str, data: bytes, content_type: str) -> None:
                raise AssertionError("probe must not upload temporary files")

        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", remote_name="config.json")

        with patch("webdav.commands.load_settings", return_value=settings), \
            patch("webdav.commands.WebDAVClient", FakeClient), \
            patch("sys.stdout", new_callable=io.StringIO) as stdout:
            exit_code = webdav_sync.command_probe(Args())

        self.assertEqual(exit_code, 0)
        output = stdout.getvalue()
        self.assertIn("configured remote file", output)
        self.assertIn("https://example.com/dav/resource/config.json", output)
        self.assertNotIn(".litellm-menu-probe", output)

    def test_checkpoint_after_retries_uses_curl_fallback(self) -> None:
        settings = webdav_sync.Settings(url="https://example.com/dav/resource/", username="webdav", password="token")
        client = webdav_sync.WebDAVClient(settings)
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


if __name__ == "__main__":
    unittest.main()
