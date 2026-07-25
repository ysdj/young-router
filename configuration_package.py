#!/usr/bin/env python3
"""Strict combined configuration packages for LiteLLM Menu.

The package is intentionally a new, self-contained format.  It can carry the
runtime-settings snapshot, the provider/model configuration document, or both.
Import only validates and returns data for callers to stage; it never changes a
live configuration file.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from config_editor_core.load import (
    CONFIG_DOCUMENT_CONFIG_KEY,
    CONFIG_DOCUMENT_DISABLED_KEY,
    load_config_document,
    normalize_config_document,
)
from config_editor_core.schema import _disabled_models_path
from runtime_settings_io import (
    MAX_PACKAGE_BYTES,
    PackageError as RuntimeSettingsPackageError,
    load_specs,
    read_settings_file,
    validate_values,
)


PACKAGE_FORMAT = "litellm-menu-configuration-package"
PACKAGE_VERSION = 1
RUNTIME_SETTINGS_SECTION = "runtime_settings"
PROVIDERS_MODELS_SECTION = "providers_models"
ALL_SECTIONS = "all"
SECTION_ORDER = (RUNTIME_SETTINGS_SECTION, PROVIDERS_MODELS_SECTION)


class ConfigurationPackageError(ValueError):
    """A combined configuration package or its explicit source is invalid."""


def _encoded_response(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False)
    # CLI responses are emitted with print(), so reserve the trailing newline
    # that the bounded native reader receives as part of stdout.
    if len(encoded.encode("utf-8")) + 1 > MAX_PACKAGE_BYTES:
        raise ConfigurationPackageError("Validated configuration exceeds the 16 MiB response limit.")
    return encoded


def normalize_sections(raw: str) -> tuple[str, ...]:
    """Return canonical selected sections from one explicit CLI value.

    ``all`` is intentionally the only shorthand.  The comma grammar is kept
    small so callers can construct it without guessing undocumented aliases.
    """

    if not isinstance(raw, str) or not raw:
        raise ConfigurationPackageError("Choose at least one configuration section.")
    values = raw.split(",")
    if any(not value for value in values):
        raise ConfigurationPackageError("Configuration sections must be comma-separated names.")
    if ALL_SECTIONS in values:
        if values != [ALL_SECTIONS]:
            raise ConfigurationPackageError("all cannot be combined with other configuration sections.")
        return SECTION_ORDER
    if len(set(values)) != len(values):
        raise ConfigurationPackageError("Configuration sections cannot be repeated.")
    unknown = sorted(set(values) - set(SECTION_ORDER))
    if unknown:
        raise ConfigurationPackageError("Unsupported configuration section(s): " + ", ".join(unknown))
    return tuple(section for section in SECTION_ORDER if section in values)


def _same_path(left: Path, right: Path) -> bool:
    try:
        return left.resolve(strict=False) == right.resolve(strict=False)
    except OSError:
        return False


def _read_limited_utf8(path: Path, label: str, *, missing_is_empty: bool = False) -> str:
    try:
        stat_result = path.stat()
    except FileNotFoundError:
        if missing_is_empty:
            return ""
        raise ConfigurationPackageError(f"{label} does not exist.") from None
    except OSError as exc:
        raise ConfigurationPackageError(f"{label} cannot be read.") from exc
    if not path.is_file() or stat_result.st_size > MAX_PACKAGE_BYTES:
        raise ConfigurationPackageError(f"{label} is not a supported size or file type.")
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ConfigurationPackageError(f"{label} cannot be read.") from exc
    if len(data) > MAX_PACKAGE_BYTES:
        raise ConfigurationPackageError(f"{label} is too large.")
    try:
        return data.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ConfigurationPackageError(f"{label} must be UTF-8.") from exc


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            raise ConfigurationPackageError("Configuration package JSON contains a duplicate key.")
        result[key] = value
    return result


def _reject_json_constant(_: str) -> object:
    raise ConfigurationPackageError("Configuration package JSON contains an unsupported value.")


def _read_provider_models_document(config_path: Path) -> dict[str, str | None]:
    """Read and validate only the explicitly supplied config document pair."""

    config_text = _read_limited_utf8(config_path, "Provider/model configuration")
    disabled_path = _disabled_models_path(config_path)
    disabled_text = (
        _read_limited_utf8(disabled_path, "Disabled provider/model configuration")
        if disabled_path.exists()
        else None
    )
    try:
        document = normalize_config_document(
            {
                CONFIG_DOCUMENT_CONFIG_KEY: config_text,
                CONFIG_DOCUMENT_DISABLED_KEY: disabled_text,
            }
        )
        # Validate that this document can be converted to the editor payload as
        # well as being syntactically valid current-schema YAML.
        load_config_document(document)
        return document
    except Exception as exc:
        # YAML parsers can echo source lines in diagnostics.  Those lines may
        # contain credentials, so never pass source validation detail through.
        raise ConfigurationPackageError("Provider/model configuration is invalid.") from exc


def _validate_provider_models_section(section: object) -> dict[str, Any]:
    if not isinstance(section, dict) or set(section) != {"document"}:
        raise ConfigurationPackageError("Provider/model section has an unsupported shape.")
    try:
        document = normalize_config_document(section["document"])
        payload = load_config_document(document)
    except Exception as exc:
        # Keep parser errors from exposing credentials embedded in invalid YAML.
        raise ConfigurationPackageError("Provider/model section is invalid.") from exc
    return {"providers": payload["providers"], "document": payload["document"]}


def _validate_runtime_settings_section(section: object) -> dict[str, str]:
    if not isinstance(section, dict) or set(section) != {"values"}:
        raise ConfigurationPackageError("Runtime settings section has an unsupported shape.")
    try:
        return validate_values(section["values"], load_specs())
    except RuntimeSettingsPackageError as exc:
        # Runtime settings validation names keys but never includes raw values.
        raise ConfigurationPackageError(str(exc)) from exc


def _read_package(path: Path) -> dict[str, object]:
    if path.suffix.lower() != ".json":
        raise ConfigurationPackageError("Configuration package input must end with .json.")
    source = _read_limited_utf8(path, "Configuration package")
    try:
        package = json.loads(
            source,
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise ConfigurationPackageError("Configuration package is not valid JSON.") from exc
    if not isinstance(package, dict) or set(package) != {"format", "version", "sections"}:
        raise ConfigurationPackageError("Configuration package has an unsupported shape.")
    if package["format"] != PACKAGE_FORMAT:
        raise ConfigurationPackageError("Configuration package format is not supported.")
    if type(package["version"]) is not int or package["version"] != PACKAGE_VERSION:
        raise ConfigurationPackageError("Configuration package version is not supported.")
    sections = package["sections"]
    if not isinstance(sections, dict) or not sections:
        raise ConfigurationPackageError("Configuration package sections must be a non-empty object.")
    unknown = sorted(set(sections) - set(SECTION_ORDER))
    if unknown:
        raise ConfigurationPackageError("Configuration package contains unsupported section(s): " + ", ".join(unknown))
    return package


def _write_package(path: Path, payload: dict[str, object]) -> None:
    if path.suffix.lower() != ".json":
        raise ConfigurationPackageError("Configuration package output must end with .json.")
    if not path.parent.is_dir():
        raise ConfigurationPackageError("Configuration package output directory does not exist.")
    if path.exists() and path.is_dir():
        raise ConfigurationPackageError("Configuration package output path is a directory.")
    if path.is_symlink():
        raise ConfigurationPackageError("Configuration package output path must not be a symlink.")
    encoded = (json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n").encode("utf-8")
    if len(encoded) > MAX_PACKAGE_BYTES:
        raise ConfigurationPackageError("Configuration package exceeds the 16 MiB limit.")
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(encoded)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
        os.chmod(path, 0o600)
    except OSError as exc:
        raise ConfigurationPackageError("Configuration package could not be written.") from exc
    finally:
        try:
            Path(temporary_name).unlink()
        except FileNotFoundError:
            pass


def export_package(
    *,
    sections: tuple[str, ...],
    config_path: Path | None,
    settings_file: Path | None,
    output_path: Path,
) -> tuple[str, ...]:
    """Export selected explicit sources into one restricted JSON package."""

    if not sections:
        raise ConfigurationPackageError("Choose at least one configuration section.")
    selected = set(sections)
    if selected - set(SECTION_ORDER) or len(selected) != len(sections):
        raise ConfigurationPackageError("Configuration sections are invalid.")
    if (config_path is None) != (PROVIDERS_MODELS_SECTION not in selected):
        raise ConfigurationPackageError("Provider/model export requires --config and no unused config path.")
    if (settings_file is None) != (RUNTIME_SETTINGS_SECTION not in selected):
        raise ConfigurationPackageError("Runtime settings export requires --settings-file and no unused settings path.")

    protected_paths = [path for path in (config_path, settings_file) if path is not None]
    if config_path is not None:
        protected_paths.append(_disabled_models_path(config_path))
    if any(_same_path(output_path, source) for source in protected_paths):
        raise ConfigurationPackageError("Configuration package output must differ from its source files.")

    package_sections: dict[str, object] = {}
    if RUNTIME_SETTINGS_SECTION in selected:
        assert settings_file is not None
        try:
            runtime_settings = read_settings_file(settings_file, load_specs())
        except RuntimeSettingsPackageError as exc:
            raise ConfigurationPackageError(str(exc)) from exc
        package_sections[RUNTIME_SETTINGS_SECTION] = {"values": runtime_settings}
    if PROVIDERS_MODELS_SECTION in selected:
        assert config_path is not None
        package_sections[PROVIDERS_MODELS_SECTION] = {
            "document": _read_provider_models_document(config_path)
        }

    ordered_sections = tuple(section for section in SECTION_ORDER if section in selected)
    _write_package(
        output_path,
        {
            "format": PACKAGE_FORMAT,
            "version": PACKAGE_VERSION,
            "sections": package_sections,
        },
    )
    return ordered_sections


def import_package(path: Path) -> dict[str, Any]:
    """Validate a package and return staged payloads without writing config."""

    package = _read_package(path)
    raw_sections = package["sections"]
    assert isinstance(raw_sections, dict)
    result: dict[str, Any] = {
        "sections": [section for section in SECTION_ORDER if section in raw_sections]
    }
    if RUNTIME_SETTINGS_SECTION in raw_sections:
        result[RUNTIME_SETTINGS_SECTION] = {
            "values": _validate_runtime_settings_section(raw_sections[RUNTIME_SETTINGS_SECTION])
        }
    if PROVIDERS_MODELS_SECTION in raw_sections:
        result[PROVIDERS_MODELS_SECTION] = _validate_provider_models_section(
            raw_sections[PROVIDERS_MODELS_SECTION]
        )
    return result


def parse_arguments(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import or export LiteLLM Menu configuration packages.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    export = subparsers.add_parser("export", help="Write one selected configuration package.")
    export.add_argument("--sections", required=True, help="runtime_settings, providers_models, or all")
    export.add_argument("--config", type=Path)
    export.add_argument("--settings-file", type=Path)
    export.add_argument("--output", required=True, type=Path)
    imported = subparsers.add_parser("import", help="Validate a package without applying it.")
    imported.add_argument("--input", required=True, type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    arguments = parse_arguments(argv)
    try:
        if arguments.command == "export":
            sections = export_package(
                sections=normalize_sections(arguments.sections),
                config_path=arguments.config,
                settings_file=arguments.settings_file,
                output_path=arguments.output,
            )
            # Do not print source data or path details; packages may carry credentials.
            print(json.dumps({"output": "written", "sections": list(sections)}, ensure_ascii=False))
        else:
            print(_encoded_response(import_package(arguments.input)))
    except ConfigurationPackageError as exc:
        print(f"Configuration package error: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
