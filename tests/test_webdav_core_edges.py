from __future__ import annotations

import os
import sys
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from young_router.webdav import core as webdav_core  # noqa: E402


class _FailingWriteHandle:
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def __enter__(self):
        return self

    def __exit__(self, *args) -> None:
        os.close(self.fd)

    def write(self, _data: bytes) -> None:
        raise RuntimeError("disk full")


def _failing_data_write_patches() -> list:
    real_fdopen = os.fdopen
    closed: set[int] = set()

    def close_fd(fd: int) -> None:
        if fd in closed:
            raise OSError(9, "Bad file descriptor")
        closed.add(fd)

    def open_fd(fd: int, mode: str, *args, **kwargs):
        if mode == "wb":
            return _FailingWriteHandle(fd)
        return real_fdopen(fd, mode, *args, **kwargs)

    return [
        patch.object(webdav_core, "_curl_binary", return_value="/bin/true"),
        patch.object(
            webdav_core.tempfile,
            "mkstemp",
            side_effect=[(101, "/nope/h"), (102, "/nope/c"), (103, "/nope/b"), (104, "/nope/d")],
        ),
        patch.object(webdav_core.os, "close", side_effect=close_fd),
        patch.object(webdav_core.os, "fdopen", side_effect=open_fd),
    ]


class CurlDataTempFileTests(unittest.TestCase):
    def test_a_failed_data_write_keeps_its_own_error(self) -> None:
        client = webdav_core.WebDAVClient(
            webdav_core.Settings(url="https://example.com/dav/resource/")
        )
        with ExitStack() as stack:
            for patcher in _failing_data_write_patches():
                stack.enter_context(patcher)
            with self.assertRaisesRegex(RuntimeError, "disk full"):
                client._curl_request(
                    "PUT",
                    "https://example.com/dav/resource/file",
                    b"body",
                    None,
                    webdav_core.SyncError("network down"),
                    1024,
                )
