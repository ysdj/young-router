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

    def test_a_task_is_named_by_the_title_codex_gives_its_thread(self) -> None:
        # The rail names rows by something a person recognises.  Codex already
        # keeps exactly that in its own index, keyed by the same thread id.
        codex_home = Path(self.directory.name) / "codex"
        codex_home.mkdir()
        (codex_home / "session_index.jsonl").write_text(
            json.dumps({"id": "task-one", "thread_name": "旧名字", "updated_at": "2026-01-01T00:00:00Z"}) + "\n"
            + json.dumps({"id": "task-one", "thread_name": "制作公司介绍页", "updated_at": "2026-01-02T00:00:00Z"}) + "\n"
            + json.dumps({"id": "task-two", "thread_name": "另一个任务", "updated_at": "2026-01-02T00:00:00Z"}) + "\n"
            + "not json\n",
            encoding="utf-8",
        )
        domain = ImageContextDomain({**self.environment, "CODEX_HOME": str(codex_home)})

        self.assertEqual(domain.tasks()[0]["title"], "制作公司介绍页")
        self.assertEqual(domain.snapshot()["task"]["task"]["title"], "制作公司介绍页")

    def test_a_task_without_a_title_states_nothing_rather_than_guessing(self) -> None:
        domain = ImageContextDomain({**self.environment, "CODEX_HOME": str(Path(self.directory.name) / "absent")})

        self.assertEqual(domain.tasks()[0]["title"], "")

    def write_selection(self, payload: dict) -> Path:
        """What Context Attachment Manager's own panel would have written."""

        path = Path(self.environment["YOUNG_ROUTER_CODEX_IMAGE_CONTEXT_DATA_DIR"]) / "selection" / "task-one.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload), encoding="utf-8")
        return path

    def test_unticking_an_image_writes_the_selection_the_pass_reads(self) -> None:
        # Manual selection is that task's own switch, set in the attachment
        # manager's panel: this app reads it and never turns it.
        path = self.write_selection(
            {
                "threadId": "task-one",
                "auto": False,
                "autoAt": "2026-01-02T00:00:00Z",
                "autoSince": "2026-01-01T00:00:00Z",
            }
        )
        self.domain.dispatch("set", {"task": "task-one", "id": "IMG-0001", "included": False})

        state = self.domain.snapshot()["task"]
        # Manual mode, because the unticked image is what decides sending here.
        self.assertEqual(state["next"], {"sent": 1, "omitted": 1})
        written = json.loads(path.read_text())
        self.assertIn("IMG-0001", written["unchecked"])
        self.assertEqual(written["threadId"], "task-one")
        # The switch and its stamps belong to that panel, not this one: a write
        # here may neither move nor drop them.
        self.assertFalse(written["auto"])
        self.assertEqual(written["autoAt"], "2026-01-02T00:00:00Z")
        self.assertEqual(written["autoSince"], "2026-01-01T00:00:00Z")

        # Ticking it again clears the entry rather than leaving a second state.
        self.domain.dispatch("set", {"task": "task-one", "id": "IMG-0001", "included": True})
        self.assertEqual(self.domain.snapshot()["task"]["next"], {"sent": 2, "omitted": 0})

    def test_in_auto_mode_a_tick_pins_the_image_instead(self) -> None:
        # Automatic selection omits earlier turns by itself, so the checkbox has
        # to mean "every turn" there -- clearing an unchecked entry would change
        # nothing that a person could see.
        path = self.write_selection({"threadId": "task-one", "auto": True})
        self.domain.dispatch("pin", {"task": "task-one", "id": "IMG-0002", "pinned": True})

        state = self.domain.snapshot()["task"]
        self.assertEqual(state["next"], {"sent": 1, "omitted": 1})
        self.assertEqual([row["id"] for row in state["images"] if row["pinned"]], ["IMG-0002"])
        written = json.loads(path.read_text())
        self.assertIn("IMG-0002", written["pinned"])
        self.assertTrue(written["auto"])

    def test_the_panel_does_not_switch_a_tasks_automatic_selection(self) -> None:
        from young_router.core.domains._shared import DomainError

        # The switch is not offered here any more: turning it is the attachment
        # manager's own action, in its own panel.
        with self.assertRaises(DomainError):
            self.domain.dispatch("auto", {"task": "task-one", "enabled": False})

        self.write_selection({"threadId": "task-one", "auto": False})
        self.assertFalse(self.domain.snapshot()["task"]["auto"])
        self.assertFalse(ImageContextDomain(self.environment).snapshot()["task"]["auto"])

    def test_a_write_keeps_the_marks_it_did_not_change(self) -> None:
        # A fork compares mark timestamps against its own start, so a rewrite
        # here may not restamp the unchecked entry it only carried forward.
        path = self.write_selection(
            {
                "threadId": "task-one",
                "unchecked": {"IMG-0001": {"id": "IMG-0001", "at": "2026-01-01T00:00:00Z"}},
                "inheritedFrom": "parent-thread",
            }
        )

        self.domain.dispatch("set", {"task": "task-one", "id": "IMG-0002", "included": False})

        written = json.loads(path.read_text())
        self.assertEqual(written["unchecked"]["IMG-0001"]["at"], "2026-01-01T00:00:00Z")
        self.assertEqual(written["inheritedFrom"], "parent-thread")

    def test_forgetting_a_task_removes_its_file_copies(self) -> None:
        folder = self.domain.cache_dir() / "files" / "task-one"
        self.assertTrue(folder.is_dir())

        self.domain.dispatch("forget", {"task": "task-one"})

        self.assertFalse((self.domain.cache_dir() / "task-one.json").exists())
        self.assertFalse(folder.exists())

    def test_opening_a_preview_names_the_image_and_closing_drops_it(self) -> None:
        self.domain.dispatch("preview", {"task": "task-one", "id": "IMG-0001"})

        preview = self.domain.snapshot()["preview"]
        self.assertEqual(preview["id"], "IMG-0001")
        self.assertEqual(preview["mime"], "image/png")
        # A snapshot is broadcast to every window and redacted field by field,
        # so the picture itself never rides in one: the id is enough to ask for
        # it, which is what the "image_context_image" read is for.
        self.assertNotIn("data_url", preview)

        self.domain.dispatch("close_preview", {})
        self.assertIsNone(self.domain.snapshot()["preview"])

    def test_an_oversized_image_is_scaled_instead_of_refused(self) -> None:
        # A 2400x3000 screenshot is 28 MB of base64, past the IPC frame budget.
        # The window still gets a picture: the stored bytes stay what the model
        # receives, and the preview is a scaled copy that says so.
        big = _png(2400, 3000, 7)
        with mock.patch.dict(os.environ, self.environment):
            store.store("task-big", {"input": [{"type": "message", "role": "user", "content": [
                {"type": "input_text", "text": "big"},
                {"type": "input_image", "image_url": _url(big)}]}]})
        domain = ImageContextDomain(self.environment)
        try:
            from PIL import Image  # noqa: F401
        except Exception:
            self.skipTest("Pillow is not installed in this interpreter")

        image = domain.image("task-big", "IMG-0001")

        self.assertGreater(image["bytes"], 6 * 1024 * 1024)
        self.assertTrue(image["downscaled"])
        self.assertTrue(image["data_url"].startswith("data:image/jpeg;base64,"))
        # Comfortably inside one IPC message, and smaller than the original.
        self.assertLess(len(image["data_url"]), 8 * 1024 * 1024)
        self.assertLessEqual(max(image["width"], image["height"]), 1600)

    def test_a_preview_that_fits_is_the_stored_image_unchanged(self) -> None:
        image = self.domain.image("task-one", "IMG-0001")

        self.assertFalse(image["downscaled"])
        self.assertTrue(image["data_url"].startswith("data:image/png;base64,"))
        self.assertEqual(image["width"], 30)
        self.assertEqual(image["height"], 20)

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
