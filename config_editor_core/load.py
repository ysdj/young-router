from __future__ import annotations

from .schema import *

def _provider_to_editor(name: str, value: Any) -> dict[str, Any]:
    provider = _as_dict(value)
    api_keys = _provider_api_keys_from_raw(provider)
    api_key = api_keys[0]["value"] if api_keys else ""
    return {
        "name": name,
        "enabled": _bool_value(provider.get("enabled"), True),
        "api_base": _string_value(provider.get("api_base")),
        "api_key": api_key,
        "api_keys": api_keys,
        "models": [],
        "extra": {
            key: _jsonable(raw_value)
            for key, raw_value in provider.items()
            if key not in {"enabled", "api_base", "api_keys"}
        },
    }


def _provider_api_keys_from_raw(provider: dict[str, Any]) -> list[dict[str, Any]]:
    keys: list[dict[str, Any]] = []
    seen_names: set[str] = set()
    seen_values: set[str] = set()
    for index, item in enumerate(_as_list(provider.get("api_keys")), start=1):
        item_dict = _as_dict(item)
        key_name = _string_value(item_dict.get("name")).strip() or f"key-{index}"
        key_value = _string_value(item_dict.get("value"))
        if not key_value or key_name in seen_names or key_value in seen_values:
            continue
        keys.append({
            "name": key_name,
            "value": key_value,
            "enabled": True,
        })
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
    name = _slug(model_name)
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
            "name": _string_value(_as_dict(item).get("name")).strip(),
            "value": _string_value(_as_dict(item).get("value")),
            "enabled": _bool_value(_as_dict(item).get("enabled"), True),
        }
        for item in _as_list(provider.get("api_keys"))
        if _string_value(_as_dict(item).get("value"))
    ]
    key_name = _unique_key_name(keys, preferred_name)
    keys.append({"name": key_name, "value": api_key})
    provider["api_keys"] = keys
    return key_name


def _model_to_editor(
    model: Any,
    enabled: bool,
    known_providers: set[str],
    provider_by_pair: dict[tuple[str, str], tuple[str, str]],
    provider_by_key: dict[str, tuple[str, str]],
    provider_by_base: dict[str, str],
    provider_keys: dict[str, list[dict[str, Any]]],
) -> dict[str, Any]:
    entry = _as_dict(model)
    params = _as_dict(entry.get("litellm_params"))
    model_info = _as_dict(entry.get("model_info"))
    provider = _string_value(model_info.get("provider")).strip()
    api_base = _string_value(params.get("api_base"))
    api_key = _string_value(params.get("api_key"))
    api_key_name = ""
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
            "supports_responses_image_generation_tool",
            UPSTREAM_URL_SURFACE_KEY,
            SUPPORTED_UPSTREAM_URL_SURFACES_KEY,
            MENU_MODEL_ENABLED_KEY,
        }
    }
    supported_upstream_url_surfaces = _upstream_url_surfaces(
        model_info.get(SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
    )
    upstream_url_surface = supported_upstream_url_surfaces[0]
    entry_extra = {
        key: _jsonable(value)
        for key, value in entry.items()
        if key not in {"model_name", "litellm_params", "model_info"}
    }
    order = _string_value(params.get("order") if params.get("order") is not None else 1).strip() or "1"

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
        "order": order,
        "ssl_verify": _string_value(params.get("ssl_verify")) if "ssl_verify" in params else "",
        "ssl_verify_present": "ssl_verify" in params,
        "deployment_id": _editor_deployment_id(model_info.get("id")),
        "supports_responses_image_generation_tool": supports_responses_image_tool,
        "supports_responses_image_generation_tool_present": supports_responses_image_tool_present,
        "upstream_url_surface": upstream_url_surface,
        "supported_upstream_url_surfaces": supported_upstream_url_surfaces,
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
            "api_keys": [{"name": key_name, "value": api_key, "enabled": True}] if api_key else [],
            "models": [],
            "extra": {},
        }
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
    provider_index[provider]["models"].append(model)


def _refresh_effective_model_enabled(providers: list[dict[str, Any]]) -> None:
    for provider in providers:
        provider_enabled = _bool_value(provider.get("enabled"), True)
        for model in _as_list(provider.get("models")):
            model_dict = _as_dict(model)
            model_dict["enabled"] = (
                provider_enabled
                and _bool_value(model_dict.get("model_enabled"), _bool_value(model_dict.get("enabled"), True))
            )


def load_config(path: pathlib.Path = CONFIG_YAML) -> dict[str, Any]:
    data = _load_yaml(path)
    raw_providers = _as_dict(data.get("providers"))
    providers = [_provider_to_editor(name, raw) for name, raw in raw_providers.items()]
    provider_index = {provider["name"]: provider for provider in providers}
    known_provider_names = set(provider_index.keys())
    provider_by_pair: dict[tuple[str, str], tuple[str, str]] = {}
    provider_by_key: dict[str, tuple[str, str]] = {}
    provider_by_base: dict[str, str] = {}
    provider_keys: dict[str, list[dict[str, Any]]] = {}

    for name, provider in provider_index.items():
        api_base = _string_value(provider.get("api_base"))
        keys = [
            {
                "name": _string_value(_as_dict(item).get("name")).strip(),
                "value": _string_value(_as_dict(item).get("value")),
                "enabled": True,
            }
            for item in _as_list(provider.get("api_keys"))
            if _string_value(_as_dict(item).get("value"))
        ]
        provider_keys[name] = keys
        for item in keys:
            api_key = item["value"]
            key_name = item["name"]
            if api_base and api_key and (api_base, api_key) not in provider_by_pair:
                provider_by_pair[(api_base, api_key)] = (name, key_name)
            if api_key and api_key not in provider_by_key:
                provider_by_key[api_key] = (name, key_name)
        if api_base and api_base not in provider_by_base:
            provider_by_base[api_base] = name

    for item in _as_list(data.get("model_list")):
        model = _model_to_editor(item, True, known_provider_names, provider_by_pair, provider_by_key, provider_by_base, provider_keys)
        _append_model_to_provider(providers, provider_index, model)

    disabled_path = _disabled_models_path(path)
    if disabled_path.exists():
        disabled_data = _load_yaml(disabled_path)
        for item in _as_list(disabled_data.get(DISABLED_MODELS_KEY)):
            model = _model_to_editor(item, False, known_provider_names, provider_by_pair, provider_by_key, provider_by_base, provider_keys)
            _append_model_to_provider(providers, provider_index, model)

    _refresh_effective_model_enabled(providers)
    return {"providers": providers, "revision": _config_revision(path)}

__all__ = [name for name in globals() if not name.startswith("__")]
