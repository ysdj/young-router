"""Edge cases of the WebDAV interval loop's own control flow."""

from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from young_router.core.domains.webdav import WebDAVSettingsDomain
from young_router.core.service import CoreStore


class WebDAVSyncSchedulerLoopTests(unittest.TestCase):
    def test_first_apply_wakes_a_scheduler_that_saw_the_saved_switch_off(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            domain = WebDAVSettingsDomain(root / "webdav.json", state_path=root / "state.json")
            core = CoreStore(domains=[domain])
            idle = threading.Event()
            ran = threading.Event()
            read_state = core.webdav_auto_sync_state

            def snapshot() -> dict:
                state = read_state()
                if not state["enabled"]:
                    idle.set()
                return state

            with (
                mock.patch.object(core, "run_automatic_webdav_sync", side_effect=lambda: ran.set()),
                mock.patch.object(core, "webdav_auto_sync_state", side_effect=snapshot),
                mock.patch.object(core, "quiet_seconds", return_value=60.0),
            ):
                loop = core.webdav_scheduler()
                loop._initial_delay = 0
                loop.start()
                try:
                    self.assertTrue(idle.wait(2))
                    core.dispatch({"domain": "webdav", "type": "patch", "payload": {
                        "url": "https://example.test/dav/", "enabled": True,
                    }})
                    self.assertFalse(ran.is_set())
                    core.apply("webdav")
                    self.assertTrue(ran.wait(2))
                finally:
                    core.shutdown()


if __name__ == "__main__":
    unittest.main()
