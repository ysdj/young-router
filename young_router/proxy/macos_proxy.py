"""The POSIX launcher for the managed multi-worker proxy.

Uvicorn normally starts every worker with ``spawn``.  Each worker then imports
the complete LiteLLM proxy independently, which dominates startup time at the
required sixteen-worker count.  A single-threaded forkserver can import the app
once and fork clean worker processes without forking the Core or the proxy
master after either has started watchdog threads.

POSIX, not macOS: ``multiprocessing.forkserver`` is what makes the preload
possible and every POSIX host has it.  macOS is where the app started, and the
name is kept because the build and process scripts name this module; the
platform boundary that matters is Windows, which has neither the forkserver nor
SIGHUP and takes LiteLLM's own CLI path instead (see
``CoreServiceController.start``).

What the workers import is ``young_router.proxy.gateway``, not bare LiteLLM: the
page and its API on ``/ui`` and ``/ipc``, the provider-side search route, the
image-context endpoint and the account-bound workers are all additions to that
app object.  A launch that skipped it would leave a proxy answering ``/v1``
correctly while everything this app adds to the same port returned 404.
"""

from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import multiprocessing.forkserver  # noqa: F401 - the guard reads its server state
import os
from pathlib import Path
import signal
import sys
import threading
import time
from typing import Any
import urllib.request

from ..lifecycle import proxy_drain_grace_seconds


# The gateway module re-exports LiteLLM's own app after adding the provider-side
# search route Codex calls, so the launcher still warms exactly one app.
PROXY_APP = "young_router.proxy.gateway:app"
PROXY_MODULE = "young_router.proxy.gateway"
SYSTEM_PROXY_SNAPSHOT_ENV = "YOUNG_ROUTER_SYSTEM_PROXY_SNAPSHOT"
# A worker handover is retried when the forkserver it was being handed to is
# gone rather than busy.  The retry costs seconds once the runtime is imported,
# and the alternative is a start that dies on a race it can see coming.
WORKER_SPAWN_ATTEMPTS = 3
WORKER_SPAWN_RETRY_SECONDS = 0.5
# CPython's own words for that condition: it raises the first when the server
# vanished between the connect and the acknowledgement, and the second when it
# was already reaped by the next attempt's ``ensure_running``.
_FORKSERVER_GONE_ERRORS = (
    "did not receive acknowledgement of fd",
    "forkserver process has died",
)
_STOP_SIGNALS = ("SIGTERM", "SIGINT")


def _bounded_port(value: str) -> int:
    try:
        port = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("port must be an integer") from exc
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError("port must be between 1 and 65535")
    return port


def _positive_workers(value: str) -> int:
    try:
        workers = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("workers must be a positive integer") from exc
    if workers <= 0:
        raise argparse.ArgumentTypeError("workers must be a positive integer")
    return workers


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the managed POSIX LiteLLM proxy")
    parser.add_argument("--config", required=True, type=Path)
    parser.add_argument("--host", choices=("127.0.0.1",), default="127.0.0.1")
    parser.add_argument("--port", required=True, type=_bounded_port)
    parser.add_argument("--workers", required=True, type=_positive_workers)
    return parser


def _worker_config(config: Path) -> dict[str, Any]:
    """Mirror the existing config-only LiteLLM CLI invocation for workers."""

    return {
        "config": str(config),
        "telemetry": False,
        "request_timeout": None,
        "drop_params": False,
        "add_function_to_prompt": False,
        "headers": None,
        "save": False,
        "use_queue": False,
    }


def _system_proxy_snapshot() -> dict[str, Any]:
    """Capture the system proxy settings before workers are forked.

    Only macOS has a SystemConfiguration database to read; every other POSIX
    host states its proxies in the environment, which is the same answer this
    returns when the lookup is switched off.  Calling the macOS reader on Linux
    raises rather than answering empty, so the platform is checked here instead
    of letting the launcher die on a name that only exists in one build.
    """

    environment_proxies = urllib.request.getproxies_environment()
    if (
        environment_proxies
        or sys.platform != "darwin"
        or os.environ.get("YOUNG_ROUTER_DISABLE_SYSTEM_PROXY_LOOKUP") == "1"
    ):
        return {
            "source": "environment",
            "proxies": dict(environment_proxies),
        }
    return {
        "source": "macos",
        "proxies": dict(urllib.request.getproxies_macosx_sysconf()),
        "settings": dict(urllib.request._get_proxy_settings()),
    }


def _prime_forkserver() -> None:
    """Start the forkserver so its preload imports while this process imports.

    ``ensure_running`` returns as soon as the process exists; the preload is
    read from the connection the first worker spawn opens, so a spawn later in
    this same process reuses the forkserver started here.
    """

    multiprocessing.forkserver._forkserver.ensure_running()


class ProxyStartCancelled(RuntimeError):
    """The proxy was asked to stop before any worker existed."""


def _handover_error_names_a_gone_forkserver(exc: BaseException) -> bool:
    """Whether ``exc`` means the forkserver is gone, not that it refused."""

    text = str(exc)
    if any(marker in text for marker in _FORKSERVER_GONE_ERRORS):
        return True
    return isinstance(exc, (ConnectionRefusedError, FileNotFoundError, ProcessLookupError))


def _forkserver_exit_clause(pid: int | None) -> str:
    """How the preloading forkserver ended, as a clause, without reaping it.

    ``waitid`` with ``WNOWAIT`` is the one way to learn a child's exit status
    while ``multiprocessing`` still owns the reaping, so the launcher can name
    what happened instead of reporting a missing acknowledgement of a file
    descriptor.
    """

    if pid is None:
        return "the proxy's forkserver went away"
    try:
        info = os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT)
    except (AttributeError, ChildProcessError, OSError):
        info = None
    if info is None:
        return f"the proxy's forkserver ({pid}) went away"
    if info.si_code == os.CLD_EXITED:
        return f"the proxy's forkserver ({pid}) exited with status {info.si_status}"
    if info.si_code in (os.CLD_KILLED, os.CLD_DUMPED):
        try:
            name = signal.Signals(info.si_status).name
        except ValueError:
            name = f"signal {info.si_status}"
        return f"the proxy's forkserver ({pid}) was killed by {name}"
    return f"the proxy's forkserver ({pid}) stopped answering"


class _WorkerHandoverGuard:
    """Hand the workers to the forkserver, and survive a forkserver that dies.

    The launcher hands every worker to one forkserver, and that forkserver
    preloads the whole LiteLLM runtime before it forks: the preload *is* the
    launcher's startup budget.  It is also the one window in which a
    process-group ``SIGTERM`` -- a replacement Core stopping the proxy it
    recorded -- kills the forkserver silently while uvicorn's own handler only
    queues that signal for this process.  This launcher then died inside
    ``connect_to_new_process`` with CPython's "did not receive acknowledgement
    of fd", which names neither the process nor the cause, so the app retried a
    start that had already been torn down.

    Two duties, both owned here: record that this process was asked to stop, so
    a planned teardown is not reported as a startup failure, and retry a
    handover whose forkserver is gone -- ``ensure_running`` starts a fresh one
    and the import cache is warm, so the retry is cheaper than a dead start.
    """

    def __init__(self, attempts: int = WORKER_SPAWN_ATTEMPTS) -> None:
        self._attempts = max(1, int(attempts))
        self._stop_requested = False
        self._recorders_installed = False
        self._lock = threading.Lock()

    @property
    def stop_requested(self) -> bool:
        return self._stop_requested

    def install(self) -> None:
        """Wrap the handover the supervisor performs for every worker.

        ``multiprocessing.forkserver`` binds that method onto the module and
        ``popen_forkserver`` calls the module-level name, so the wrapper has to
        replace the module's binding; a class attribute is never reached.
        """

        module = multiprocessing.forkserver
        original = getattr(module, "connect_to_new_process", None)
        if original is None or getattr(original, "_young_router_handover_guard", False):
            return
        guard = self

        def connect_to_new_process(fds: Any) -> Any:
            guard._record_stop_signals()
            cause: BaseException | None = None
            for attempt in range(1, guard._attempts + 1):
                try:
                    return original(fds)
                except (RuntimeError, OSError) as exc:
                    if not _handover_error_names_a_gone_forkserver(exc):
                        raise
                    cause = exc
                    if guard._stop_requested:
                        raise ProxyStartCancelled(
                            f"{_forkserver_exit_clause(_forkserver_pid())} while this proxy was being stopped"
                        ) from exc
                    if attempt >= guard._attempts:
                        break
                    time.sleep(WORKER_SPAWN_RETRY_SECONDS)
            raise RuntimeError(
                f"{_forkserver_exit_clause(_forkserver_pid())} before it forked a worker"
                f" ({guard._attempts} handover attempts, last error: {cause})"
            ) from cause

        connect_to_new_process._young_router_handover_guard = True  # type: ignore[attr-defined]
        module.connect_to_new_process = connect_to_new_process  # type: ignore[assignment]

    def _record_stop_signals(self) -> None:
        """Note a stop signal, then let uvicorn's own handler keep its answer.

        uvicorn installs its handlers while its supervisor is constructed, which
        is after this process could have installed any of its own: recording
        here, chained onto whatever it installed, is what keeps a planned stop
        distinguishable from a forkserver that died on its own.
        """

        if self._recorders_installed:
            return
        with self._lock:
            if self._recorders_installed:
                return
            self._recorders_installed = True
        for name in _STOP_SIGNALS:
            signum = getattr(signal, name, None)
            if signum is None:
                continue
            try:
                previous = signal.getsignal(signum)
                if not callable(previous):
                    # The default disposition already answers a stop; a recorder
                    # that replaced it would keep this process alive instead.
                    continue

                def record(sig: int, frame: Any, previous: Any = previous) -> None:
                    self._stop_requested = True
                    previous(sig, frame)

                signal.signal(signum, record)
            except (ValueError, OSError, RuntimeError):
                # Not the main thread, or a platform without the signal: the
                # retry is then unconditional, which is still better than a
                # start that cannot name its own failure.
                continue


def _forkserver_pid() -> int | None:
    pid = getattr(multiprocessing.forkserver._forkserver, "_forkserver_pid", None)
    return pid if isinstance(pid, int) and pid > 0 else None


def _install_worker_handover_guard() -> _WorkerHandoverGuard:
    guard = _WorkerHandoverGuard()
    guard.install()
    return guard


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    os.environ["WORKER_CONFIG"] = json.dumps(
        _worker_config(args.config),
        separators=(",", ":"),
    )
    os.environ[SYSTEM_PROXY_SNAPSHOT_ENV] = json.dumps(
        _system_proxy_snapshot(),
        separators=(",", ":"),
    )

    multiprocessing.set_forkserver_preload([PROXY_MODULE])
    uvicorn_subprocess = importlib.import_module("uvicorn._subprocess")
    uvicorn_subprocess.spawn = multiprocessing.get_context("forkserver")
    # Start the forkserver now: its preload is the complete LiteLLM runtime,
    # and it is the launcher's whole startup budget.  Everything this process
    # imports below is deliberately kept out of that runtime (see
    # ``launcher_settings``), so the preload runs while uvicorn is prepared and
    # nothing has been forked or served yet when this line returns.
    _prime_forkserver()
    uvicorn = importlib.import_module("uvicorn")
    from .launcher_settings import _websocket_max_frame_bytes
    from .state import settle_stale_recent_requests

    # Every new proxy process starts with an empty in-flight registry, so any
    # request row an earlier process left unfinished can never receive its
    # terminal callback.  Close those rows before the workers start serving.
    settle_stale_recent_requests()
    # The same reading applies to the concurrency leases: every lease in the
    # shared file belongs to a worker this start replaced.  Left alone, each one
    # would hold a slot on its route until its lease aged out, and the routes
    # would serve less than their cap for hours.
    from .concurrency import reset_leases

    reset_leases()

    # The one handover that can lose the whole start: see the guard.
    _install_worker_handover_guard()
    try:
        uvicorn.run(
            PROXY_APP,
            host=args.host,
            port=args.port,
            workers=args.workers,
            loop="uvloop",
            # Uvicorn otherwise cancels a still-streaming request after its 30 s
            # default, even though the managed service gives the old worker the
            # configured drain window.  Keep the client connection alive for that
            # same window so an app restart does not synthesize router_restarting.
            # The drain belongs to a replacement the Core performs on purpose: a
            # restart or an install never waits here, it kills this process with the
            # rest of the captured tree (``scripts/app-processes.sh stop``).
            timeout_graceful_shutdown=proxy_drain_grace_seconds(),
            # Codex sends each Responses turn request -- including the immutable
            # replay prefix with signed image bytes -- as one WebSocket text
            # frame; uvicorn's 16 MiB default closes those with code 1009 before
            # the request reaches the pipeline (Codex then reports "websocket
            # closed by server before response.completed" and reconnects in a
            # loop).
            ws_max_size=_websocket_max_frame_bytes(),
        )
    except ProxyStartCancelled as exc:
        # A stop that lands during the preload is the answer, not a failure: the
        # Core that owned this proxy replaced itself and starts its own, so the
        # port is released here instead of after a traceback.
        print(f"proxy start cancelled: {exc}", file=sys.stderr, flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
