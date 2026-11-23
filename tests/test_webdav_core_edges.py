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


class LoginLeaseTests(unittest.TestCase):
    def setUp(self) -> None:
        self.settings = webdav_core.Settings(
            url="https://example.com/dav/config.json",
            username="dav-user",
            password="dav-password",
        )

    def test_lock_returns_a_token_and_unlocks_on_release(self) -> None:
        client = webdav_core.WebDAVClient(self.settings)
        with patch.object(
            client,
            "request",
            return_value=(200, {"Lock-Token": "<opaquelocktoken:fixture>"}, b""),
        ) as request:
            lease = client.acquire_login_lease("account-a")
            self.assertEqual("<opaquelocktoken:fixture>", lease.token)
            lease.release()

        self.assertEqual(2, request.call_count)
        lock_call = request.call_args_list[0]
        self.assertEqual("LOCK", lock_call.args[0])
        self.assertEqual(webdav_core.login_lease_url(self.settings, "account-a"), lock_call.args[1])
        self.assertEqual("0", lock_call.kwargs["headers"]["Depth"])
        self.assertIn(b"<D:exclusive/>", lock_call.kwargs["data"])
        self.assertEqual("UNLOCK", request.call_args_list[1].args[0])
        self.assertEqual("<opaquelocktoken:fixture>", request.call_args_list[1].kwargs["headers"]["Lock-Token"])

    def test_attempt_write_is_conditioned_on_still_owning_the_lock(self) -> None:
        client = webdav_core.WebDAVClient(self.settings)
        with patch.object(
            client,
            "request",
            return_value=(200, {"Lock-Token": "<opaquelocktoken:fixture>"}, b""),
        ) as request:
            with client.acquire_login_lease("account-a") as lease:
                lease.write_attempt("2026-01-01T00:00:00.000000Z", "pending")

        self.assertEqual(["LOCK", "PUT", "UNLOCK"], [call.args[0] for call in request.call_args_list])
        self.assertEqual("(<opaquelocktoken:fixture>)", request.call_args_list[1].kwargs["headers"]["If"])

    def test_busy_lock_is_distinguished_from_unavailable_coordination(self) -> None:
        client = webdav_core.WebDAVClient(self.settings)
        for status, expected in (
            (405, webdav_core.WebDAVLoginLeaseError),
            (501, webdav_core.WebDAVLoginLeaseError),
            (403, webdav_core.WebDAVLoginLeaseError),
        ):
            with self.subTest(status=status):
                error = webdav_core.WebDAVHTTPError("LOCK", "https://example.com/lock", status, "fixture")
                with patch.object(client, "request", side_effect=error):
                    with self.assertRaises(expected) as raised:
                        client.acquire_login_lease("account-a")
                    self.assertNotIsInstance(raised.exception, webdav_core.WebDAVLoginLeaseBusy)

        error = webdav_core.WebDAVHTTPError("LOCK", "https://example.com/lock", 423, "Locked")
        with patch.object(client, "request", side_effect=error):
            with self.assertRaises(webdav_core.WebDAVLoginLeaseBusy):
                client.acquire_login_lease("account-a")
