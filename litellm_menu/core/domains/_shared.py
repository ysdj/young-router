"""Private helpers shared by the staged Core settings domains."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from ..persistence import PersistenceError, read_bytes
from ..security import safe_exception_message


class LegacyDomainError(ValueError):
    """A deliberately source-safe error from a legacy-backed Core domain."""


def _mapping(value: object, label: str = "payload") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise LegacyDomainError(f"{label} must be an object")
    return dict(value)


def _copy_mapping(value: object, label: str = "payload") -> dict[str, Any]:
    return copy.deepcopy(_mapping(value, label))


def _action_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise LegacyDomainError("A settings action is required")
    return value.strip().replace("-", "_").replace("/", "_").replace(".", "_").lower()


def _safe_problem(_: BaseException, fallback: str) -> LegacyDomainError:
    """Never pass source parser output (which can contain credentials) on."""

    return LegacyDomainError(fallback)


def _file_bytes(path: Path) -> bytes | None:
    try:
        return read_bytes(path)
    except PersistenceError as exc:
        raise LegacyDomainError(safe_exception_message(exc)) from None


def _same_file(path: Path, expected: bytes | None) -> bool:
    return _file_bytes(path) == expected


def _default_runtime_root() -> Path:
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _default_provider_config_path() -> Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    return Path(configured).expanduser() if configured else _default_runtime_root() / "config.yaml"


def _default_runtime_settings_path() -> Path:
    configured = os.environ.get("LITELLM_MENU_RUNTIME_SETTINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else _default_runtime_root() / "runtime-settings.env"


def _default_webdav_enabled_path(settings_path: Path) -> Path:
    configured = os.environ.get("LITELLM_WEBDAV_SYNC_ENABLED_FILE", "").strip()
    return Path(configured).expanduser() if configured else settings_path.parent / ".litellm-runtime" / "webdav-sync.enabled"


def _default_webdav_status_path(settings_path: Path) -> Path:
    configured = os.environ.get("LITELLM_WEBDAV_SYNC_STATUS_FILE", "").strip()
    return Path(configured).expanduser() if configured else settings_path.parent / ".litellm-runtime" / "webdav-sync-status.json"


def _selected_identifier(data: Mapping[str, Any], *keys: str) -> object:
    for key in keys:
        if key in data:
            return data[key]
    target = data.get("target")
    if isinstance(target, Mapping):
        for key in keys:
            if key in target:
                return target[key]
    return None


def _index(value: object, length: int, label: str) -> int:
    if type(value) is not int or value < 0 or value >= length:
        raise LegacyDomainError(f"The selected {label} is unavailable")
    return value


def _move(values: list[Any], source: object, destination: object, label: str) -> None:
    source_index = _index(source, len(values), label)
    if type(destination) is not int or destination < 0 or destination >= len(values):
        raise LegacyDomainError(f"The selected {label} destination is unavailable")
    item = values.pop(source_index)
    values.insert(destination, item)


def _direction_destination(source: int, length: int, data: Mapping[str, Any]) -> int:
    destination = data.get("to", data.get("destination"))
    if type(destination) is int:
        return destination
    direction = data.get("direction")
    if direction == "up":
        return max(0, source - 1)
    if direction == "down":
        return min(length - 1, source + 1)
    raise LegacyDomainError("A move destination is required")
