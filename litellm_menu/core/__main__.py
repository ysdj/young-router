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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the LiteLLM Menu Python Core IPC service")
    parser.add_argument("--metadata", type=Path, help="private Core metadata path")
    parser.add_argument("--endpoint-file", type=Path, help="private endpoint descriptor path")
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
    core = CoreStore.with_default_domains(
        metadata_path=args.metadata,
        claude_settings_path=args.claude_settings,
        language_path=args.language_file,
        runtime_root=args.runtime_root,
    )
    server = CoreIPCServer(core, address=args.address, port=args.port)
    stop = threading.Event()

    def request_stop(_signum: int, _frame: Any) -> None:
        stop.set()

    signal.signal(signal.SIGTERM, request_stop)
    signal.signal(signal.SIGINT, request_stop)
    try:
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
