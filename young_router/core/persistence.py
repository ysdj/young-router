"""Atomic, private JSON persistence for Python Core metadata and packages."""

from __future__ import annotations

import json
import os
from pathlib import Path
import stat
import tempfile
from collections.abc import Mapping
from typing import Any


MAX_PERSISTED_BYTES = 16 * 1024 * 1024
PRIVATE_MODE = 0o600


class PersistenceError(ValueError):
    """A filesystem failure safe to report to the UI."""


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise PersistenceError("Core state contains a duplicate JSON key")
        result[key] = value
    return result


def _reject_constant(_: str) -> object:
    raise PersistenceError("Core state contains an unsupported JSON value")


def _assert_regular_target(path: Path) -> None:
    try:
        details = path.lstat()
    except FileNotFoundError:
        return
    except OSError:
        raise PersistenceError("Core state could not be inspected") from None
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise PersistenceError("Core state path must be a regular file")


def _ensure_parent(path: Path) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    except OSError:
        raise PersistenceError("Core state directory could not be prepared") from None


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


def atomic_write_bytes(path: Path | str, data: bytes, *, mode: int = PRIVATE_MODE) -> None:
    """Replace ``path`` atomically without following a final symlink."""

    target = Path(path).expanduser()
    if not isinstance(data, (bytes, bytearray)) or len(data) > MAX_PERSISTED_BYTES:
        raise PersistenceError("Core state exceeds the size limit")
    _assert_regular_target(target)
    _ensure_parent(target)
    temporary: str | None = None
    descriptor: int | None = None
    try:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", suffix=".tmp", dir=target.parent)
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            descriptor = None
            handle.write(bytes(data))
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
            raise PersistenceError("Core state permissions could not be secured") from None
        _fsync_directory(target.parent)
    except PersistenceError:
        raise
    except OSError:
        raise PersistenceError("Core state could not be written") from None
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


def atomic_write_text(path: Path | str, text: str, *, mode: int = PRIVATE_MODE) -> None:
    if not isinstance(text, str):
        raise PersistenceError("Core text payload is invalid")
    atomic_write_bytes(Path(path), text.encode("utf-8"), mode=mode)


def atomic_write_json(path: Path | str, payload: Mapping[str, Any], *, mode: int = PRIVATE_MODE) -> None:
    if not isinstance(payload, Mapping):
        raise PersistenceError("Core state must be a JSON object")
    try:
        encoded = (json.dumps(dict(payload), ensure_ascii=False, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError):
        raise PersistenceError("Core state contains an unsupported value") from None
    atomic_write_bytes(Path(path), encoded, mode=mode)


def read_bytes(path: Path | str, *, max_bytes: int = MAX_PERSISTED_BYTES) -> bytes | None:
    target = Path(path).expanduser()
    _assert_regular_target(target)
    try:
        details = target.stat()
    except FileNotFoundError:
        return None
    except OSError:
        raise PersistenceError("Core state could not be read") from None
    if details.st_size > max_bytes:
        raise PersistenceError("Core state exceeds the size limit")
    try:
        data = target.read_bytes()
    except OSError:
        raise PersistenceError("Core state could not be read") from None
    if len(data) > max_bytes:
        raise PersistenceError("Core state exceeds the size limit")
    return data


def read_text(path: Path | str, *, max_bytes: int = MAX_PERSISTED_BYTES) -> str | None:
    data = read_bytes(path, max_bytes=max_bytes)
    if data is None:
        return None
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError:
        raise PersistenceError("Core state must be UTF-8") from None


def read_json(path: Path | str, *, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
    text = read_text(path)
    if text is None:
        return dict(default or {})
    try:
        loaded = json.loads(text, object_pairs_hook=_reject_duplicate_keys, parse_constant=_reject_constant)
    except PersistenceError:
        raise
    except (TypeError, json.JSONDecodeError):
        raise PersistenceError("Core state is not valid JSON") from None
    if not isinstance(loaded, dict):
        raise PersistenceError("Core state must be a JSON object")
    return loaded


class AtomicJSONStore:
    """A reusable private JSON file boundary used by Core metadata."""

    def __init__(self, path: Path | str, *, mode: int = PRIVATE_MODE):
        self.path = Path(path).expanduser()
        self.mode = int(mode)

    def read(self, *, default: Mapping[str, Any] | None = None) -> dict[str, Any]:
        return read_json(self.path, default=default)

    def write(self, payload: Mapping[str, Any]) -> None:
        atomic_write_json(self.path, payload, mode=self.mode)


__all__ = [
    "AtomicJSONStore",
    "MAX_PERSISTED_BYTES",
    "PRIVATE_MODE",
    "PersistenceError",
    "atomic_write_bytes",
    "atomic_write_json",
    "atomic_write_text",
    "read_bytes",
    "read_json",
    "read_text",
]
