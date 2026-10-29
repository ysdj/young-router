"""Private helpers shared by the staged Core settings domains."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from collections.abc import Mapping
from typing import Any

from ... import config_paths
from ..persistence import PersistenceError, read_bytes
from ..security import safe_exception_message


class DomainError(ValueError):
    """A deliberately source-safe error from a staged Core domain."""


def _mapping(value: object, label: str = "payload") -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise DomainError(f"{label} must be an object")
    return dict(value)


def _copy_mapping(value: object, label: str = "payload") -> dict[str, Any]:
    return copy.deepcopy(_mapping(value, label))


def _action_name(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DomainError("A settings action is required")
    return value.strip().replace("-", "_").replace("/", "_").replace(".", "_").lower()


def _safe_problem(_: BaseException, fallback: str) -> DomainError:
    """Never pass source parser output (which can contain credentials) on."""

    return DomainError(fallback)


def _file_bytes(path: Path) -> bytes | None:
    try:
        return read_bytes(path)
    except PersistenceError as exc:
        raise DomainError(safe_exception_message(exc)) from None


def _same_file(path: Path, expected: bytes | None) -> bool:
    return _file_bytes(path) == expected


def _default_runtime_root() -> Path:
    return config_paths.runtime_root()


def _default_provider_config_path() -> Path:
    # One resolver, not this module's own: the usage-log reader, the Codex
    # editor, the WebDAV bundle and the JSON config API must all name the
    # document the provider editor writes, or an installation holds two.
    return config_paths.provider_config_path()


def _default_runtime_settings_path() -> Path:
    configured = os.environ.get("YOUNG_ROUTER_RUNTIME_SETTINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else _default_runtime_root() / "runtime-settings.env"


def _runtime_root_for_config(config_path: Path) -> Path:
    """The installation root a configuration document belongs to.

    Everything an installation shares lives beside its configuration, not
    beside the directory that document happens to sit in: the runtime directory
    is one level down from the root, and ``LITELLM_CONFIG_FILE`` may name a
    path that is nowhere near it.  A reader that owns no cache of its own -- a
    pane snapshot resolving metadata the Codex domain refreshes -- must find
    the one file by this root, or it reads a second cache nobody writes.
    """

    parent = Path(config_path).expanduser().parent
    return parent.parent if parent.name == config_paths.RUNTIME_DIRECTORY else parent


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
        raise DomainError(f"The selected {label} is unavailable")
    return value


def _move(values: list[Any], source: object, destination: object, label: str) -> None:
    source_index = _index(source, len(values), label)
    if type(destination) is not int or destination < 0 or destination >= len(values):
        raise DomainError(f"The selected {label} destination is unavailable")
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
    raise DomainError("A move destination is required")

def local_proxy_endpoint() -> tuple[str, str]:
    """The managed local proxy URL (with ``/v1``) and its master key.

    Shared by the Codex and Claude "use local API" actions so both adopt the
    exact port the runtime settings select (falling back to the default port)
    and the configured LiteLLM master key (falling back to the built-in one).
    """

    from .. import codex_config

    port = codex_config.DEFAULT_PORT
    try:
        from ..runtime_settings_io import load_specs, read_settings_file

        values = read_settings_file(_default_runtime_settings_path(), load_specs())
        candidate = str(values.get("LITELLM_PORT", "")).strip()
        if candidate.isdigit() and 0 < int(candidate) < 65536:
            port = candidate
    except Exception:
        pass
    key = ""
    candidates = (
        codex_config.default_config_path(),
        _default_runtime_root() / ".litellm_runtime" / "config.yaml",
        _default_runtime_root() / ".litellm-runtime" / "config.yaml",
    )
    for candidate in candidates:
        try:
            key = codex_config.local_api_key(codex_config.load_yaml(candidate))
            break
        except Exception:
            continue
    if not key:
        # Returning the built-in placeholder here is what makes this failure
        # misleading: the client is handed a key the proxy will refuse with
        # "No connected db.", which names a database instead of the key.
        raise DomainError(
            "The local proxy's master key could not be read from its config; "
            "the client was not switched to it."
        )
    return f"http://127.0.0.1:{port}/v1", key
