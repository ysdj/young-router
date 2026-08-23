from __future__ import annotations

import pathlib
import re
from typing import Any

from .schema import (
    CONFIG_YAML,
    DEFAULT_API_KEY_NAME,
    DISABLED_MODELS_KEY,
    MENU_API_KEY_NAME_KEY,
    MENU_MANUAL_ORDER_KEY,
    MENU_MODEL_ENABLED_KEY,
    MENU_ORDER_MODE_KEY,
    MENU_PROVIDER_KEY_ID_KEY,
    MENU_PROVIDER_AUTH_KEY,
    MENU_RELAY_CATALOG_MODE_KEY,
    MENU_RELAY_KEYS_KEY,
    MENU_RELAY_KEYS_VERSION,
    MENU_RELAY_SOURCE_MODEL_KEY,
    MENU_ROUTE_KEY,
    MENU_PROVIDER_SOURCE_KEY,
    UPSTREAM_PROTOCOL_MODE_KEY,
    UPSTREAM_URL_SURFACE_KEY,
    _as_dict,
    _as_list,
    _bool_value,
    _config_revision,
    _disabled_models_path,
    _editor_deployment_id,
    _jsonable,
    _menu_order,
    _provider_key_id,
    _provider_auth,
    _provider_source,
    _relay_source,
    _stable_provider_key_id,
    _string_value,
    _upstream_protocol_mode,
    _upstream_url_surface,
    infer_upstream_fallback_surface,
    load_yaml_text,
)


CONFIG_DOCUMENT_CONFIG_KEY = "config"
CONFIG_DOCUMENT_DISABLED_KEY = "disabled"
CONFIG_DOCUMENT_KEYS = {CONFIG_DOCUMENT_CONFIG_KEY, CONFIG_DOCUMENT_DISABLED_KEY}

def _provider_to_editor(name: str, value: Any) -> dict[str, Any]:
    provider = _as_dict(value)
    api_keys = _provider_api_keys_from_raw(name, provider)
    api_key = api_keys[0]["value"] if api_keys else ""
    source = _provider_source(provider.get(MENU_PROVIDER_SOURCE_KEY))
    auth = _provider_auth(provider.get(MENU_PROVIDER_AUTH_KEY))
    extra = {
        key: _jsonable(raw_value)
        for key, raw_value in provider.items()
        if key not in {"enabled", "api_base", "api_keys"}
    }
    # Legacy documents get deterministic independent IDs on first load. The
    # dumper persists them in this existing provider-extra extension without
    # ever copying credential values into metadata.
    extra[MENU_RELAY_KEYS_KEY] = _relay_key_metadata(api_keys)
    return {
        "name": name,
        "enabled": _bool_value(provider.get("enabled"), True),
        "api_base": _string_value(provider.get("api_base")),
        "api_key": api_key,
        "api_keys": api_keys,
        "models": [],
        "extra": extra,
        "provider_type": source["kind"],
        "relay_station_id": source.get("station_id", ""),
        "auth_kind": auth["kind"],
        "auth_credential_ref": auth.get("credential_ref", ""),
    }


def _relay_key_metadata(api_keys: list[dict[str, Any]]) -> dict[str, Any]:
    """Persist stable ProviderKey slots separately from credential values."""

    return {
        "version": MENU_RELAY_KEYS_VERSION,
        "slots": [
            {
                "id": _provider_key_id(item.get("id"), required=True),
                "api_key_name": _string_value(item.get("name")).strip(),
                "source": _relay_source(item.get("source")),
            }
            for item in api_keys
        ],
    }


def _provider_api_keys_from_raw(provider_name: str, provider: dict[str, Any]) -> list[dict[str, Any]]:
    raw_slots = _as_dict(provider.get(MENU_RELAY_KEYS_KEY)).get("slots")
    slots_by_name: dict[str, dict[str, Any]] = {}
    if isinstance(raw_slots, list):
        for raw_slot in raw_slots:
            slot = _as_dict(raw_slot)
            key_name = _string_value(slot.get("api_key_name")).strip()
            if key_name:
                slots_by_name[key_name] = slot
    keys: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_values: set[str] = set()
    for index, item in enumerate(_as_list(provider.get("api_keys")), start=1):
        item_dict = _as_dict(item)
        key_name = _string_value(item_dict.get("name")).strip() or f"key-{index}"
        key_value = _string_value(item_dict.get("value"))
        if not key_value or key_name in seen_names or key_value in seen_values:
            continue
        slot = slots_by_name.get(key_name, {})
        keys.append(
            {
                "id": _provider_key_id(slot.get("id"))
                or _stable_provider_key_id(provider_name, key_name),
                "name": key_name,
                "value": key_value,
                "source": _relay_source(slot.get("source")),
            }
        )
        seen_names.add(key_name)
        seen_values.add(key_value)
    return keys


def _unique_key_name(existing: list[dict[str, Any]], preferred: str) -> str:
    base = preferred.strip() or DEFAULT_API_KEY_NAME
    used = {str(item.get("name", "")).strip() for item in existing}
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"


def _key_name_from_model_name(model_name: str) -> str:
    name = re.sub(r"[^A-Za-z0-9_.-]+", "-", model_name.strip())
    name = re.sub(r"-+", "-", name).strip("-")
    if not name:
        return "imported"
    return name[:40]


def _key_name_for_value(provider: dict[str, Any], api_key: str) -> str:
    for item in _as_list(provider.get("api_keys")):
        item_dict = _as_dict(item)
        if _string_value(item_dict.get("value")) == api_key:
            return _string_value(item_dict.get("name")).strip()
    return ""


def _ensure_provider_key(provider: dict[str, Any], api_key: str, preferred_name: str = DEFAULT_API_KEY_NAME) -> str:
    api_key = _string_value(api_key)
    if not api_key:
        keys = _as_list(provider.get("api_keys"))
        if len(keys) == 1:
            return _string_value(_as_dict(keys[0]).get("name")).strip()
        return ""

    existing = _key_name_for_value(provider, api_key)
    if existing:
        return existing

    keys = [
        {
            "id": _provider_key_id(_as_dict(item).get("id"))
            or _stable_provider_key_id(provider.get("name"), _string_value(_as_dict(item).get("name")).strip()),
            "name": _string_value(_as_dict(item).get("name")).strip(),
            "value": _string_value(_as_dict(item).get("value")),
            "source": _relay_source(_as_dict(item).get("source")),
        }
        for item in _as_list(provider.get("api_keys"))
        if _string_value(_as_dict(item).get("value"))
    ]
    key_name = _unique_key_name(keys, preferred_name)
    keys.append(
        {
            "id": _stable_provider_key_id(provider.get("name"), key_name),
            "name": key_name,
            "value": api_key,
            "source": {"kind": "independent"},
        }
    )
    provider["api_keys"] = keys
    extra = dict(_as_dict(provider.get("extra")))
    extra[MENU_RELAY_KEYS_KEY] = _relay_key_metadata(keys)
    provider["extra"] = extra
    return key_name


def _model_to_editor(
    model: Any,
    enabled: bool,
    known_providers: set[str],
    provider_by_pair: dict[tuple[str, str], tuple[str, str]],
    provider_by_key: dict[str, tuple[str, str]],
    provider_by_base: dict[str, str],
    provider_keys: dict[str, list[dict[str, Any]]],
    provider_by_key_id: dict[str, tuple[str, str]],
) -> dict[str, Any]:
    entry = _as_dict(model)
    params = _as_dict(entry.get("litellm_params"))
    model_info = _as_dict(entry.get("model_info"))
    provider = _string_value(model_info.get("provider")).strip()
    api_base = _string_value(params.get("api_base"))
    api_key = _string_value(params.get("api_key"))
    api_key_name = _string_value(model_info.get(MENU_API_KEY_NAME_KEY)).strip()
    provider_key_id = _provider_key_id(model_info.get(MENU_PROVIDER_KEY_ID_KEY))
    if provider_key_id:
        match = provider_by_key_id.get(provider_key_id)
        if match:
            provider, api_key_name = match
    if not provider:
        match = provider_by_pair.get((api_base, api_key))
        if match:
            provider, api_key_name = match
    if not provider and api_key:
        match = provider_by_key.get(api_key)
        if match:
            provider, api_key_name = match
    if not provider and api_base:
        provider = provider_by_base.get(api_base, "")
    if not provider and len(known_providers) == 1:
        provider = next(iter(known_providers))
    if provider and api_key and not api_key_name:
        for item in provider_keys.get(provider, []):
            if item.get("value") == api_key:
                api_key_name = item.get("name", "")
                break
    if provider and not provider_key_id:
        for item in provider_keys.get(provider, []):
            if api_key_name and item.get("name") == api_key_name:
                provider_key_id = _string_value(item.get("id")).strip()
                break
            if api_key and item.get("value") == api_key:
                provider_key_id = _string_value(item.get("id")).strip()
                break

    litellm_extra = {
        key: _jsonable(value)
        for key, value in params.items()
        if key not in {"model", "api_base", "api_key", "order", "ssl_verify"}
    }
    model_info_extra = {
        key: _jsonable(value)
        for key, value in model_info.items()
        if key not in {
            "id",
            "provider",
            MENU_ROUTE_KEY,
            MENU_API_KEY_NAME_KEY,
            MENU_PROVIDER_KEY_ID_KEY,
            MENU_RELAY_CATALOG_MODE_KEY,
            MENU_RELAY_SOURCE_MODEL_KEY,
            MENU_ORDER_MODE_KEY,
            MENU_MANUAL_ORDER_KEY,
            "supports_responses_image_generation_tool",
            UPSTREAM_PROTOCOL_MODE_KEY,
            UPSTREAM_URL_SURFACE_KEY,
            "supported_upstream_url_surfaces",
            "x-litellm-menu-upstream-url-surface-order",
            MENU_MODEL_ENABLED_KEY,
        }
    }
    raw_upstream_url_surface = model_info.get(UPSTREAM_URL_SURFACE_KEY)
    upstream_url_surface = (
        _upstream_url_surface(raw_upstream_url_surface)
        if raw_upstream_url_surface is not None
        else infer_upstream_fallback_surface(params.get("model"))
    )
    upstream_protocol_mode = _upstream_protocol_mode(
        model_info.get(UPSTREAM_PROTOCOL_MODE_KEY)
    )
    entry_extra = {
        key: _jsonable(value)
        for key, value in entry.items()
        if key not in {"model_name", "litellm_params", "model_info"}
    }
    order = _string_value(params.get("order") if params.get("order") is not None else 1).strip() or "1"
    manual_order = _string_value(model_info.get(MENU_MANUAL_ORDER_KEY)).strip() or order
    order_mode = _string_value(model_info.get(MENU_ORDER_MODE_KEY)).strip() or "manual"
    catalog_mode = _string_value(model_info.get(MENU_RELAY_CATALOG_MODE_KEY)).strip() or "independent"
    effective_order = _menu_order(order, "Route order")

    supports_responses_image_tool = bool(
        model_info.get("supports_responses_image_generation_tool")
    )
    supports_responses_image_tool_present = (
        "supports_responses_image_generation_tool" in model_info
    )

    return {
        "enabled": enabled,
        "model_enabled": _bool_value(model_info.get(MENU_MODEL_ENABLED_KEY), enabled),
        "provider": provider,
        "model_name": _string_value(entry.get("model_name")),
        "litellm_model": _string_value(params.get("model")),
        "api_base": api_base,
        "api_key": api_key,
        "api_key_name": api_key_name,
        "provider_key_id": provider_key_id,
        "catalog_mode": catalog_mode,
        "source_model_id": _string_value(model_info.get(MENU_RELAY_SOURCE_MODEL_KEY)).strip(),
        "order_mode": order_mode,
        "manual_order": manual_order,
        "effective_order": effective_order,
        "order": order,
        "ssl_verify": _string_value(params.get("ssl_verify")) if "ssl_verify" in params else "",
        "ssl_verify_present": "ssl_verify" in params,
        "deployment_id": _editor_deployment_id(model_info.get("id")),
        "supports_responses_image_generation_tool": supports_responses_image_tool,
        "supports_responses_image_generation_tool_present": supports_responses_image_tool_present,
        "upstream_url_surface": upstream_url_surface,
        "upstream_protocol_mode": upstream_protocol_mode,
        "entry_extra": entry_extra,
        "litellm_extra": litellm_extra,
        "model_info_extra": model_info_extra,
    }


def _append_model_to_provider(
    providers: list[dict[str, Any]],
    provider_index: dict[str, dict[str, Any]],
    model: dict[str, Any],
) -> None:
    provider = str(model.get("provider", "")).strip()
    if provider not in provider_index:
        api_key = str(model.get("api_key", "")).strip()
        key_name = str(model.get("api_key_name", "")).strip() or DEFAULT_API_KEY_NAME
        provider_index[provider] = {
            "name": provider,
            "enabled": True,
            "api_base": str(model.get("api_base", "")).strip(),
            "api_key": api_key,
            "api_keys": [
                {
                    "id": _string_value(model.get("provider_key_id")).strip()
                    or _stable_provider_key_id(provider, key_name),
                    "name": key_name,
                    "value": api_key,
                    "source": {"kind": "independent"},
                }
            ]
            if api_key
            else [],
            "models": [],
            "extra": {},
        }
        provider_index[provider]["extra"][MENU_RELAY_KEYS_KEY] = _relay_key_metadata(
            provider_index[provider]["api_keys"]
        )
        providers.append(provider_index[provider])
    provider_entry = provider_index[provider]
    if not str(provider_entry.get("api_base", "")).strip():
        provider_entry["api_base"] = str(model.get("api_base", "")).strip()
    key_name = _ensure_provider_key(
        provider_entry,
        str(model.get("api_key", "")).strip(),
        str(model.get("api_key_name", "")).strip() or _key_name_from_model_name(str(model.get("model_name", ""))),
    )
    if key_name:
        model["api_key_name"] = key_name
        for item in _as_list(provider_entry.get("api_keys")):
            item_dict = _as_dict(item)
            if _string_value(item_dict.get("name")).strip() == key_name:
                model["provider_key_id"] = _string_value(item_dict.get("id")).strip()
                break
    provider_index[provider]["models"].append(model)


def _refresh_model_enabled_states(providers: list[dict[str, Any]]) -> None:
    for provider in providers:
        for model in _as_list(provider.get("models")):
            model_dict = _as_dict(model)
            model_dict["enabled"] = _bool_value(
                model_dict.get("model_enabled"),
                _bool_value(model_dict.get("enabled"), True),
            )


def normalize_config_document(document: Any) -> dict[str, str | None]:
    if not isinstance(document, dict) or set(document) != CONFIG_DOCUMENT_KEYS:
        raise ValueError("Config document must contain exactly config and disabled fields")

    config_text = document.get(CONFIG_DOCUMENT_CONFIG_KEY)
    disabled_text = document.get(CONFIG_DOCUMENT_DISABLED_KEY)
    if not isinstance(config_text, str):
        raise ValueError("Config document config must be text")
    if disabled_text is not None and not isinstance(disabled_text, str):
        raise ValueError("Config document disabled must be text or null")

    load_yaml_text(config_text, pathlib.Path("config.yaml"))
    if disabled_text is not None:
        load_yaml_text(disabled_text, pathlib.Path("config.disabled-models.yaml"))
    return {
        CONFIG_DOCUMENT_CONFIG_KEY: config_text,
        CONFIG_DOCUMENT_DISABLED_KEY: disabled_text,
    }


def config_document_from_path(path: pathlib.Path = CONFIG_YAML) -> dict[str, str | None]:
    config_text = path.read_text(encoding="utf-8")
    disabled_path = _disabled_models_path(path)
    disabled_text = (
        disabled_path.read_text(encoding="utf-8") if disabled_path.exists() else None
    )
    return normalize_config_document(
        {
            CONFIG_DOCUMENT_CONFIG_KEY: config_text,
            CONFIG_DOCUMENT_DISABLED_KEY: disabled_text,
        }
    )


def load_config_document(document: Any) -> dict[str, Any]:
    normalized_document = normalize_config_document(document)
    data = load_yaml_text(
        normalized_document[CONFIG_DOCUMENT_CONFIG_KEY], pathlib.Path("config.yaml")
    )
    raw_providers = _as_dict(data.get("providers"))
    providers = [_provider_to_editor(name, raw) for name, raw in raw_providers.items()]
    provider_index = {provider["name"]: provider for provider in providers}
    known_provider_names = set(provider_index.keys())
    provider_by_pair: dict[tuple[str, str], tuple[str, str]] = {}
    provider_by_key: dict[str, tuple[str, str]] = {}
    provider_by_base: dict[str, str] = {}
    provider_keys: dict[str, list[dict[str, Any]]] = {}
    provider_by_key_id: dict[str, tuple[str, str]] = {}

    for name, provider in provider_index.items():
        api_base = _string_value(provider.get("api_base"))
        keys = [
            {
                "id": _string_value(_as_dict(item).get("id")).strip(),
                "name": _string_value(_as_dict(item).get("name")).strip(),
                "value": _string_value(_as_dict(item).get("value")),
                "source": _relay_source(_as_dict(item).get("source")),
            }
            for item in _as_list(provider.get("api_keys"))
            if _string_value(_as_dict(item).get("value"))
        ]
        provider_keys[name] = keys
        for item in keys:
            api_key = item["value"]
            key_name = item["name"]
            provider_key_id = item["id"]
            if provider_key_id:
                provider_by_key_id[provider_key_id] = (name, key_name)
            if api_base and api_key and (api_base, api_key) not in provider_by_pair:
                provider_by_pair[(api_base, api_key)] = (name, key_name)
            if api_key and api_key not in provider_by_key:
                provider_by_key[api_key] = (name, key_name)
        if api_base and api_base not in provider_by_base:
            provider_by_base[api_base] = name

    for item in _as_list(data.get("model_list")):
        model = _model_to_editor(
            item,
            True,
            known_provider_names,
            provider_by_pair,
            provider_by_key,
            provider_by_base,
            provider_keys,
            provider_by_key_id,
        )
        _append_model_to_provider(providers, provider_index, model)

    disabled_text = normalized_document[CONFIG_DOCUMENT_DISABLED_KEY]
    if disabled_text is not None:
        disabled_data = load_yaml_text(
            disabled_text, pathlib.Path("config.disabled-models.yaml")
        )
        for item in _as_list(disabled_data.get(DISABLED_MODELS_KEY)):
            model = _model_to_editor(
                item,
                False,
                known_provider_names,
                provider_by_pair,
                provider_by_key,
                provider_by_base,
                provider_keys,
                provider_by_key_id,
            )
            _append_model_to_provider(providers, provider_index, model)

    _refresh_model_enabled_states(providers)
    return {"providers": providers, "document": normalized_document}


def load_config(path: pathlib.Path = CONFIG_YAML) -> dict[str, Any]:
    payload = load_config_document(config_document_from_path(path))
    payload["revision"] = _config_revision(path)
    return payload
