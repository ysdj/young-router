"""Runtime Settings schema and validation shared by the configuration package."""

from __future__ import annotations

import ast
import math
from dataclasses import dataclass
from pathlib import Path
import re


MAX_PACKAGE_BYTES = 16 * 1024 * 1024
RETAIN_EXISTING_VALUE = "__LITELLM_MENU_RETAIN_EXISTING__"


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


def _source_path() -> Path:
    return Path(__file__).resolve().parent / "service" / "runtime_settings.sh"


def _configure_source_path() -> Path:
    return Path(__file__).resolve().parent / "service" / "runtime_settings_configure.sh"


def _literal_specs(source: Path, end_marker: str) -> list[object]:
    try:
        text = source.read_text(encoding="utf-8")
        body = text.split("SPECS = [", 1)[1].split(end_marker, 1)[0]
        result = ast.literal_eval("[" + body + "]")
    except (IndexError, OSError, SyntaxError, ValueError) as exc:
        raise PackageError("Runtime Settings schema is unavailable.") from exc
    if not isinstance(result, list):
        raise PackageError("Runtime Settings schema is unavailable.")
    return result


def load_specs() -> dict[str, RuntimeSettingSpec]:
    """Read the one authoritative display/save schema from the service files."""

    displayed = _literal_specs(_source_path(), "]\n\n\ndef read_configured")
    configurable = _literal_specs(_configure_source_path(), "]\nSPEC_BY_KEY")
    configured_by_key: dict[str, tuple[object, ...]] = {}
    for item in configurable:
        if not isinstance(item, tuple) or len(item) != 5 or not isinstance(item[0], str):
            raise PackageError("Runtime Settings schema is unavailable.")
        configured_by_key[item[0]] = item

    specs: dict[str, RuntimeSettingSpec] = {}
    for item in displayed:
        if not isinstance(item, dict):
            raise PackageError("Runtime Settings schema is unavailable.")
        key = item.get("key")
        if not isinstance(key, str) or key in specs:
            raise PackageError("Runtime Settings schema is unavailable.")
        configured = configured_by_key.get(key)
        if configured is None:
            raise PackageError("Runtime Settings schema is unavailable.")
        kind = item.get("kind")
        default = item.get("default")
        minimum = item.get("minimum")
        maximum = item.get("maximum")
        if (
            not isinstance(kind, str)
            or not isinstance(default, str)
            or kind != configured[1]
            or default != str(configured[2])
            or minimum != configured[3]
            or maximum != configured[4]
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
    if set(specs) != set(configured_by_key):
        raise PackageError("Runtime Settings schema is unavailable.")
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


def read_settings_file(path: Path, specs: dict[str, RuntimeSettingSpec]) -> dict[str, str]:
    """Read a complete effective settings snapshot for the Runtime Settings UI."""

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
        if key not in specs or key in raw_values:
            raise PackageError(f"Runtime settings file has invalid content at line {line_number}.")
        raw_values[key] = raw

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
