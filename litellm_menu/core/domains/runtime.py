"""Runtime staged settings domain."""

from __future__ import annotations

import base64
import copy
import hashlib
import json
from pathlib import Path
import tempfile
from collections.abc import Mapping
from typing import Any

from ..persistence import PersistenceError, atomic_write_text
from ..security import REDACTED, safe_exception_message
from ._shared import (
    DomainError,
    _action_name,
    _default_runtime_settings_path,
    _file_bytes,
    _mapping,
    _safe_problem,
    _same_file,
)


_DSH_VISION_ROUTER_CONFIG_KEY = "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON"
_DSH_VISION_ROUTER_QUICK_KEYS = {
    "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED": "enabled",
    "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND": "backend",
    "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK": "freeFallback",
    "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS": "timeoutSeconds",
    "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS": "maxTokens",
}
_DSH_VISION_ROUTER_LOCAL_QUICK_KEYS = {
    "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED": "localOllama",
    "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED": "localLmStudio",
}
_DSH_VISION_ROUTER_ALL_QUICK_KEYS = tuple(
    (*_DSH_VISION_ROUTER_QUICK_KEYS, *_DSH_VISION_ROUTER_LOCAL_QUICK_KEYS)
)
_DSH_VISION_ROUTER_DEFAULTS = {
    "LITELLM_MENU_DSH_VISION_ROUTER_ENABLED": "on",
    "LITELLM_MENU_DSH_VISION_ROUTER_BACKEND": "auto",
    "LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK": "on",
    "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS": "45",
    "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS": "4096",
    "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_OLLAMA_ENABLED": "off",
    "LITELLM_MENU_DSH_VISION_ROUTER_LOCAL_LM_STUDIO_ENABLED": "off",
}
_DSH_MISSING = object()


def _dsh_json_payload(raw: object) -> dict[str, Any]:
    if not isinstance(raw, str):
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return copy.deepcopy(parsed) if isinstance(parsed, dict) else {}


def _dsh_json_text(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), allow_nan=False)


def _dsh_effective_quick_values(values: Mapping[str, str]) -> dict[str, str]:
    """Project the JSON document into the compact controls above it.

    The quick controls and the editor intentionally share one source of truth.
    The legacy ``inherit`` values remain in the staged storage shape only so an
    older settings file can be opened and rewritten safely; they are never
    exposed as the current UI value.
    """

    payload = _dsh_json_payload(values.get(_DSH_VISION_ROUTER_CONFIG_KEY, ""))
    projected = dict(values)
    enabled = payload.get("enabled", True)
    free_fallback = payload.get("freeFallback", True)
    backend = payload.get("backend", "auto")
    if not isinstance(backend, str) or backend not in {"auto", "local", "api", "off"}:
        backend = "auto"
    projected["LITELLM_MENU_DSH_VISION_ROUTER_ENABLED"] = "on" if enabled is not False else "off"
    projected["LITELLM_MENU_DSH_VISION_ROUTER_BACKEND"] = backend
    projected["LITELLM_MENU_DSH_VISION_ROUTER_FREE_FALLBACK"] = "on" if free_fallback is not False else "off"
    for key, json_key in _DSH_VISION_ROUTER_QUICK_KEYS.items():
        if json_key not in {"timeoutSeconds", "maxTokens"}:
            continue
        value = payload.get(json_key, int(_DSH_VISION_ROUTER_DEFAULTS[key]))
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            numeric = int(_DSH_VISION_ROUTER_DEFAULTS[key])
        projected[key] = str(numeric)
    for key, json_key in _DSH_VISION_ROUTER_LOCAL_QUICK_KEYS.items():
        local = payload.get(json_key)
        projected[key] = "on" if isinstance(local, dict) and local.get("enabled") is True else "off"
    return projected


def _dsh_set_json_value(payload: dict[str, Any], key: str, value: str) -> None:
    json_key = _DSH_VISION_ROUTER_QUICK_KEYS.get(key) or _DSH_VISION_ROUTER_LOCAL_QUICK_KEYS.get(key)
    if json_key is None:
        return
    if key in _DSH_VISION_ROUTER_LOCAL_QUICK_KEYS:
        local = payload.get(json_key)
        if not isinstance(local, dict):
            local = {}
        local["enabled"] = value == "on"
        payload[json_key] = local
        return
    if json_key in {"enabled", "freeFallback"}:
        payload[json_key] = value == "on"
    elif json_key in {"timeoutSeconds", "maxTokens"}:
        if value.strip():
            payload[json_key] = int(value)
        else:
            payload.pop(json_key, None)
    else:
        payload[json_key] = value


class RuntimeSettingsDomain:
    """Staged runtime settings backed by ``runtime_settings_io`` validation."""

    name = "runtime"
    _SECRET_KEYS = frozenset({
        "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON",
        "LITELLM_MENU_PI_WEB_ACCESS_CONFIG_JSON",
    })

    def __init__(self, settings_path: Path | str | None = None):
        self.settings_path = Path(settings_path).expanduser() if settings_path else _default_runtime_settings_path()
        self.specs: dict[str, Any] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._raw_values: dict[str, str] = {}
        self._draft_values: dict[str, str] = {}
        # Only used while an older staged session still has an explicit quick
        # override. New edits always write the shared JSON document directly.
        self._dsh_legacy_bases: dict[str, tuple[bool, object]] = {}
        self._baseline_bytes: bytes | None = None
        self.revision = 0
        self.reload()

    @staticmethod
    def _defaults(specs: Mapping[str, Any]) -> dict[str, str]:
        from runtime_settings_io import normalize_payload_value

        return {key: normalize_payload_value(spec, spec.default) for key, spec in specs.items()}

    def _load(self) -> tuple[dict[str, Any], dict[str, str], bytes | None]:
        from runtime_settings_io import load_specs, read_settings_file

        from ..runtime_settings_schema import runtime_settings_metadata

        try:
            specs = load_specs()
            metadata_items = runtime_settings_metadata()
            metadata = {
                str(item["key"]): copy.deepcopy(item)
                for item in metadata_items
                if isinstance(item, Mapping) and isinstance(item.get("key"), str)
            }
            baseline = _file_bytes(self.settings_path)
            values = read_settings_file(self.settings_path, specs)
            values = self._canonicalize_dsh_values(values)
        except DomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings could not be loaded") from None
        self._metadata = metadata
        return specs, values, baseline

    @staticmethod
    def _canonicalize_dsh_values(values: Mapping[str, str]) -> dict[str, str]:
        """Fold legacy quick overrides into the persisted JSON document."""

        result = dict(values)
        payload = _dsh_json_payload(result.get(_DSH_VISION_ROUTER_CONFIG_KEY, ""))
        changed = False
        for key in _DSH_VISION_ROUTER_ALL_QUICK_KEYS:
            raw = result.get(key, "inherit")
            if raw not in {"", "inherit"}:
                _dsh_set_json_value(payload, key, raw)
                changed = True
        if changed:
            result[_DSH_VISION_ROUTER_CONFIG_KEY] = _dsh_json_text(payload)
        for key in _DSH_VISION_ROUTER_ALL_QUICK_KEYS:
            result[key] = "" if key in {"LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS"} else "inherit"
        return result

    def _field_projection(self, key: str, spec: Any, value: str) -> dict[str, Any]:
        default = self._defaults({key: spec})[key]
        # dsh quick fields are projections of the JSON document. Expose the
        # effective JSON defaults to the UI instead of the legacy storage
        # marker (``inherit``/empty), so the field metadata has the same
        # meaning as the displayed value.
        display_default = _DSH_VISION_ROUTER_DEFAULTS.get(key, default)
        metadata = self._metadata.get(key, {})
        is_secret = key in self._SECRET_KEYS or metadata.get("secret") is True
        configured = value != display_default
        baseline_value = self._raw_values.get(key, default)
        ui_kind = {
            "bool": "toggle",
            "bool_auto": "toggle",
            "enum": "choice",
            "int": "integer",
            "optional_int": "integer",
            "float": "number",
            "optional_float": "number",
            "mb": "number",
            "optional_mb": "number",
            "string": "text",
        }.get(spec.kind, spec.kind)
        options = list(spec.options)
        if spec.kind == "bool_auto" and not options:
            options = ["auto", "off"]
        if key in _DSH_VISION_ROUTER_ALL_QUICK_KEYS:
            options = [option for option in options if option != "inherit"]
        return {
            "id": key,
            "key": key,
            "kind": ui_kind,
            "storage_kind": spec.kind,
            "category": str(metadata.get("category", "Runtime")),
            "label": str(metadata.get("label", key)),
            "unit": str(metadata.get("unit", "")),
            "help": str(metadata.get("help", "")),
            "default": display_default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "options": options,
            "value": REDACTED if is_secret and configured else value,
            "configured": configured,
            "retained": is_secret and baseline_value != default,
            "will_clear": is_secret and baseline_value != default and value == default,
            "secret": is_secret,
            "retain_existing": str(metadata.get("retain_existing", "")),
        }

    def _is_secret_setting(self, key: str) -> bool:
        return key in self._SECRET_KEYS or self._metadata.get(key, {}).get("secret") is True

    def snapshot(self) -> dict[str, Any]:
        projected = _dsh_effective_quick_values(self._draft_values)
        fields = [self._field_projection(key, spec, projected.get(key, "")) for key, spec in self.specs.items()]
        defaults = self._defaults(self.specs)
        values = {
            key: (REDACTED if key in self._SECRET_KEYS and value != defaults.get(key, "") else value)
            for key, value in projected.items()
        }
        return {
            "domain": self.name,
            "revision": self.revision,
            "fields": fields,
            "settings": fields,
            "values": values,
            "raw_editor_available": True,
        }

    def draft_state(self) -> object:
        return copy.deepcopy(self._draft_values)

    def _validate_values(self, values: Mapping[str, object]) -> dict[str, str]:
        from runtime_settings_io import validate_values

        try:
            return validate_values(dict(values), self.specs)
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings are invalid") from None

    def _set_values(self, value: object) -> None:
        data = _mapping(value, "runtime values")
        values = data.get("values", data)
        updates = _mapping(values, "runtime values")
        draft = copy.deepcopy(self._draft_values)
        quick_updates: dict[str, str] = {}
        if _DSH_VISION_ROUTER_CONFIG_KEY in updates:
            self._dsh_legacy_bases.clear()
        for key, item in updates.items():
            if key not in self.specs:
                raise DomainError("Runtime settings contain an unsupported field")
            if item == "__LITELLM_MENU_RETAIN_EXISTING__":
                if key not in self._SECRET_KEYS:
                    raise DomainError("Runtime settings are invalid")
                continue
            if not isinstance(item, str):
                raise DomainError("Runtime setting values must be text")
            if key in _DSH_VISION_ROUTER_ALL_QUICK_KEYS:
                quick_updates[key] = item
            else:
                draft[key] = item
        if quick_updates:
            payload = _dsh_json_payload(draft.get(_DSH_VISION_ROUTER_CONFIG_KEY, ""))
            from runtime_settings_io import normalize_payload_value

            for key, item in quick_updates.items():
                legacy_inherit = item == "inherit" or (
                    item == ""
                    and key in {
                        "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
                        "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS",
                    }
                )
                if legacy_inherit:
                    previous = self._dsh_legacy_bases.pop(key, None)
                    if previous is None:
                        continue
                    json_key = _DSH_VISION_ROUTER_QUICK_KEYS.get(key) or _DSH_VISION_ROUTER_LOCAL_QUICK_KEYS.get(key)
                    if json_key is None:
                        continue
                    present, previous_value = previous
                    if not present:
                        payload.pop(json_key, None)
                    else:
                        payload[json_key] = copy.deepcopy(previous_value)
                    continue
                try:
                    item = normalize_payload_value(self.specs[key], item)
                except Exception as exc:
                    raise _safe_problem(exc, "Runtime settings are invalid") from None
                json_key = _DSH_VISION_ROUTER_QUICK_KEYS.get(key) or _DSH_VISION_ROUTER_LOCAL_QUICK_KEYS.get(key)
                if json_key is not None and key not in self._dsh_legacy_bases:
                    local = payload.get(json_key, _DSH_MISSING)
                    self._dsh_legacy_bases[key] = (local is not _DSH_MISSING, copy.deepcopy(local))
                _dsh_set_json_value(payload, key, item)
            draft[_DSH_VISION_ROUTER_CONFIG_KEY] = _dsh_json_text(payload)
            # Quick fields are a projection of JSON, never a second persisted
            # override layer. ``inherit`` is retained only for old files and
            # old IPC callers; the UI does not offer it.
            for key in quick_updates:
                draft[key] = "" if key in {"LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS", "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS"} else "inherit"
        self._draft_values = self._validate_values(draft)

    def _set_setting(self, data: Mapping[str, Any]) -> None:
        key = data.get("key")
        if not isinstance(key, str) or key not in self.specs:
            raise DomainError("Runtime settings contain an unsupported field")
        value = data.get("value")
        if isinstance(value, bool):
            if self.specs[key].kind == "bool_auto":
                value = "auto" if value else "off"
            elif self.specs[key].kind == "bool":
                value = "1" if value else "0"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise DomainError("Runtime setting values must be text")
        self._set_values({"values": {key: value}})

    def _clear_setting(self, data: Mapping[str, Any]) -> None:
        key = data.get("key")
        if not isinstance(key, str) or key not in self.specs:
            raise DomainError("Runtime settings contain an unsupported field")
        if key in _DSH_VISION_ROUTER_ALL_QUICK_KEYS:
            payload = _dsh_json_payload(self._draft_values.get(_DSH_VISION_ROUTER_CONFIG_KEY, ""))
            json_key = _DSH_VISION_ROUTER_QUICK_KEYS.get(key) or _DSH_VISION_ROUTER_LOCAL_QUICK_KEYS.get(key)
            if json_key is not None:
                if key in {
                    "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
                    "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS",
                }:
                    payload.pop(json_key, None)
                else:
                    _dsh_set_json_value(payload, key, _DSH_VISION_ROUTER_DEFAULTS[key])
                draft = copy.deepcopy(self._draft_values)
                draft[_DSH_VISION_ROUTER_CONFIG_KEY] = _dsh_json_text(payload)
                draft[key] = "" if key in {
                    "LITELLM_MENU_DSH_VISION_ROUTER_TIMEOUT_SECONDS",
                    "LITELLM_MENU_DSH_VISION_ROUTER_MAX_TOKENS",
                } else "inherit"
                self._dsh_legacy_bases.pop(key, None)
                self._draft_values = self._validate_values(draft)
                return
        self._draft_values[key] = self._defaults({key: self.specs[key]})[key]

    def _set_raw(self, data: Mapping[str, Any]) -> None:
        source = data.get("raw_text", data.get("text", data.get("settings_text")))
        if not isinstance(source, str):
            raise DomainError("Runtime settings text must be text")
        try:
            with tempfile.TemporaryDirectory(prefix="litellm-core-runtime-validate-") as directory:
                path = Path(directory) / "runtime-settings.env"
                atomic_write_text(path, source)
                from runtime_settings_io import read_settings_file

                values = read_settings_file(path, self.specs)
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings are invalid") from None
        self._dsh_legacy_bases.clear()
        self._draft_values = self._canonicalize_dsh_values(values)

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        name = _action_name(action)
        data = _mapping(payload or {})
        if name in {"set", "patch", "replace"}:
            self._set_values(data)
        elif name in {"set_setting", "setsetting"}:
            self._set_setting(data)
        elif name in {"clear_setting", "clearsetting"}:
            self._clear_setting(data)
        elif name in {"set_raw", "setraw"}:
            self._set_raw(data)
        elif name in {"restore_defaults", "defaults"}:
            self._dsh_legacy_bases.clear()
            self._draft_values = self._defaults(self.specs)
        elif name in {"reset", "cancel", "reload"}:
            self._dsh_legacy_bases.clear()
            self._draft_values = copy.deepcopy(self._raw_values)
        else:
            raise DomainError("The requested runtime action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "setting" or not isinstance(target, str) or target not in self.specs:
            raise DomainError("The requested secret field is unavailable")
        if not self._is_secret_setting(target):
            raise DomainError("The requested secret field is unavailable")
        default = self._defaults({target: self.specs[target]})[target]
        return self._draft_values.get(target, default) != default

    def trusted_secret_value(self, field: str, target: str | None = None) -> str:
        """Return the one runtime JSON document authorized for native editing."""

        if field != "setting" or target not in {
            "LITELLM_MENU_PI_WEB_ACCESS_CONFIG_JSON",
            "LITELLM_MENU_DSH_VISION_ROUTER_CONFIG_JSON",
        }:
            raise DomainError("The requested secret field is unavailable")
        if target not in self.specs or not self._is_secret_setting(target):
            raise DomainError("The requested secret field is unavailable")
        default = self._defaults({target: self.specs[target]})[target]
        value = self._draft_values.get(target, default)
        if not isinstance(value, str):
            raise DomainError("The requested secret field is unavailable")
        return value

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "setting" or not isinstance(target, str) or target not in self.specs:
            raise DomainError("The requested secret field is unavailable")
        if not self._is_secret_setting(target):
            raise DomainError("The requested secret field is unavailable")
        self._set_values({"values": {target: value}})
        self.revision += 1

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        try:
            values = self._draft_values
            if payload is not None:
                data = _mapping(payload)
                values = copy.deepcopy(values)
                updates = _mapping(data.get("values", data), "runtime values")
                values.update(updates)
            self._validate_values(values)
        except DomainError:
            return {"valid": False, "errors": ["Runtime settings are invalid"]}
        return {"valid": True, "errors": []}

    @staticmethod
    def _stored_value(spec: Any, value: str) -> str:
        if spec.kind not in {"mb", "optional_mb"}:
            if spec.kind == "json":
                encoded = base64.urlsafe_b64encode(value.encode("utf-8")).decode("ascii").rstrip("=")
                return f"base64:{encoded}"
            return value
        if spec.kind == "optional_mb" and not value.strip():
            return ""
        try:
            bytes_value = round(float(value) * 1024 * 1024)
        except (TypeError, ValueError, OverflowError):
            raise DomainError("Runtime settings are invalid") from None
        return str(bytes_value)

    def _encoded_draft(self) -> str:
        normalized = self._validate_values(self._draft_values)
        defaults = self._defaults(self.specs)
        lines = ["# LiteLLM Menu runtime thresholds. Generated by Python Core."]
        for key, spec in self.specs.items():
            if normalized[key] == defaults[key]:
                continue
            lines.append(f"{key}={self._stored_value(spec, normalized[key])}")
        return "\n".join(lines) + "\n"

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        if payload is not None:
            data = _mapping(payload)
            if "values" in data:
                self._set_values(data)
        validation = self.validate()
        if not validation["valid"]:
            raise DomainError("Runtime settings are invalid")
        if not _same_file(self.settings_path, self._baseline_bytes):
            raise DomainError("Runtime settings changed on disk; reload before applying")
        try:
            atomic_write_text(self.settings_path, self._encoded_draft())
        except PersistenceError as exc:
            raise DomainError(safe_exception_message(exc)) from None
        self.reload()
        return {"applied": True, **self.snapshot()}

    def external_disk_state(self) -> dict[str, bool]:
        """Compare the settings source with the last load/apply baseline."""

        current = _file_bytes(self.settings_path)
        return {"changed": current != self._baseline_bytes, "exists": current is not None}

    def external_disk_identity(self) -> str:
        """Return a bounded opaque identity for Core's conflict tracker."""

        current = _file_bytes(self.settings_path)
        if current is None:
            return "missing"
        return hashlib.sha256(b"present\\0" + current).hexdigest()

    def rebase_external_disk(self) -> dict[str, Any]:
        """Accept current disk bytes as Apply's baseline without losing the draft."""

        self._baseline_bytes = _file_bytes(self.settings_path)
        self.revision += 1
        return self.snapshot()

    def reload(self) -> dict[str, Any]:
        specs, values, baseline = self._load()
        self.specs = specs
        self._raw_values = copy.deepcopy(values)
        self._draft_values = copy.deepcopy(values)
        self._baseline_bytes = baseline
        self.revision += 1
        return self.snapshot()

    def export(self, *, include_sensitive: bool = False) -> dict[str, Any]:
        if include_sensitive:
            return {"domain": self.name, "values": copy.deepcopy(self._draft_values)}
        return self.snapshot()

    def import_package(self, payload: object) -> None:
        data = _mapping(payload, "runtime package")
        self._set_values(data.get("values", data))
        self.revision += 1

    def probe(self, _payload: Mapping[str, Any] | None = None) -> dict[str, Any]:
        validation = self.validate()
        return {"ok": bool(validation["valid"]), "protocols": [], "detail": "Runtime validation completed" if validation["valid"] else "Runtime validation failed"}

__all__ = ["RuntimeSettingsDomain"]
