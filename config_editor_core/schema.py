#!/usr/bin/env python3
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import pathlib
import re
import shutil
import sys
import tempfile
import secrets
from typing import Any

try:
    import yaml
except Exception as exc:  # pragma: no cover - exercised by menu error path
    print(f"PyYAML is required to edit config.yaml: {exc}", file=sys.stderr)
    sys.exit(1)


ROOT = pathlib.Path(__file__).resolve().parent


def _default_config_yaml() -> pathlib.Path:
    config_file = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    if config_file:
        return pathlib.Path(config_file).expanduser()

    runtime_root = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip()
    if not runtime_root:
        runtime_root = os.environ.get("LITELLM_MENU_HOME", "").strip()
    if runtime_root:
        return pathlib.Path(runtime_root).expanduser() / "config.yaml"

    return pathlib.Path.home() / ".litellm-menu" / "config.yaml"


CONFIG_YAML = _default_config_yaml()
DISABLED_MODELS_KEY = "disabled_model_list"
DEFAULT_API_KEY_NAME = "default"
MENU_MODEL_ENABLED_KEY = "x-litellm-menu-model-enabled"
MENU_ROUTE_KEY = "route_key"
MENU_API_KEY_NAME_KEY = "api_key_name"
RANDOM_DEPLOYMENT_ID_RE = re.compile(r"^[0-9a-f]{8}$")
UPSTREAM_URL_SURFACE_KEY = "upstream_url_surface"
SUPPORTED_UPSTREAM_URL_SURFACES_KEY = "supported_upstream_url_surfaces"
UPSTREAM_URL_SURFACES = {"openai/chat", "openai/responses", "anthropic"}
CURRENT_HOOK_CALLBACK = "litellm_menu.callbacks.image_generation_routing_hook"


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _jsonable(value: Any) -> Any:
    try:
        json.dumps(value)
        return value
    except TypeError:
        return str(value)


def _string_value(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _bool_value(value: Any, default: bool = True) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on", "enabled"}:
        return True
    if text in {"0", "false", "no", "off", "disabled"}:
        return False
    return default


def _positive_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float):
        return int(value) if value.is_integer() and value > 0 else None
    text = str(value).strip().replace(",", "")
    if not re.fullmatch(r"[0-9]+", text):
        return None
    number = int(text)
    return number if number > 0 else None


def _upstream_url_surfaces(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError(
            f"{SUPPORTED_UPSTREAM_URL_SURFACES_KEY} must be a non-empty list"
        )
    modes: list[str] = []
    for index, item in enumerate(value, start=1):
        if not isinstance(item, str) or item not in UPSTREAM_URL_SURFACES:
            raise ValueError(
                f"{SUPPORTED_UPSTREAM_URL_SURFACES_KEY}[{index}] must be one of "
                "openai/responses, anthropic, openai/chat"
            )
        if item in modes:
            raise ValueError(
                f"{SUPPORTED_UPSTREAM_URL_SURFACES_KEY} contains duplicate {item}"
            )
        modes.append(item)
    return modes


def _editor_deployment_id(value: Any) -> str:
    return _string_value(value).strip()


def _validate_current_schema(data: dict[str, Any], path: pathlib.Path) -> None:
    is_disabled_file = path.name.endswith(".disabled-models.yaml")
    required_section = DISABLED_MODELS_KEY if is_disabled_file else "model_list"
    if not isinstance(data.get(required_section), list):
        raise ValueError(
            f"{path.name} must contain {required_section} as a list"
        )
    if is_disabled_file and "model_list" in data:
        raise ValueError(
            f"{path.name} must not contain model_list; use {DISABLED_MODELS_KEY}"
        )
    if not is_disabled_file and DISABLED_MODELS_KEY in data:
        raise ValueError(
            f"{path.name} must not contain {DISABLED_MODELS_KEY}; "
            f"use {_disabled_models_path(path).name}"
        )

    settings = _as_dict(data.get("litellm_settings"))
    callbacks = settings.get("callbacks")
    if callbacks is not None and not isinstance(callbacks, list):
        raise ValueError(f"{path.name} litellm_settings.callbacks must be a list")
    for callback in _as_list(callbacks):
        callback_path = _string_value(callback).strip()
        if callback_path and callback_path != CURRENT_HOOK_CALLBACK:
            raise ValueError(
                f"{path.name} litellm_settings.callbacks contains unsupported callback {callback_path}; "
                f"use {CURRENT_HOOK_CALLBACK}"
            )

    providers = _as_dict(data.get("providers"))
    for provider_name, raw_provider in providers.items():
        provider = _as_dict(raw_provider)
        if "api_key" in provider:
            raise ValueError(
                f"{path.name} provider {provider_name} uses unsupported scalar api_key; "
                "use api_keys: [{name, value}]"
            )
        if "disabled_api_keys" in provider:
            raise ValueError(
                f"{path.name} provider {provider_name} uses unsupported disabled_api_keys; "
                "remove unused API keys instead"
            )
        raw_keys = provider.get("api_keys")
        if raw_keys is None:
            continue
        if not isinstance(raw_keys, list):
            raise ValueError(
                f"{path.name} provider {provider_name} api_keys must be a list of objects"
            )
        for index, raw_key in enumerate(raw_keys, start=1):
            key = _as_dict(raw_key)
            if not key:
                raise ValueError(
                    f"{path.name} provider {provider_name} api_keys[{index}] must be an object"
                )
            if "api_key" in key:
                raise ValueError(
                    f"{path.name} provider {provider_name} api_keys[{index}] uses unsupported api_key; "
                    "use value"
                )
            if not _string_value(key.get("value")):
                raise ValueError(
                    f"{path.name} provider {provider_name} api_keys[{index}] needs value"
                )

    section_names = (DISABLED_MODELS_KEY,) if is_disabled_file else ("model_list",)
    for section_name in section_names:
        for index, raw_model in enumerate(_as_list(data.get(section_name)), start=1):
            if not isinstance(raw_model, dict):
                raise ValueError(
                    f"{path.name} {section_name}[{index}] must be an object"
                )
            if not isinstance(raw_model.get("model_info"), dict):
                raise ValueError(
                    f"{path.name} {section_name}[{index}] model_info must be an object"
                )
            model_info = raw_model["model_info"]
            for unsupported_key in (
                "upstream_api_mode",
                "supported_upstream_api_modes",
                "supports_responses_endpoint",
                "supports_image_generation",
                "max_input_tokens",
                "context_metadata_source",
                "context_metadata_model_id",
            ):
                if unsupported_key in model_info:
                    replacement = (
                        "supports_responses_image_generation_tool"
                        if unsupported_key == "supports_image_generation"
                        else (
                            "supported_upstream_url_surfaces"
                            if unsupported_key in {
                                "upstream_api_mode",
                                "supported_upstream_api_modes",
                                "supports_responses_endpoint",
                            }
                            else "remove it"
                        )
                    )
                    raise ValueError(
                        f"{path.name} {section_name}[{index}] uses unsupported {unsupported_key}; "
                        f"use {replacement}"
                    )
            deployment_id = _string_value(model_info.get("id")).strip()
            if deployment_id and not RANDOM_DEPLOYMENT_ID_RE.fullmatch(deployment_id):
                raise ValueError(
                    f"{path.name} {section_name}[{index}] model_info.id must be an 8 character hex deployment token"
                )
            try:
                surfaces = _upstream_url_surfaces(
                    model_info.get(SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
                )
            except ValueError as exc:
                raise ValueError(
                    f"{path.name} {section_name}[{index}] {exc}"
                ) from exc
            if model_info.get(UPSTREAM_URL_SURFACE_KEY) != surfaces[0]:
                raise ValueError(
                    f"{path.name} {section_name}[{index}] {UPSTREAM_URL_SURFACE_KEY} "
                    f"must equal the first {SUPPORTED_UPSTREAM_URL_SURFACES_KEY} item"
                )


def _load_yaml(path: pathlib.Path) -> dict[str, Any]:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a YAML mapping")
    _validate_current_schema(data, path)
    return data


def _disabled_models_path(config_path: pathlib.Path) -> pathlib.Path:
    return config_path.with_name(f"{config_path.stem}.disabled-models.yaml")


def _file_revision(path: pathlib.Path) -> dict[str, Any]:
    if not path.exists():
        return {"exists": False, "sha256": ""}
    return {
        "exists": True,
        "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


def _config_revision(config_path: pathlib.Path) -> dict[str, Any]:
    return {
        "config": _file_revision(config_path),
        "disabled": _file_revision(_disabled_models_path(config_path)),
    }


def _assert_expected_revision(path: pathlib.Path, expected_revision: Any) -> None:
    if expected_revision is None:
        return
    if expected_revision != _config_revision(path):
        raise ValueError(
            "config.yaml changed on disk since this editor window loaded. "
            "Close and reopen Edit Models Config, then apply your changes again."
        )

__all__ = [name for name in globals() if not name.startswith("__")]
