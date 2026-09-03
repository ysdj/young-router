"""Bounded append-only logs shared by Core and proxy processes."""

from __future__ import annotations

import os
import threading

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows uses a single Core launcher.
    fcntl = None  # type: ignore[assignment]


LOG_MAX_BYTES_ENV = "LITELLM_MENU_LOG_MAX_BYTES"
DEFAULT_LOG_MAX_BYTES = 10 * 1024 * 1024
MIN_LOG_MAX_BYTES = 256 * 1024
LOG_BACKUP_SEGMENTS_ENV = "LITELLM_MENU_LOG_BACKUP_SEGMENTS"
DEFAULT_LOG_BACKUP_SEGMENTS = 2
MIN_LOG_BACKUP_SEGMENTS = 1
MAX_LOG_BACKUP_SEGMENTS = 8
_LOCAL_LOCK = threading.RLock()


def log_backup_segments() -> int:
    value = os.getenv(LOG_BACKUP_SEGMENTS_ENV, "").strip()
    if not value:
        return DEFAULT_LOG_BACKUP_SEGMENTS
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_LOG_BACKUP_SEGMENTS
    return max(MIN_LOG_BACKUP_SEGMENTS, min(parsed, MAX_LOG_BACKUP_SEGMENTS))


def log_max_bytes() -> int:
    value = os.getenv(LOG_MAX_BYTES_ENV, "").strip()
    if not value:
        return DEFAULT_LOG_MAX_BYTES
    try:
        parsed = int(value)
    except ValueError:
        return DEFAULT_LOG_MAX_BYTES
    return max(parsed, MIN_LOG_MAX_BYTES)


def _stream_path(stream: object) -> str | None:
    try:
        name = getattr(stream, "name", None)
        if isinstance(name, str) and os.path.isabs(name):
            return name
        descriptor = stream.fileno()  # type: ignore[attr-defined]
        configured = os.getenv("LITELLM_MENU_SERVICE_LOG", "").strip()
        if configured:
            stream_details = os.fstat(descriptor)
            configured_details = os.stat(configured)
            if (
                stream_details.st_dev == configured_details.st_dev
                and stream_details.st_ino == configured_details.st_ino
            ):
                return os.path.abspath(configured)
        path = os.readlink(f"/dev/fd/{descriptor}")
    except (AttributeError, OSError, ValueError):
        return None
    return path if os.path.isabs(path) else None


def _shift_backup_segments(path: str, segments: int) -> None:
    """Rotate ``.1``..``.N`` backups before writing a fresh ``.1`` tail.

    A busy service can rotate several times in minutes; keeping only one
    backup then discards the very window that recorded an incident.  Shift
    the older segments (``.1`` -> ``.2``, ...) so a bounded number of
    previous tails remain readable.
    """

    for index in range(segments, 0, -1):
        current = f"{path}.{index}"
        if index >= segments:
            try:
                os.unlink(current)
            except FileNotFoundError:
                pass
            continue
        try:
            os.replace(current, f"{path}.{index + 1}")
        except FileNotFoundError:
            pass


def _write_backup(descriptor: int, path: str, size: int, maximum: int) -> None:
    with open(path, "rb") as source:
        source.seek(max(0, size - maximum))
        tail = source.read(maximum)
    backup = f"{path}.1"
    temporary = f"{backup}.rotate.{os.getpid()}.tmp"
    try:
        _shift_backup_segments(path, log_backup_segments())
        with open(temporary, "wb") as target:
            target.write(tail)
        os.chmod(temporary, 0o600)
        os.replace(temporary, backup)
    finally:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass


def _with_rotation_lock(path: str, operation: object) -> object:
    lock_descriptor = os.open(f"{path}.rotate.lock", os.O_RDWR | os.O_CREAT, 0o600)
    try:
        if fcntl is not None:
            fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
        return operation()  # type: ignore[operator]
    finally:
        if fcntl is not None:
            fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
        os.close(lock_descriptor)


def _is_managed_log_stream(stream: object) -> bool:
    configured = os.getenv("LITELLM_MENU_SERVICE_LOG", "").strip()
    if not configured:
        return False
    path = _stream_path(stream)
    if path is None:
        return False
    try:
        return os.path.samefile(path, configured)
    except OSError:
        return os.path.abspath(path) == os.path.abspath(configured)


def write_bounded_stream(stream: object, text: str, *, maximum_bytes: int | None = None) -> int:
    """Write one console fragment while enforcing the current/backup file cap."""

    path = _stream_path(stream)
    if path is None or not _is_managed_log_stream(stream):
        return stream.write(text)  # type: ignore[attr-defined]
    maximum = maximum_bytes if maximum_bytes is not None else log_max_bytes()

    def write() -> int:
        stream.flush()  # type: ignore[attr-defined]
        descriptor = stream.fileno()  # type: ignore[attr-defined]
        size = os.fstat(descriptor).st_size
        if size + len(text.encode("utf-8")) > maximum:
            _write_backup(descriptor, path, size, maximum)
            os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_SET)
        written = stream.write(text)  # type: ignore[attr-defined]
        stream.flush()  # type: ignore[attr-defined]
        return written

    with _LOCAL_LOCK:
        return int(_with_rotation_lock(path, write))


def append_bounded_log(path: str, data: bytes, *, maximum_bytes: int | None = None) -> None:
    """Append one record and rotate the previous segment under a process lock."""

    maximum = maximum_bytes if maximum_bytes is not None else log_max_bytes()
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)

    def append() -> None:
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            size = os.fstat(descriptor).st_size
            if size + len(data) > maximum:
                _write_backup(descriptor, path, size, maximum)
                os.ftruncate(descriptor, 0)
            os.lseek(descriptor, 0, os.SEEK_END)
            os.write(descriptor, data)
            os.fchmod(descriptor, 0o600)
        finally:
            os.close(descriptor)

    with _LOCAL_LOCK:
        _with_rotation_lock(path, append)


__all__ = [
    "DEFAULT_LOG_MAX_BYTES",
    "LOG_MAX_BYTES_ENV",
    "MIN_LOG_MAX_BYTES",
    "append_bounded_log",
    "log_max_bytes",
    "write_bounded_stream",
]
