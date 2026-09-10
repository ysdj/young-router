"""Claude Desktop on third-party (3P) local configuration.

Claude Desktop 3P keeps the active configuration in a small local library,
separate from Claude Code's ``~/.claude/settings.json``.  This adapter owns
only that library's active JSON document and never places its API key in the
public Core snapshot.
"""

from __future__ import annotations

import copy
import hashlib
import json
import os
import pathlib
import re
import stat
import sys
import tempfile
import uuid
from collections.abc import Callable, Mapping
from typing import Any
from urllib.parse import parse_qsl, urlsplit


CONFIG_LIBRARY_ENV = "CLAUDE_DESKTOP_CONFIG_LIBRARY"
DEVELOPER_SETTINGS_ENV = "CLAUDE_DESKTOP_DEVELOPER_SETTINGS"
CONFIG_META_FILENAME = "_meta.json"
_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SECRET_MARKERS = ("token", "key", "secret", "password", "credential", "authorization")
_SENSITIVE_QUERY_MARKERS = ("key", "token", "secret", "password", "passwd", "credential", "auth")
_REDACTED = "configured"


class ClaudeDesktopConfigError(ValueError):
    """A safe, user-facing Claude Desktop configuration error."""


def default_config_library_path() -> pathlib.Path:
    explicit = os.environ.get(CONFIG_LIBRARY_ENV, "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser()
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Claude-3p" / "configLibrary"
    if os.name == "nt":
        local_app_data = os.environ.get("LOCALAPPDATA", "").strip()
        root = pathlib.Path(local_app_data).expanduser() if local_app_data else pathlib.Path.home() / "AppData" / "Local"
        return root / "Claude-3p" / "configLibrary"
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = pathlib.Path(config_home).expanduser() if config_home else pathlib.Path.home() / ".config"
    return root / "Claude-3p" / "configLibrary"


def default_developer_settings_path() -> pathlib.Path:
    explicit = os.environ.get(DEVELOPER_SETTINGS_ENV, "").strip()
    if explicit:
        return pathlib.Path(explicit).expanduser()
    if sys.platform == "darwin":
        return pathlib.Path.home() / "Library" / "Application Support" / "Claude" / "developer_settings.json"
    if os.name == "nt":
        app_data = os.environ.get("APPDATA", "").strip()
        root = pathlib.Path(app_data).expanduser() if app_data else pathlib.Path.home() / "AppData" / "Roaming"
        return root / "Claude" / "developer_settings.json"
    config_home = os.environ.get("XDG_CONFIG_HOME", "").strip()
    root = pathlib.Path(config_home).expanduser() if config_home else pathlib.Path.home() / ".config"
    return root / "Claude" / "developer_settings.json"


def _safe_read(path: pathlib.Path) -> tuple[str, bool]:
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ClaudeDesktopConfigError("Claude Desktop configuration could not be loaded")
        return path.read_text(encoding="utf-8"), True
    except FileNotFoundError:
        return "{}\n", False
    except (OSError, UnicodeError):
        raise ClaudeDesktopConfigError("Claude Desktop configuration could not be loaded") from None


def _current_bytes(path: pathlib.Path) -> bytes | None:
    try:
        details = path.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise ClaudeDesktopConfigError("Claude Desktop configuration changed on disk; reload before applying")
        return path.read_bytes()
    except FileNotFoundError:
        return None
    except ClaudeDesktopConfigError:
        raise
    except OSError:
        raise ClaudeDesktopConfigError("Claude Desktop configuration changed on disk; reload before applying") from None


def _atomic_write(path: pathlib.Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(path.parent, 0o700)
    except OSError:
        pass
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            delete=False,
        ) as handle:
            temporary = handle.name
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
    except OSError:
        if temporary is not None:
            try:
                os.unlink(temporary)
            except OSError:
                pass
        raise ClaudeDesktopConfigError("Claude Desktop configuration could not be saved") from None


def _is_http_url(value: str) -> bool:
    parsed = urlsplit(value)
    return parsed.scheme in {"http", "https"} and bool(parsed.netloc) and not parsed.username and not parsed.password


def _public_url(value: object) -> str | None:
    if not isinstance(value, str) or not _is_http_url(value):
        return None
    parsed = urlsplit(value)
    if parsed.fragment:
        return None
    for key, _ in parse_qsl(parsed.query, keep_blank_values=True):
        if any(marker in key.lower() for marker in _SENSITIVE_QUERY_MARKERS):
            return None
    return value


def _redact(value: object, key: object = "") -> object:
    key_text = str(key).lower()
    if any(marker in key_text for marker in _SECRET_MARKERS):
        if value in (None, "", False, [], {}):
            return value
        return _REDACTED
    if isinstance(value, Mapping):
        return {str(name): _redact(item, name) for name, item in value.items()}
    if isinstance(value, list):
        return [_redact(item) for item in value]
    return value


def _json_object(text: str, label: str) -> dict[str, Any]:
    try:
        value = json.loads(text) if text.strip() else {}
    except json.JSONDecodeError:
        raise ClaudeDesktopConfigError(f"Claude Desktop {label} is invalid JSON") from None
    if not isinstance(value, dict):
        raise ClaudeDesktopConfigError(f"Claude Desktop {label} must be a JSON object")
    return value


def _string(value: object, label: str, *, allow_empty: bool = False) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or "\n" in value or "\r" in value:
        raise ClaudeDesktopConfigError(f"{label} must be a single-line string")
    result = value.strip()
    if not result and not allow_empty:
        raise ClaudeDesktopConfigError(f"{label} must be a non-empty string")
    return result


def validate_config(config: Mapping[str, Any]) -> list[str]:
    errors: list[str] = []
    try:
        provider = config.get("inferenceProvider")
        if provider is not None:
            _string(provider, "inferenceProvider")
        base_url = config.get("inferenceGatewayBaseUrl")
        if base_url is not None:
            base_url = _string(base_url, "inferenceGatewayBaseUrl")
            if base_url and not _is_http_url(base_url):
                raise ClaudeDesktopConfigError("inferenceGatewayBaseUrl must be an http or https URL")
        auth_scheme = config.get("inferenceGatewayAuthScheme")
        if auth_scheme is not None and auth_scheme not in {"bearer", "x-api-key"}:
            raise ClaudeDesktopConfigError("inferenceGatewayAuthScheme must be bearer or x-api-key")
        credential_kind = config.get("inferenceCredentialKind")
        if credential_kind is not None:
            _string(credential_kind, "inferenceCredentialKind")
        if "inferenceGatewayApiKey" in config:
            _string(config.get("inferenceGatewayApiKey"), "inferenceGatewayApiKey", allow_empty=True)
        models = config.get("inferenceModels")
        if models is not None:
            if not isinstance(models, list):
                raise ClaudeDesktopConfigError("inferenceModels must be a list")
            for item in models:
                if isinstance(item, str):
                    _string(item, "inferenceModels entry")
                    continue
                if isinstance(item, Mapping):
                    if item.get("name") is None:
                        raise ClaudeDesktopConfigError(
                            "inferenceModels entry name must be a non-empty string"
                        )
                    _string(item.get("name"), "inferenceModels entry name")
                    continue
                raise ClaudeDesktopConfigError(
                    "inferenceModels entries must be model names or objects with a name"
                )
        headers = config.get("inferenceCustomHeaders")
        if headers is not None:
            if not isinstance(headers, Mapping) or any(
                not isinstance(name, str) or not isinstance(value, str) for name, value in headers.items()
            ):
                raise ClaudeDesktopConfigError("inferenceCustomHeaders must be an object of strings")
    except ClaudeDesktopConfigError as exc:
        errors.append(str(exc))
    return errors


def _model_names(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    names: list[str] = []
    for item in value:
        name = item if isinstance(item, str) else item.get("name") if isinstance(item, Mapping) else None
        if isinstance(name, str) and name.strip():
            names.append(name.strip())
    return names


class ClaudeDesktopConfig:
    """Draft and persistence boundary for Claude Desktop's active 3P JSON."""

    def __init__(self, library_path: pathlib.Path | str | None = None, *, loader: Callable[[pathlib.Path], tuple[str, bool]] | None = None):
        self.config_library_path = pathlib.Path(library_path).expanduser() if library_path else default_config_library_path()
        self.desktop_meta_path = self.config_library_path / CONFIG_META_FILENAME
        self.desktop_config_path: pathlib.Path | None = None
        self._loader = loader or _safe_read
        self._meta: dict[str, Any] = {}
        self._meta_draft: dict[str, Any] = {}
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._exists = False
        self._baseline_meta_bytes: bytes | None = None
        self._baseline_config_bytes: bytes | None = None
        self.reload()

    @staticmethod
    def _config_id(value: object) -> str | None:
        return value if isinstance(value, str) and _ID_PATTERN.fullmatch(value) else None

    def _load_from_disk(self) -> None:
        meta_text, meta_exists = self._loader(self.desktop_meta_path)
        meta = _json_object(meta_text, "configuration metadata") if meta_exists else {}
        applied_id = self._config_id(meta.get("appliedId"))
        if meta.get("appliedId") not in (None, "") and applied_id is None:
            raise ClaudeDesktopConfigError("Claude Desktop configuration metadata has an invalid appliedId")
        config_path = self.config_library_path / f"{applied_id}.json" if applied_id else None
        config_text, config_exists = self._loader(config_path) if config_path is not None else ("{}\n", False)
        config = _json_object(config_text, "configuration") if config_exists else {}
        errors = validate_config(config)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        self.desktop_config_path = config_path
        self._meta = copy.deepcopy(meta)
        self._meta_draft = copy.deepcopy(meta)
        self._raw = copy.deepcopy(config)
        self._draft = copy.deepcopy(config)
        self._exists = config_exists
        self._baseline_meta_bytes = meta_text.encode("utf-8") if meta_exists else None
        self._baseline_config_bytes = config_text.encode("utf-8") if config_exists else None

    def reload(self) -> dict[str, Any]:
        self._load_from_disk()
        return self.snapshot()

    def reset_draft(self) -> None:
        self._meta_draft = copy.deepcopy(self._meta)
        self._draft = copy.deepcopy(self._raw)

    def is_dirty(self) -> bool:
        return self._meta_draft != self._meta or self._draft != self._raw

    def draft_state(self) -> dict[str, Any]:
        return {"meta": copy.deepcopy(self._meta_draft), "config": copy.deepcopy(self._draft)}

    def persistence_paths(self) -> tuple[pathlib.Path, ...]:
        paths = [self.desktop_meta_path]
        if self.desktop_config_path is not None:
            paths.append(self.desktop_config_path)
        return tuple(paths)

    def external_disk_state(self) -> dict[str, bool]:
        meta_bytes = _current_bytes(self.desktop_meta_path)
        config_bytes = _current_bytes(self.desktop_config_path) if self.desktop_config_path is not None else None
        changed = meta_bytes != self._baseline_meta_bytes or config_bytes != self._baseline_config_bytes
        return {"changed": changed, "exists": config_bytes is not None}

    def external_disk_identity(self) -> str:
        meta_bytes = _current_bytes(self.desktop_meta_path)
        config_bytes = _current_bytes(self.desktop_config_path) if self.desktop_config_path is not None else None
        digest = hashlib.sha256()
        for value in (meta_bytes, config_bytes):
            digest.update(b"missing\0" if value is None else b"present\0" + value)
        return digest.hexdigest()

    def rebase_external_disk(self) -> None:
        draft = copy.deepcopy(self._draft)
        self._load_from_disk()
        self._draft = draft
        if self._draft != self._raw and self.desktop_config_path is None:
            self._ensure_target()

    def snapshot(self) -> dict[str, Any]:
        provider = self._draft.get("inferenceProvider")
        base_url = self._draft.get("inferenceGatewayBaseUrl")
        auth_scheme = self._draft.get("inferenceGatewayAuthScheme", "bearer")
        return {
            "available": True,
            "config_exists": self._exists,
            "provider": provider if isinstance(provider, str) else None,
            "gateway_configured": bool(base_url),
            "gateway_url": _public_url(base_url),
            "auth_scheme": auth_scheme if isinstance(auth_scheme, str) else "bearer",
            "credential_kind": self._draft.get("inferenceCredentialKind") if isinstance(self._draft.get("inferenceCredentialKind"), str) else None,
            "credential_configured": bool(self._draft.get("inferenceGatewayApiKey")),
            "models_configured": bool(self._draft.get("inferenceModels")),
            "model_names": _model_names(self._draft.get("inferenceModels")),
        }

    def raw_text(self, *, include_sensitive: bool = False) -> str:
        value = self._draft if include_sensitive else _redact(self._draft)
        return json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def set_raw_text(self, text: str) -> dict[str, Any]:
        candidate = _json_object(text, "configuration")
        errors = validate_config(candidate)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        if candidate != self._draft:
            self._ensure_target()
        self._draft = candidate
        return self.snapshot()

    def validate(self) -> dict[str, Any]:
        errors = validate_config(self._draft)
        return {"valid": not errors, "errors": errors}

    def _ensure_target(self) -> None:
        if self.desktop_config_path is not None:
            return
        config_id = str(uuid.uuid4())
        self.desktop_config_path = self.config_library_path / f"{config_id}.json"
        entries = self._meta_draft.get("entries")
        if not isinstance(entries, list):
            entries = []
        entries = [copy.deepcopy(item) for item in entries if isinstance(item, Mapping)]
        entries.append({"id": config_id, "name": "Default"})
        self._meta_draft = {**self._meta_draft, "appliedId": config_id, "entries": entries}

    def patch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        allowed = {"inferenceProvider", "inferenceGatewayBaseUrl", "inferenceGatewayAuthScheme", "inferenceCredentialKind", "inferenceModels", "inferenceCustomHeaders"}
        if set(payload).difference(allowed):
            raise ClaudeDesktopConfigError("Unknown Claude Desktop configuration field")
        candidate = copy.deepcopy(self._draft)
        for key, value in payload.items():
            if value is None or value == "":
                candidate.pop(key, None)
            else:
                candidate[key] = copy.deepcopy(value)
        errors = validate_config(candidate)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        # A no-op patch must not manufacture a new configuration file.  This
        # matters on first launch where the library can legitimately be empty.
        if candidate != self._draft:
            self._ensure_target()
        self._draft = candidate
        return self.snapshot()

    def set_model_names(self, value: object) -> dict[str, Any]:
        if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
            raise ClaudeDesktopConfigError("Claude Desktop model names must be a list of strings")
        names = [_string(item, "Claude Desktop model name") for item in value]
        normalized = [name for name in names if name is not None]

        existing_by_name: dict[str, object] = {}
        existing = self._draft.get("inferenceModels")
        if isinstance(existing, list):
            for item in existing:
                item_names = _model_names([item])
                if item_names and item_names[0] not in existing_by_name:
                    existing_by_name[item_names[0]] = copy.deepcopy(item)

        next_entries: list[object] = []
        for name in normalized:
            existing_entry = existing_by_name.get(name)
            if isinstance(existing_entry, Mapping):
                next_entries.append({**copy.deepcopy(dict(existing_entry)), "name": name})
            elif isinstance(existing_entry, str):
                next_entries.append(name)
            else:
                next_entries.append({"name": name})

        candidate = copy.deepcopy(self._draft)
        if next_entries:
            candidate["inferenceModels"] = next_entries
        else:
            candidate.pop("inferenceModels", None)
        errors = validate_config(candidate)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        if candidate != self._draft:
            self._ensure_target()
        self._draft = candidate
        return self.snapshot()

    def secret_present(self, field: str) -> bool:
        if field != "desktop_gateway_api_key":
            raise ClaudeDesktopConfigError("The requested secret field is unavailable")
        return bool(self._draft.get("inferenceGatewayApiKey"))

    def stage_secret(self, field: str, value: str) -> None:
        if field != "desktop_gateway_api_key":
            raise ClaudeDesktopConfigError("The requested secret field is unavailable")
        if bool(value) != self.secret_present(field):
            self._ensure_target()
        if value:
            self._draft["inferenceGatewayApiKey"] = value
        else:
            self._draft.pop("inferenceGatewayApiKey", None)
        errors = validate_config(self._draft)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])

    def secret_value(self, field: str) -> str:
        if field != "desktop_gateway_api_key":
            raise ClaudeDesktopConfigError("The requested secret field is unavailable")
        value = self._draft.get("inferenceGatewayApiKey", "")
        if not isinstance(value, str):
            raise ClaudeDesktopConfigError("The requested secret field is unavailable")
        return value

    def apply(self) -> None:
        if not self.is_dirty():
            return
        if _current_bytes(self.desktop_meta_path) != self._baseline_meta_bytes:
            raise ClaudeDesktopConfigError("Claude Desktop configuration changed on disk; reload before applying")
        if self.desktop_config_path is not None and _current_bytes(self.desktop_config_path) != self._baseline_config_bytes:
            raise ClaudeDesktopConfigError("Claude Desktop configuration changed on disk; reload before applying")
        errors = validate_config(self._draft)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        if self.desktop_config_path is None:
            self._ensure_target()
        config_text = json.dumps(self._draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        meta_text = json.dumps(self._meta_draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
        if self.desktop_config_path is None:
            raise ClaudeDesktopConfigError("Claude Desktop configuration target is unavailable")
        _atomic_write(self.desktop_config_path, config_text)
        meta_written = self._meta_draft != self._meta or self._baseline_meta_bytes is None
        if meta_written:
            _atomic_write(self.desktop_meta_path, meta_text)
        self._raw = copy.deepcopy(self._draft)
        self._meta = copy.deepcopy(self._meta_draft)
        self._exists = True
        self._baseline_config_bytes = _current_bytes(self.desktop_config_path)
        self._baseline_meta_bytes = _current_bytes(self.desktop_meta_path) if meta_written else self._baseline_meta_bytes


class ClaudeDeveloperSettings:
    """Claude Desktop's own developer-mode settings file.

    Claude Desktop reads ``allowDevTools`` from ``developer_settings.json`` in
    Electron's user-data directory.  This is a separate source from both the
    third-party inference profile and Claude Code's settings.json.
    """

    def __init__(
        self,
        path: pathlib.Path | str | None = None,
        *,
        loader: Callable[[pathlib.Path], tuple[str, bool]] | None = None,
    ):
        self.path = pathlib.Path(path).expanduser() if path else default_developer_settings_path()
        self._loader = loader or _safe_read
        self._raw: dict[str, Any] = {}
        self._draft: dict[str, Any] = {}
        self._exists = False
        self._baseline_bytes: bytes | None = None
        self.reload()

    @staticmethod
    def _validate(value: Mapping[str, Any]) -> list[str]:
        allow_dev_tools = value.get("allowDevTools")
        if allow_dev_tools is not None and not isinstance(allow_dev_tools, bool):
            return ["allowDevTools must be true or false"]
        return []

    def reload(self) -> dict[str, Any]:
        text, exists = self._loader(self.path)
        value = _json_object(text, "developer settings") if exists else {}
        errors = self._validate(value)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        self._raw = copy.deepcopy(value)
        self._draft = copy.deepcopy(value)
        self._exists = exists
        self._baseline_bytes = text.encode("utf-8") if exists else None
        return self.snapshot()

    def snapshot(self) -> dict[str, Any]:
        return {
            "available": True,
            "file_exists": self._exists,
            # Claude Desktop itself uses ``allowDevTools ?? false``.
            "developer_mode_enabled": self._draft.get("allowDevTools") is True,
        }

    def draft_state(self) -> dict[str, Any]:
        return copy.deepcopy(self._draft)

    def reset_draft(self) -> None:
        self._draft = copy.deepcopy(self._raw)

    def is_dirty(self) -> bool:
        return self._draft != self._raw

    def persistence_paths(self) -> tuple[pathlib.Path, ...]:
        return (self.path,)

    def external_disk_state(self) -> dict[str, bool]:
        current = _current_bytes(self.path)
        return {"changed": current != self._baseline_bytes, "exists": current is not None}

    def external_disk_identity(self) -> str:
        current = _current_bytes(self.path)
        return "missing" if current is None else hashlib.sha256(b"present\0" + current).hexdigest()

    def rebase_external_disk(self) -> None:
        self._baseline_bytes = _current_bytes(self.path)

    def raw_text(self) -> str:
        return json.dumps(self._draft, ensure_ascii=False, indent=2, sort_keys=True) + "\n"

    def set_raw_text(self, text: str) -> dict[str, Any]:
        value = _json_object(text, "developer settings")
        errors = self._validate(value)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        self._draft = value
        return self.snapshot()

    def patch(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        if set(payload) != {"allowDevTools"} or not isinstance(payload.get("allowDevTools"), bool):
            raise ClaudeDesktopConfigError("allowDevTools must be true or false")
        self._draft["allowDevTools"] = payload["allowDevTools"]
        return self.snapshot()

    def validate(self) -> dict[str, Any]:
        errors = self._validate(self._draft)
        return {"valid": not errors, "errors": errors}

    def apply(self) -> None:
        if not self.is_dirty():
            return
        if _current_bytes(self.path) != self._baseline_bytes:
            raise ClaudeDesktopConfigError("Claude Desktop developer settings changed on disk; reload before applying")
        errors = self._validate(self._draft)
        if errors:
            raise ClaudeDesktopConfigError(errors[0])
        text = self.raw_text()
        _atomic_write(self.path, text)
        self._raw = copy.deepcopy(self._draft)
        self._exists = True
        self._baseline_bytes = text.encode("utf-8")


__all__ = [
    "ClaudeDesktopConfig",
    "ClaudeDesktopConfigError",
    "ClaudeDeveloperSettings",
    "default_config_library_path",
    "default_developer_settings_path",
]
