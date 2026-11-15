"""Edge cases of the WebDAV interval loop's own control flow."""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from young_router.core.webdav_scheduler import WebDAVSyncScheduler


class WebDAVSyncSchedulerLoopTests(unittest.TestCase):
    def test_an_idle_sleep_that_reports_no_kick_does_not_end_the_loop(self) -> None:
        """Only stop() ends the loop; a wait returning False goes back to the top."""

        class IdleScheduler(WebDAVSyncScheduler):
            def __init__(self, **kwargs) -> None:
                super().__init__(**kwargs)
                self.waits = 0

            def _wait(self, seconds: float | None) -> bool:
                self.waits += 1
                if self.waits >= 2:
                    self._stop.set()
                return False

        scheduler = IdleScheduler(
            snapshot=lambda: {"enabled": False, "configured": True, "sync_interval_minutes": 30},
            sync=lambda: self.fail("a disabled scheduler must not sync"),
        )
        scheduler._loop()
        self.assertEqual(2, scheduler.waits)


if __name__ == "__main__":
    unittest.main()
