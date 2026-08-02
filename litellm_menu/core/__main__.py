"""Run the Core/IPC process used by the native desktop hosts.

The launcher writes a private endpoint descriptor for the host and then keeps
the in-process Core alive until SIGTERM/SIGINT.  The descriptor contains the
one-shot bootstrap credential only when it is explicitly requested by the
launcher; it is always written with mode ``0600`` and never logged.
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import signal
import threading
from typing import Any

from .ipc import CoreIPCServer
from .persistence import AtomicJSONStore, PersistenceError
from .service import CoreStore


def _positive_pid(value: str) -> int:
    try:
        pid = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("parent PID must be a positive integer") from exc
    if pid <= 0:
        raise argparse.ArgumentTypeError("parent PID must be a positive integer")
    return pid


def _watch_parent(parent_pid: int, stop: threading.Event, *, poll_interval: float = 0.25) -> None:
    """Stop Core when its original native host is no longer its parent."""

    if os.name == "nt":
        _watch_windows_parent(parent_pid, stop, poll_interval=poll_interval)
        return

    while not stop.is_set():
        # Foundation launches Core directly. Checking the parent relationship
        # also avoids treating an unrelated process that reused the PID as the
        # original host.
        if os.getppid() != parent_pid:
            stop.set()
            return
        stop.wait(poll_interval)


def _watch_windows_parent(parent_pid: int, stop: threading.Event, *, poll_interval: float) -> None:
    """Hold the original Windows process object so PID reuse cannot fool us."""

    import ctypes
    from ctypes import wintypes

    synchronize = 0x00100000
    wait_timeout = 258
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.WaitForSingleObject.argtypes = (wintypes.HANDLE, wintypes.DWORD)
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
    kernel32.CloseHandle.restype = wintypes.BOOL
    handle = kernel32.OpenProcess(synchronize, False, parent_pid)
    if not handle:
        stop.set()
        return
    try:
        wait_ms = max(1, int(poll_interval * 1000))
        while not stop.is_set():
            result = kernel32.WaitForSingleObject(handle, wait_ms)
            if result == wait_timeout:
                continue
            # WAIT_OBJECT_0 means the host exited; WAIT_FAILED and every
            # other result are equally unsafe to keep Core alive against.
            stop.set()
            return
    finally:
        kernel32.CloseHandle(handle)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the LiteLLM Menu Python Core IPC service")
    parser.add_argument("--metadata", type=Path, help="private Core metadata path")
    parser.add_argument("--endpoint-file", type=Path, help="private endpoint descriptor path")
    parser.add_argument("--parent-pid", type=_positive_pid, help="native host process identifier")
    parser.add_argument("--claude-settings", type=Path, help="explicit Claude settings path")
    parser.add_argument("--language-file", type=Path, help="explicit language preference path")
    parser.add_argument("--runtime-root", type=Path, help="private managed runtime root")
    parser.add_argument("--address", default="127.0.0.1", help="loopback address (never a public interface)")
    parser.add_argument("--port", type=int, default=0, help="port; zero chooses a random ephemeral port")
    parser.add_argument("--print-endpoint", action="store_true", help="print the private descriptor for a supervising host")
    return parser


def _write_endpoint(path: Path, server: CoreIPCServer) -> None:
    descriptor = server.endpoint_descriptor(include_bootstrap_token=True)
    # AtomicJSONStore rejects symlink targets and applies 0600 to both the
    # temporary file and final descriptor.
    AtomicJSONStore(path).write(descriptor)


def run(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    # Windows validates the parent by opening the supplied process object in
    # the watchdog. Python's getppid() is not the durable identity there.
    if args.parent_pid is not None and os.name != "nt" and os.getppid() != args.parent_pid:
        return 0
    stop = threading.Event()
    parent_watchdog: threading.Thread | None = None
    if args.parent_pid is not None:
        parent_watchdog = threading.Thread(
            target=_watch_parent,
            args=(args.parent_pid, stop),
            name="litellm-core-parent-watchdog",
            daemon=True,
        )
        parent_watchdog.start()
    core = CoreStore.with_default_domains(
        metadata_path=args.metadata,
        claude_settings_path=args.claude_settings,
        language_path=args.language_file,
        runtime_root=args.runtime_root,
    )
    server = CoreIPCServer(core, address=args.address, port=args.port)

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
        if stop.is_set():
            return 0
        server.start()
        if args.endpoint_file is not None:
            _write_endpoint(args.endpoint_file, server)
        if args.print_endpoint:
            # This is intended for a supervising native process.  It is the
            # only CLI mode that emits the descriptor; regular logs contain
            # no endpoint, token, or user path.
            import json

            print(json.dumps(server.endpoint_descriptor(include_bootstrap_token=True), separators=(",", ":")))
        stop.wait()
        return 0
    except (PersistenceError, OSError):
        return 1
    finally:
        stop.set()
        if parent_watchdog is not None and parent_watchdog is not threading.current_thread():
            parent_watchdog.join(timeout=1.0)
        try:
            core.shutdown()
        except Exception:
            # The owner-aware controller refuses unrelated processes. A failed
            # best-effort signal cleanup must not expose local diagnostics.
            pass
        server.stop()
        if args.endpoint_file is not None:
            try:
                args.endpoint_file.unlink()
            except FileNotFoundError:
                pass
            except OSError:
                pass


if __name__ == "__main__":
    raise SystemExit(run())
