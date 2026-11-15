"""Edge cases for the image context pass."""

from __future__ import annotations

import base64
import os
import unittest
from unittest import mock

from young_router.proxy import image_context as image_context_module


def _request(items: list) -> dict:
    return {
        "call_type": "aresponses",
        "headers": {"Originator": "codex_cli_rs"},
        "input": items,
    }


def _one_image_request() -> dict:
    raw = base64.b64encode(b"not-a-real-png").decode("ascii")
    return _request(
        [
            {
                "type": "message",
                "role": "user",
                "content": [
                    {"type": "input_text", "text": "look"},
                    {
                        "type": "input_image",
                        "image_url": "data:image/png;base64," + raw,
                    },
                ],
            }
        ]
    )


def _large_png() -> bytes:
    """A PNG header the scanner can size, padded past the default byte floor."""

    return (
        b"\x89PNG\r\n\x1a\n"
        + b"\x00" * 8
        + (12).to_bytes(4, "big")
        + (34).to_bytes(4, "big")
        + b"x" * (70 * 1024)
    )


def _message(role: str, text: str, image_url: str | None = None) -> dict:
    content = [
        {"type": "input_text" if role == "user" else "output_text", "text": text}
    ]
    if image_url is not None:
        content.append({"type": "input_image", "image_url": image_url})
    return {"type": "message", "role": role, "content": content}


class ImageContextEdgeCaseTests(unittest.TestCase):
    def test_ask_ids_accept_four_digit_ids(self) -> None:
        items = [
            {
                "type": "message",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": "需要 IMG-0004, need IMG-0005",
                    }
                ],
            }
        ]
        self.assertEqual(
            image_context_module._requested_ids(items), ["IMG-0004", "IMG-0005"]
        )

    def test_disabled_decline_does_not_scan_the_replay(self) -> None:
        request = _one_image_request()
        with mock.patch.dict(
            os.environ, {image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "0"}
        ), mock.patch.object(image_context_module, "scan") as scan:
            self.assertIsNone(
                image_context_module._with_codex_image_context(request)
            )
        scan.assert_not_called()

    def test_a_late_decline_reuses_the_scan_it_already_has(self) -> None:
        request = _one_image_request()
        real_scan = image_context_module.scan
        with mock.patch.dict(
            os.environ, {image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "1"}
        ), mock.patch.object(
            image_context_module, "scan", wraps=real_scan
        ) as scan:
            self.assertIsNone(
                image_context_module._with_codex_image_context(request)
            )
        self.assertEqual(scan.call_count, 1)

    def test_a_bare_base64_generated_result_is_scanned(self) -> None:
        encoded = base64.b64encode(_large_png()).decode("ascii")
        found = image_context_module.scan(
            [{"type": "image_generation_call", "result": encoded}]
        )
        self.assertEqual([image.image_id for image in found], ["IMG-001"])
        self.assertEqual(found[0].kind, "generated")

    def test_a_generated_picture_is_scanned_but_never_omitted(self) -> None:
        encoded = base64.b64encode(_large_png()).decode("ascii")
        request = _request(
            [
                _message("user", "first", "data:image/png;base64," + encoded),
                {"type": "image_generation_call", "result": encoded},
                _message("assistant", "done"),
                _message("user", "second"),
            ]
        )
        with mock.patch.dict(
            os.environ,
            {
                image_context_module.IMAGE_CONTEXT_ENABLED_ENV: "1",
                image_context_module.IMAGE_CONTEXT_MODE_ENV: "auto",
                image_context_module.IMAGE_CONTEXT_KEEP_RECENT_ENV: "0",
                image_context_module.IMAGE_CONTEXT_MIN_BYTES_ENV: "1",
            },
        ), mock.patch.object(image_context_module, "_trace") as trace:
            rewritten = image_context_module._with_codex_image_context(request)
        self.assertIsNotNone(rewritten)
        self.assertEqual(trace.call_args.args[3], 2)
        self.assertEqual(
            [image.image_id for image in trace.call_args.args[1]], ["IMG-001"]
        )
