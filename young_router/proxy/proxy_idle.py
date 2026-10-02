"""Keep the managed proxy's idle timer loops cheap.

The managed macOS proxy is one supervisor process plus sixteen workers, and
every one of them runs a uvicorn serve loop.  Each of those loops is a timer
that fires ten times a second while the router is idle, and the supervisor adds
a health-check round trip to all sixteen workers twice a second.  On a laptop
that constant timer traffic is what keeps the machine out of its deep idle
states and lands the app on the battery panel's significant-energy list.

None of this is on the request path: a request is served by the event loop's
I/O callbacks, while the idle tick only refreshes the HTTP ``Date`` header,
notifies the parent when a callback is configured, and looks for a stop
request.  These patches keep every one of those duties at its documented
cadence and remove only the redundant wakeups:

* ``Server.main_loop`` ticks once per second instead of ten times per second.
  The counter handed to ``on_tick`` stays in uvicorn's own unit (tenths of a
  second), so the ``Date`` header is still rewritten once per second and the
  counter's 24-hour modulo keeps its meaning.  A stop signal still ends the
  serve loop at once: ``Server.handle_exit`` releases the wait instead of
  leaving the worker to notice the stop on its next tick.
* ``Process.is_alive`` still asks the operating system about every worker on
  every supervisor pass, and only the inter-process health-check round trip
  waits for its own interval.  A crashed worker is therefore still replaced
  within the supervisor's own cadence, and a hung one is noticed one
  health-check later.

Both patches install repeatedly and from interpreter startup, so the
supervisor and its workers (``sitecustomize``) and the LiteLLM CLI
(``patches.install_all``) share one implementation.  A uvicorn that no longer
exposes a target is left alone instead of failing the launch.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

IDLE_TICK_SECONDS = 1.0
"""Seconds between serve-loop ticks while the proxy has nothing to do.

uvicorn refreshes the ``Date`` header once per second, so a one-second tick
keeps the header exactly as fresh as upstream does while removing nine of
every ten timer wakeups.
"""

SUPERVISOR_HEALTHCHECK_INTERVAL_SECONDS = 5.0
"""Seconds between inter-process health checks of one worker.

The supervisor's own pass cadence is unchanged; this is only how often one
worker is asked to answer through the multiprocessing pipe.
"""

_IDLE_TICK_PATCH_ATTR = "_young_router_idle_tick_patch"
_TICK_WAKE_ATTR = "_young_router_idle_tick_wake"
# uvicorn counts tenths of a second, and refreshes the ``Date`` header when the
# counter is divisible by ten.
_TICK_COUNTER_STEP = 10
_TICK_COUNTER_MODULUS = 864000


def _idle_tick_seconds() -> float:
    return IDLE_TICK_SECONDS


def _release_idle_tick_wait(server: Any) -> None:
    """Wake a serve loop that is waiting out its idle tick."""

    waiting = getattr(server, _TICK_WAKE_ATTR, None)
    if waiting is None:
        return
    loop, wake = waiting
    try:
        loop.call_soon_threadsafe(wake.set)
    except RuntimeError:  # the loop is already closed; the serve loop is done
        pass


def _install_uvicorn_idle_tick_patch() -> None:
    """Replace uvicorn's ten-hertz serve-loop tick with an idle-aware one."""

    try:
        from uvicorn.server import Server
    except Exception:  # pragma: no cover - uvicorn is optional in tests
        return

    original_main_loop = getattr(Server, "main_loop", None)
    original_handle_exit = getattr(Server, "handle_exit", None)
    if original_main_loop is None or original_handle_exit is None:
        return
    if getattr(original_main_loop, _IDLE_TICK_PATCH_ATTR, False):
        return

    async def main_loop(self: Any) -> None:
        loop = asyncio.get_running_loop()
        wake = asyncio.Event()
        setattr(self, _TICK_WAKE_ATTR, (loop, wake))
        try:
            counter = 0
            should_exit = await self.on_tick(counter)
            while not should_exit:
                # Clear before waiting so a stop that lands between ``on_tick``
                # and this line is seen by the check below instead of being
                # swallowed until the tick expires.
                wake.clear()
                if not self.should_exit:
                    try:
                        await asyncio.wait_for(wake.wait(), _idle_tick_seconds())
                    except (asyncio.TimeoutError, TimeoutError):
                        pass
                counter = (counter + _TICK_COUNTER_STEP) % _TICK_COUNTER_MODULUS
                should_exit = await self.on_tick(counter)
        finally:
            setattr(self, _TICK_WAKE_ATTR, None)

    def handle_exit(self: Any, sig: int, frame: Any) -> None:
        original_handle_exit(self, sig, frame)
        _release_idle_tick_wait(self)

    setattr(main_loop, _IDLE_TICK_PATCH_ATTR, True)
    setattr(main_loop, "_original_main_loop", original_main_loop)
    setattr(handle_exit, _IDLE_TICK_PATCH_ATTR, True)
    setattr(handle_exit, "_original_handle_exit", original_handle_exit)
    Server.main_loop = main_loop  # type: ignore[method-assign]
    Server.handle_exit = handle_exit  # type: ignore[method-assign]


def _install_uvicorn_supervisor_healthcheck_patch() -> None:
    """Keep the supervisor's worker liveness pass, but not its ping cadence."""

    try:
        from uvicorn.supervisors.multiprocess import Process
    except Exception:  # pragma: no cover - uvicorn is optional in tests
        return

    original_is_alive = getattr(Process, "is_alive", None)
    if original_is_alive is None:
        return
    if getattr(original_is_alive, _IDLE_TICK_PATCH_ATTR, False):
        return

    def is_alive(self: Any, timeout: float = 5) -> bool:
        # A worker that is gone is reported on this pass, exactly as before.
        if not self.process.is_alive():
            return False
        checked_at = getattr(self, "_young_router_healthcheck_at", None)
        now = time.monotonic()
        if checked_at is not None and now - checked_at < SUPERVISOR_HEALTHCHECK_INTERVAL_SECONDS:
            return True
        setattr(self, "_young_router_healthcheck_at", now)
        return self.ping(self.config.timeout_worker_healthcheck if timeout is None else timeout)

    setattr(is_alive, _IDLE_TICK_PATCH_ATTR, True)
    setattr(is_alive, "_original_is_alive", original_is_alive)
    Process.is_alive = is_alive  # type: ignore[method-assign]


def install_proxy_idle_patches() -> None:
    """Install every idle-loop patch.  Safe to call repeatedly."""

    _install_uvicorn_idle_tick_patch()
    _install_uvicorn_supervisor_healthcheck_patch()


__all__ = [
    "IDLE_TICK_SECONDS",
    "SUPERVISOR_HEALTHCHECK_INTERVAL_SECONDS",
    "install_proxy_idle_patches",
]
