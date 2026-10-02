"""Atomic, private file replacement shared by every writer this app owns.

Core metadata, the raw editors, the WebDAV sync, and the proxy's own state
files all replace a file the same way.  One implementation keeps the symlink
refusal, the flush/fsync, and the private mode identical everywhere, and each
caller names its own failure by handing in an ``error`` factory instead of
re-implementing the mechanics.

The module is deliberately dependency-free: importing it must stay cheap for
interpreter startup (``sitecustomize``), the proxy, and the Core alike.
"""

from __future__ import annotations

import os
from pathlib import Path
import stat
import tempfile
from typing import Callable, NoReturn

PRIVATE_MODE = 0o600

#: Failure reasons an ``error`` factory may receive.  They name the step that
#: failed, never the file, so a caller can build its own user-facing sentence.
REASON_INVALID = "invalid"
REASON_TOO_LARGE = "too_large"
REASON_INSPECT = "inspect"
REASON_SYMLINK = "symlink"
REASON_NOT_REGULAR = "not_regular"
REASON_PARENT = "parent"
REASON_WRITE = "write"
REASON_PERMISSIONS = "permissions"

ErrorFactory = Callable[[str], Exception]


class AtomicWriteError(OSError):
    """A file could not be replaced atomically."""

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or f"file could not be replaced atomically ({reason})")
        self.reason = reason


def atomic_write_bytes(
    path: Path | str,
    data: bytes,
    *,
    mode: int = PRIVATE_MODE,
    parent_mode: int | None = None,
    max_bytes: int | None = None,
    error: ErrorFactory | None = None,
) -> None:
    """Replace ``path`` atomically without following a final symlink."""

    fail = _build_fail(error)
    target = Path(path).expanduser()
    if not isinstance(data, (bytes, bytearray)):
        fail(REASON_INVALID)
    if max_bytes is not None and len(data) > max_bytes:
        fail(REASON_TOO_LARGE)
    try:
        _replace(target, bytes(data), mode=mode, parent_mode=parent_mode)
    except AtomicWriteError as exc:
        fail(exc.reason)


def atomic_write_text(
    path: Path | str,
    text: str,
    *,
    mode: int = PRIVATE_MODE,
    parent_mode: int | None = None,
    max_bytes: int | None = None,
    error: ErrorFactory | None = None,
) -> None:
    if not isinstance(text, str):
        _build_fail(error)(REASON_INVALID)
    atomic_write_bytes(
        path,
        text.encode("utf-8"),
        mode=mode,
        parent_mode=parent_mode,
        max_bytes=max_bytes,
        error=error,
    )


def _build_fail(error: ErrorFactory | None) -> Callable[[str], NoReturn]:
    factory = error or (lambda reason: AtomicWriteError(reason))

    def fail(reason: str) -> NoReturn:
        raise factory(reason)

    return fail


def _replace(target: Path, data: bytes, *, mode: int, parent_mode: int | None) -> None:
    _assert_regular_target(target)
    try:
        target.parent.mkdir(parents=True, exist_ok=True, mode=parent_mode or 0o777)
    except OSError:
        raise AtomicWriteError(REASON_PARENT) from None
    if parent_mode is not None:
        try:
            os.chmod(target.parent, parent_mode)
        except OSError:
            pass
    temporary: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        # Recheck immediately before replacing so a concurrent caller cannot
        # swap the destination for a symlink after the first lstat.
        _assert_regular_target(target)
        os.replace(temporary, target)
        temporary = None
        try:
            os.chmod(target, mode)
        except OSError:
            raise AtomicWriteError(REASON_PERMISSIONS) from None
        _fsync_directory(target.parent)
    except AtomicWriteError:
        raise
    except OSError:
        raise AtomicWriteError(REASON_WRITE) from None
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
        if temporary:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass


def _assert_regular_target(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise AtomicWriteError(REASON_INSPECT) from None
    if stat.S_ISLNK(details.st_mode):
        raise AtomicWriteError(REASON_SYMLINK)
    if not stat.S_ISREG(details.st_mode):
        raise AtomicWriteError(REASON_NOT_REGULAR)


def _fsync_directory(path: Path) -> None:
    try:
        descriptor = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


__all__ = [
    "AtomicWriteError",
    "PRIVATE_MODE",
    "REASON_INSPECT",
    "REASON_INVALID",
    "REASON_NOT_REGULAR",
    "REASON_PARENT",
    "REASON_PERMISSIONS",
    "REASON_SYMLINK",
    "REASON_TOO_LARGE",
    "REASON_WRITE",
    "atomic_write_bytes",
    "atomic_write_text",
]
