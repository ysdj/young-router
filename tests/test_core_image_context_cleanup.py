"""Core's cleanup loop: the image-context day passes while nothing writes."""

from __future__ import annotations

import threading
import unittest

from young_router.core.image_context_cleanup import ImageContextSweeper


class ImageContextSweeperTests(unittest.TestCase):
    def test_start_sweeps_once_then_waits_until_stopped(self) -> None:
        swept = threading.Event()
        calls: list[int] = []

        def sweep() -> int:
            calls.append(1)
            swept.set()
            return 1

        sweeper = ImageContextSweeper(sweep=sweep, interval=3600)
        sweeper.start()
        thread = sweeper._thread
        try:
            self.assertTrue(swept.wait(2.0))
            # One long wait, not a poll: nothing else runs until the hour or
            # the stop arrives.
            self.assertEqual(len(calls), 1)
            self.assertIsNotNone(thread)
        finally:
            sweeper.stop()
        self.assertFalse(thread.is_alive())

    def test_a_failed_sweep_does_not_end_the_loop(self) -> None:
        attempts: list[int] = []

        def sweep() -> None:
            attempts.append(1)
            raise OSError("the cache folder went away")

        sweeper = ImageContextSweeper(sweep=sweep, interval=3600)
        # ``run_once`` is the loop body; a store that cannot be read is due
        # again next interval, not a reason to stop sweeping forever.
        sweeper.run_once()
        sweeper.run_once()

        self.assertEqual(len(attempts), 2)

    def test_core_owns_one_loop_for_the_stores_rule(self) -> None:
        from young_router.core.service import CoreStore

        # The entry point starts whatever this answers, so the store's rule is
        # applied while Core runs even when no request writes a replay.
        core = CoreStore(domains=[])
        sweeper = core.image_context_sweeper()
        try:
            self.assertIs(sweeper, core.image_context_sweeper())
        finally:
            core._stop_image_context_sweeper()
