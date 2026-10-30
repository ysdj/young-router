"""The panel's own domain: one task's images, and the user's choice per image."""

from __future__ import annotations

import base64
import json
import os
import random
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest import mock

from young_router.core.domains.image_context import ImageContextDomain
from young_router.proxy import image_context_store as store


def _png(width: int, height: int, seed: int = 1) -> bytes:
    generator = random.Random(seed)
    rows = b"".join(
        b"\x00" + bytes(generator.randrange(256) for _ in range(width * 3))
        for _ in range(height)
    )

    def chunk(kind: bytes, payload: bytes) -> bytes:
        body = kind + payload
        return (
            struct.pack(">I", len(payload))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(rows))
        + chunk(b"IEND", b"")
    )


def _url(raw: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


class ImageContextDomainTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        self.environment = {
            "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_CACHE_DIR": str(root / "replays"),
            "YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR": str(root / "cam"),
        }
        with mock.patch.dict(os.environ, self.environment):
            store.store(
                "task-one",
                {
                    "input": [
                        {"type": "message", "role": "user", "content": [
                            {"type": "input_text", "text": "一"},
                            {"type": "input_image", "image_url": _url(_png(30, 20))}]},
                        {"type": "message", "role": "user", "content": [
                            {"type": "input_text", "text": "二"},
                            {"type": "input_image", "image_url": _url(_png(12, 9, 2))}]},
                    ]
                },
            )
        self.domain = ImageContextDomain(self.environment)

    def test_the_snapshot_carries_the_tasks_and_the_open_one(self) -> None:
        snapshot = self.domain.snapshot()

        self.assertEqual([task["id"] for task in snapshot["tasks"]], ["task-one"])
        self.assertEqual(snapshot["task"]["task"]["id"], "task-one")
        # Newest first, so the image being worked on is the one at the top.
        self.assertEqual([image["id"] for image in snapshot["task"]["images"]], ["IMG-0002", "IMG-0001"])

    def test_unticking_an_image_writes_the_selection_the_pass_reads(self) -> None:
        # Manual selection: here a tick is the plain send/don't-send.
        self.domain.dispatch("auto", {"task": "task-one", "enabled": False})
        self.domain.dispatch("set", {"task": "task-one", "id": "IMG-0001", "included": False})

        state = self.domain.snapshot()["task"]
        # Manual mode, because the unticked image is what decides sending here.
        self.assertEqual(state["next"], {"sent": 1, "omitted": 1})
        written = json.loads(
            (Path(self.environment["YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR"]) / "selection" / "task-one.json").read_text()
        )
        self.assertIn("IMG-0001", written["unchecked"])
        self.assertEqual(written["threadId"], "task-one")

        # Ticking it again clears the entry rather than leaving a second state.
        self.domain.dispatch("set", {"task": "task-one", "id": "IMG-0001", "included": True})
        self.assertEqual(self.domain.snapshot()["task"]["next"], {"sent": 2, "omitted": 0})

    def test_in_auto_mode_a_tick_pins_the_image_instead(self) -> None:
        # Automatic selection omits earlier turns by itself, so the checkbox has
        # to mean "every turn" there -- clearing an unchecked entry would change
        # nothing that a person could see.
        self.domain.dispatch("auto", {"task": "task-one", "enabled": True})
        self.domain.dispatch("pin", {"task": "task-one", "id": "IMG-0002", "pinned": True})

        state = self.domain.snapshot()["task"]
        self.assertEqual(state["next"], {"sent": 1, "omitted": 1})
        self.assertEqual([row["id"] for row in state["images"] if row["pinned"]], ["IMG-0002"])
        written = json.loads(
            (Path(self.environment["YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR"]) / "selection" / "task-one.json").read_text()
        )
        self.assertIn("IMG-0002", written["pinned"])
        self.assertTrue(written["auto"])

    def test_automatic_selection_is_per_task_and_survives_a_reload(self) -> None:
        self.domain.dispatch("auto", {"task": "task-one", "enabled": False})

        self.assertFalse(self.domain.snapshot()["task"]["auto"])
        self.assertFalse(ImageContextDomain(self.environment).snapshot()["task"]["auto"])

    def test_opening_a_preview_carries_the_bytes_and_closing_drops_them(self) -> None:
        self.domain.dispatch("preview", {"task": "task-one", "id": "IMG-0001"})

        preview = self.domain.snapshot()["preview"]
        self.assertEqual(preview["mime"], "image/png")
        self.assertTrue(preview["data_url"].startswith("data:image/png;base64,"))

        self.domain.dispatch("close_preview", {})
        self.assertIsNone(self.domain.snapshot()["preview"])

    def test_an_unknown_image_is_refused(self) -> None:
        from young_router.core.domains._shared import DomainError

        with self.assertRaises(DomainError):
            self.domain.dispatch("set", {"task": "task-one", "id": "IMG-0404", "included": False})

    def test_a_task_cannot_name_a_file_outside_the_cache(self) -> None:
        from young_router.core.domains._shared import DomainError

        with self.assertRaises(DomainError):
            self.domain.dispatch("open", {"task": "../../etc/passwd"})


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
