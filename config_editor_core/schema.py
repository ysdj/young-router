#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import math
import os
import pathlib
import re
import sys
from typing import Any

try:
    import yaml
except Exception as exc:  # pragma: no cover - exercised by menu error path
    print(f"PyYAML is required to edit config.yaml: {exc}", file=sys.stderr)
    sys.exit(1)


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
MENU_RELAY_KEYS_KEY = "x-litellm-menu-relay-keys"
MENU_RELAY_KEYS_VERSION = 1
MENU_PROVIDER_KEY_ID_KEY = "x-litellm-menu-provider-key-id"
MENU_RELAY_CATALOG_MODE_KEY = "x-litellm-menu-relay-catalog-mode"
MENU_RELAY_SOURCE_MODEL_KEY = "x-litellm-menu-relay-source-model"
MENU_ORDER_MODE_KEY = "x-litellm-menu-order-mode"
MENU_MANUAL_ORDER_KEY = "x-litellm-menu-manual-order"
MENU_PROVIDER_SOURCE_KEY = "x-litellm-menu-provider-source"
MENU_PROVIDER_AUTH_KEY = "x-litellm-menu-provider-auth"
PROVIDER_KEY_SOURCE_KINDS = {"independent", "relay"}
PROVIDER_SOURCE_KINDS = {"custom", "relay"}
PROVIDER_AUTH_KINDS = {"api_key", "openai_login", "claude_login"}
MODEL_CATALOG_MODES = {"independent", "relay_linked"}
MODEL_ORDER_MODES = {"manual", "relay_multiplier"}
RANDOM_DEPLOYMENT_ID_RE = re.compile(r"^[0-9a-f]{8}$")
UPSTREAM_URL_SURFACE_KEY = "upstream_url_surface"
UPSTREAM_URL_SURFACES = {"openai/chat", "openai/responses", "anthropic"}
UPSTREAM_PROTOCOL_MODE_KEY = "upstream_protocol_mode"
UPSTREAM_PROTOCOL_MODES = {"fallback", "fixed"}
CURRENT_HOOK_CALLBACK = "litellm_menu.callbacks.image_generation_routing_hook"
YAML_MAX_EXPANDED_NODES = 100_000
YAML_MAX_NESTING_DEPTH = 100
YAML_MAX_FINAL_STRUCTURE_NODES = 100_000
YAML_MAX_FINAL_SCALAR_BYTES = 8 * 1024 * 1024


class _YamlStructureLimitExceeded(Exception):
    pass


def _checked_yaml_total(current: int, increment: int, limit: int) -> int:
    if increment > limit - current:
        raise _YamlStructureLimitExceeded
    return current + increment


def _validate_yaml_event_limits(text: str) -> None:
    anchors: dict[str, tuple[int, int]] = {}
    frames: list[dict[str, Any]] = []
    document_cost = 0

    def add_node(cost: int, depth: int) -> None:
        nonlocal document_cost
        if depth > YAML_MAX_NESTING_DEPTH:
            raise _YamlStructureLimitExceeded
        if frames:
            frame = frames[-1]
            frame["cost"] = _checked_yaml_total(
                frame["cost"], cost, YAML_MAX_EXPANDED_NODES
            )
            frame["child_depth"] = max(frame["child_depth"], depth)
        else:
            document_cost = _checked_yaml_total(
                document_cost, cost, YAML_MAX_EXPANDED_NODES
            )

    for event in yaml.parse(text):
        if isinstance(event, (yaml.events.MappingStartEvent, yaml.events.SequenceStartEvent)):
            if len(frames) + 1 > YAML_MAX_NESTING_DEPTH:
                raise _YamlStructureLimitExceeded
            frames.append({"anchor": event.anchor, "cost": 1, "child_depth": 0})
            continue

        if isinstance(event, yaml.events.ScalarEvent):
            add_node(1, 1)
            if event.anchor:
                anchors[event.anchor] = (1, 1)
            continue

        if isinstance(event, yaml.events.AliasEvent):
            target = anchors.get(event.anchor)
            if target is None:
                # Recursive aliases point at an anchor that is still being built.
                raise _YamlStructureLimitExceeded
            add_node(*target)
            continue

        if isinstance(event, (yaml.events.MappingEndEvent, yaml.events.SequenceEndEvent)):
            if not frames:
                raise _YamlStructureLimitExceeded
            frame = frames.pop()
            depth = 1 + frame["child_depth"]
            if depth > YAML_MAX_NESTING_DEPTH:
                raise _YamlStructureLimitExceeded
            anchor = frame["anchor"]
            if anchor:
                anchors[anchor] = (frame["cost"], depth)
            add_node(frame["cost"], depth)


def _validate_loaded_yaml_limits(data: Any) -> None:
    node_count = 0
    scalar_bytes = 0
    active_containers: set[int] = set()

    def visit(value: Any, depth: int) -> None:
        nonlocal node_count, scalar_bytes
        if depth > YAML_MAX_NESTING_DEPTH:
            raise _YamlStructureLimitExceeded
        node_count = _checked_yaml_total(
            node_count, 1, YAML_MAX_FINAL_STRUCTURE_NODES
        )

        if isinstance(value, str):
            scalar_bytes = _checked_yaml_total(
                scalar_bytes,
                len(value.encode("utf-8")),
                YAML_MAX_FINAL_SCALAR_BYTES,
            )
            return
        if isinstance(value, bytes):
            scalar_bytes = _checked_yaml_total(
                scalar_bytes, len(value), YAML_MAX_FINAL_SCALAR_BYTES
            )
            return

        if isinstance(value, dict):
            children = (item for pair in value.items() for item in pair)
        elif isinstance(value, (list, tuple, set, frozenset)):
            children = iter(value)
        else:
            return

        identity = id(value)
        if identity in active_containers:
            raise _YamlStructureLimitExceeded
        active_containers.add(identity)
        try:
            for child in children:
                visit(child, depth + 1)
        finally:
            active_containers.remove(identity)

    visit(data, 1)


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


def _menu_metadata_text(value: Any, field: str, *, required: bool = False) -> str:
    """Validate opaque, secret-free IDs used by editor-owned metadata."""

    text = value.strip() if isinstance(value, str) else ""
    if (
        (required and not text)
        or len(text.encode("utf-8")) > 256
        or any(char in text for char in "\x00\r\n")
    ):
        raise ValueError(f"{field} is invalid")
    return text


def _provider_key_id(value: Any, *, required: bool = False) -> str:
    return _menu_metadata_text(value, "Provider key ID", required=required)


def _relay_source(value: Any, *, required: bool = False) -> dict[str, str]:
    """Return the only supported, non-secret ProviderKey source fields."""

    if value is None:
        if required:
            raise ValueError("Provider key source is invalid")
        return {"kind": "independent"}
    if not isinstance(value, dict):
        raise ValueError("Provider key source is invalid")
    if set(value).difference({"kind", "station_id", "account_id", "resource_id"}):
        raise ValueError("Provider key source is invalid")
    kind = value.get("kind", "independent")
    if not isinstance(kind, str) or kind not in PROVIDER_KEY_SOURCE_KINDS:
        raise ValueError("Provider key source is invalid")
    if kind == "independent":
        if any(value.get(key) not in (None, "") for key in ("station_id", "account_id", "resource_id")):
            raise ValueError("Independent provider key source cannot reference a relay")
        return {"kind": "independent"}
    source = {"kind": "relay"}
    for key in ("station_id", "account_id", "resource_id"):
        source[key] = _menu_metadata_text(value.get(key), f"Relay {key}", required=True)
    return source


def _provider_source(value: Any, *, required: bool = False) -> dict[str, str]:
    """Return the provider URL/name source without duplicating either value."""

    if value is None:
        if required:
            raise ValueError("Provider source is invalid")
        return {"kind": "custom"}
    if not isinstance(value, dict):
        raise ValueError("Provider source is invalid")
    if set(value).difference({"kind", "station_id"}):
        raise ValueError("Provider source is invalid")
    kind = value.get("kind", "custom")
    if not isinstance(kind, str) or kind not in PROVIDER_SOURCE_KINDS:
        raise ValueError("Provider source is invalid")
    station_id = value.get("station_id")
    if kind == "custom":
        if station_id not in (None, ""):
            raise ValueError("Custom provider source cannot reference a relay station")
        return {"kind": "custom"}
    return {
        "kind": "relay",
        "station_id": _menu_metadata_text(
            station_id, "Relay station ID", required=True
        ),
    }


def _provider_auth(value: Any, *, required: bool = False) -> dict[str, str]:
    """Validate secret-free provider authentication metadata."""

    if value is None:
        if required:
            raise ValueError("Provider authentication is invalid")
        return {"kind": "api_key"}
    if not isinstance(value, dict) or set(value).difference({"kind", "credential_ref"}):
        raise ValueError("Provider authentication is invalid")
    kind = value.get("kind", "api_key")
    if not isinstance(kind, str) or kind not in PROVIDER_AUTH_KINDS:
        raise ValueError("Provider authentication is invalid")
    credential_ref = _menu_metadata_text(
        value.get("credential_ref"),
        "Provider credential reference",
        required=kind != "api_key",
    )
    if kind == "api_key":
        if credential_ref:
            raise ValueError("API key authentication cannot reference a login credential")
        return {"kind": "api_key"}
    return {"kind": kind, "credential_ref": credential_ref}


def _stable_provider_key_id(provider_name: Any, api_key_name: Any) -> str:
    """Derive a migration-safe ID without ever using the credential value."""

    provider = _string_value(provider_name).strip()
    key_name = _string_value(api_key_name).strip()
    digest = hashlib.sha256(
        f"litellm-menu-provider-key-v1\x1f{provider}\x1f{key_name}".encode("utf-8")
    ).hexdigest()[:32]
    # Avoid a token-like ``key-<long value>`` shape: generic snapshot
    # redaction correctly treats that pattern as a possible credential.
    return f"provider-slot-{digest}"


def _menu_order(value: Any, field: str) -> int | float:
    if isinstance(value, bool):
        raise ValueError(f"{field} is invalid")
    if isinstance(value, (int, float)):
        result = value
    elif isinstance(value, str) and value.strip():
        try:
            result = float(value.strip())
        except ValueError as exc:
            raise ValueError(f"{field} is invalid") from exc
    else:
        raise ValueError(f"{field} is invalid")
    if not math.isfinite(result):
        raise ValueError(f"{field} is invalid")
    return int(result) if isinstance(result, float) and result.is_integer() else result


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


def _upstream_url_surface(value: Any) -> str:
    if not isinstance(value, str) or value not in UPSTREAM_URL_SURFACES:
        raise ValueError(
            f"{UPSTREAM_URL_SURFACE_KEY} must be one of "
            "openai/responses, anthropic, openai/chat"
        )
    return value


def _upstream_protocol_mode(value: Any) -> str:
    if value is None:
        return "fallback"
    if not isinstance(value, str) or value.strip().lower() not in UPSTREAM_PROTOCOL_MODES:
        raise ValueError(
            f"{UPSTREAM_PROTOCOL_MODE_KEY} must be one of fallback, fixed"
        )
    return value.strip().lower()


def infer_upstream_fallback_surface(value: Any) -> str:
    """Infer the fallback protocol from the exact upstream model identifier.

    The public model alias is deliberately not accepted here.  A route may
    expose a Claude-shaped public name while targeting a Kimi or GPT model,
    and that alias must not change the adapter used as the fallback.
    """

    model = _string_value(value).strip().lower()
    if "/" in model:
        prefix, raw_model = model.split("/", 1)
        if prefix in {"openai", "anthropic"}:
            model = raw_model.strip()
    tokens = [token for token in re.split(r"[^a-z0-9]+", model) if token]
    if any(token in {"claude", "anthropic"} for token in tokens):
        return "anthropic"
    if any(
        token in {"gpt", "openai", "o1", "o2", "o3", "o4", "codex"}
        or token.startswith(("gpt", "o1", "o2", "o3", "o4"))
        for token in tokens
    ):
        return "openai/responses"
    return "openai/chat"


def canonical_litellm_model(value: Any, surface: str, adapter: str | None = None) -> str:
    """Store a LiteLLM adapter prefix derived from the selected upstream surface."""

    model = _string_value(value).strip()
    if not model:
        return ""
    if "/" in model:
        existing_prefix, raw_model = model.split("/", 1)
        if existing_prefix in {"openai", "anthropic", "chatgpt"}:
            model = raw_model.strip()
    if not model:
        return ""
    if adapter is not None and adapter not in {"openai", "anthropic", "chatgpt"}:
        raise ValueError("LiteLLM adapter is invalid")
    prefix = adapter or ("anthropic" if surface == "anthropic" else "openai")
    return f"{prefix}/{model}"


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
    seen_provider_key_ids: set[str] = set()
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
        raw_keys = provider.get("api_keys", [])
        if not isinstance(raw_keys, list):
            raise ValueError(
                f"{path.name} provider {provider_name} api_keys must be a list of objects"
            )
        api_key_names: set[str] = set()
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
            if "enabled" in key:
                raise ValueError(
                    f"{path.name} provider {provider_name} api_keys[{index}] uses unsupported enabled; "
                    "remove unused API keys instead"
                )
            if not _string_value(key.get("value")):
                raise ValueError(
                    f"{path.name} provider {provider_name} api_keys[{index}] needs value"
                )
            key_name = _menu_metadata_text(
                key.get("name"), "Provider API key name", required=True
            )
            if key_name in api_key_names:
                raise ValueError(
                    f"{path.name} provider {provider_name} has duplicate API key name {key_name}"
                )
            api_key_names.add(key_name)

        relay_keys = provider.get(MENU_RELAY_KEYS_KEY)
        if relay_keys is not None:
            if not isinstance(relay_keys, dict) or set(relay_keys).difference({"version", "slots"}):
                raise ValueError(
                    f"{path.name} provider {provider_name} {MENU_RELAY_KEYS_KEY} is invalid"
                )
            if relay_keys.get("version") != MENU_RELAY_KEYS_VERSION:
                raise ValueError(
                    f"{path.name} provider {provider_name} {MENU_RELAY_KEYS_KEY} version is unsupported"
                )
            slots = relay_keys.get("slots")
            if not isinstance(slots, list):
                raise ValueError(
                    f"{path.name} provider {provider_name} {MENU_RELAY_KEYS_KEY}.slots must be a list"
                )
            seen_slot_names: set[str] = set()
            for slot_index, raw_slot in enumerate(slots, start=1):
                if not isinstance(raw_slot, dict) or set(raw_slot) != {"id", "api_key_name", "source"}:
                    raise ValueError(
                        f"{path.name} provider {provider_name} relay key slot #{slot_index} is invalid"
                    )
                slot_id = _provider_key_id(raw_slot.get("id"), required=True)
                key_name = _menu_metadata_text(
                    raw_slot.get("api_key_name"), "Provider API key name", required=True
                )
                _relay_source(raw_slot.get("source"), required=True)
                if slot_id in seen_provider_key_ids:
                    raise ValueError(
                        f"{path.name} contains duplicate provider key ID {slot_id}"
                    )
                if key_name in seen_slot_names or key_name not in api_key_names:
                    raise ValueError(
                        f"{path.name} provider {provider_name} relay key slot #{slot_index} does not match one API key"
                    )
                seen_provider_key_ids.add(slot_id)
                seen_slot_names.add(key_name)

        provider_source = provider.get(MENU_PROVIDER_SOURCE_KEY)
        if provider_source is not None:
            try:
                _provider_source(provider_source, required=True)
            except ValueError as exc:
                raise ValueError(
                    f"{path.name} provider {provider_name} {MENU_PROVIDER_SOURCE_KEY} is invalid"
                ) from exc
        provider_auth = provider.get(MENU_PROVIDER_AUTH_KEY)
        if provider_auth is not None:
            try:
                _provider_auth(provider_auth, required=True)
            except ValueError as exc:
                raise ValueError(
                    f"{path.name} provider {provider_name} {MENU_PROVIDER_AUTH_KEY} is invalid"
                ) from exc

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
                "supports_vision",
                "max_input_tokens",
                "context_metadata_source",
                "context_metadata_model_id",
            ):
                if unsupported_key in model_info:
                    replacement = (
                        "supports_responses_image_generation_tool"
                        if unsupported_key == "supports_image_generation"
                        else (
                            "upstream_url_surface"
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
            if UPSTREAM_URL_SURFACE_KEY in model_info:
                try:
                    _upstream_url_surface(model_info.get(UPSTREAM_URL_SURFACE_KEY))
                except ValueError as exc:
                    raise ValueError(
                        f"{path.name} {section_name}[{index}] {exc}"
                    ) from exc
            try:
                _upstream_protocol_mode(model_info.get(UPSTREAM_PROTOCOL_MODE_KEY))
            except ValueError as exc:
                raise ValueError(
                    f"{path.name} {section_name}[{index}] {exc}"
                ) from exc
            if MENU_PROVIDER_KEY_ID_KEY in model_info:
                try:
                    _provider_key_id(model_info.get(MENU_PROVIDER_KEY_ID_KEY), required=True)
                except ValueError as exc:
                    raise ValueError(
                        f"{path.name} {section_name}[{index}] {exc}"
                    ) from exc
            catalog_mode = model_info.get(MENU_RELAY_CATALOG_MODE_KEY, "independent")
            if not isinstance(catalog_mode, str) or catalog_mode not in MODEL_CATALOG_MODES:
                raise ValueError(
                    f"{path.name} {section_name}[{index}] {MENU_RELAY_CATALOG_MODE_KEY} is invalid"
                )
            if catalog_mode == "relay_linked" and not _string_value(
                model_info.get(MENU_PROVIDER_KEY_ID_KEY)
            ).strip():
                raise ValueError(
                    f"{path.name} {section_name}[{index}] relay-linked model needs {MENU_PROVIDER_KEY_ID_KEY}"
                )
            if MENU_RELAY_SOURCE_MODEL_KEY in model_info:
                try:
                    _menu_metadata_text(
                        model_info.get(MENU_RELAY_SOURCE_MODEL_KEY),
                        "Relay source model",
                        required=False,
                    )
                except ValueError as exc:
                    raise ValueError(
                        f"{path.name} {section_name}[{index}] {exc}"
                    ) from exc
            order_mode = model_info.get(MENU_ORDER_MODE_KEY, "manual")
            if not isinstance(order_mode, str) or order_mode not in MODEL_ORDER_MODES:
                raise ValueError(
                    f"{path.name} {section_name}[{index}] {MENU_ORDER_MODE_KEY} is invalid"
                )
            if MENU_MANUAL_ORDER_KEY in model_info:
                try:
                    _menu_order(model_info.get(MENU_MANUAL_ORDER_KEY), "Manual route order")
                except ValueError as exc:
                    raise ValueError(
                        f"{path.name} {section_name}[{index}] {exc}"
                    ) from exc


def safe_load_yaml_text(text: str, source_name: str) -> Any:
    """Load YAML with bounded aliases and structure, without schema validation."""

    try:
        _validate_yaml_event_limits(text)
        data = yaml.safe_load(text)
        _validate_loaded_yaml_limits(data)
    except _YamlStructureLimitExceeded:
        raise ValueError(f"{source_name} exceeds safe YAML structure limits") from None
    except yaml.YAMLError:
        raise ValueError(f"{source_name} is not valid YAML") from None
    return data


def load_yaml_text(text: str, path: pathlib.Path) -> dict[str, Any]:
    data = safe_load_yaml_text(text, path.name)
    if not isinstance(data, dict):
        raise ValueError(f"{path.name} must be a YAML mapping")
    _validate_current_schema(data, path)
    return data


def _load_yaml(path: pathlib.Path) -> dict[str, Any]:
    return load_yaml_text(path.read_text(encoding="utf-8"), path)


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
            "Close and reopen Providers & Models, then apply your changes again."
        )
