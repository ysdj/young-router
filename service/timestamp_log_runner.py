#!/usr/bin/env python3
"""Run a local script and timestamp every line written to its local log."""

from __future__ import annotations

import datetime as dt
import os
import re
import subprocess
import sys
from typing import BinaryIO


_TIMESTAMP_PREFIX = re.compile(
    rb"^\[\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z\]"
)


def _timestamp() -> bytes:
    return (
        dt.datetime.now(dt.timezone.utc)
        .isoformat(timespec="seconds")
        .replace("+00:00", "Z")
        .encode("ascii")
    )


def _write_record(log: BinaryIO, raw: bytes) -> None:
    line = raw.rstrip(b"\r\n")
    if not line:
        return
    if _TIMESTAMP_PREFIX.match(line):
        log.write(line + b"\n")
    else:
        log.write(b"[" + _timestamp() + b"] " + line + b"\n")
    log.flush()


def main(arguments: list[str]) -> int:
    if len(arguments) < 3:
        return 64
    service, log_path, root, *command = arguments
    if not command:
        command = ["run-native"]
    elif command == ["--"]:
        command = []
    if not os.path.isfile(service):
        try:
            with open(log_path, "ab", buffering=0) as log:
                _write_record(log, b"timestamp log runner could not start the local script")
        except OSError:
            pass
        return 1
    try:
        with open(log_path, "ab", buffering=0) as log:
            process = subprocess.Popen(
                ["/bin/bash", service, *command],
                cwd=root,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                env=os.environ.copy(),
                close_fds=True,
            )
            assert process.stdout is not None
            for raw in iter(process.stdout.readline, b""):
                _write_record(log, raw)
            return process.wait()
    except Exception:
        # Keep the fallback record safe and canonical too.  The underlying
        # exception can include paths or environment details that do not belong
        # in a user-visible log.
        try:
            with open(log_path, "ab", buffering=0) as log:
                _write_record(log, b"timestamp log runner could not start the local script")
        except OSError:
            pass
        return 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
