"""Publish a burst of state revisions as one event.

A commit used to project the whole state and fan it out inside its own write
lane, so the response of every edit was timed by a cold projection plus one
copy per open window.  The same edit is usually followed by more of them (a
debounced Apply, the pane's own follow-up, a background reconcile), and every
one of those produced a full snapshot nobody had time to read.

The split is: a write path marks an event pending and answers with its
revision; one thread of its own waits out a short window, then projects and
fans out once.  Three revisions in that window are one event carrying the last
one, which is all a window can draw anyway.

The window is a frame at 60 Hz.  It has to be long enough that a dispatch and
the bookkeeping that lands right behind it share one event, and short enough
that a window does not read the wait as lag; ``MAX_WINDOW_SECONDS`` is the
ceiling on whatever a caller asks for.
"""

from __future__ import annotations

import threading
import time
from typing import Callable

DEFAULT_WINDOW_SECONDS = 0.016
"""How long a burst gathers before it is published: one frame at 60 Hz."""

MAX_WINDOW_SECONDS = 0.05
"""The slowest a caller may make publishing, whatever window it asks for."""

IDLE_SECONDS = 1.0
"""How long the thread waits for another request before it ends itself.

One Core publishes on every edit for as long as it runs, but a test process
builds hundreds of stores that publish once and never again.  Ending the idle
thread gives those their thread back, and the next request starts a new one.
"""

_JOIN_SECONDS = 5.0
"""How long ``stop`` waits for a send already in flight."""


class CoalescingEmitter:
    """Send at most one event per window, from one thread of its own.

    ``send`` is called with no arguments and does the projection and fan-out
    itself.  It runs on this class's thread (or on the caller's, from
    ``flush``), never inside the write lane of the mutation that asked for it.

    ``request`` is what a mutation calls.  It is cheap and non-blocking by
    design: everything expensive happens on the thread it wakes.
    """

    def __init__(
        self,
        send: Callable[[], None],
        *,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
    ) -> None:
        if not callable(send):
            raise TypeError("send must be callable")
        window = float(window_seconds)
        if window < 0.0:
            raise ValueError("window_seconds must not be negative")
        self._send = send
        self._window = min(window, MAX_WINDOW_SECONDS)
        self._lock = threading.Lock()
        # One sender at a time: the thread and a ``flush`` caller may both see
        # the same pending request and must not publish it twice.
        self._sending = threading.Lock()
        # Set while a request is waiting to be published; also the thread's
        # wake-up.
        self._wake = threading.Event()
        self._requests = 0
        self._covered = 0
        self._stopping = False
        self._thread: threading.Thread | None = None

    # -- the write path ---------------------------------------------------
    def request(self) -> None:
        """Mark one event pending and make sure the window is running."""

        with self._lock:
            self._requests += 1
            self._wake.set()
            if self._thread is None and not self._stopping:
                thread = threading.Thread(
                    target=self._run,
                    name="young-router-core-emitter",
                    daemon=True,
                )
                self._thread = thread
                thread.start()

    def claim(self) -> None:
        """Record that a send outside this thread answered every pending request.

        ``CoreStore._emit`` is still callable directly: a test drives it, and a
        transitional claim asks for it because the event has to be visible
        before the statement that follows.  Such a send publishes now, so the
        request it answers must not also produce a second event a window later.
        The emitter's own send already claimed its batch, and must not claim
        again: a request that arrives during it is the next event.
        """

        if threading.current_thread() is self._thread:
            return
        with self._lock:
            self._covered = self._requests
            self._wake.clear()

    # -- tests and shutdown ----------------------------------------------
    def flush(self) -> None:
        """Publish a pending request now, on the calling thread."""

        if threading.current_thread() is self._thread:
            # Called from inside this thread's own send: that send already
            # claimed its batch, and taking the sender again would deadlock.
            return
        with self._sending:
            self._deliver()

    def stop(self) -> None:
        """End the thread for good, publishing whatever is still pending."""

        with self._lock:
            self._stopping = True
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=_JOIN_SECONDS)
        # A quit that lands right after a commit still owes that commit's
        # event; nothing else will send it once this object is done.
        self.flush()

    # -- the window -------------------------------------------------------
    def _run(self) -> None:
        while True:
            if not self._wake.wait(timeout=IDLE_SECONDS):
                with self._lock:
                    if self._stopping or self._requests > self._covered:
                        continue
                    self._thread = None
                    return
            with self._lock:
                if self._stopping:
                    return
            # Let the rest of the burst land before publishing it once.
            time.sleep(self._window)
            with self._sending:
                with self._lock:
                    if self._stopping:
                        return
                self._deliver()

    def _deliver(self) -> None:
        with self._lock:
            self._wake.clear()
            if self._requests <= self._covered:
                return
            pending = self._requests
            # Claimed before the send: a request that arrives while it runs is
            # a new event rather than one silently folded into this one.
            self._covered = pending
        try:
            self._send()
        except Exception:
            # A publisher that raised must not end the thread; ``CoreStore``
            # already contains a subscriber that raised.
            pass
