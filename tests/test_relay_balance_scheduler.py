from __future__ import annotations

import threading
import unittest

from young_router.core.relay_balance_scheduler import RelayBalanceScheduler
from young_router.core.scheduled_tasks import (
    RELAY_NEGATIVE_BALANCE,
    RELAY_SIGNED_IN_BALANCE,
    ScheduledTasks,
)


class RelayBalanceSchedulerTests(unittest.TestCase):
    def test_failed_read_still_waits_its_interval_and_same_account_runs_once(self) -> None:
        mono = [0.0]
        wall = [1000.0]
        state = {
            RELAY_NEGATIVE_BALANCE: (300, {"negative": None, "shared": 0.0}),
            RELAY_SIGNED_IN_BALANCE: (1800, {"signed": None, "shared": 0.0}),
        }
        calls = []
        tasks = ScheduledTasks()

        def refresh(account_ids, cancelled):
            self.assertFalse(cancelled())
            calls.append(account_ids)
            return {"negative": "failed", "shared": "succeeded"}

        scheduler = RelayBalanceScheduler(
            schedule=lambda: state, refresh=refresh, tasks=tasks,
            clock=lambda: mono[0], wall_clock=lambda: wall[0],
        )
        # An observation of age 1,000 seconds is already due for the negative
        # task. At 1,800 (before handling it) both tasks want that same read.
        self.assertEqual((0.0, {RELAY_NEGATIVE_BALANCE: ["shared"]}), scheduler._plan())
        mono[0] += 800.0
        wall[0] += 800.0
        _, due = scheduler._plan()
        scheduler._run(due)
        self.assertEqual([["negative", "shared"]], calls)
        self.assertEqual((300.0, {}), scheduler._plan())

        _, records = tasks.view(None)
        histories = {row["id"]: row["history"] for row in records or []}
        negative = histories[RELAY_NEGATIVE_BALANCE][0]
        signed = histories[RELAY_SIGNED_IN_BALANCE][0]
        self.assertEqual(("partial", 1, 1), (negative["outcome"], negative["succeeded"], negative["failed"]))
        self.assertEqual(("succeeded", 1), (signed["outcome"], signed["succeeded"]))

    def test_runtime_change_and_manual_observation_recompute_next_due(self) -> None:
        mono = [0.0]
        wall = [1000.0]
        state = {RELAY_NEGATIVE_BALANCE: (1800, {"negative": None})}
        scheduler = RelayBalanceScheduler(
            schedule=lambda: state, refresh=lambda *_: {},
            clock=lambda: mono[0], wall_clock=lambda: wall[0],
        )
        self.assertEqual((1800.0, {}), scheduler._plan())
        mono[0] += 120.0
        wall[0] += 120.0
        state[RELAY_NEGATIVE_BALANCE] = (300, {"negative": None})
        self.assertEqual((180.0, {}), scheduler._plan())
        state[RELAY_NEGATIVE_BALANCE] = (300, {"negative": wall[0]})
        self.assertEqual((300.0, {}), scheduler._plan())

    def test_kick_wakes_idle_loop_and_stop_unblocks_it(self) -> None:
        planning = threading.Event()
        performed = threading.Event()
        state = {}

        def schedule():
            answer = dict(state)
            planning.set()
            return answer

        def refresh(accounts, cancelled):
            performed.set()
            return dict.fromkeys(accounts, "succeeded")

        scheduler = RelayBalanceScheduler(schedule=schedule, refresh=refresh)
        try:
            scheduler.start()
            self.assertTrue(planning.wait(1))
            state[RELAY_NEGATIVE_BALANCE] = (300, {"negative": 0.0})
            scheduler.kick()
            self.assertTrue(performed.wait(1))
        finally:
            scheduler.stop()
        self.assertFalse(scheduler._thread.is_alive())


if __name__ == "__main__":
    unittest.main()
