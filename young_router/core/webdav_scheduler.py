"""Run the WebDAV sync on its own interval while its switch is on.

The settings pane offers an enable switch and a number of minutes, so something
has to read them: without a loop of its own the interval would be a value
nothing acts on, and the pane's "last sync" would only ever move when a reader
pressed the button.

One long wait per interval is deliberate.  The proxy's idle-timer patches exist
because frequent wakeups keep a laptop out of its deep idle states, so this
thread computes the next due time, sleeps that long in a single ``Event`` wait,
and is woken early only by a settings change (or by its own bound, which keeps a
missed wake harmless).  A run that fails or is skipped still counts as an
attempt, so an unreachable server is retried once per interval instead of in a
tight loop; the failure itself lands in the sync status file the pane reads.
"""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Mapping

INITIAL_DELAY_SECONDS = 120.0
"""Grace period before a machine that has never synced runs its first one."""

MAX_SLEEP_SECONDS = 300.0
"""Longest single wait, so a changed setting is noticed even without a kick."""

SETTLE_SECONDS = 30.0
"""Quiet time after a Core action before an automatic sync may start.

A sync holds the store for its whole round trip.  Starting one while the reader
is typing would make the window they are looking at wait, so an automatic run
waits for a moment of quiet — the manual button never does.
"""

SETTLE_GRACE_SECONDS = 120.0
"""How long that courtesy outlasts a busy Core.

Waiting for quiet is a courtesy, and it must not become the reason nothing ever
syncs: a pane that dispatches on a poll keeps a Core busy forever, so after this
long the loop runs anyway.
"""


class WebDAVSyncScheduler:
    """Wait out the configured interval, then run the configured sync.

    ``snapshot`` reports what the saved settings say now (switch, interval, and
    the last sync stamp) and ``sync`` runs one sync.  Both are callables, so the
    Core keeps ownership of dispatch and locking while tests drive the loop with
    fakes and a fake clock.
    """

    def __init__(
        self,
        *,
        snapshot: Callable[[], Mapping[str, Any]],
        sync: Callable[[], None],
        settle_seconds: Callable[[], float] | None = None,
        clock: Callable[[], float] = time.time,
        initial_delay: float = INITIAL_DELAY_SECONDS,
        max_sleep: float = MAX_SLEEP_SECONDS,
        settle: float = SETTLE_SECONDS,
        settle_grace: float = SETTLE_GRACE_SECONDS,
    ) -> None:
        self._snapshot = snapshot
        self._sync = sync
        self._settle_seconds = settle_seconds
        self._clock = clock
        self._initial_delay = max(0.0, float(initial_delay))
        self._max_sleep = max(0.0, float(max_sleep))
        self._settle = max(0.0, float(settle))
        self._settle_grace = max(0.0, float(settle_grace))
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_attempt: float | None = None
        self._started_at: float | None = None
        self._due_since: float | None = None

    # -- lifecycle ---------------------------------------------------------
    def start(self) -> None:
        if self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="young-router-webdav-sync",
            daemon=True,
        )
        self._thread.start()

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)

    def kick(self) -> None:
        """Re-read the settings now — call this when they change."""

        self._wake.set()

    # -- scheduling --------------------------------------------------------
    def next_run(self) -> tuple[float | None, bool]:
        """Return ``(seconds_to_wait, fire_now)`` for the current settings.

        ``None`` as the delay means nothing is scheduled at all: the switch is
        off, the endpoint is unconfigured, or the interval is 0 ("only when I
        press the button").  The loop then sleeps until something changes.
        """

        state = self._read_state()
        if state is None:
            return (None, False)
        interval = self._interval_seconds(state)
        if interval is None:
            return (None, False)
        due = self._due_in(interval, state.get("last_sync_at"))
        if due > 0:
            self._due_since = None
            return (min(due, self._max_sleep), False)
        now = self._clock()
        if self._due_since is None:
            self._due_since = now
        settled = self._settled_in()
        if settled > 0 and now - self._due_since < self._settle_grace:
            return (min(settled, self._max_sleep), False)
        return (0.0, True)

    def _read_state(self) -> Mapping[str, Any] | None:
        try:
            state = self._snapshot()
        except Exception:
            # Unreadable settings are the pane's to report; this loop only has
            # to stay quiet until they are readable again.
            return None
        return state if isinstance(state, Mapping) else None

    def _interval_seconds(self, state: Mapping[str, Any]) -> float | None:
        if state.get("enabled") is not True or state.get("configured") is not True:
            return None
        minutes = state.get("sync_interval_minutes")
        if isinstance(minutes, bool) or not isinstance(minutes, (int, float)):
            return None
        if minutes <= 0:
            return None
        return float(minutes) * 60.0

    def _due_in(self, interval: float, last_sync_at: object) -> float:
        from ..webdav import core as webdav_core

        now = self._clock()
        last = webdav_core.timestamp_epoch(last_sync_at)
        if self._last_attempt is not None and (last is None or self._last_attempt > last):
            # The previous attempt left no stamp (it failed, or it was skipped
            # while the reader had unsaved changes) — or there is no state file
            # at all.  Count the attempt, or a server that never answers would
            # be asked again on every pass.
            last = self._last_attempt
        if last is None:
            # Nothing recorded yet: sync shortly after the first look, not
            # instantly, so a launch does not race the window that asked for it.
            # The grace period has to expire against this clock, or every pass
            # would ask for the same delay and never fire.
            if self._started_at is None:
                self._started_at = now
            return self._initial_delay - (now - self._started_at)
        # A stamp in the future (a clock that moved back) must not park the loop
        # for longer than one interval.
        return min(interval, max(0.0, last + interval - now))

    def _settled_in(self) -> float:
        if self._settle <= 0 or self._settle_seconds is None:
            return 0.0
        try:
            quiet = float(self._settle_seconds())
        except Exception:
            return 0.0
        return max(0.0, self._settle - quiet)

    # -- loop --------------------------------------------------------------
    def _wait(self, seconds: float | None) -> bool:
        """Wait out one delay; True when a kick asked to re-read instead."""

        if seconds is None:
            self._wake.wait()
            woken = self._wake.is_set()
            self._wake.clear()
            return woken
        if seconds <= 0:
            return False
        woken = self._wake.wait(seconds)
        if woken:
            self._wake.clear()
        return woken

    def _loop(self) -> None:
        while not self._stop.is_set():
            delay, fire = self.next_run()
            if fire:
                self._run()
                continue
            if delay is None:
                if self._wait(None) or self._stop.is_set():
                    continue
                return
            if self._wait(delay):
                continue
            # The wait elapsed: recompute, which fires when the time is up.

    def _run(self) -> None:
        # Stamp the attempt before running so the next pass measures from here
        # whatever the sync does, then let the sync's own status file carry the
        # outcome.
        self._last_attempt = self._clock()
        self._due_since = None
        try:
            self._sync()
        except Exception:
            return
