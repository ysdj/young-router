"""Live schedules and bounded, local history for the Tasks & Logs view."""

from __future__ import annotations

import threading
import time
from collections.abc import Iterable, Mapping
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
from itertools import islice
from pathlib import Path

from .oplog import note
from .persistence import AtomicJSONStore, PersistenceError
from .security import safe_exception_message, safe_public

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
_ISSUE_LIMIT = 8
_OUTCOMES = ("succeeded", "partial", "failed", "skipped")
_COUNTS = ("succeeded", "failed", "skipped")

TaskFacts = dict[str, str | int | bool]


@dataclass(frozen=True)
class TaskResult:
    outcome: str
    detail: TaskFacts | None = None


def _stamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat(timespec="milliseconds")


def _saved_stamp(value: object) -> bool:
    if not isinstance(value, str) or len(value) > 40:
        return False
    try:
        return datetime.fromisoformat(value).tzinfo is not None
    except ValueError:
        return False


def _safe_detail(value: object) -> dict:
    if not isinstance(value, Mapping):
        return {}
    return safe_public({
        key: item for key, item in value.items()
        if isinstance(key, str) and len(key) <= 80 and type(item) in (str, int, bool)
    })


def _safe_issues(issues: Iterable[object]) -> list[dict]:
    return [detail for issue in islice(issues, _ISSUE_LIMIT) if (detail := _safe_detail(issue))]


def _duration_ms(started_at: str, finished_at: str) -> int:
    started = datetime.fromisoformat(started_at)
    finished = datetime.fromisoformat(finished_at)
    return max(0, round((finished - started).total_seconds() * 1000))


def task_exception_detail(error: Exception, *, phase: str) -> TaskFacts:
    """Keep a useful failure explanation without persisting a traceback."""

    return {
        "phase": phase,
        "error_type": type(error).__name__,
        "message": safe_exception_message(error),
    }


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
            cleaned_rows = []
            for row in rows[:HISTORY_LIMIT]:
                if not isinstance(row, dict):
                    continue
                started_at = row.get("started_at")
                finished_at = row.get("finished_at")
                if row.get("outcome") not in _OUTCOMES or not _saved_stamp(started_at) or not _saved_stamp(finished_at):
                    continue
                cleaned = {
                    "started_at": started_at,
                    "finished_at": finished_at,
                    "duration_ms": _duration_ms(started_at, finished_at),
                    "outcome": row["outcome"],
                    **{key: row[key] for key in _COUNTS if type(row.get(key)) is int and row[key] >= 0},
                }
                detail = _safe_detail(row.get("detail"))
                if detail:
                    cleaned["detail"] = detail
                issues = row.get("issues")
                if isinstance(issues, list):
                    saved_issues = _safe_issues(issues)
                    if saved_issues:
                        cleaned["issues"] = saved_issues
                cleaned_rows.append(cleaned)
            result[task_id] = cleaned_rows
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
        detail: TaskFacts | None = None, issues: Iterable[TaskFacts] = (),
    ) -> None:
        finished = _stamp(time.time())
        saved_detail = _safe_detail(detail)
        saved_issues = _safe_issues(issues)
        # Serialize durable writes, but never hold the view lock during disk I/O.
        with self._writer:
            with self._lock:
                started = self._running.pop(task_id, None)
                if started is None:
                    return
                row = {
                    "started_at": started, "finished_at": finished,
                    "duration_ms": _duration_ms(started, finished), "outcome": outcome,
                }
                for key, count in zip(_COUNTS, (succeeded, failed, skipped)):
                    if count is not None:
                        row[key] = count
                if saved_detail:
                    row["detail"] = saved_detail
                if saved_issues:
                    row["issues"] = saved_issues
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

    def finish_batch(self, task_id: str, outcomes: Iterable[TaskResult]) -> None:
        """Summarize account results; incomplete work counts as a failure."""
        succeeded = failed = skipped = 0
        partial = False
        issues: dict[tuple[tuple[str, str | int | bool], ...], tuple[TaskFacts, int]] = {}
        for result in outcomes:
            if result.outcome == "succeeded":
                succeeded += 1
            elif result.outcome == "skipped":
                skipped += 1
            else:
                failed += 1
                partial |= result.outcome == "partial"
                if result.detail:
                    identity = tuple(sorted(result.detail.items()))
                    if identity in issues:
                        detail, count = issues[identity]
                        issues[identity] = (detail, count + 1)
                    elif len(issues) < _ISSUE_LIMIT:
                        issues[identity] = (result.detail, 1)
        if partial or (succeeded and (failed or skipped)):
            result = "partial"
        elif failed:
            result = "failed"
        else:
            result = "succeeded" if succeeded else "skipped"
        self.finish(task_id, result, succeeded=succeeded, failed=failed, skipped=skipped,
                    issues=({**detail, "accounts": count} for detail, count in issues.values()))

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
