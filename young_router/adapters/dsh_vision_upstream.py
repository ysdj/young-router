"""Drive the staged ``dsh-vision-router`` package through the bundled Node.

The vision fallback runs inside the Python LiteLLM process and upstream is a
Node module, so the provider chain is computed by a worker process that imports
the staged release.  This module owns that worker's lifetime: it starts one on
first use, sends it the router's configuration document, and hands back the
chain upstream itself produced.

Two properties matter more than the call itself:

* The worker is authoritative.  Its answer replaces the hardcoded chain in
  :mod:`dsh_vision_router` whenever it is reachable, so the shipped routing
  follows the staged upstream release.
* A failure is never fatal.  A missing bundle, a broken Node runtime, or a
  worker that cannot import the package all resolve to ``None``, and the caller
  keeps using the reviewed fallback chain.  The vision path is entered only
  after a model has already rejected the image, so a degraded bridge must cost
  an answer, not the request.

The worker is cached per configuration document: the chain only changes when the
operator edits the vision settings, and a provider chain is walked in order
across several images in one turn.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import threading
from typing import Any

from ..node_runtime import node_command as _node_command


_WORKER_ENV = "YOUNG_ROUTER_DSH_VISION_ROUTER_WORKER"
_NODE_ENV = "YOUNG_ROUTER_DSH_VISION_ROUTER_NODE"
_PACKAGE_DIR_NAME = "dsh-vision-router"
_WORKER_FILE_NAME = "dsh_vision_worker.mjs"
#: The worker answers one line per request; a hung import must not hold a
#: request open, and the first import loads sharp and the whole peer closure.
_CALL_TIMEOUT_SECONDS = 20.0
_STARTUP_TIMEOUT_SECONDS = 20.0


class VisionWorkerUnavailable(RuntimeError):
    """The staged upstream package could not be asked for its chain."""


def worker_path() -> Path:
    configured = os.environ.get(_WORKER_ENV, "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(__file__).with_name(_WORKER_FILE_NAME)


def package_root() -> Path:
    return Path(__file__).with_name(_PACKAGE_DIR_NAME)


def available() -> bool:
    """True when both the worker and the staged package are present."""

    return worker_path().is_file() and (package_root() / "lib" / "core-primitives.js").is_file()


def node_command() -> str:
    return _node_command(
        env_name=_NODE_ENV,
        label="dsh-vision-router",
        unavailable=VisionWorkerUnavailable,
    )


class _Worker:
    """One long-lived worker process, with its answers cached per config."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._cache: dict[str, list[dict[str, Any]] | None] = {}

    def _ensure_process(self) -> subprocess.Popen[str]:
        process = self._process
        if process is not None and process.poll() is None:
            return process
        if not available():
            raise VisionWorkerUnavailable(
                f"the staged {_PACKAGE_DIR_NAME} package is missing; "
                "run scripts/update_dsh_vision_router.py before building"
            )
        try:
            process = subprocess.Popen(
                [node_command(), str(worker_path())],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                bufsize=1,
            )
        except (OSError, VisionWorkerUnavailable) as exc:
            raise VisionWorkerUnavailable(f"could not start the vision worker: {exc}") from exc
        self._process = process
        return process

    def _stop_process(self) -> None:
        process, self._process = self._process, None
        if process is None:
            return
        for stream in (process.stdin, process.stdout):
            try:
                if stream is not None:
                    stream.close()
            except OSError:
                pass
        if process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()

    def chain(self, config: dict[str, Any]) -> list[dict[str, Any]] | None:
        """The provider chain upstream computed, or None when it cannot be asked.

        None is a degrade, never an empty chain: an empty list is a legitimate
        answer (``backend: "off"``) and must not be confused with a failure that
        would otherwise silently drop every vision provider.
        """

        key = json.dumps(config, sort_keys=True, default=str)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
            try:
                providers = self._ask(config)
            except VisionWorkerUnavailable:
                self._cache[key] = None
                return None
            except (OSError, ValueError, json.JSONDecodeError):
                # A worker that died mid-conversation is restarted on the next
                # miss; drop the process so this one is never reused.
                self._stop_process()
                self._cache[key] = None
                return None
            self._cache[key] = providers
            return providers

    def _ask(self, config: dict[str, Any]) -> list[dict[str, Any]] | None:
        process = self._ensure_process()
        assert process.stdin is not None and process.stdout is not None
        try:
            process.stdin.write(json.dumps({"config": config}) + "\n")
            process.stdin.flush()
        except (BrokenPipeError, ValueError, OSError) as exc:
            self._stop_process()
            raise VisionWorkerUnavailable(f"the vision worker stopped reading: {exc}") from exc
        line = _read_line(process)
        if line is None:
            self._stop_process()
            raise VisionWorkerUnavailable("the vision worker closed before answering")
        try:
            payload = json.loads(line)
        except json.JSONDecodeError as exc:
            raise VisionWorkerUnavailable(f"the vision worker answered invalid JSON: {exc}") from exc
        if not isinstance(payload, dict):
            raise VisionWorkerUnavailable("the vision worker answered a non-object")
        if payload.get("source") == "unavailable":
            raise VisionWorkerUnavailable(str(payload.get("error") or "the vision worker could not load the staged package"))
        providers = payload.get("providers")
        if not isinstance(providers, list):
            raise VisionWorkerUnavailable("the vision worker answered without a provider list")
        return [row for row in providers if isinstance(row, dict)]

    def invalidate(self) -> None:
        with self._lock:
            self._cache.clear()

    def shutdown(self) -> None:
        with self._lock:
            self._stop_process()


def _read_line(process: subprocess.Popen[str]) -> str | None:
    import selectors

    selector = selectors.DefaultSelector()
    assert process.stdout is not None
    selector.register(process.stdout, selectors.EVENT_READ)
    buffer = ""
    try:
        while "\n" not in buffer:
            if not selector.select(_CALL_TIMEOUT_SECONDS):
                return None
            chunk = process.stdout.readline()
            if chunk == "":
                return None
            buffer += chunk
    finally:
        selector.close()
    return buffer.split("\n", 1)[0]


_WORKER = _Worker()


def provider_chain(config: dict[str, Any]) -> list[dict[str, Any]] | None:
    """Ask the staged upstream package for its chain, or None to degrade."""

    return _WORKER.chain(config)


def invalidate_cache() -> None:
    _WORKER.invalidate()


def shutdown() -> None:
    _WORKER.shutdown()


__all__ = [
    "VisionWorkerUnavailable",
    "available",
    "invalidate_cache",
    "node_command",
    "package_root",
    "provider_chain",
    "shutdown",
    "worker_path",
]
