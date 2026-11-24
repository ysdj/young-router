"""Run relay login checks and group refreshes independently of open panes."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence

from .oplog import note
from .scheduled_tasks import RELAY_GROUPS, RELAY_LOGIN, ScheduledTasks, TaskResult, task_exception_detail

RELAY_MAINTENANCE_SECONDS = 30 * 60
SHARED_LOGIN_RECHECK_SECONDS = 60


class RelayMaintenance:
    def __init__(
        self, *, tasks: ScheduledTasks, accounts: Callable[[], Mapping[str, Sequence[str]]],
        waiting_for_peer: Callable[[], Sequence[str]],
        run: Callable[[str, str, Callable[[], bool]], TaskResult], interval: float = RELAY_MAINTENANCE_SECONDS,
    ) -> None:
        self._tasks = tasks
        self._accounts = accounts
        self._waiting_for_peer = waiting_for_peer
        self._run = run
        self._interval = interval
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="young-router-relay-maintenance", daemon=True)
        self._thread.start()

    def kick(self) -> None:
        self._wake.set()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _loop(self) -> None:
        next_runs: dict[str, float] = {}
        peer_check_at: float | None = None
        while True:
            self._wake.clear()
            if self._stop.is_set():
                break
            try:
                accounts = self._accounts()
                waiting = self._waiting_for_peer()
            except Exception as exc:
                note("relay maintenance could not read the accounts")
                for task_id in (RELAY_LOGIN, RELAY_GROUPS):
                    self._tasks.begin(task_id)
                    self._tasks.finish(task_id, "failed", detail=task_exception_detail(exc, phase="load_accounts"))
                    self._tasks.plan(task_id, interval_seconds=self._interval, delay_seconds=self._interval)
                self._wake.wait(self._interval)
                continue
            now = time.monotonic()
            if not waiting:
                peer_check_at = None
            elif peer_check_at is None:
                peer_check_at = now + SHARED_LOGIN_RECHECK_SECONDS
            schedule: dict[str, float] = {}
            due = None
            for task_id in (RELAY_LOGIN, RELAY_GROUPS):
                if not accounts.get(task_id):
                    next_runs.pop(task_id, None)
                    delay = None
                else:
                    planned = next_runs.setdefault(task_id, now + self._interval)
                    if task_id == RELAY_LOGIN and peer_check_at is not None:
                        planned = min(planned, peer_check_at)
                    schedule[task_id] = planned
                    delay = planned - now
                    if delay <= 0 and due is None:
                        due = task_id
                self._tasks.plan(task_id, interval_seconds=self._interval, delay_seconds=delay)
            if self._stop.is_set():
                break
            if due is not None:
                if due == RELAY_LOGIN and peer_check_at is not None:
                    peer_check_at = now + SHARED_LOGIN_RECHECK_SECONDS
                if next_runs[due] <= now:
                    next_runs[due] = now + self._interval
                    self._run_due(due, accounts[due])
                else:
                    self._run_due(due, waiting)
            else:
                self._wake.wait(min(schedule.values()) - now if schedule else None)
        for task_id in (RELAY_LOGIN, RELAY_GROUPS):
            self._tasks.plan(task_id, interval_seconds=self._interval, delay_seconds=None)

    def _run_due(self, task_id: str, account_ids: Sequence[str]) -> None:
        self._tasks.begin(task_id)
        outcomes = []
        for account_id in account_ids:
            if self._stop.is_set():
                outcomes.append(TaskResult("skipped"))
                continue
            try:
                outcomes.append(self._run(task_id, account_id, self._stop.is_set))
            except Exception as exc:
                note("relay maintenance pass could not be completed")
                outcomes.append(TaskResult("failed", task_exception_detail(exc, phase="run_account")))
        self._tasks.finish_batch(task_id, outcomes)
