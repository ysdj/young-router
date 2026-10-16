"""Fast macOS launcher for the managed multi-worker LiteLLM proxy.

Uvicorn normally starts every macOS worker with ``spawn``.  Each worker then
imports the complete LiteLLM proxy independently, which dominates startup
time at the required sixteen-worker count.  A single-threaded forkserver can
import the app once and fork clean worker processes without forking the Core
or the proxy master after either has started watchdog threads.
"""

from __future__ import annotations

import argparse
import importlib
import json
import multiprocessing
import os
from pathlib import Path
from typing import Any
import urllib.request

from ..lifecycle import proxy_drain_grace_seconds


PROXY_APP = "litellm.proxy.proxy_server:app"
PROXY_MODULE = "litellm.proxy.proxy_server"
SYSTEM_PROXY_SNAPSHOT_ENV = "YOUNG_ROUTER_SYSTEM_PROXY_SNAPSHOT"


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
    parser = argparse.ArgumentParser(description="Run the managed macOS LiteLLM proxy")
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
    """Capture macOS proxy settings before workers are forked."""

    environment_proxies = urllib.request.getproxies_environment()
    if environment_proxies or os.environ.get("YOUNG_ROUTER_DISABLE_SYSTEM_PROXY_LOOKUP") == "1":
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
        # replay prefix with signed image bytes -- as one WebSocket text frame;
        # uvicorn's 16 MiB default closes those with code 1009 before the
        # request reaches the pipeline (Codex then reports "websocket closed
        # by server before response.completed" and reconnects in a loop).
        ws_max_size=_websocket_max_frame_bytes(),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
