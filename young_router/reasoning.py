from __future__ import annotations

from collections.abc import Mapping
import os
from pathlib import Path
from typing import Any

from .core.model_contexts import (
    ModelContextRegistry,
    ReasoningCapability,
    default_context_cache_path,
    legacy_context_cache_paths,
)
from . import request_context as _request_context_module


_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
_registry_state: tuple[tuple[str, str, str, int | None], ModelContextRegistry] | None = None


def _runtime_root() -> Path:
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "YOUNG_ROUTER_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".young-router"


def _runtime_config_path() -> Path:
    configured = os.environ.get("LITELLM_CONFIG_FILE", "").strip()
    return Path(configured).expanduser() if configured else _runtime_root() / "config.yaml"


def _runtime_settings_path() -> Path:
    configured = os.environ.get("YOUNG_ROUTER_RUNTIME_SETTINGS_FILE", "").strip()
    return Path(configured).expanduser() if configured else _runtime_root() / "runtime-settings.env"


def _codex_home() -> Path:
    configured_home = os.environ.get("CODEX_HOME", "").strip()
    return Path(configured_home).expanduser() if configured_home else Path.home() / ".codex"


def _reasoning_cache_path() -> Path:
    return default_context_cache_path(_runtime_root())


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
        legacy_cache_paths=legacy_context_cache_paths(_codex_home()),
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


def _without_reasoning_parameters(value: Any) -> Any:
    """Return the same request with every client reasoning parameter removed.

    A route that cannot translate a client reasoning request does not need one:
    the model keeps its provider default thinking behavior while the optional
    ``reasoning``/``reasoning_effort`` fields are the only part of the request
    the upstream rejected. Return the original object when there is nothing to
    remove, so callers can treat identity as "no change".
    """

    if not isinstance(value, dict):
        return value

    changed = False
    updated: dict[Any, Any] = {}
    for key, item in value.items():
        if key in {"reasoning_effort", "reasoning"}:
            changed = True
            continue
        if key in {"extra_body", "litellm_params"} and isinstance(item, dict):
            stripped = _without_reasoning_parameters(item)
            if stripped is not item:
                changed = True
                updated[key] = stripped
                continue
        updated[key] = item
    return (updated if changed else value)


def _request_has_reasoning_parameters(request_kwargs: Any) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    return _without_reasoning_parameters(request_kwargs) is not request_kwargs


# Google's OpenAI-compatible surface takes the native thinking configuration
# under an ``extra_body.google`` namespace, and Gemini 3 relays that cannot map
# an aliased model through their own OpenAI reasoning table still honor it.
# Only levels those relays translate correctly are sent: xhigh/max are clamped
# down by the gateway itself, so they are mapped here instead.
_GOOGLE_THINKING_CONFIG_MODE = "google_thinking_level"
_REASONING_STRIP_MODE = "strip"
_GOOGLE_THINKING_LEVEL_BY_EFFORT = {
    "none": "minimal",
    "minimal": "minimal",
    "low": "low",
    "medium": "medium",
    "high": "high",
    "xhigh": "high",
    "max": "high",
}


def _request_reasoning_level(request_kwargs: Any) -> str | None:
    """Return the client's canonical reasoning level from any request shape."""

    if not isinstance(request_kwargs, dict):
        return None
    level = _canonical_reasoning_level(request_kwargs.get("reasoning_effort"))
    if level is not None:
        return level
    reasoning = request_kwargs.get("reasoning")
    if isinstance(reasoning, dict):
        level = _canonical_reasoning_level(reasoning.get("effort"))
        if level is not None:
            return level
    for container_key in ("extra_body", "litellm_params"):
        container = request_kwargs.get(container_key)
        if not isinstance(container, dict):
            continue
        level = _canonical_reasoning_level(container.get("reasoning_effort"))
        if level is not None:
            return level
        nested = container.get("reasoning")
        if isinstance(nested, dict):
            level = _canonical_reasoning_level(nested.get("effort"))
            if level is not None:
                return level
    return None


def _with_google_thinking_level(request_kwargs: dict) -> dict | None:
    """Translate the client's reasoning level into the native Google shape.

    The rejected OpenAI fields are removed in the same pass.  A client that
    already chose a native thinking configuration keeps it untouched.

    Relays built on the OpenAI-SDK convention merge a literal ``extra_body``
    field themselves, while litellm flattens its own ``extra_body`` kwarg into
    top-level body fields.  Nesting the Google namespace once inside that kwarg
    is what makes the gateway actually see the thinking configuration; a
    top-level ``google`` key is ignored there.
    """

    if not isinstance(request_kwargs, dict):
        return None
    level = _request_reasoning_level(request_kwargs)
    if level is None:
        return None
    thinking_level = _GOOGLE_THINKING_LEVEL_BY_EFFORT.get(level)
    if thinking_level is None:
        return None
    stripped = _without_reasoning_parameters(request_kwargs)
    if not isinstance(stripped, dict):
        return None
    updated = dict(stripped)
    extra_body = updated.get("extra_body")
    extra_body = dict(extra_body) if isinstance(extra_body, dict) else {}
    gateway_body = extra_body.get("extra_body")
    gateway_body = dict(gateway_body) if isinstance(gateway_body, dict) else {}
    google = gateway_body.get("google")
    google = dict(google) if isinstance(google, dict) else {}
    thinking_config = google.get("thinking_config")
    thinking_config = (
        dict(thinking_config) if isinstance(thinking_config, dict) else {}
    )
    if any(
        key in thinking_config for key in ("thinking_level", "thinking_budget")
    ):
        return None
    thinking_config["thinking_level"] = thinking_level
    google["thinking_config"] = thinking_config
    gateway_body["google"] = google
    extra_body["extra_body"] = gateway_body
    updated["extra_body"] = extra_body
    return updated


def _with_model_reasoning_parameter_support(request_kwargs: dict) -> dict | None:
    """Normalize client reasoning fields for a route learned to refuse them."""

    from . import routing as _routing_module

    if not _request_has_reasoning_parameters(request_kwargs):
        return None
    state = _routing_module._reasoning_parameters_compat_cached(request_kwargs)
    if not isinstance(state, dict):
        return None
    mode = state.get("mode")
    applied: dict | None = None
    if mode == _GOOGLE_THINKING_CONFIG_MODE:
        applied = _with_google_thinking_level(request_kwargs)
    if applied is None:
        applied = _without_reasoning_parameters(request_kwargs)
        mode = _REASONING_STRIP_MODE
    if not isinstance(applied, dict) or applied is request_kwargs:
        return None
    _routing_module._trace_reasoning_parameters_compat_applied(
        applied,
        mode=mode,
    )
    return applied


def _mark_reasoning_parameters_compat_retry(retry_kwargs: dict) -> dict:
    metadata = _request_context_module._request_metadata_dict(
        retry_kwargs, "litellm_metadata"
    )
    retry_metadata = dict(metadata) if metadata else {}
    retry_metadata[
        _routing_module_ref()._REASONING_PARAMETERS_COMPAT_RETRY_METADATA_KEY
    ] = True
    retry_kwargs["litellm_metadata"] = retry_metadata
    return retry_kwargs


def _routing_module_ref():
    from . import routing as _routing_module

    return _routing_module


def _reasoning_parameters_compat_retry_candidates(
    exception: Exception,
    request_kwargs: Any,
) -> list[tuple[str, dict]]:
    """Build the compatible replays for a thinking-configuration rejection.

    The native Google thinking shape keeps the client's requested level and is
    tried first for a Gemini-family upstream; the field-stripping replay keeps
    every other route working with the provider default thinking behavior.
    """

    routing = _routing_module_ref()
    if not isinstance(request_kwargs, dict):
        return []
    if routing._request_attempted_reasoning_parameters_compat_retry(request_kwargs):
        return []
    if not routing._is_reasoning_configuration_unsupported_error(exception):
        return []
    candidates: list[tuple[str, dict]] = []
    model = request_kwargs.get("model")
    if isinstance(model, str) and "gemini" in model.casefold():
        google_kwargs = _with_google_thinking_level(request_kwargs)
        if google_kwargs is not None:
            candidates.append(
                (
                    _GOOGLE_THINKING_CONFIG_MODE,
                    _mark_reasoning_parameters_compat_retry(google_kwargs),
                )
            )
    stripped = _without_reasoning_parameters(request_kwargs)
    if isinstance(stripped, dict) and stripped is not request_kwargs:
        candidates.append(
            (
                _REASONING_STRIP_MODE,
                _mark_reasoning_parameters_compat_retry(dict(stripped)),
            )
        )
    return candidates


__all__ = [
    "_with_model_reasoning_mapping",
    "_with_model_reasoning_parameter_support",
    "_reasoning_parameters_compat_retry_candidates",
    "_request_has_reasoning_parameters",
    "_request_reasoning_level",
    "_without_reasoning_parameters",
    "_with_google_thinking_level",
]
