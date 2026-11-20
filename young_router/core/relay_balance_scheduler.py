"""Sleep until the next relay balance reading is due."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import threading
import time

from .oplog import note
from .scheduled_tasks import RELAY_NEGATIVE_BALANCE, RELAY_SIGNED_IN_BALANCE, ScheduledTasks


# One task's configured interval and each eligible account's last reading.
BalanceSchedule = Mapping[str, tuple[float, Mapping[str, float | None]]]
_BALANCE_TASKS = (RELAY_NEGATIVE_BALANCE, RELAY_SIGNED_IN_BALANCE)
_PLAN_RETRY_SECONDS = 60.0


class RelayBalanceScheduler:
    """Schedule one read per account from its last observation or attempt."""

    def __init__(
        self,
        *,
        schedule: Callable[[], BalanceSchedule],
        refresh: Callable[[list[str], Callable[[], bool]], Mapping[str, str]],
        tasks: ScheduledTasks | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._schedule = schedule
        self._refresh = refresh
        self._tasks = tasks
        self._clock = clock
        self._wall_clock = wall_clock
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._eligible_since: dict[str, float] = {}
        self._last_attempt: dict[str, float] = {}
        self._intervals: dict[str, float] = {}

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="young-router-relay-balance-refresh",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def kick(self) -> None:
        """Recalculate after a saved setting or account observation changed."""

        self._wake.set()

    def _plan(self) -> tuple[float | None, dict[str, list[str]]]:
        schedule = self._schedule()
        now = self._clock()
        wall_now = self._wall_clock()
        eligible = {account_id for _, accounts in schedule.values() for account_id in accounts}
        for account_id in self._eligible_since.keys() - eligible:
            del self._eligible_since[account_id]
            self._last_attempt.pop(account_id, None)

        delay: float | None = None
        due: dict[str, list[str]] = {}
        self._intervals = {task_id: interval for task_id, (interval, _) in schedule.items()}
        for task_id, (interval, accounts) in schedule.items():
            remaining = []
            ready = []
            for account_id, observed_at in accounts.items():
                first_eligible = self._eligible_since.setdefault(account_id, now)
                # Recent manual reads count, including an observation read
                # before startup. A failed poll still counts as an attempt.
                anchor = self._last_attempt.get(account_id)
                if observed_at is not None and observed_at <= wall_now:
                    reading = now - (wall_now - observed_at)
                    anchor = reading if anchor is None else max(anchor, reading)
                waits = (first_eligible if anchor is None else anchor) + interval - now
                remaining.append(waits)
                if waits <= 0:
                    ready.append(account_id)
            task_delay = max(0.0, min(remaining)) if remaining else None
            if self._tasks is not None:
                self._tasks.plan(task_id, interval_seconds=interval, delay_seconds=task_delay)
            if task_delay is not None:
                delay = task_delay if delay is None else min(delay, task_delay)
            if ready:
                due[task_id] = ready
        return delay, due

    def _run(self, due: Mapping[str, list[str]]) -> None:
        accounts = list(dict.fromkeys(account_id for batch in due.values() for account_id in batch))
        self._last_attempt.update(dict.fromkeys(accounts, self._clock()))
        tasks = self._tasks
        if tasks is not None:
            for task_id in due:
                tasks.begin(task_id)
        try:
            outcomes = self._refresh(accounts, self._stop.is_set)
        except Exception:
            note("relay balance refresh could not be completed")
            outcomes = dict.fromkeys(accounts, "failed")
        if tasks is not None:
            for task_id, batch in due.items():
                tasks.finish_batch(task_id, (outcomes.get(account_id, "skipped") for account_id in batch))

    def _loop(self) -> None:
        while True:
            # Clear before reading state so a kick during planning is not lost.
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                delay, due = self._plan()
            except Exception:
                note("relay balance scheduler could not read the accounts")
                if self._tasks is not None:
                    for task_id in _BALANCE_TASKS:
                        self._tasks.begin(task_id)
                        self._tasks.finish(task_id, "failed")
                        self._tasks.plan(task_id, interval_seconds=self._intervals.get(task_id), delay_seconds=_PLAN_RETRY_SECONDS)
                self._wake.wait(_PLAN_RETRY_SECONDS)
                continue
            if self._stop.is_set():
                break
            if not due or self._wake.is_set():
                self._wake.wait(delay)
                continue
            self._run(due)
        if self._tasks is not None:
            for task_id in _BALANCE_TASKS:
                self._tasks.plan(task_id, interval_seconds=self._intervals.get(task_id), delay_seconds=None)
