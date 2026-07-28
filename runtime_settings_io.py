"""Runtime Settings schema and validation shared by the configuration package."""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path
import re

from litellm_menu.core.runtime_settings_schema import runtime_settings_metadata


MAX_PACKAGE_BYTES = 16 * 1024 * 1024
RETAIN_EXISTING_VALUE = "__LITELLM_MENU_RETAIN_EXISTING__"
RETIRED_PERSISTED_SETTINGS = frozenset(
    {
        "LITELLM_CONFIG_WATCH_INTERVAL",
        "LITELLM_CONFIG_WATCH_SETTLE_INTERVAL",
    }
)


class PackageError(ValueError):
    """A package or source settings file is not safe to use."""


@dataclass(frozen=True)
class RuntimeSettingSpec:
    key: str
    kind: str
    default: str
    minimum: float | int | None
    maximum: float | int | None
    options: tuple[str, ...]


def load_specs() -> dict[str, RuntimeSettingSpec]:
    """Build the validation schema from bundled Core-owned Python data."""

    specs: dict[str, RuntimeSettingSpec] = {}
    for item in runtime_settings_metadata():
        if not isinstance(item, dict):
            raise PackageError("Runtime Settings schema is unavailable.")
        key = item.get("key")
        if not isinstance(key, str) or key in specs:
            raise PackageError("Runtime Settings schema is unavailable.")
        kind = item.get("kind")
        default = item.get("default")
        minimum = item.get("minimum")
        maximum = item.get("maximum")
        if (
            not isinstance(kind, str)
            or not isinstance(default, str)
        ):
            raise PackageError("Runtime Settings schema is unavailable.")
        options = item.get("options", [])
        if not isinstance(options, list) or not all(isinstance(option, str) for option in options):
            raise PackageError("Runtime Settings schema is unavailable.")
        specs[key] = RuntimeSettingSpec(
            key=key,
            kind=kind,
            default=default,
            minimum=minimum,
            maximum=maximum,
            options=tuple(options),
        )
    return specs


def _number_text(value: float) -> str:
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _normalize_number(spec: RuntimeSettingSpec, raw: str) -> str:
    text = raw.strip() or spec.default
    if spec.kind == "int":
        if not re.fullmatch(r"\d+", text):
            raise PackageError(f"{spec.key} must be an integer.")
        numeric: float | int = int(text)
        normalized = str(numeric)
    elif spec.kind == "mb":
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise PackageError(f"{spec.key} must be a number of MB.")
        numeric = float(text)
        if not math.isfinite(numeric):
            raise PackageError(f"{spec.key} must be finite.")
        normalized = _number_text(numeric)
    else:
        if not re.fullmatch(r"\d+(?:\.\d+)?", text):
            raise PackageError(f"{spec.key} must be a number.")
        numeric = float(text)
        if not math.isfinite(numeric):
            raise PackageError(f"{spec.key} must be finite.")
        normalized = _number_text(numeric)
    if spec.minimum is not None and numeric < spec.minimum:
        raise PackageError(f"{spec.key} must be at least {spec.minimum}.")
    if spec.maximum is not None and numeric > spec.maximum:
        raise PackageError(f"{spec.key} must be at most {spec.maximum}.")
    return normalized


def normalize_payload_value(spec: RuntimeSettingSpec, raw: object) -> str:
    """Normalize a value exactly as the Runtime Settings save payload expects."""

    if not isinstance(raw, str):
        raise PackageError(f"{spec.key} must be a string.")
    if raw == RETAIN_EXISTING_VALUE:
        raise PackageError(f"{spec.key} cannot use the retain-existing marker in a package.")
    if spec.kind in {"int", "float", "mb"}:
        return _normalize_number(spec, raw)
    if spec.kind == "string" and any(character in raw for character in "\n\r#"):
        raise PackageError(f"{spec.key} cannot contain newlines or #.")
    text = raw.strip() or spec.default
    if spec.kind == "bool":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on"}:
            return "1"
        if lowered in {"0", "false", "no", "off"}:
            return "0"
        raise PackageError(f"{spec.key} must be a boolean.")
    if spec.kind == "bool_auto":
        lowered = text.lower()
        if lowered in {"1", "true", "yes", "on", "auto", "enabled"}:
            return "auto"
        if lowered in {"0", "false", "no", "off", "disabled"}:
            return "off"
        raise PackageError(f"{spec.key} must be a boolean.")
    if spec.kind == "enum":
        lowered = text.lower()
        if lowered not in spec.options:
            raise PackageError(f"{spec.key} must be one of: {', '.join(spec.options)}")
        return lowered
    if spec.kind == "string":
        if spec.key == "LITELLM_MENU_WEB_SEARCH_REGION" and any(
            character.isspace() for character in text
        ):
            raise PackageError(f"{spec.key} cannot contain whitespace.")
        return text
    raise PackageError("Runtime Settings schema is unavailable.")


def _default_payload_value(spec: RuntimeSettingSpec) -> str:
    return normalize_payload_value(spec, spec.default)


def validate_values(values: object, specs: dict[str, RuntimeSettingSpec]) -> dict[str, str]:
    if not isinstance(values, dict):
        raise PackageError("Runtime settings values must be an object.")
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise PackageError("Unknown runtime setting(s): " + ", ".join(unknown))
    normalized = {
        key: normalize_payload_value(specs[key], raw)
        for key, raw in values.items()
    }
    effective = {key: _default_payload_value(spec) for key, spec in specs.items()}
    effective.update(normalized)
    if (
        int(effective["LITELLM_MENU_WEB_SEARCH_READ_RESULTS"])
        > int(effective["LITELLM_MENU_WEB_SEARCH_MAX_RESULTS"])
    ):
        raise PackageError(
            "LITELLM_MENU_WEB_SEARCH_READ_RESULTS cannot exceed "
            "LITELLM_MENU_WEB_SEARCH_MAX_RESULTS."
        )
    return normalized


def _read_limited_utf8(path: Path, label: str, *, missing_is_empty: bool = False) -> str:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        if missing_is_empty:
            return ""
        raise PackageError(f"{label} does not exist.") from None
    except OSError as exc:
        raise PackageError(f"{label} cannot be read.") from exc
    if not path.is_file() or stat_result.st_size > MAX_PACKAGE_BYTES:
        raise PackageError(f"{label} is not a supported size or file type.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise PackageError(f"{label} cannot be read.") from exc
    if len(data) > MAX_PACKAGE_BYTES:
        raise PackageError(f"{label} is too large.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise PackageError(f"{label} must be UTF-8.") from exc


def _read_persisted_values(path: Path, specs: dict[str, RuntimeSettingSpec]) -> dict[str, str]:
    """Read stored settings while tolerating retired local keys."""

    source = _read_limited_utf8(path, "Runtime settings file", missing_is_empty=True)
    raw_values: dict[str, str] = {}
    for line_number, line in enumerate(source.splitlines(), start=1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if "#" in stripped or "=" not in stripped:
            raise PackageError(f"Runtime settings file has invalid content at line {line_number}.")
        key, raw = stripped.split("=", 1)
        key = key.strip()
        raw = raw.strip()
        if key in RETIRED_PERSISTED_SETTINGS:
            continue
        if key not in specs or key in raw_values:
            raise PackageError(f"Runtime settings file has invalid content at line {line_number}.")
        raw_values[key] = raw
    return raw_values


def read_settings_file(path: Path, specs: dict[str, RuntimeSettingSpec]) -> dict[str, str]:
    """Read a complete effective settings snapshot for the Runtime Settings UI."""

    raw_values = _read_persisted_values(path, specs)

    values = {key: _default_payload_value(spec) for key, spec in specs.items()}
    for key, raw in raw_values.items():
        spec = specs[key]
        if spec.kind == "mb":
            if not re.fullmatch(r"\d+", raw):
                raise PackageError(f"{key} must be stored as integer bytes.")
            stored_bytes = int(raw)
            minimum_bytes = int(round(float(spec.minimum or 0) * 1024 * 1024))
            maximum_bytes = int(round(float(spec.maximum or 0) * 1024 * 1024))
            if stored_bytes < minimum_bytes or stored_bytes > maximum_bytes:
                raise PackageError(f"{key} is outside its allowed range.")
            values[key] = _number_text(stored_bytes / (1024 * 1024))
        else:
            values[key] = normalize_payload_value(spec, raw)
    return validate_values(values, specs)


def read_configured_settings_file(path: Path, specs: dict[str, RuntimeSettingSpec]) -> dict[str, str]:
    """Return validated persisted values without retired keys."""

    raw_values = _read_persisted_values(path, specs)
    effective = read_settings_file(path, specs)
    configured: dict[str, str] = {}
    for key, raw in raw_values.items():
        spec = specs[key]
        configured[key] = raw if spec.kind == "mb" else effective[key]
    return configured
