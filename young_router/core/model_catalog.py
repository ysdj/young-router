"""Managed Codex model catalog sourced from LiteLLM and native Codex metadata."""

from __future__ import annotations

import copy
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from .model_contexts import ModelContextRegistry
from .native_codex_catalog import load_native_catalog
from .persistence import PersistenceError, atomic_write_json, read_json


CATALOG_FILE_NAME = "model-catalog.json"
# The rebrand-era name this app managed before the neutral spelling.  Releases
# between the LiteLLM Menu era and the current one wrote this file and pointed
# Codex at it, so it stays recognised as a managed catalog target and is
# migrated to the current name (file first, then the key) instead of being
# treated as a foreign, user-owned path.
PREVIOUS_CATALOG_FILE_NAME = "young-router-model-catalog.json"
# Releases before the Young Router rebrand managed the same catalog under the
# retired LiteLLM Menu name.  Codex config files written back then still point
# at that path, so it stays recognised as a managed catalog target and is
# migrated (never treated as a foreign, user-owned path).
LEGACY_CATALOG_FILE_NAME = "litellm-menu-model-catalog.json"
CATALOG_DESCRIPTION = "Young Router exposed model"
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


def previous_managed_catalog_path(codex_home: Path | str) -> Path:
    """Return the rebrand-era catalog path this app still owns."""

    return Path(codex_home).expanduser() / PREVIOUS_CATALOG_FILE_NAME


def legacy_managed_catalog_path(codex_home: Path | str) -> Path:
    """Return the pre-rebrand LiteLLM Menu catalog path this app still owns."""

    return Path(codex_home).expanduser() / LEGACY_CATALOG_FILE_NAME


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
    ``/v1/models`` response.  Only names that also appear in the configured
    model list are eligible: worker processes can transiently carry extra
    routes that are not part of the user's public model set (for example a
    runtime-added ``openai/<model>`` alias present on a subset of workers),
    and those flapping names must neither rewrite the catalog nor enter the
    public model list.  The configured names act as an allowlist over the
    live exposure, never as a fallback: a configured model whose route is
    currently unavailable drops out of the catalog, and an unavailable Menu
    still yields an empty catalog.

    The active and review models are kept first *only when LiteLLM exposes
    them*; every other eligible ID follows in endpoint order.
    """

    if not isinstance(payload, Mapping):
        return []

    configured: set[str] = set()
    configured_models = payload.get("models")
    if isinstance(configured_models, Sequence) and not isinstance(
        configured_models, (str, bytes, bytearray)
    ):
        for entry in configured_models:
            if not isinstance(entry, Mapping):
                continue
            name = entry.get("model")
            if isinstance(name, str) and name.strip():
                configured.add(name.strip())

    names: list[str] = []
    seen: set[str] = set()

    def add(value: object) -> None:
        if not isinstance(value, str):
            return
        name = value.strip()
        if name and name in configured and name not in seen:
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


def profile_has_instructions(model: object) -> bool:
    """Whether a native profile carries the client's agent instructions."""

    if not isinstance(model, Mapping):
        return False
    messages = model.get("model_messages")
    if isinstance(messages, Mapping) and str(messages.get("instructions_template") or "").strip():
        return True
    instructions = model.get("base_instructions")
    return isinstance(instructions, str) and bool(instructions.strip())


def _profile_priority(model: Mapping[str, Any]) -> int:
    """Codex's own hierarchy field; a missing value sorts after every bound."""

    value = model.get("priority")
    return value if isinstance(value, int) and not isinstance(value, bool) else 1_000_000


def select_inherited_profile(native_models: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """Return the base profile that models without a native slug inherit.

    Codex declares its own hierarchy: ``priority`` (lower is the flagship) and
    ``visibility`` (``hide`` covers experimental/cyber variants and the
    auto-review agent).  Selecting by those declared fields instead of the
    array order keeps the choice explainable and independent of catalog
    reordering; callers that must not re-map when a newer generation appears
    pin the returned slug and keep using it while the client still offers it.
    """

    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for index, raw_model in enumerate(native_models):
        if not isinstance(raw_model, Mapping):
            continue
        slug = raw_model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        visibility = raw_model.get("visibility")
        if str(visibility or "").strip().lower() != "list":
            continue
        if not profile_has_instructions(raw_model):
            continue
        candidates.append((_profile_priority(raw_model), index, copy.deepcopy(dict(raw_model))))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        return candidates[0][2]
    # A catalog with an unexpected visibility spelling still has to yield
    # usable instructions, so fall back to the first profile that carries
    # them; hidden-only catalogs are the only case where that can pick a
    # non-listed profile.
    for raw_model in native_models:
        if profile_has_instructions(raw_model):
            return copy.deepcopy(dict(raw_model))
    return None


def _native_profile_for_name(
    name: str,
    native_models: Sequence[Mapping[str, Any]],
    base_slug: str | None = None,
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

    # A public alias may not exist in the native list. In that case inherit the
    # pinned base profile (the one this app resolved and has been using), or
    # the client's current highest-priority visible agent profile. Either way
    # the profile object is copied as supplied by the installed CLI: v1/v2/v3
    # and any future native fields pass through without a compatibility branch.
    pinned = str(base_slug or "").strip().casefold()
    if pinned:
        chosen = by_name.get(pinned)
        if chosen is not None and profile_has_instructions(chosen[0]):
            return copy.deepcopy(chosen[0]), False
    selected = select_inherited_profile(native_models)
    if selected is not None:
        return selected, False
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


def _apply_direct_tool_surface(model: dict[str, Any]) -> None:
    """Reset an inherited code-mode surface to the classic direct tools.

    Custom routes inherit whichever native profile carries model
    instructions — today always a GPT profile whose ``unified_exec`` shell
    and ``tool_mode: "code_mode_only"`` assume a model trained on the GPT
    code-mode ``exec`` script surface. Third-party chat routes are trained
    on ordinary per-call JSON function tools, so give inherited profiles the
    classic surface instead: a ``shell_command`` shell and ``direct`` tool
    mode. ``apply_patch_tool_type`` stays as inherited because the function
    (JSON) variant no longer exists in current Codex releases.
    """

    model["shell_type"] = "shell_command"
    model["tool_mode"] = "direct"


def _apply_search_tool_capability(
    model: dict[str, Any],
    registry: ModelContextRegistry,
    name: str,
) -> None:
    """Keep the catalog's search-tool advertisement aligned with the route.

    The inherited native profile advertises hosted search for the profile it
    was copied from.  The route may not deliver that tool: an explicit false
    capability or an unidentified non-GPT route must not advertise it.  When
    the route configuration is unreadable the native profile is left
    untouched.
    """

    capability = registry.search_tool_capability_for(name)
    if capability is None:
        return
    model["supports_search_tool"] = bool(capability)
    if not capability:
        model.pop("web_search_tool_type", None)


def _catalog_model(
    name: str,
    priority: int,
    registry: ModelContextRegistry,
    native_models: Sequence[Mapping[str, Any]],
    base_slug: str | None = None,
) -> dict[str, Any]:
    native_model, exact_native_match = _native_profile_for_name(name, native_models, base_slug)
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
            _apply_direct_tool_surface(model)

    # These are the route-specific fields owned by Young Router. Everything
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
    _apply_search_tool_capability(model, registry, name)
    # An exact native profile may contain Codex-only modes such as ``ultra``
    # that activate task delegation rather than map directly to a provider
    # reasoning effort.  The route capability registry intentionally knows
    # only provider-wire efforts, so applying it here would erase those native
    # modes.  Custom public aliases still need the registry's safe surface.
    if not exact_native_match:
        _apply_reasoning_capability(model, registry, name)
    return model


def catalog_payload(
    names: Sequence[str],
    *,
    registry: ModelContextRegistry | None = None,
    native_models: Sequence[Mapping[str, Any]] | None = None,
    base_slug: str | None = None,
) -> dict[str, Any]:
    context_registry = registry or ModelContextRegistry()
    resolved_native = load_native_catalog() if native_models is None else native_models
    return {
        "models": [
            _catalog_model(name, index + 1, context_registry, resolved_native, base_slug)
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


def catalog_carries_native_profile(path: Path | str) -> bool:
    """Whether the managed catalog holds an installed client's own profile.

    A file generated while no Codex client was installed carries this app's
    minimal fallback profile; one generated from a client carries that
    client's instructions (``model_messages.instructions_template``).  The
    distinction lets a refresh keep the better file when the client's bundled
    catalog is temporarily unreadable, for example while Codex is updating.
    """

    try:
        payload = read_json(path, default=None)
    except PersistenceError:
        return False
    models = payload.get("models") if isinstance(payload, Mapping) else None
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
        return False
    for model in models:
        if not isinstance(model, Mapping):
            continue
        messages = model.get("model_messages")
        if isinstance(messages, Mapping) and str(messages.get("instructions_template") or "").strip():
            return True
    return False


def catalog_is_current(
    path: Path | str,
    names: Sequence[str],
    *,
    registry: ModelContextRegistry | None = None,
    native_models: Sequence[Mapping[str, Any]] | None = None,
    base_slug: str | None = None,
) -> bool:
    try:
        resolved_native = native_models
        if resolved_native is None:
            resolved_native = load_native_catalog()
            if not resolved_native and catalog_carries_native_profile(path):
                # The installed client's bundled catalog could not be read right
                # now (an in-flight update, or a transient failure).  Rewriting
                # the file from this app's minimal fallback profile would
                # replace the client's own instructions for every model, so the
                # file already on disk counts as current and the next refresh
                # retries the read.
                return True
        return read_json(path, default=None) == catalog_payload(
            names,
            registry=registry,
            native_models=resolved_native,
            base_slug=base_slug,
        )
    except PersistenceError:
        return False


def write_catalog(
    path: Path | str,
    names: Sequence[str],
    *,
    registry: ModelContextRegistry | None = None,
    native_models: Sequence[Mapping[str, Any]] | None = None,
    base_slug: str | None = None,
) -> None:
    atomic_write_json(
        path,
        catalog_payload(
            names,
            registry=registry,
            native_models=native_models,
            base_slug=base_slug,
        ),
    )


__all__ = [
    "CATALOG_FILE_NAME",
    "LEGACY_CATALOG_FILE_NAME",
    "PREVIOUS_CATALOG_FILE_NAME",
    "catalog_carries_native_profile",
    "catalog_is_current",
    "catalog_model_names",
    "catalog_names_from_editor",
    "catalog_payload",
    "legacy_managed_catalog_path",
    "managed_catalog_path",
    "previous_managed_catalog_path",
    "profile_has_instructions",
    "select_inherited_profile",
    "selected_model_names",
    "write_catalog",
]
