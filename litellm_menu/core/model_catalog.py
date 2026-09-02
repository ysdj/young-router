"""Managed Codex model catalog sourced from LiteLLM and native Codex metadata."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .model_contexts import ModelContextRegistry
from .native_codex_catalog import load_native_catalog
from .persistence import PersistenceError, atomic_write_json, read_json


CATALOG_FILE_NAME = "litellm-menu-model-catalog.json"
CATALOG_DESCRIPTION = "LiteLLM Menu exposed model"
_REASONING_LEVELS = (
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth"},
    {"effort": "high", "description": "Greater reasoning depth for complex tasks"},
    {"effort": "xhigh", "description": "Extra reasoning depth for complex tasks"},
)
_REASONING_DESCRIPTIONS = {
    "none": "No reasoning",
    "minimal": "Minimal reasoning for faster responses",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth for complex tasks",
    "xhigh": "Extra reasoning depth for complex tasks",
    "max": "Maximum reasoning depth for the hardest tasks",
}


def managed_catalog_path(codex_home: Path | str) -> Path:
    return Path(codex_home).expanduser() / CATALOG_FILE_NAME


def selected_model_names(config: object) -> list[str]:
    """Return the explicitly configured Codex and review models, in order."""

    if not isinstance(config, Mapping):
        return []
    names: list[str] = []
    seen: set[str] = set()
    for key in ("model", "review_model"):
        value = config.get(key)
        if not isinstance(value, str):
            continue
        name = value.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def catalog_names_from_editor(payload: object) -> list[str]:
    """Return public model names available to the Codex model catalog.

    ``exposed_models`` is populated from the authenticated local LiteLLM
    ``/v1/models`` response and is the sole source of catalog entries. The
    active and review models are kept first *only when LiteLLM exposes them*;
    every other exposed ID follows in endpoint order. Configured deployments,
    protocol/mode metadata, route health, and stale selected names are never
    used as fallbacks. An unavailable Menu therefore yields an empty catalog.
    """

    if not isinstance(payload, Mapping):
        return []

    names: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        name = value.strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)

    exposed = payload.get("exposed_models")
    if not isinstance(exposed, Sequence) or isinstance(exposed, (str, bytes, bytearray)):
        return []

    exposed_names: list[str] = []
    exposed_seen: set[str] = set()
    for value in exposed:
        if not isinstance(value, str):
            continue
        name = value.strip()
        if not name or name in exposed_seen:
            continue
        exposed_seen.add(name)
        exposed_names.append(name)

    exposed_set = set(exposed_names)
    for name in selected_model_names(payload.get("structured")):
        if name in exposed_set:
            add(name)
    for name in exposed_names:
        add(name)
    return names


def _fallback_reasoning_profile(name: str) -> tuple[str, tuple[dict[str, str], ...]]:
    return "medium", _REASONING_LEVELS


def _fallback_catalog_model(name: str, priority: int) -> dict[str, Any]:
    """Keep the catalog usable when no native Codex executable is installed."""

    default_reasoning_level, supported_reasoning_levels = _fallback_reasoning_profile(name)
    return {
        "slug": name,
        "display_name": name,
        "description": CATALOG_DESCRIPTION,
        "default_reasoning_level": default_reasoning_level,
        "supported_reasoning_levels": [dict(level) for level in supported_reasoning_levels],
        "shell_type": "shell_command",
        "visibility": "list",
        "supported_in_api": True,
        "priority": priority,
        "base_instructions": "You are Codex, a coding agent.",
        "support_verbosity": True,
        "truncation_policy": {"mode": "tokens", "limit": 10_000},
        "supports_parallel_tool_calls": True,
        "experimental_supported_tools": [],
    }


def _name_candidates(name: str) -> tuple[str, ...]:
    normalized = name.strip().casefold()
    if not normalized:
        return ()
    result = [normalized]
    for separator in ("/", "@"):
        if separator in normalized:
            suffix = normalized.rsplit(separator, 1)[-1].strip()
            if suffix and suffix not in result:
                result.append(suffix)
    return tuple(result)


def _native_profile_for_name(
    name: str,
    native_models: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any] | None, bool]:
    """Find an exact native profile, or an agent profile for a custom route."""

    by_name: dict[str, tuple[dict[str, Any], bool]] = {}
    for raw_model in native_models:
        if not isinstance(raw_model, Mapping):
            continue
        slug = raw_model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        model = copy.deepcopy(dict(raw_model))
        normalized_slug = slug.strip().casefold()
        by_name.setdefault(normalized_slug, (model, True))

    for candidate in _name_candidates(name):
        exact = by_name.get(candidate)
        if exact is not None:
            return exact

    # A LiteLLM public alias may not exist in the native list. In that case
    # inherit the first native profile that carries model instructions. This
    # deliberately does not inspect or synthesize a version number: v1/v2/v3
    # and any future native fields are copied as supplied by the installed CLI.
    for raw_model in native_models:
        if not isinstance(raw_model, Mapping):
            continue
        messages = raw_model.get("model_messages")
        instructions = raw_model.get("base_instructions")
        has_messages = isinstance(messages, Mapping) and bool(messages.get("instructions_template"))
        if has_messages or (isinstance(instructions, str) and instructions.strip()):
            return copy.deepcopy(dict(raw_model)), False
    return None, False


def _apply_reasoning_capability(model: dict[str, Any], registry: ModelContextRegistry, name: str) -> None:
    capability = registry.reasoning_for(name)
    if capability is None:
        return
    model["supported_reasoning_levels"] = [
        {
            "effort": level,
            "description": _REASONING_DESCRIPTIONS.get(level, f"{level} reasoning"),
        }
        for level in capability.supported_levels
    ]
    if capability.default_level is not None:
        model["default_reasoning_level"] = capability.default_level


def _catalog_model(
    name: str,
    priority: int,
    registry: ModelContextRegistry,
    native_models: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    native_model, exact_native_match = _native_profile_for_name(name, native_models)
    if native_model is None:
        # The native profile is an enhancement, not a second hard-coded model
        # schema. Keep the existing minimal contract for hosts without Codex.
        model = _fallback_catalog_model(name, priority)
    else:
        # Deep-copy the whole object so fields added by a future native catalog
        # (for example a new multi-agent version or tool policy) pass through
        # without this project needing a compatibility branch for each version.
        model = native_model
        model["slug"] = name
        if not exact_native_match:
            model["display_name"] = name

    # These are the route-specific fields owned by LiteLLM Menu. Everything
    # else remains native metadata whenever a native profile was available.
    model["priority"] = priority
    # Keep the public catalog shape stable when a native profile is available
    # as well as when the fallback profile is used.  A native executable may
    # omit this optional capability field.
    model.setdefault("supports_parallel_tool_calls", True)
    context = registry.record_for(name)
    # Codex displays the effective window and derives automatic compaction at
    # 90% when no explicit threshold is supplied.  Keeping the raw window,
    # hard override ceiling, and effective percentage separate matches its
    # native model metadata contract.
    model["context_window"] = context.context_window
    model["max_context_window"] = context.max_context_window
    model["effective_context_window_percent"] = context.effective_context_window_percent
    # An exact native profile may contain Codex-only modes such as ``ultra``
    # that activate task delegation rather than map directly to a provider
    # reasoning effort.  The route capability registry intentionally knows
    # only provider-wire efforts, so applying it here would erase those native
    # modes.  Custom public aliases still need the registry's safe surface.
    if not exact_native_match:
        _apply_reasoning_capability(model, registry, name)
    return model


def catalog_payload(names: Sequence[str], *, registry: ModelContextRegistry | None = None) -> dict[str, Any]:
    context_registry = registry or ModelContextRegistry()
    native_models = load_native_catalog()
    return {
        "models": [
            _catalog_model(name, index + 1, context_registry, native_models)
            for index, name in enumerate(names)
        ]
    }


def catalog_model_names(path: Path | str) -> list[str] | None:
    try:
        payload = read_json(path, default={"models": None})
    except PersistenceError:
        return None
    if not isinstance(payload, Mapping):
        return None
    models = payload.get("models", [])
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
        return None
    result: list[str] = []
    for model in models:
        if not isinstance(model, Mapping):
            return None
        slug = model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            return None
        result.append(slug.strip())
    return result


def catalog_is_current(
    path: Path | str,
    names: Sequence[str],
    *,
    registry: ModelContextRegistry | None = None,
) -> bool:
    try:
        return read_json(path, default=None) == catalog_payload(names, registry=registry)
    except PersistenceError:
        return False


def write_catalog(
    path: Path | str,
    names: Sequence[str],
    *,
    registry: ModelContextRegistry | None = None,
) -> None:
    atomic_write_json(path, catalog_payload(names, registry=registry))


__all__ = [
    "CATALOG_FILE_NAME",
    "catalog_is_current",
    "catalog_model_names",
    "catalog_names_from_editor",
    "catalog_payload",
    "managed_catalog_path",
    "selected_model_names",
    "write_catalog",
]
