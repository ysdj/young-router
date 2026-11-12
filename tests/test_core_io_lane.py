"""A station read lends its domain's lane out, and takes it back after.

A dispatch holds its domain's write lane for the whole action.  That is right
for work that is a transaction and wrong for a read that is a conversation: the
online-usage page read can spend a station's whole budget on one page, and the
log tab's own polls take the same lane to re-derive their view -- so the tab
stopped updating, and a filter or pause queued behind a station.

``CoreStore._release_lane_for_io`` is the gate a domain wraps one such call in.
These tests pin what it must and must not do: while it is open the lane is free
(other domains' writes, the log polls and snapshots proceed), it is retaken
before the dispatch's bookkeeping, and a cross-domain transaction never lends
its locks -- a half-committed provider/relay pair is what they exist to prevent.
"""

from __future__ import annotations

import contextlib
import os
import tempfile
import threading
import time
import unittest
from typing import Any

from young_router.core.domains.logs import LogsDomain
from young_router.core.service import CORE_LOCK_BUDGET_SECONDS, CoreStore



def _another_thread_can_take(lock: threading.RLock, timeout: float = 0.05) -> bool:
    """Whether a *different* thread can take the lock (an RLock is reentrant)."""

    answer: list[bool] = []

    def take() -> None:
        got = lock.acquire(timeout=timeout)
        if got:
            lock.release()
        answer.append(got)

    thread = threading.Thread(target=take)
    thread.start()
    thread.join(timeout=2)
    return bool(answer and answer[0])

class _GateDomain:
    """A domain whose one action talks to a station inside the gate."""

    name = "slow"

    def __init__(self, wait: float = 0.5) -> None:
        self.wait = wait
        self._draft = {"value": 0}
        self._gate = None
        self.entered = threading.Event()
        self.leave = threading.Event()
        self.gated = 0

    def bind_io_gate(self, gate: Any) -> None:
        self._gate = gate

    def draft_state(self) -> object:
        return dict(self._draft)

    def snapshot(self) -> dict[str, object]:
        return {"domain": self.name, "value": self._draft["value"]}

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, object]:
        # The gate is used exactly the way the logs domain uses it: around the
        # one call that leaves the process.
        with self._gate():
            self.gated += 1
            first = self.gated == 1
            self.entered.set()
            if first:
                self.leave.wait(self.wait)
        self._draft["value"] += 1
        return {"operation_summary": {"operation": "bump"}}

    def validate(self, payload: object | None = None) -> dict[str, object]:
        return {"valid": True, "issues": []}

    def apply(self, payload: object | None = None) -> None:
        return None

    def reload(self) -> None:
        return None

    def external_disk_state(self) -> dict[str, object]:
        return {"changed": False}


class _OtherDomain(_GateDomain):
    name = "other"


class IoLaneTests(unittest.TestCase):
    def test_a_gated_read_frees_the_lane_for_everyone_else(self) -> None:
        station = _GateDomain(wait=2.0)
        other = _OtherDomain(wait=0)
        core = CoreStore(domains=[station, other])
        core.snapshot()
        worker = threading.Thread(target=lambda: core.dispatch({"domain": "slow", "type": "bump"}))
        worker.start()
        self.assertTrue(station.entered.wait(2.0), "the fixture never reached its station read")

        started = time.perf_counter()
        core.dispatch({"domain": "other", "type": "bump"})
        other_elapsed = time.perf_counter() - started
        started = time.perf_counter()
        core.snapshot()
        snapshot_elapsed = time.perf_counter() - started
        # A write in the same domain is a different question: inside the gate
        # nothing holds that domain, so it proceeds too -- which is what lets
        # the log tab's own poll re-derive its view while a page is read.
        started = time.perf_counter()
        core.dispatch({"domain": "slow", "type": "bump"})
        same_domain_elapsed = time.perf_counter() - started
        station.leave.set()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())

        # The second, same-domain dispatch ran while the first was still in
        # its gate: two station reads, neither waiting for the other.
        self.assertEqual(2, station.gated)
        for label, elapsed in (
            ("another domain's write", other_elapsed),
            ("a snapshot", snapshot_elapsed),
            ("the same domain's write", same_domain_elapsed),
        ):
            self.assertLess(elapsed, CORE_LOCK_BUDGET_SECONDS, f"{label} waited {elapsed * 1000:.0f} ms")
        self.assertEqual((), core._lock.overruns())

    def test_the_lane_is_retaken_for_the_rest_of_the_dispatch(self) -> None:
        station = _GateDomain(wait=0)
        other = _OtherDomain(wait=0)
        core = CoreStore(domains=[station, other])
        core.snapshot()
        seen: list[bool] = []

        original = station.dispatch

        def dispatch(action: str, payload: object | None = None) -> dict[str, object]:
            result = original(action, payload)
            # The gate is closed again here: this thread holds the lane once
            # more, so another thread cannot be inside it.
            seen.append(_another_thread_can_take(core._domain_locks["slow"]))
            return result

        station.dispatch = dispatch  # type: ignore[method-assign]
        core.dispatch({"domain": "slow", "type": "bump"})
        self.assertEqual([False], seen, "the lane was not retaken after the gate")

    def test_a_cross_domain_transaction_never_lends_its_locks(self) -> None:
        station = _GateDomain(wait=0)
        other = _OtherDomain(wait=0)
        core = CoreStore(domains=[station, other])
        core.snapshot()
        core._domain_locks.setdefault("a", threading.RLock())
        lent: list[bool] = []

        with core._domain_scope("slow", "other"):
            with core._release_lane_for_io():
                # A reader looking for either lock is turned away: the pair is
                # one promise until the scope ends.
                lent.append(_another_thread_can_take(core._domain_locks["slow"]))
        self.assertEqual([False], lent)


class _BlockingUsageReader:
    """A relay that answers one page, slowly -- the station's own budget."""

    def __init__(self) -> None:
        self.entered = threading.Event()
        self.leave = threading.Event()
        self.calls = 0

    def refresh(self, source_id: str | None = None, *, page: int = 1, rows_per_page: int | None = None):
        self.calls += 1
        self.entered.set()
        self.leave.wait(5.0)
        return [{"timestamp": "2026-01-01T00:00:10Z", "source": "newapi", "model": "m"}], "", 1


class LogsStationReadTests(unittest.TestCase):
    def test_a_log_poll_answers_while_a_usage_page_is_being_read(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reader = _BlockingUsageReader()
            logs = LogsDomain(directory, online_usage_reader=reader)
            other = _OtherDomain(wait=0)
            core = CoreStore(domains=[logs, other])
            core.snapshot()
            worker = threading.Thread(
                target=lambda: core.dispatch(
                    {"domain": "logs", "type": "logs.refresh_online_usage",
                     "payload": {"tab": "online-usage", "source": "one"}}
                )
            )
            worker.start()
            self.assertTrue(reader.entered.wait(5.0), "the fixture never asked the station")

            started = time.perf_counter()
            view = core.log_view("requests")
            poll_elapsed = time.perf_counter() - started
            started = time.perf_counter()
            core.dispatch({"domain": "other", "type": "bump"})
            other_elapsed = time.perf_counter() - started
            reader.leave.set()
            worker.join(timeout=6)
            self.assertFalse(worker.is_alive())

            self.assertIn("log", view)
            self.assertLess(poll_elapsed, CORE_LOCK_BUDGET_SECONDS, f"the log poll waited {poll_elapsed * 1000:.0f} ms")
            self.assertLess(other_elapsed, CORE_LOCK_BUDGET_SECONDS, f"another domain waited {other_elapsed * 1000:.0f} ms")
            self.assertEqual(1, reader.calls)
            # The answer landed: the page the station returned is the tab's.
            tab = logs.view("online-usage")["log"]
            self.assertEqual("one", tab["source_id"])
            self.assertEqual(1, tab["line_count"])


if __name__ == "__main__":
    unittest.main()
