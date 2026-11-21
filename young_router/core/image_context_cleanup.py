"""Drop image-context replays once their day is up, while Core is running.

The store sweeps after every write, but a write is exactly what stops happening
when the pass is switched off or a task is abandoned -- the two states in which
yesterday's pictures would otherwise stay on disk.  Core owns the clock instead:
one sweep when the loop starts, then one long wait per interval, woken only by
Core stopping.  A long wait is deliberate; the retention rule counts in days,
and frequent wakeups keep a laptop out of its idle states.

``sweep`` takes no arguments and returns how many replays it removed, which is
what ``image_context_store.sweep`` already does.  Tests drive this class with a
fake, so the loop itself never has to touch a real cache.
"""

from __future__ import annotations

import threading
from typing import Callable

from .scheduled_tasks import IMAGE_CONTEXT_CLEANUP, ScheduledTasks, TaskFacts, task_exception_detail

INTERVAL_SECONDS = 60 * 60
"""One hour: often enough for a one-day rule, rarely enough to stay idle."""


class ImageContextSweeper:
    """Apply the store's own age rule from outside the request path."""

    def __init__(
        self, *, sweep: Callable[[], object], interval: float = INTERVAL_SECONDS,
        tasks: ScheduledTasks | None = None,
    ) -> None:
        self._sweep = sweep
        self._tasks = tasks
        self._interval = max(1.0, float(interval))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="young-router-image-context-cleanup",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def run_once(self) -> None:
        if self._tasks is not None:
            self._tasks.begin(IMAGE_CONTEXT_CLEANUP)
        outcome = "succeeded"
        detail: TaskFacts | None = None
        try:
            removed = self._sweep()
            if type(removed) is int:
                detail = {"removed": removed}
        except Exception as exc:
            # A sweep that failed is simply due again next interval; it must
            # never take Core down or stop the loop.
            outcome = "failed"
            detail = task_exception_detail(exc, phase="sweep")
        if self._tasks is not None:
            self._tasks.finish(IMAGE_CONTEXT_CLEANUP, outcome, detail=detail)

    def _loop(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            if self._tasks is not None:
                self._tasks.plan(IMAGE_CONTEXT_CLEANUP, interval_seconds=self._interval, delay_seconds=self._interval)
            if self._stop.wait(self._interval):
                break
        if self._tasks is not None:
            self._tasks.plan(IMAGE_CONTEXT_CLEANUP, interval_seconds=self._interval, delay_seconds=None)
