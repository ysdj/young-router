"""Runtime staged settings domain."""

from __future__ import annotations

import copy
import hashlib
from pathlib import Path
import tempfile
from collections.abc import Mapping
from typing import Any

from ..persistence import PersistenceError, atomic_write_text
from ..security import REDACTED, safe_exception_message
from ._shared import (
    LegacyDomainError,
    _action_name,
    _default_runtime_settings_path,
    _file_bytes,
    _mapping,
    _safe_problem,
    _same_file,
)

class RuntimeSettingsDomain:
    """Staged runtime settings backed by ``runtime_settings_io`` validation."""

    name = "runtime"
    _SECRET_KEYS = frozenset({"LITELLM_MENU_VISION_BRIDGE_API_KEY"})

    def __init__(self, settings_path: Path | str | None = None):
        self.settings_path = Path(settings_path).expanduser() if settings_path else _default_runtime_settings_path()
        self.specs: dict[str, Any] = {}
        self._metadata: dict[str, dict[str, Any]] = {}
        self._raw_values: dict[str, str] = {}
        self._draft_values: dict[str, str] = {}
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
        except LegacyDomainError:
            raise
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings could not be loaded") from None
        self._metadata = metadata
        return specs, values, baseline

    def _field_projection(self, key: str, spec: Any, value: str) -> dict[str, Any]:
        default = self._defaults({key: spec})[key]
        metadata = self._metadata.get(key, {})
        is_secret = key in self._SECRET_KEYS or metadata.get("secret") is True
        baseline_value = self._raw_values.get(key, default)
        ui_kind = {
            "bool": "toggle",
            "bool_auto": "toggle",
            "enum": "choice",
            "int": "integer",
            "float": "number",
            "mb": "number",
            "string": "text",
        }.get(spec.kind, spec.kind)
        options = list(spec.options)
        if spec.kind == "bool_auto" and not options:
            options = ["auto", "off"]
        return {
            "id": key,
            "key": key,
            "kind": ui_kind,
            "storage_kind": spec.kind,
            "category": str(metadata.get("category", "Runtime")),
            "label": str(metadata.get("label", key)),
            "unit": str(metadata.get("unit", "")),
            "help": str(metadata.get("help", "")),
            "default": spec.default,
            "minimum": spec.minimum,
            "maximum": spec.maximum,
            "options": options,
            "value": REDACTED if is_secret and value else value,
            "configured": value != default,
            "retained": is_secret and baseline_value != default,
            "will_clear": is_secret and baseline_value != default and value == default,
            "secret": is_secret,
            "retain_existing": str(metadata.get("retain_existing", "")),
        }

    def _is_secret_setting(self, key: str) -> bool:
        return key in self._SECRET_KEYS or self._metadata.get(key, {}).get("secret") is True

    def snapshot(self) -> dict[str, Any]:
        fields = [self._field_projection(key, spec, self._draft_values.get(key, "")) for key, spec in self.specs.items()]
        values = {
            key: (REDACTED if key in self._SECRET_KEYS and value else value)
            for key, value in self._draft_values.items()
        }
        return {
            "domain": self.name,
            "revision": self.revision,
            "fields": fields,
            "settings": fields,
            "values": values,
            "raw_editor_available": True,
        }

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
        for key, item in updates.items():
            if key not in self.specs:
                raise LegacyDomainError("Runtime settings contain an unsupported field")
            if item == "__LITELLM_MENU_RETAIN_EXISTING__":
                if key not in self._SECRET_KEYS:
                    raise LegacyDomainError("Runtime settings are invalid")
                continue
            if not isinstance(item, str):
                raise LegacyDomainError("Runtime setting values must be text")
            draft[key] = item
        self._draft_values = self._validate_values(draft)

    def _set_setting(self, data: Mapping[str, Any]) -> None:
        key = data.get("key")
        if not isinstance(key, str) or key not in self.specs:
            raise LegacyDomainError("Runtime settings contain an unsupported field")
        value = data.get("value")
        if isinstance(value, bool):
            if self.specs[key].kind == "bool_auto":
                value = "auto" if value else "off"
            elif self.specs[key].kind == "bool":
                value = "1" if value else "0"
        elif isinstance(value, (int, float)) and not isinstance(value, bool):
            value = str(value)
        if not isinstance(value, str):
            raise LegacyDomainError("Runtime setting values must be text")
        self._set_values({"values": {key: value}})

    def _clear_setting(self, data: Mapping[str, Any]) -> None:
        key = data.get("key")
        if not isinstance(key, str) or key not in self.specs:
            raise LegacyDomainError("Runtime settings contain an unsupported field")
        self._draft_values[key] = self._defaults({key: self.specs[key]})[key]

    def _set_raw(self, data: Mapping[str, Any]) -> None:
        source = data.get("raw_text", data.get("text", data.get("settings_text")))
        if not isinstance(source, str):
            raise LegacyDomainError("Runtime settings text must be text")
        try:
            with tempfile.TemporaryDirectory(prefix="litellm-core-runtime-validate-") as directory:
                path = Path(directory) / "runtime-settings.env"
                atomic_write_text(path, source)
                from runtime_settings_io import read_settings_file

                values = read_settings_file(path, self.specs)
        except Exception as exc:
            raise _safe_problem(exc, "Runtime settings are invalid") from None
        self._draft_values = values

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
            self._draft_values = self._defaults(self.specs)
        elif name in {"reset", "cancel", "reload"}:
            self._draft_values = copy.deepcopy(self._raw_values)
        else:
            raise LegacyDomainError("The requested runtime action is unavailable")
        self.revision += 1
        return self.snapshot()

    def secret_present(self, field: str, target: str | None = None) -> bool:
        if field != "setting" or not isinstance(target, str) or target not in self.specs:
            raise LegacyDomainError("The requested secret field is unavailable")
        if not self._is_secret_setting(target):
            raise LegacyDomainError("The requested secret field is unavailable")
        return bool(self._draft_values.get(target, ""))

    def stage_secret(self, field: str, target: str | None, value: str) -> None:
        if field != "setting" or not isinstance(target, str) or target not in self.specs:
            raise LegacyDomainError("The requested secret field is unavailable")
        if not self._is_secret_setting(target):
            raise LegacyDomainError("The requested secret field is unavailable")
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
        except LegacyDomainError:
            return {"valid": False, "errors": ["Runtime settings are invalid"]}
        return {"valid": True, "errors": []}

    @staticmethod
    def _stored_value(spec: Any, value: str) -> str:
        if spec.kind != "mb":
            return value
        try:
            bytes_value = round(float(value) * 1024 * 1024)
        except (TypeError, ValueError, OverflowError):
            raise LegacyDomainError("Runtime settings are invalid") from None
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
            raise LegacyDomainError("Runtime settings are invalid")
        if not _same_file(self.settings_path, self._baseline_bytes):
            raise LegacyDomainError("Runtime settings changed on disk; reload before applying")
        try:
            atomic_write_text(self.settings_path, self._encoded_draft())
        except PersistenceError as exc:
            raise LegacyDomainError(safe_exception_message(exc)) from None
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
