from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

from .core.model_contexts import (
    ModelContextRegistry,
    ReasoningCapability,
    default_context_cache_path,
)
from . import request_context as _request_context_module


_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_registry_state: tuple[tuple[str, str, str, int | None], ModelContextRegistry] | None = None


def _runtime_root() -> Path:
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _runtime_config_path() -> Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    return Path(configured).expanduser() if configured else _runtime_root() / "config.yaml"


def _runtime_settings_path() -> Path:
    configured = os.environ.get("LITELLM_MENU_RUNTIME_SETTINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else _runtime_root() / "runtime-settings.env"


def _reasoning_cache_path() -> Path:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    codex_home = Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"
    return default_context_cache_path(codex_home)


def _cache_mtime_ns(path: Path) -> int | None:
    try:
        return path.stat().st_mtime_ns
    except FileNotFoundError:
        return None


def _reasoning_registry() -> ModelContextRegistry:
    global _registry_state

    runtime_config = _runtime_config_path()
    runtime_settings = _runtime_settings_path()
    cache = _reasoning_cache_path()
    key = (
        str(runtime_config),
        str(runtime_settings),
        str(cache),
        _cache_mtime_ns(cache),
    )
    if _registry_state is not None and _registry_state[0] == key:
        return _registry_state[1]
    registry = ModelContextRegistry(
        runtime_config_path=runtime_config,
        runtime_settings_path=runtime_settings,
        cache_path=cache,
        refresh_enabled=False,
    )
    _registry_state = (key, registry)
    return registry


def _canonical_reasoning_level(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if normalized == "off":
        normalized = "none"
    return normalized if normalized in _REASONING_LEVELS else None


def _clamp_reasoning_level(
    requested: str,
    supported_levels: tuple[str, ...],
) -> str | None:
    if requested in supported_levels:
        return requested
    requested_index = _REASONING_LEVELS.index(requested)
    for candidate in _REASONING_LEVELS[requested_index:]:
        if candidate in supported_levels:
            return candidate
    for candidate in reversed(_REASONING_LEVELS[:requested_index]):
        if candidate in supported_levels:
            return candidate
    return supported_levels[0] if supported_levels else None


def _mapped_reasoning_effort(
    value: object,
    capability: ReasoningCapability,
) -> object:
    requested = _canonical_reasoning_level(value)
    if requested is None:
        return value
    selected = _clamp_reasoning_level(requested, capability.supported_levels)
    if selected is None:
        return value
    mapping = capability.thinking_level_map
    if mapping is not None and selected in mapping:
        mapped = mapping[selected]
        if mapped is not None:
            return mapped
    return selected


def _map_reasoning_fields(
    value: Any,
    capability: ReasoningCapability,
    *,
    in_reasoning: bool = False,
) -> tuple[Any, bool]:
    if not isinstance(value, dict):
        mapped = _mapped_reasoning_effort(value, capability) if in_reasoning else value
        return mapped, mapped != value

    changed = False
    updated: dict[Any, Any] = {}
    for key, item in value.items():
        if key == "reasoning_effort":
            mapped = _mapped_reasoning_effort(item, capability)
            updated[key] = mapped
            changed = changed or mapped != item
            continue
        if key == "reasoning" and isinstance(item, dict):
            mapped, item_changed = _map_reasoning_fields(
                item,
                capability,
                in_reasoning=True,
            )
            updated[key] = mapped
            changed = changed or item_changed
            continue
        if in_reasoning and key == "effort":
            mapped = _mapped_reasoning_effort(item, capability)
            updated[key] = mapped
            changed = changed or mapped != item
            continue
        if key in {"extra_body", "litellm_params"} and isinstance(item, dict):
            mapped, item_changed = _map_reasoning_fields(item, capability)
            updated[key] = mapped
            changed = changed or item_changed
            continue
        updated[key] = item
    return (updated if changed else value), changed


def _provider_names(request_kwargs: Mapping[str, Any]) -> list[str]:
    litellm_params = request_kwargs.get("litellm_params")
    litellm_params = litellm_params if isinstance(litellm_params, Mapping) else {}
    model_info = _request_context_module._request_model_info(dict(request_kwargs))
    result: list[str] = []
    for value in (
        litellm_params.get("custom_llm_provider"),
        request_kwargs.get("custom_llm_provider"),
        model_info.get("provider"),
    ):
        if not isinstance(value, str):
            continue
        provider = value.strip().casefold()
        if provider and provider not in result:
            result.append(provider)
    return result


def _deployment_model_ids(request_kwargs: Mapping[str, Any]) -> list[str]:
    litellm_params = request_kwargs.get("litellm_params")
    litellm_params = litellm_params if isinstance(litellm_params, Mapping) else {}
    model_info = _request_context_module._request_model_info(dict(request_kwargs))
    providers = _provider_names(request_kwargs)
    result: list[str] = []
    for value in (
        litellm_params.get("model"),
        model_info.get("model"),
        request_kwargs.get("model"),
    ):
        if not isinstance(value, str) or not value.strip():
            continue
        model_id = value.strip()
        candidates = [
            f"{provider}/{model_id}"
            for provider in providers
            if not model_id.casefold().startswith(f"{provider}/")
        ] + [model_id]
        for candidate in candidates:
            if candidate not in result:
                result.append(candidate)
    return result


def _reasoning_capability_for_request(
    request_kwargs: Mapping[str, Any],
    registry: ModelContextRegistry,
) -> ReasoningCapability | None:
    for model_id in _deployment_model_ids(request_kwargs):
        capability = registry.reasoning_for_model_id(model_id)
        if capability is not None:
            return capability
    return None


def _with_model_reasoning_mapping(request_kwargs: dict) -> dict | None:
    capability = _reasoning_capability_for_request(request_kwargs, _reasoning_registry())
    if capability is None:
        return None
    mapped, changed = _map_reasoning_fields(request_kwargs, capability)
    return mapped if changed and isinstance(mapped, dict) else None


__all__ = ["_with_model_reasoning_mapping"]
