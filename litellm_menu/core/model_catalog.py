"""Managed Codex model catalog restricted to explicit Codex selections."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .model_contexts import ModelContextRegistry
from .persistence import PersistenceError, atomic_write_json, read_json


CATALOG_FILE_NAME = "litellm-menu-model-catalog.json"
CATALOG_DESCRIPTION = "LiteLLM Menu selected Codex model"
_REASONING_LEVELS = (
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth"},
    {"effort": "high", "description": "Greater reasoning depth for complex tasks"},
    {"effort": "xhigh", "description": "Extra reasoning depth for complex tasks"},
)
_GPT_56_SOL_REASONING_LEVELS = (
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balanced reasoning for everyday tasks"},
    {"effort": "high", "description": "Greater reasoning depth for complex problems"},
    {"effort": "xhigh", "description": "Extra high reasoning depth for complex problems"},
    {"effort": "max", "description": "Maximum reasoning depth for the hardest problems"},
    {"effort": "ultra", "description": "Maximum reasoning with automatic task delegation"},
)
_GPT_56_SOL_PUBLIC_NAMES = frozenset({"gpt-5.6-sol", "5.6 sol", "5.6-sol"})


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


def _reasoning_profile(name: str) -> tuple[str, tuple[dict[str, str], ...]]:
    if name.strip().casefold() in _GPT_56_SOL_PUBLIC_NAMES:
        return "low", _GPT_56_SOL_REASONING_LEVELS
    return "medium", _REASONING_LEVELS


def _catalog_model(name: str, priority: int, registry: ModelContextRegistry) -> dict[str, Any]:
    # These are the required fields accepted by Codex's model-catalog parser.
    # The public route remains the slug sent to LiteLLM Menu.
    default_reasoning_level, supported_reasoning_levels = _reasoning_profile(name)
    model = {
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
    context = registry.record_for(name)
    # Codex displays the effective window and derives automatic compaction at
    # 90% when no explicit threshold is supplied.  Keeping the raw window,
    # hard override ceiling, and effective percentage separate matches its
    # native model metadata contract.
    model["context_window"] = context.context_window
    model["max_context_window"] = context.max_context_window
    model["effective_context_window_percent"] = context.effective_context_window_percent
    return model


def catalog_payload(names: Sequence[str], *, registry: ModelContextRegistry | None = None) -> dict[str, Any]:
    context_registry = registry or ModelContextRegistry()
    return {"models": [_catalog_model(name, index + 1, context_registry) for index, name in enumerate(names)]}


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
    "catalog_payload",
    "managed_catalog_path",
    "selected_model_names",
    "write_catalog",
]
