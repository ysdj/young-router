"""Bring the managed proxy back without a person pressing 重启服务.

A proxy can be *owned and alive* while nothing answers on its port: the master
outlives the workers that served it, or a replacement died between its launch
and its first healthy answer.  The service row projected that as 不健康 and
offered one recovery control, so the only way back was a person noticing the
row and clicking it.  Nothing about that state is a decision the user made, so
nothing about it may require one.

The loop lives in Core, not in a window: a background launch has no window, and
the app's own launch retry only runs while the primary host happens to be
drawing the row.  One pass is ``CoreStore.supervise_service_once``, which owns
the intent, the patience, and the retry ladder; this class only makes sure that
pass keeps happening while Core is alive.
"""

from __future__ import annotations

import threading
from typing import Any, Callable

from .oplog import note as _note

INITIAL_DELAY_SECONDS = 5.0
"""Let the app's own launch start run first, so two starts do not race."""

CHECK_INTERVAL_SECONDS = 15.0
"""One pass per interval.  A healthy pass is one memoized liveness answer; the
value matches the window's own settled-state poll, and a window that is not
open still gets the recovery."""


class ServiceSupervisor:
    """Run one supervision pass per interval until Core stops.

    ``supervise`` performs the pass and returns whether it touched the proxy.
    Tests drive it with a fake, so the loop itself never touches a process.
    """

    def __init__(
        self,
        *,
        supervise: Callable[[], Any],
        interval: float = CHECK_INTERVAL_SECONDS,
        initial_delay: float = INITIAL_DELAY_SECONDS,
    ) -> None:
        self._supervise = supervise
        self._interval = max(1.0, float(interval))
        self._initial_delay = max(0.0, float(initial_delay))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="young-router-service-supervisor",
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
        try:
            self._supervise()
        except Exception as exc:
            # A pass that failed is simply due again next interval; it must
            # never take Core down or stop the loop.  It must still be said:
            # this loop exists for the installation nobody is looking at, and
            # a pass that keeps failing is invisible everywhere else.
            _note("a recovery pass failed", trace=exc)

    def _loop(self) -> None:
        if self._stop.wait(self._initial_delay):
            return
        while not self._stop.is_set():
            self.run_once()
            if self._stop.wait(self._interval):
                return
