"""The store's write order: a copy must never exist before its replay.

The hourly sweep deletes every file folder no fresh replay names, so a copy
written ahead of its replay can be removed while it is still being written.
"""

from __future__ import annotations

import base64
import os
import tempfile
import unittest
from unittest import mock

from young_router.proxy import image_context_store as store_module


def _png() -> bytes:
    return b"\x89PNG\r\n\x1a\n" + bytes(range(64))


class StoreWriteOrderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        environment = mock.patch.dict(
            os.environ,
            {
                store_module.REPLAY_CACHE_DIR_ENV: os.path.join(
                    self.directory.name, "replays"
                )
            },
        )
        environment.start()
        self.addCleanup(environment.stop)

    def request(self) -> dict:
        url = "data:image/png;base64," + base64.b64encode(_png()).decode("ascii")
        part = {"type": "input_image", "image_url": url}
        return {"input": [{"type": "message", "role": "user", "content": [part]}]}

    def test_the_replay_is_published_before_the_copies_are_written(self) -> None:
        published = []
        original = store_module._write_files

        def note_order(thread_id, images) -> None:
            replay = os.path.join(store_module.cache_dir(), f"{thread_id}.json")
            published.append(os.path.isfile(replay))
            original(thread_id, images)

        with mock.patch.object(store_module, "_write_files", note_order):
            self.assertEqual(1, store_module.store("t1", self.request()))

        self.assertEqual([True], published)

    def test_a_sweep_during_the_write_keeps_the_copies(self) -> None:
        real_decode = base64.b64decode

        def sweep_first(encoded, **kwargs):
            # Stands in for Core's hourly sweep landing in the middle of a write.
            store_module.sweep()
            return real_decode(encoded, **kwargs)

        with mock.patch.object(store_module.base64, "b64decode", sweep_first):
            self.assertEqual(1, store_module.store("t1", self.request()))

        self.assertEqual(["IMG-001"], list(store_module.paths("t1")))
