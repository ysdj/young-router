"""The Core store lock is a 200 ms lock, not a transaction lock.

Every window needs ``CoreStore.snapshot`` for its next frame, and a relay
Apply legitimately waits on a station for minutes.  These tests pin the
separation: remote work holds the mutation lane, while the state lock is
released and snapshots answer from the last committed projection.  The
``_BudgetLock`` records any hold that crosses the budget, so a future slow
call inside a ``with self._lock`` block fails here instead of as a frozen
window on somebody's machine.
"""

from __future__ import annotations

import copy
import os
import threading
import time
import unittest
from unittest import mock

from young_router.core.service import CORE_LOCK_BUDGET_SECONDS, CoreLockBudgetError, CoreStore, _BudgetLock


class SlowDomain:
    """A domain whose one action is a remote-style wait."""

    name = "slow"

    def __init__(self, wait: float = 0.6, *, dispatch_wait: float | None = None) -> None:
        self.wait = wait
        self.dispatch_wait = wait if dispatch_wait is None else dispatch_wait
        self._draft = {"value": 0}
        self.reloaded = 0
        self.applied = 0

    def draft_state(self) -> object:
        return copy.deepcopy(self._draft)

    def snapshot(self) -> dict[str, object]:
        return {"domain": self.name, "value": self._draft["value"]}

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, object]:
        time.sleep(self.dispatch_wait)
        self._draft["value"] += 1
        return {"operation_summary": {"operation": "slow_bump"}}

    def validate(self, payload: object | None = None) -> dict[str, object]:
        return {"valid": True, "issues": []}

    def apply(self, payload: object | None = None) -> None:
        time.sleep(self.wait)
        self.applied += 1

    def reload(self) -> None:
        time.sleep(self.wait)
        self.reloaded += 1

    def external_disk_state(self) -> dict[str, object]:
        return {"changed": getattr(self, "external_changed", False)}


class SlowSnapshotDomain(SlowDomain):
    """A domain whose projection is expensive, like the logs view."""

    def snapshot(self) -> dict[str, object]:
        time.sleep(0.4)
        return {"domain": self.name, "value": self._draft["value"], "rows": ["row"] * 64}


class LockBudgetTests(unittest.TestCase):
    def tearDown(self) -> None:
        os.environ.pop("YOUNG_ROUTER_CORE_LOCK_STRICT", None)

    def test_a_remote_dispatch_never_holds_the_state_lock(self) -> None:
        domain = SlowDomain(wait=0.7)
        core = CoreStore(domains=[domain])
        committed = core.snapshot()
        self.assertEqual(0, committed["revision"])

        failure: list[BaseException] = []

        def dispatch() -> None:
            try:
                core.dispatch({"domain": "slow", "type": "bump"})
            except BaseException as exc:  # pragma: no cover - reported below
                failure.append(exc)

        worker = threading.Thread(target=dispatch)
        worker.start()
        # Let the dispatch enter the adapter's long call.
        time.sleep(0.15)
        started = time.perf_counter()
        during = core.snapshot()
        elapsed = time.perf_counter() - started
        self.assertLess(elapsed, CORE_LOCK_BUDGET_SECONDS, "snapshot waited on remote work")
        self.assertEqual(0, during["revision"], "an in-flight dispatch published a revision")
        self.assertEqual(0, during["domains"]["slow"]["value"])
        self.assertEqual((), core._lock.overruns())
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual([], failure)
        after = core.snapshot()
        self.assertEqual(1, after["revision"])
        self.assertEqual(1, after["domains"]["slow"]["value"])
        self.assertEqual((), core._lock.overruns())

    def test_a_remote_reload_never_holds_the_state_lock(self) -> None:
        domain = SlowDomain(wait=0.5)
        core = CoreStore(domains=[domain])
        core.snapshot()
        worker = threading.Thread(target=lambda: core.reload("slow"))
        worker.start()
        time.sleep(0.1)
        started = time.perf_counter()
        core.snapshot()
        self.assertLess(time.perf_counter() - started, CORE_LOCK_BUDGET_SECONDS)
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, domain.reloaded)
        self.assertEqual((), core._lock.overruns())

    def test_a_slow_apply_never_holds_the_state_lock(self) -> None:
        domain = SlowDomain(wait=0.6, dispatch_wait=0)
        core = CoreStore(domains=[domain])
        core.snapshot()
        core.dispatch({"domain": "slow", "type": "bump"})
        committed = core.snapshot()["revision"]
        worker = threading.Thread(target=lambda: core.apply("slow"))
        worker.start()
        time.sleep(0.1)
        started = time.perf_counter()
        during = core.snapshot()
        self.assertLess(time.perf_counter() - started, CORE_LOCK_BUDGET_SECONDS)
        self.assertEqual(committed, during["revision"], "an in-flight apply published its revision early")
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        self.assertEqual(1, domain.applied)
        self.assertEqual(committed + 1, core.snapshot()["revision"])
        self.assertEqual((), core._lock.overruns())

    def test_the_live_service_probe_runs_outside_the_state_lock(self) -> None:
        def status(_operation: str) -> dict[str, object]:
            time.sleep(0.4)
            return {"state": "running", "pid": 1, "port": 49173}

        core = CoreStore(service_handlers={"status": status})
        core.snapshot()
        released = threading.Event()
        stop = threading.Event()

        def poll() -> None:
            # Another window's read must get the state lock while the status
            # probe is in flight.
            while not stop.is_set():
                with core._lock:
                    released.set()
                time.sleep(0.005)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        core.snapshot()
        self.assertTrue(released.wait(0.2), "the status probe held the state lock")
        stop.set()
        thread.join(timeout=2)
        self.assertEqual((), core._lock.overruns())

    def test_adopting_an_external_edit_does_not_hold_the_state_lock(self) -> None:
        domain = SlowDomain(wait=0.5)
        domain.external_changed = False
        core = CoreStore(domains=[domain])
        core.snapshot()
        domain.external_changed = True
        released = threading.Event()
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                with core._lock:
                    released.set()
                time.sleep(0.005)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        core.snapshot()
        self.assertEqual(1, domain.reloaded)
        self.assertTrue(released.wait(0.2), "adopting an external edit held the state lock")
        stop.set()
        thread.join(timeout=2)
        self.assertEqual((), core._lock.overruns())

    def test_snapshots_keep_answering_during_a_long_dispatch(self) -> None:
        domain = SlowDomain(wait=0.8)
        core = CoreStore(domains=[domain])
        core.snapshot()
        worst = [0.0]
        stop = threading.Event()

        def read() -> None:
            while not stop.is_set():
                started = time.perf_counter()
                core.snapshot()
                worst[0] = max(worst[0], time.perf_counter() - started)
                time.sleep(0.005)

        readers = [threading.Thread(target=read, daemon=True) for _ in range(3)]
        for reader in readers:
            reader.start()
        worker = threading.Thread(target=lambda: core.dispatch({"domain": "slow", "type": "bump"}))
        worker.start()
        worker.join(timeout=5)
        stop.set()
        for reader in readers:
            reader.join(timeout=2)
        self.assertFalse(worker.is_alive())
        self.assertLess(worst[0], CORE_LOCK_BUDGET_SECONDS, f"a snapshot waited {worst[0] * 1000:.0f} ms")
        self.assertEqual(1, core.snapshot()["revision"])
        self.assertEqual((), core._lock.overruns())

    def test_a_slow_projection_does_not_hold_the_state_lock(self) -> None:
        domain = SlowSnapshotDomain(wait=0)
        core = CoreStore(domains=[domain])
        released = threading.Event()
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                with core._lock:
                    released.set()
                time.sleep(0.005)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        started = time.perf_counter()
        core.snapshot()
        elapsed = time.perf_counter() - started
        stop.set()
        thread.join(timeout=2)
        self.assertGreaterEqual(elapsed, 0.35, "the fixture did not exercise a slow projection")
        self.assertTrue(released.wait(0.2), "the domain projection held the state lock")
        self.assertEqual((), core._lock.overruns())

    def test_a_large_action_summary_is_copied_outside_the_state_lock(self) -> None:
        core = CoreStore(domains=[SlowDomain(wait=0)])
        core.snapshot()
        # A fetch result carries every model the station answered; its deep
        # copy used to be part of the snapshot's critical section.
        core._last_actions["providers_models"] = {"models": [f"model-{index}" for index in range(120_000)]}
        acquisitions: list[float] = []
        stop = threading.Event()

        def poll() -> None:
            while not stop.is_set():
                with core._lock:
                    acquisitions.append(time.perf_counter())
                time.sleep(0.002)

        thread = threading.Thread(target=poll, daemon=True)
        thread.start()
        started = time.perf_counter()
        payload = core.snapshot()
        finished = time.perf_counter()
        stop.set()
        thread.join(timeout=2)
        self.assertEqual(120_000, len(payload["action_summaries"]["providers_models"]["models"]))
        self.assertTrue(
            any(started <= at <= finished for at in acquisitions),
            "the action summary was copied under the state lock",
        )
        self.assertEqual((), core._lock.overruns())

    def test_the_budget_lock_names_an_overrun(self) -> None:
        lock = _BudgetLock(budget=0.01)
        with lock:
            time.sleep(0.03)
        overruns = lock.overruns()
        self.assertEqual(1, len(overruns))
        self.assertGreater(float(overruns[0]["seconds"]), 0.01)
        self.assertIn("test_core_lock_budget", "".join(overruns[0]["stack"]))  # type: ignore[arg-type]

    def test_strict_mode_rejects_an_overrun(self) -> None:
        with mock.patch.dict(os.environ, {"YOUNG_ROUTER_CORE_LOCK_STRICT": "1"}):
            lock = _BudgetLock(budget=0.01)
            with self.assertRaises(CoreLockBudgetError):
                with lock:
                    time.sleep(0.03)


if __name__ == "__main__":
    unittest.main()
