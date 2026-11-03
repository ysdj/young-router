"""Keep the generated client model lists current with no window open.

The Codex catalog and Claude Desktop's picker are both generated from the
router's public model names.

Three things can make those names change, and each has its own trigger:

* **An edit in this app.**  Saving a provider rewrites the router's document and
  reloads the proxy, and every reload regenerates the lists.  That path is
  immediate and carries nothing extra -- the reload is already happening.
* **An edit nobody told this process about.**  A document restored from a
  backup, a model toggled by another tool, a file replaced while the app was
  closed.  This loop covers it by watching the document's own timestamp, so the
  lists follow within a second rather than on a clock.
* **A window that is open anyway** re-derives the list on its snapshot, which
  costs nothing extra.

The refresh is idempotent -- both generators compare the document on disk with
what the router serves and write only a difference -- so watching rather than
polling is safe: a pass that finds nothing new writes nothing.

Why a settle window: the router serves ``/v1/models`` from its *running* process,
and LiteLLM reloads its workers on its own schedule after the document changes.
A single read taken the instant the file moved would still answer the old list
and, finding no difference, never look again.  A detected change is therefore
followed by a short ladder of retries -- each one cheap, each one able to be the
pass that lands.
"""

from __future__ import annotations

import os
import pathlib
import threading
import time
from typing import Any, Callable, Iterable


#: How often the watched document is stat'ed.  This is a metadata read, not a
#: request, so it can be frequent without touching the proxy.
POLL_SECONDS = 1.0
#: Retry points after a change, in seconds.  The router reloads its workers on
#: its own clock; these are the passes that catch it.
SETTLE_SECONDS = (0.0, 2.0, 6.0, 15.0, 40.0)
#: A pass taken anyway, this often, so an installation whose document never
#: moves still reconciles a list an older release left behind.
FALLBACK_SECONDS = 300.0


def _signature(path: pathlib.Path) -> tuple[int, int] | None:
    """The watched document's identity, or ``None`` when it is not there."""

    try:
        stat = path.stat()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size)


class ClientModelSyncScheduler:
    """A daemon thread that re-derives the client model lists when they can change."""

    def __init__(
        self,
        *,
        refresh: Callable[[], None],
        watch_paths: Iterable[pathlib.Path | str] = (),
        poll_seconds: float = POLL_SECONDS,
        settle_seconds: Iterable[float] = SETTLE_SECONDS,
        fallback_seconds: float = FALLBACK_SECONDS,
    ):
        self._refresh = refresh
        self._paths = tuple(pathlib.Path(path).expanduser() for path in watch_paths)
        self._poll = max(0.05, float(poll_seconds))
        self._settle = tuple(float(value) for value in settle_seconds)
        self._fallback = max(0.0, float(fallback_seconds))
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._last_signature: tuple[tuple[Any, ...], ...] | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run,
            name="young-router-client-model-sync",
            daemon=True,
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _observe(self) -> tuple[tuple[Any, ...], ...]:
        return tuple(_signature(path) for path in self._paths)

    def _run(self) -> None:
        # One pass at start: an installation whose document never moves still
        # reconciles a list an older release left behind.
        self._refresh_quietly()
        self._last_signature = self._observe()
        next_fallback = time.monotonic() + self._fallback
        pending: list[float] = []
        while not self._stop.wait(self._poll):
            if self._paths:
                observed = self._observe()
                if observed != self._last_signature:
                    self._last_signature = observed
                    now = time.monotonic()
                    pending = [now + offset for offset in self._settle]
            now = time.monotonic()
            due = [at for at in pending if at <= now]
            if due:
                pending = [at for at in pending if at > now]
                self._refresh_quietly()
            elif self._fallback and now >= next_fallback:
                next_fallback = now + self._fallback
                self._refresh_quietly()

    def _refresh_quietly(self) -> None:
        try:
            self._refresh()
        except Exception:
            # A refresh is bookkeeping about a client's file.  Whatever failed --
            # a proxy still starting, a document another process holds -- the
            # next pass tries again, and this process's health does not depend
            # on it.
            return


def attach(core: Any, watch_paths: Iterable[Any] = ()) -> ClientModelSyncScheduler:
    """Wire the loop to one Core store and the documents its routes live in."""

    adopt = getattr(core, "adopt_external_route_change", None)

    def refresh() -> None:
        # A change this process did not make is also invisible to the running
        # proxy: reloading is what makes ``/v1/models`` answer the new document,
        # and that reload regenerates the client lists by itself.  A change made
        # in this app already reloaded, so asking again is a no-op that coalesces
        # into the reload already queued.
        if callable(adopt):
            adopt()
        core.refresh_client_model_lists()

    return ClientModelSyncScheduler(refresh=refresh, watch_paths=watch_paths)


__all__ = [
    "FALLBACK_SECONDS",
    "POLL_SECONDS",
    "SETTLE_SECONDS",
    "ClientModelSyncScheduler",
    "attach",
]
