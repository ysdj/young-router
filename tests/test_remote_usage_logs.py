from __future__ import annotations

import io
import tempfile
import textwrap
import unittest
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
import sys

sys.path.insert(0, str(ROOT))
import remote_usage_logs  # noqa: E402


class FakeResponse:
    def __init__(self, payload: object) -> None:
        import json

        self.body = json.dumps(payload).encode("utf-8")
        self.status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def getcode(self) -> int:
        return self.status

    def read(self, size: int = -1) -> bytes:
        return self.body if size < 0 else self.body[:size]


class RemoteUsageLogsTests(unittest.TestCase):
    def write_config(self) -> Path:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        path = Path(directory.name) / "config.yaml"
        path.write_text(
            textwrap.dedent(
                """
                providers:
                  relay:
                    api_base: https://relay.example.test/v1
                    api_keys:
                      - name: default
                        value: replace-me
                model_list:
                  - model_name: default-chat
                    litellm_params:
                      model: openai/default-chat
                      api_base: https://relay.example.test/v1
                    model_info:
                      id: a1b2c3d4
                      provider: relay
                      api_key_name: default
                      upstream_url_surface: openai/responses
                      supported_upstream_url_surfaces: [openai/responses]
                """
            ).lstrip(),
            encoding="utf-8",
        )
        return path

    def test_newapi_logs_are_sanitized_and_do_not_print_path_or_credential(self) -> None:
        path = self.write_config()
        seen: list[str] = []

        def opener(request, timeout):
            seen.append(request.full_url)
            self.assertEqual(request.get_header("Authorization"), "Bearer replace-me")
            return FakeResponse(
                {
                    "success": True,
                    "data": {
                        "items": [
                            {
                                "created_at": "2026-07-18T10:00:00Z",
                                "model_name": "example-model",
                                "status": "success",
                                "total_tokens": 42,
                                "request_body": "SECRET_REQUEST_BODY",
                                "path": "/private/secret.txt",
                            }
                        ]
                    },
                }
            )

        with patch("remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1)

        self.assertEqual(seen, ["https://relay.example.test/api/log/token"])
        self.assertIn("relay (newapi)", output)
        self.assertIn("example-model", output)
        self.assertIn("tokens=42", output)
        self.assertRegex(
            output.splitlines()[0],
            r"^Updated \d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$",
        )
        self.assertNotIn("replace-me", output)
        self.assertNotIn("SECRET_REQUEST_BODY", output)
        self.assertNotIn("/private/secret.txt", output)

    def test_unsupported_endpoints_do_not_expose_remote_error_text(self) -> None:
        path = self.write_config()

        def opener(request, timeout):
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

        with patch("remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1)

        self.assertEqual(output, "No configured relay exposed a supported online usage-log endpoint.")

    def test_gateway_key_log_feed_is_used_directly(self) -> None:
        path = self.write_config()
        seen: list[tuple[str, str | None]] = []

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            self.assertTrue(request.full_url.endswith("/log/token"))
            return FakeResponse(
                {
                    "success": True,
                    "data": [
                        {
                            "created_at": 1784368800,
                            "model_name": "gateway-model",
                            "prompt_tokens": 3,
                            "completion_tokens": 5,
                            "content": "SECRET_RESPONSE_TEXT",
                            "ip": "203.0.113.9",
                        }
                    ],
                }
            )

        with patch("remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            output = remote_usage_logs.render(path, 1)

        self.assertEqual(
            seen,
            [
                ("https://relay.example.test/api/log/token", "Bearer replace-me"),
            ],
        )
        self.assertIn("relay (newapi)", output)
        self.assertIn("gateway-model", output)
        self.assertIn("tokens=8", output)
        self.assertRegex(output, r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z  gateway-model")
        self.assertNotIn("replace-me", output)
        self.assertNotIn("SECRET_RESPONSE_TEXT", output)
        self.assertNotIn("203.0.113.9", output)

    def test_sub2api_credential_usage_summary_is_sanitized(self) -> None:
        target = remote_usage_logs.UsageTarget(
            provider="relay",
            api_base="https://relay.example.test/v1",
            api_key="replace-me",
        )
        seen: list[tuple[str, str | None]] = []

        def opener(request, timeout):
            seen.append((request.full_url, request.get_header("Authorization")))
            if request.full_url.endswith("/v1/usage"):
                return FakeResponse(
                    {
                        "mode": "unrestricted",
                        "model_stats": [
                            {
                                "model": "sub2api-model",
                                "input_tokens": 7,
                                "output_tokens": 11,
                                "request_id": "private-request-id",
                                "ip_address": "203.0.113.10",
                            }
                        ],
                    }
                )
            raise HTTPError(request.full_url, 404, "not found", {}, io.BytesIO())

        with patch("remote_usage_logs.isolated_http_opener") as opener_factory:
            opener_factory.return_value.open.side_effect = opener
            source, rows = remote_usage_logs.fetch_target(target, 1)

        self.assertEqual("sub2api", source)
        self.assertEqual(1, len(rows))
        self.assertIn("sub2api-model", rows[0])
        self.assertIn("tokens=18", rows[0])
        self.assertNotIn("private-request-id", rows[0])
        self.assertNotIn("203.0.113.10", rows[0])
        self.assertEqual(
            seen,
            [
                ("https://relay.example.test/api/log/token", "Bearer replace-me"),
                ("https://relay.example.test/v1/usage", "Bearer replace-me"),
            ],
        )


if __name__ == "__main__":
    unittest.main()
