"""Bounded UTF-8 reads and duplicate-key guards shared by this app's readers.

Every JSON document this app reads is read the same way: a bounded UTF-8 text
read and a parse that refuses a repeated key instead of silently keeping the
last one.  The readers live in different packages (Core, the config editor,
the WebDAV sync, the standalone scripts), so the mechanics live here and each
reader hands in the error it wants raised.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

MessageError = Callable[[str], Exception]
KeyErrorFactory = Callable[[str], Exception]


def read_limited_utf8(
    path: Path,
    label: str,
    *,
    max_bytes: int,
    missing_is_empty: bool = False,
    error: MessageError,
) -> str:
    """Read one bounded regular UTF-8 file, or answer "" when allowed."""

    try:
        stat_result = path.stat()
    except FileNotFoundError:
        if missing_is_empty:
            return ""
        raise error(f"{label} does not exist.") from None
    except OSError as exc:
        raise error(f"{label} cannot be read.") from exc
    if not path.is_file() or stat_result.st_size > max_bytes:
        raise error(f"{label} is not a supported size or file type.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise error(f"{label} cannot be read.") from exc
    if len(data) > max_bytes:
        raise error(f"{label} is too large.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise error(f"{label} must be UTF-8.") from exc


def duplicate_key_hook(error: KeyErrorFactory) -> Callable[[list[tuple[str, Any]]], dict[str, Any]]:
    """Build an ``object_pairs_hook`` that refuses a repeated JSON key."""

    def hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise error(key)
            result[key] = value
        return result

    return hook


__all__ = ["duplicate_key_hook", "read_limited_utf8"]
