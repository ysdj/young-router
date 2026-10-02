"""Atomic, private JSON persistence for Python Core metadata and packages."""

from __future__ import annotations

import json
from pathlib import Path
import stat
from collections.abc import Mapping
from typing import Any

from .. import atomic_io as _atomic_io
from ..atomic_io import (
    PRIVATE_MODE,
    REASON_INSPECT,
    REASON_INVALID,
    REASON_NOT_REGULAR,
    REASON_PARENT,
    REASON_PERMISSIONS,
    REASON_SYMLINK,
    REASON_TOO_LARGE,
    REASON_WRITE,
)


MAX_PERSISTED_BYTES = 16 * 1024 * 1024


class PersistenceError(ValueError):
    """A filesystem failure safe to report to the UI."""


# One sentence per failed step, so the shared writer still speaks Core's own
# vocabulary when it refuses a write.
_WRITE_ERROR_MESSAGES = {
    REASON_INVALID: "Core state exceeds the size limit",
    REASON_TOO_LARGE: "Core state exceeds the size limit",
    REASON_INSPECT: "Core state could not be inspected",
    REASON_SYMLINK: "Core state path must be a regular file",
    REASON_NOT_REGULAR: "Core state path must be a regular file",
    REASON_PARENT: "Core state directory could not be prepared",
    REASON_WRITE: "Core state could not be written",
    REASON_PERMISSIONS: "Core state permissions could not be secured",
}


def _write_error(reason: str) -> Exception:
    return PersistenceError(_WRITE_ERROR_MESSAGES.get(reason, "Core state could not be written"))


from .. import json_input as _json_input


# One duplicate key is refused the same way everywhere; the message stays
# Core's own.
_reject_duplicate_keys = _json_input.duplicate_key_hook(
    lambda key: PersistenceError("Core state contains a duplicate JSON key")
)


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


def atomic_write_bytes(path: Path | str, data: bytes, *, mode: int = PRIVATE_MODE) -> None:
    """Replace ``path`` atomically without following a final symlink."""

    _atomic_io.atomic_write_bytes(
        path,
        data,
        mode=mode,
        parent_mode=0o700,
        max_bytes=MAX_PERSISTED_BYTES,
        error=_write_error,
    )


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
