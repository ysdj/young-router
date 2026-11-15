from __future__ import annotations

import sys
import tempfile
import textwrap
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from young_router.webdav import core as webdav_core  # noqa: E402
from young_router.webdav import operations as webdav_operations  # noqa: E402


class WebDAVOperationsEdgeTests(unittest.TestCase):
    def _settings(self) -> webdav_core.Settings:
        return webdav_core.Settings(
            url="https://webdav.example.test/config/", remote_name="config.json"
        )

    def _bundle(self) -> tuple[bytes, dict]:
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "config.yaml"
            config_path.write_text(
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
            bundle, snapshot = webdav_core.create_bundle(config_path)
        return bundle, snapshot.manifest or {}

    def test_missing_sidecar_and_bundle_mean_no_remote(self) -> None:
        class Client:
            def __init__(self, code: int) -> None:
                self.code = code

            def get(self, url: str, *, max_bytes: int) -> bytes:
                raise webdav_core.WebDAVHTTPError("GET", url, self.code, "Missing", b"")

        for code in (409, 410, 501):
            with self.subTest(code=code):
                self.assertIsNone(
                    webdav_operations.read_remote_manifest(Client(code), self._settings())
                )

    def test_a_bundle_the_server_refuses_reads_as_no_remote(self) -> None:
        class Client:
            def get(self, url: str, *, max_bytes: int) -> bytes:
                raise webdav_core.WebDAVHTTPError("GET", url, 403, "Forbidden", b"")

        self.assertIsNone(
            webdav_operations.read_remote_manifest(Client(), self._settings())
        )

    def test_a_gone_sidecar_falls_back_to_the_bundle(self) -> None:
        bundle, manifest = self._bundle()

        class Client:
            def get(self, url: str, *, max_bytes: int) -> bytes:
                if url.endswith(".manifest.json"):
                    raise webdav_core.WebDAVHTTPError("GET", url, 410, "Gone", b"")
                return bundle

        remote = webdav_operations.read_remote_manifest(Client(), self._settings())
        self.assertEqual(manifest["summary"], remote["summary"])
