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


PROXY_APP = "litellm.proxy.proxy_server:app"
PROXY_MODULE = "litellm.proxy.proxy_server"
SYSTEM_PROXY_SNAPSHOT_ENV = "LITELLM_MENU_SYSTEM_PROXY_SNAPSHOT"


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
    if environment_proxies or os.environ.get("LITELLM_MENU_DISABLE_SYSTEM_PROXY_LOOKUP") == "1":
        return {
            "source": "environment",
            "proxies": dict(environment_proxies),
        }
    return {
        "source": "macos",
        "proxies": dict(urllib.request.getproxies_macosx_sysconf()),
        "settings": dict(urllib.request._get_proxy_settings()),
    }


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
    uvicorn = importlib.import_module("uvicorn")
    uvicorn.run(
        PROXY_APP,
        host=args.host,
        port=args.port,
        workers=args.workers,
        loop="uvloop",
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
