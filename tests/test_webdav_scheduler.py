"""The WebDAV interval loop: when it fires, and when it stays quiet.

The pane offers an enable switch and an interval, so the loop is what turns
those two settings into actual syncs.  These tests drive it with a fake clock
and a fake snapshot, which is the only way to check the timing rules — due now,
never synced, clock moved back, and "do not retry in a tight loop" — without
waiting real minutes.
"""

from __future__ import annotations

import datetime as dt
import threading
import time
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from young_router.core.webdav_scheduler import WebDAVSyncScheduler


def stamp(epoch: float) -> str:
    """One of webdav.core's UTC stamps for a given epoch second."""

    return dt.datetime.fromtimestamp(epoch, dt.timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class FakeClock:
    def __init__(self, start: float = 1_700_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class WebDAVSyncSchedulerTests(unittest.TestCase):
    def build(self, state: dict, *, sync=None, clock=None, **kwargs):
        self.calls: list[float] = []
        self.clock = clock or FakeClock()
        scheduler = WebDAVSyncScheduler(
            snapshot=lambda: state,
            sync=sync or (lambda: self.calls.append(self.clock())),
            clock=self.clock,
            **kwargs,
        )
        return scheduler, state

    def enabled(self, **overrides) -> dict:
        state = {
            "enabled": True,
            "configured": True,
            "sync_interval_minutes": 30,
            "last_sync_at": None,
        }
        state.update(overrides)
        return state

    def test_a_machine_that_never_synced_waits_for_the_grace_period(self) -> None:
        scheduler, _state = self.build(self.enabled(), initial_delay=120.0, settle=0.0)
        self.assertEqual((120.0, False), scheduler.next_run())

    def test_an_overdue_machine_fires_at_once(self) -> None:
        clock = FakeClock()
        state = self.enabled(last_sync_at=stamp(clock.now - 3 * 3600))
        scheduler, _state = self.build(state, clock=clock, settle=0.0)
        self.assertEqual((0.0, True), scheduler.next_run())

    def test_a_scheduled_machine_waits_out_the_rest_of_its_interval(self) -> None:
        clock = FakeClock()
        state = self.enabled(last_sync_at=stamp(clock.now - 600.0))
        scheduler, _state = self.build(state, clock=clock, settle=0.0)
        delay, fire = scheduler.next_run()
        self.assertFalse(fire)
        # 1200 seconds are left of the 30-minute interval; the loop still wakes
        # every few minutes so a changed setting cannot hide behind a long sleep.
        self.assertAlmostEqual(300.0, delay)

    def test_a_stamp_from_the_future_waits_one_interval_not_forever(self) -> None:
        """A clock that moved back must not park the loop for days."""

        clock = FakeClock()
        state = self.enabled(last_sync_at=stamp(clock.now + 48 * 3600))
        scheduler, _state = self.build(state, clock=clock, settle=0.0)
        self.assertEqual((300.0, False), scheduler.next_run())

    def test_the_switch_the_endpoint_and_the_interval_each_stop_the_loop(self) -> None:
        cases = {
            "switch off": self.enabled(enabled=False),
            "no endpoint": self.enabled(configured=False),
            "manual only": self.enabled(sync_interval_minutes=0),
            "unreadable interval": self.enabled(sync_interval_minutes="soon"),
        }
        for label, state in cases.items():
            with self.subTest(label):
                scheduler, _state = self.build(state, settle=0.0)
                self.assertEqual((None, False), scheduler.next_run())

    def test_unreadable_settings_stay_quiet_instead_of_raising(self) -> None:
        scheduler = WebDAVSyncScheduler(
            snapshot=lambda: (_ for _ in ()).throw(RuntimeError("no settings")),
            sync=lambda: self.fail("an unreadable snapshot must not sync"),
            clock=FakeClock(),
        )
        self.assertEqual((None, False), scheduler.next_run())

    def test_a_failed_run_is_retried_once_per_interval_not_in_a_loop(self) -> None:
        """The retry counts the attempt, so a dead server is not hammered."""

        clock = FakeClock()
        state = self.enabled(last_sync_at=stamp(clock.now - 3600.0))

        def fail() -> None:
            raise RuntimeError("The remote is unreachable")

        scheduler, _state = self.build(state, sync=fail, clock=clock, settle=0.0)
        delay, fire = scheduler.next_run()
        self.assertTrue(fire)
        scheduler._run()
        # The sync left no stamp, so only the attempt records that it happened.
        self.assertEqual((300.0, False), scheduler.next_run())

    def test_a_reader_typing_holds_an_automatic_run_until_the_store_is_quiet(self) -> None:
        clock = FakeClock()
        quiet = {"seconds": 0.0}
        scheduler, _state = self.build(
            self.enabled(last_sync_at=stamp(clock.now - 3600.0)),
            clock=clock,
            settle_seconds=lambda: quiet["seconds"],
            settle=30.0,
        )
        self.assertEqual((30.0, False), scheduler.next_run())
        quiet["seconds"] = 30.0
        self.assertEqual((0.0, True), scheduler.next_run())

    def test_a_core_that_is_never_quiet_still_gets_its_sync(self) -> None:
        """The settle courtesy must not starve the interval.

        A pane that dispatches on a poll wakes Core every few seconds; before
        the grace, the loop waits, and after it the loop runs anyway.
        """

        clock = FakeClock()
        scheduler, _state = self.build(
            self.enabled(last_sync_at=stamp(clock.now - 3600.0)),
            clock=clock,
            settle_seconds=lambda: 0.0,
            settle=30.0,
            settle_grace=120.0,
        )
        self.assertEqual((30.0, False), scheduler.next_run())
        clock.advance(119.0)
        # Still inside the grace: wait for quiet again.
        self.assertEqual((30.0, False), scheduler.next_run())
        clock.advance(2.0)
        # Past it: the interval wins over a Core that never goes quiet.
        self.assertEqual((0.0, True), scheduler.next_run())

    def test_the_loop_runs_once_and_stops(self) -> None:
        """The threaded loop itself: it fires on its grace period, then stops."""

        calls: list[float] = []
        ran = threading.Event()
        scheduler = WebDAVSyncScheduler(
            snapshot=lambda: self.enabled(sync_interval_minutes=1),
            sync=lambda: (calls.append(time.time()), ran.set()),
            # Real time here: this is the only test that waits through the loop.
            initial_delay=0.05,
            max_sleep=0.05,
            settle=0.0,
        )
        scheduler.start()
        try:
            self.assertTrue(ran.wait(2.0), "the interval loop never ran a sync")
            # The state file never gains a stamp in this test, so the attempt is
            # the anchor: no second run inside the first interval.
            time.sleep(0.2)
        finally:
            scheduler.stop()
        self.assertEqual(1, len(calls))
        self.assertFalse(scheduler._thread and scheduler._thread.is_alive())

    def test_a_kick_makes_the_loop_re_read_its_settings(self) -> None:
        """A saved setting takes effect at once, not after the whole interval."""

        clock = FakeClock()
        state = self.enabled(last_sync_at=stamp(clock.now - 10.0))
        scheduler, _state = self.build(state, clock=clock, max_sleep=5.0, settle=0.0)
        self.assertGreater(scheduler.next_run()[0] or 0.0, 0.0)
        # Turning the interval down makes it due immediately; the pane's patch
        # wakes the loop instead of leaving it asleep.
        state["last_sync_at"] = stamp(clock.now - 3600.0)
        scheduler.kick()
        self.assertEqual((0.0, True), scheduler.next_run())

    def test_stop_ends_a_loop_that_has_nothing_scheduled(self) -> None:
        scheduler, _state = self.build(self.enabled(enabled=False), settle=0.0)
        scheduler.start()
        time.sleep(0.05)
        scheduler.stop()
        self.assertFalse(scheduler._thread and scheduler._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
