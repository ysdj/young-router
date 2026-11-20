"""Live schedules and bounded, local history for the Tasks & Logs view."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path

from .oplog import note
from .persistence import AtomicJSONStore, PersistenceError

RELAY_LOGIN = "relay-login"
RELAY_GROUPS = "relay-groups"
RELAY_NEGATIVE_BALANCE = "relay-negative-balance"
RELAY_SIGNED_IN_BALANCE = "relay-signed-in-balance"
WEBDAV_SYNC = "webdav-sync"
CLIENT_MODEL_SYNC = "client-model-sync"
IMAGE_CONTEXT_CLEANUP = "image-context-cleanup"
PROXY_SUPERVISION = "proxy-supervision"

TASK_IDS = (
    RELAY_LOGIN, RELAY_GROUPS, RELAY_NEGATIVE_BALANCE, RELAY_SIGNED_IN_BALANCE,
    WEBDAV_SYNC, CLIENT_MODEL_SYNC, IMAGE_CONTEXT_CLEANUP, PROXY_SUPERVISION,
)
HISTORY_LIMIT = 20
_OUTCOMES = ("succeeded", "partial", "failed", "skipped")
_COUNTS = ("succeeded", "failed", "skipped")


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")


def _saved_stamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


class ScheduledTasks:
    """Schedulers publish their actual deadlines here; viewing them runs no work."""

    def __init__(self, path: Path | None = None) -> None:
        self._store = AtomicJSONStore(path) if path is not None else None
        self._lock = threading.Lock()
        self._writer = threading.Lock()
        self._revision = 0
        self._plans: dict[str, tuple[int | float | None, str | None, str]] = {}
        self._running: dict[str, str] = {}
        self._history: dict[str, list[dict]] = self._read_history()

    def _read_history(self) -> dict[str, list[dict]]:
        if self._store is None:
            return {}
        try:
            document = self._store.read()
        except PersistenceError:
            note("scheduled task history could not be read")
            return {}
        result = {}
        for task_id in TASK_IDS:
            rows = document.get(task_id)
            if not isinstance(rows, list):
                continue
            result[task_id] = [
                {
                    "started_at": row["started_at"], "finished_at": row["finished_at"],
                    "outcome": row["outcome"],
                    **{key: row[key] for key in _COUNTS if type(row.get(key)) is int and row[key] >= 0},
                }
                for row in rows[:HISTORY_LIMIT]
                if isinstance(row, dict)
                and row.get("outcome") in _OUTCOMES
                and _saved_stamp(row.get("started_at"))
                and _saved_stamp(row.get("finished_at"))
            ]
        return result

    def plan(
        self, task_id: str, *, interval_seconds: int | float | None,
        delay_seconds: float | None, state: str | None = None,
    ) -> None:
        # Publish whole seconds: rechecking the same deadline should not turn
        # scheduling-clock jitter into a new log revision on every pass.
        deadline = None if delay_seconds is None else _stamp(round(time.time() + max(0.0, delay_seconds)))
        plan = (interval_seconds, deadline, state or ("idle" if deadline is None else "waiting"))
        with self._lock:
            if self._plans.get(task_id) != plan:
                self._plans[task_id] = plan
                self._revision += 1

    def begin(self, task_id: str) -> None:
        with self._lock:
            self._running[task_id] = _stamp(time.time())
            self._revision += 1

    def finish(
        self, task_id: str, outcome: str = "succeeded", *,
        succeeded: int | None = None, failed: int | None = None, skipped: int | None = None,
    ) -> None:
        finished = _stamp(time.time())
        # Serialize durable writes, but never hold the view lock during disk I/O.
        with self._writer:
            with self._lock:
                started = self._running.pop(task_id, None)
                if started is None:
                    return
                row = {"started_at": started, "finished_at": finished, "outcome": outcome}
                for key, count in zip(_COUNTS, (succeeded, failed, skipped)):
                    if count is not None:
                        row[key] = count
                history = self._history.setdefault(task_id, [])
                history.insert(0, row)
                del history[HISTORY_LIMIT:]
                self._revision += 1
                payload = dict(self._history)
            if self._store is not None:
                try:
                    self._store.write(payload)
                except PersistenceError:
                    note("scheduled task history could not be written")

    def finish_batch(self, task_id: str, outcomes: Iterable[str]) -> None:
        """Summarize account results; incomplete work counts as a failure."""
        succeeded = failed = skipped = 0
        partial = False
        for outcome in outcomes:
            if outcome == "succeeded":
                succeeded += 1
            elif outcome == "skipped":
                skipped += 1
            else:
                failed += 1
                partial |= outcome == "partial"
        if partial or (succeeded and (failed or skipped)):
            result = "partial"
        elif failed:
            result = "failed"
        else:
            result = "succeeded" if succeeded else "skipped"
        self.finish(task_id, result, succeeded=succeeded, failed=failed, skipped=skipped)

    def view(self, known_revision: int | None) -> tuple[int, list[dict] | None]:
        with self._lock:
            if known_revision == self._revision:
                return self._revision, None
            records = []
            for task_id in TASK_IDS:
                interval, deadline, state = self._plans.get(task_id, (None, None, "idle"))
                started = self._running.get(task_id)
                records.append({
                    "id": task_id,
                    "state": "running" if started is not None else state,
                    "interval_seconds": interval,
                    "next_run_at": deadline if started is None else None,
                    "running_since": started,
                    "history": deepcopy(self._history.get(task_id, [])),
                })
            return self._revision, records
