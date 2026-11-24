"""Managed Codex model catalog sourced from LiteLLM and native Codex metadata."""

from __future__ import annotations

import copy
from collections.abc import Callable, Mapping, Sequence
import json
import os
from pathlib import Path
import threading
from typing import Any, Final

from .model_contexts import ModelContextRegistry
from .model_contexts import (
    SEARCH_TOOL_CAPABILITY_BRIDGE,
    SEARCH_TOOL_CAPABILITY_UNSUPPORTED,
    ReasoningCapability,
    default_context_cache_path,
    litellm_mode,
    published_reasoning_levels,
)
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
# The model-list-plugin writes its own catalog under this name and points Codex
# at it.  This app does not write it; the name is recognised so the URL delivery
# below can tell a machine-managed pointer (which URL delivery supersedes) from
# a catalog a person wrote by hand (which it must leave alone).
PLUGIN_CATALOG_FILE_NAME = "model-list-plugin-catalog.json"
#: Where Core serves the catalog for a client that fetches it itself.
#:
#: ``/codex/models`` is OpenAI's own Codex backend spelling -- that backend
#: answers ``https://chatgpt.com/backend-api/codex/models``, which is the same
#: leaf this app's own ``providers_models._CHATGPT_MODELS_ENDPOINT`` reads -- and
#: it stays out of ``/v1``, which belongs to LiteLLM.
#:
#: The data plane's ``/v1/models?client_version=`` is *not* used for this: it
#: translates the route table on its own terms, so an alias loses
#: ``model_messages`` entirely, 6 of 11 routes came back with no reasoning
#: level and 4 with no context window, and ``base_instructions`` was the
#: release tag's generic prompt instead of the installed client's inherited
#: one.  A catalog is prompt-bearing: the installed client and this app's
#: registry are the only producers whose fields the client would have used.
SERVED_CATALOG_ROUTE = "/codex/models"
#: Codex bounds an explicitly configured catalog response at 1 MiB and fails the
#: whole fetch above it -- and an explicit catalog replaces its bundled list
#: instead of falling back to it, so an oversized body leaves that client with
#: no models at all.  This body therefore stays under the bound.
MAX_SERVED_CATALOG_BYTES = 1024 * 1024
#: Room for JSON framing the byte count above is checked against.
SERVED_CATALOG_RESERVE_BYTES = 8 * 1024
#: Fields that make an entry large without making it selectable: the client's
#: own prompt text.  Only a catalog that would otherwise exceed the bound is
#: reduced to this shape, where the client falls back to its bundled text.
SERVED_CATALOG_OMIT_FIELDS = ("model_messages", "base_instructions")
#: The deprecated top-level instruction field.  Clients from 0.163 on read
#: ``model_messages.instructions_template``; ``ModelsResponse`` still accepts
#: this field only to *promote* it into that template when the template is
#: absent, and re-emits it when serializing.  This app writes no legacy form at
#: all: every entry it generates carries the template, because the mirror was
#: 242 KB of the same text twice.  The name stays here because a native
#: profile read back from a client may still carry it.
LEGACY_INSTRUCTIONS_FIELD = "base_instructions"
#: The instructions of this app's own minimal profile, used when no client is
#: installed.  It is a value, not a shape, so a file built from it stays
#: distinguishable from one built from a client's own prompt -- the guard that
#: keeps a temporary client-update failure from downgrading the good file.
FALLBACK_INSTRUCTIONS: Final = "You are Codex, a coding agent."

#: Spellings this app pointed a client at before the current one.  A document
#: that still names one is *this app's* catalog, not an operator's own URL: the
#: next refresh rewrites it rather than treating it as a foreign row, because an
#: address that stopped answering drops the client back to its bundled list.
#:
#: The rewrite is the migration -- no redirect is served for a retired path.
#: Codex builds its catalog client with ``ClientRedirectPolicy::Reject`` whenever
#: ``model_catalog_url`` is set, so a 301/302 would be refused and the client
#: would lose its list anyway; a redirect route would only look like a fix.
#:
#: ``/v1/models`` is handled beside this list rather than inside it: it is a
#: generic path every gateway serves, so it only counts as this app's address
#: when it is the same origin as that provider row's inference endpoint.
RETIRED_SERVED_CATALOG_ROUTES = ("/codex/catalog",)
CATALOG_DESCRIPTION = "Young Router exposed model"
_REASONING_LEVELS = (
    {"effort": "low", "description": "Fast responses with lighter reasoning"},
    {"effort": "medium", "description": "Balances speed and reasoning depth"},
    {"effort": "high", "description": "Greater reasoning depth for complex tasks"},
    {"effort": "xhigh", "description": "Extra reasoning depth for complex tasks"},
)
#: The summary the client asks for when a model's catalog entry names one.
#: A route answers thinking as ``response.reasoning_summary_*`` frames and the
#: proxy delivers them only to a request that named a visible summary, so a
#: catalog entry left on the native default (``none``) hides every route's
#: thinking behind a manual ``model_reasoning_summary`` edit in the client's own
#: TOML.  ``auto`` is the value both clients treat as "show the thinking", while
#: ``detailed``/``concise``/``raw`` stay the user's own choice.
DEFAULT_REASONING_SUMMARY = "auto"

_REASONING_DESCRIPTIONS = {
    "none": "No reasoning",
    "minimal": "Minimal reasoning for faster responses",
    "low": "Fast responses with lighter reasoning",
    "medium": "Balances speed and reasoning depth",
    "high": "Greater reasoning depth for complex tasks",
    "xhigh": "Extra reasoning depth for complex tasks",
    "max": "Maximum reasoning depth for the hardest tasks",
    # Codex's own wording, kept verbatim so a custom route's slider reads the
    # same as the client's native profile.
    "ultra": "Maximum reasoning with automatic task delegation",
}


def managed_catalog_path(codex_home: Path | str) -> Path:
    return Path(codex_home).expanduser() / CATALOG_FILE_NAME


def previous_managed_catalog_path(codex_home: Path | str) -> Path:
    """Return the rebrand-era catalog path this app still owns."""

    return Path(codex_home).expanduser() / PREVIOUS_CATALOG_FILE_NAME


def legacy_managed_catalog_path(codex_home: Path | str) -> Path:
    """Return the pre-rebrand LiteLLM Menu catalog path this app still owns."""

    return Path(codex_home).expanduser() / LEGACY_CATALOG_FILE_NAME


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

    The order is the exposure's own, which is already alphabetical by public
    name: ``codex_config.local_exposed_models`` owns that one order for every
    picker this app writes, so this filters and never re-ranks.  The active and
    review models hold no special place -- a picker is one alphabetical list.
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

    exposed = payload.get("exposed_models")
    if not isinstance(exposed, Sequence) or isinstance(exposed, (str, bytes, bytearray)):
        return []

    names: list[str] = []
    seen: set[str] = set()
    for value in exposed:
        if not isinstance(value, str):
            continue
        name = value.strip()
        if name and name in configured and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _fallback_reasoning_profile() -> tuple[str, tuple[dict[str, str], ...]]:
    return "medium", _REASONING_LEVELS


def _fallback_catalog_model(name: str, priority: int) -> dict[str, Any]:
    """Keep the catalog usable when no native Codex executable is installed."""

    default_reasoning_level, supported_reasoning_levels = _fallback_reasoning_profile()
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
        "model_messages": {"instructions_template": FALLBACK_INSTRUCTIONS},
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

    candidates: list[tuple[int, int, Mapping[str, Any]]] = []
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
        candidates.append((_profile_priority(raw_model), index, raw_model))
    if candidates:
        candidates.sort(key=lambda item: (item[0], item[1]))
        return copy.deepcopy(dict(candidates[0][2]))
    # A catalog with an unexpected visibility spelling still has to yield
    # usable instructions, so fall back to the first profile that carries
    # them; hidden-only catalogs are the only case where that can pick a
    # non-listed profile.
    for raw_model in native_models:
        if profile_has_instructions(raw_model):
            return copy.deepcopy(dict(raw_model))
    return None


def _native_profiles_by_name(
    native_models: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    """Index native profiles by slug without copying the profiles themselves."""

    by_name: dict[str, Mapping[str, Any]] = {}
    for raw_model in native_models:
        if not isinstance(raw_model, Mapping):
            continue
        slug = raw_model.get("slug")
        if not isinstance(slug, str) or not slug.strip():
            continue
        by_name.setdefault(slug.strip().casefold(), raw_model)
    return by_name


def _native_profile_for_name(
    name: str,
    native_models: Sequence[Mapping[str, Any]],
    base_slug: str | None = None,
    native_profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> tuple[dict[str, Any] | None, bool]:
    """Find an exact native profile, or an agent profile for a custom route."""

    by_name = native_profiles
    if by_name is None:
        by_name = _native_profiles_by_name(native_models)

    for candidate in _name_candidates(name):
        exact = by_name.get(candidate)
        if exact is not None:
            return copy.deepcopy(dict(exact)), True

    # A public alias may not exist in the native list. In that case inherit the
    # pinned base profile (the one this app resolved and has been using), or
    # the client's current highest-priority visible agent profile. Either way
    # the profile object is copied as supplied by the installed CLI: v1/v2/v3
    # and any future native fields pass through without a compatibility branch.
    pinned = str(base_slug or "").strip().casefold()
    if pinned:
        chosen = by_name.get(pinned)
        if chosen is not None and profile_has_instructions(chosen):
            return copy.deepcopy(dict(chosen)), False
    selected = select_inherited_profile(native_models)
    if selected is not None:
        return selected, False
    return None, False


def _route_reasoning_capability(
    registry: ModelContextRegistry,
    name: str,
) -> ReasoningCapability | None:
    """The public route's reasoning surface, resolved the way Pi's listing does it.

    ``reasoning_for`` is the route's *common* surface, and it answers ``None``
    when any one of a group's deployments resolves nothing -- which is every
    relay alias the provider catalog names differently
    (``opencode-go/space-bunny`` while the route is called ``space-bunny``).
    Reading only that answer left the model on the inherited native profile, so
    the managed catalog published ``low``..``ultra`` for a route the same
    registry reports as ``low``/``high``/``max`` on ``/v1/models``: one route,
    two ladders, and a default the route does not have.  The public name's own
    resolution is where the request path ends up as well, so it is the one
    fallback and it can only narrow the answer, never contradict one that
    resolved.
    """

    capability = registry.reasoning_for(name)
    if capability is not None and capability.supported_levels:
        return capability
    return registry.reasoning_for_model_id(name)


#: The LiteLLM listing ``mode`` markers that name a generator rather than a
#: conversation.  ``image`` covers ``image_generation``/``image_edit`` and
#: ``video`` covers ``video_generation``; a chat, responses, embedding or
#: audio mode carries neither.
_GENERATION_LISTING_MODE_MARKERS: Final = ("image", "video")
_CONVERSATIONAL_LISTING_MODES: Final = frozenset({"chat", "completion", "responses"})


def listing_mode_serves_generation(mode: object) -> bool:
    """Whether one listing row's ``mode`` names media generation.

    This is the listing's own statement, so it needs no catalog lookup and is
    the first filter every Claude view applies.  A missing or unreadable mode is
    not evidence of a generator; the registry answers that case instead.
    """

    if not isinstance(mode, str):
        return False
    normalized = mode.strip().lower()
    return bool(normalized) and any(
        marker in normalized for marker in _GENERATION_LISTING_MODE_MARKERS
    )


def _listing_mode_serves_conversation(mode: object) -> bool:
    """Whether LiteLLM's mode says this deployment answers text turns."""

    return isinstance(mode, str) and mode.strip().casefold() in _CONVERSATIONAL_LISTING_MODES


def _route_serves_only_generation_model(
    registry: ModelContextRegistry,
    name: str,
    states_generation: Callable[[Sequence[str]], bool],
) -> bool:
    """Whether this route resolves a generator and no thinking conversation.

    The public catalogs (pi.dev, models.dev) already state the modality, so the
    prefix heuristic the provider pane uses is not needed here.  A route earns
    the answer when one of its deployments resolves an image or video contract
    -- from a catalog's modality or from LiteLLM's own ``mode``, which is the
    table that carries a name no public catalog has listed yet -- and no
    deployment states a thinking level above the disabled step: a group that
    also serves a reasoning text model keeps its ladder, and a deployment the
    registry cannot resolve (a retired alias beside two live ones) does not veto
    the two that agree.
    """

    model_ids = registry.route_model_ids(name) or [name]
    if not states_generation(model_ids):
        return False
    # A public route may group several deployments under one name. A video
    # backend beside an ordinary chat backend is still a conversation the
    # picker can select, even when that chat model exposes no reasoning ladder.
    # Ask LiteLLM's own mode for each concrete id before using the reasoning
    # ladder as the fallback for catalogs that do not state their wire mode.
    if any(
        _listing_mode_serves_conversation(litellm_mode([model_id]))
        for model_id in model_ids
    ):
        return False
    resolved = [
        capability
        for capability in (
            registry.reasoning_for_model_id(model_id) for model_id in model_ids
        )
        if capability is not None
    ]
    return all(
        capability.supported_levels in (("none",), ())
        for capability in resolved
    )


def _route_is_image_model(registry: ModelContextRegistry, name: str) -> bool:
    """Whether this public route serves only image generation."""

    return _route_serves_only_generation_model(
        registry, name, registry.states_image_generation
    )


def _route_is_generation_model(
    registry: ModelContextRegistry, name: str
) -> bool:
    """Whether this public route serves only image or video generation."""

    return _route_serves_only_generation_model(
        registry,
        name,
        lambda model_ids: (
            registry.states_image_generation(model_ids)
            or registry.states_video_generation(model_ids)
        ),
    )


#: One registry for the picker filter below.  It is a read of the public
#: catalogs' cache, keyed by the cache file it read, exactly as
#: ``proxy.reasoning`` keeps its own: resolving a route's modality per name
#: would otherwise re-parse the runtime document once per listed model.
_IMAGE_FILTER_REGISTRY_LOCK = threading.RLock()
_IMAGE_FILTER_REGISTRY: tuple[str, ModelContextRegistry] | None = None


def _image_filter_registry(
    runtime_root: Path | str | None = None,
    runtime_config_path: Path | str | None = None,
) -> ModelContextRegistry:
    """The registry the client-picker filter reads, built from one install.

    Resolved exactly as ``proxy.reasoning`` resolves its own: the same runtime
    document, the same settings file, the same cache and the same retired
    copies.  The picker filter and the catalog entry must answer from one set of
    records, or a route could read as an image model in one and a chat model in
    the other.  Refresh stays off: this is a read of whatever the pane already
    fetched, not a second fetcher racing it.

    ``runtime_root`` and ``runtime_config_path`` name the installation and its
    provider document, so a domain built against another pair reads that pair's
    records rather than the process's own -- the same rule ``_app_state_root``
    applies to the rest of a domain's state.
    """

    global _IMAGE_FILTER_REGISTRY

    from .. import config_paths as _config_paths
    from .model_contexts import legacy_context_cache_paths

    root = _config_paths.runtime_root(runtime_root)
    config_path = (
        Path(runtime_config_path).expanduser()
        if runtime_config_path is not None
        else _config_paths.provider_config_path(root)
    )
    cache = default_context_cache_path(root)
    key = f"{cache}\0{config_path}"
    with _IMAGE_FILTER_REGISTRY_LOCK:
        if _IMAGE_FILTER_REGISTRY is not None and _IMAGE_FILTER_REGISTRY[0] == key:
            return _IMAGE_FILTER_REGISTRY[1]
        configured_settings = os.environ.get(
            "YOUNG_ROUTER_RUNTIME_SETTINGS_FILE", ""
        ).strip()
        configured_home = os.environ.get("CODEX_HOME", "").strip()
        codex_home = (
            Path(configured_home).expanduser()
            if configured_home
            else Path.home() / ".codex"
        )
        registry = ModelContextRegistry(
            runtime_config_path=config_path,
            runtime_settings_path=(
                Path(configured_settings).expanduser()
                if configured_settings
                else root / "runtime-settings.env"
            ),
            cache_path=cache,
            legacy_cache_paths=legacy_context_cache_paths(codex_home),
            refresh_enabled=False,
        )
        _IMAGE_FILTER_REGISTRY = (key, registry)
        return registry


def route_serves_only_images(
    name: str,
    *,
    registry: ModelContextRegistry | None = None,
) -> bool:
    """Whether this public route serves image generation and nothing else.

    A client's model picker is a list of conversations to have, and a generator
    is not one: the client reaches its own image tool, never by naming the route
    as the model of a turn.  Listing one therefore offered a choice that could
    only fail, and the recorded turn for ``gpt-image-2.5-sunburst`` showed what
    failing looked like -- Codex sent ``exec_command`` and ``apply_patch`` to a
    route that answers its own request body.

    ``/v1/models`` is deliberately not this question for clients that are not
    Claude.  The router keeps publishing every route it serves, because Pi and
    anything else speaking the raw OpenAI shape may legitimately call the
    generator; the Codex catalog entry reads this answer, and the generated
    Claude pickers plus the Claude discovery view read
    ``route_serves_only_generation``.

    A registry that cannot be built answers ``False``: an unreadable cache is
    not evidence that a route is an image model, and the listing's own ``mode``
    remains the only filter on that path.
    """

    if not isinstance(name, str) or not name.strip():
        return False
    try:
        resolved = registry or _image_filter_registry()
        return _route_is_image_model(resolved, name.strip())
    except Exception:
        return False


def route_serves_only_generation(
    name: str,
    *,
    registry: ModelContextRegistry | None = None,
) -> bool:
    """Whether this public route serves only image or video generation.

    Same question as ``route_serves_only_images``, with the video modality
    included: a video generator also holds no conversation, so listing it in a
    Claude model picker offers a choice that can only fail.  The two answers
    share one resolution; this one asks the registry for both modalities.

    A registry that cannot be built answers ``False``: an unreadable cache is
    not evidence of a generator, and a listing's own ``mode`` remains the only
    filter on that path.
    """

    if not isinstance(name, str) or not name.strip():
        return False
    try:
        resolved = registry or _image_filter_registry()
        return _route_is_generation_model(resolved, name.strip())
    except Exception:
        return False


def _apply_reasoning_capability(model: dict[str, Any], capability: ReasoningCapability | None) -> None:
    """Publish one route's provider-wire ladder on a catalog entry.

    An image model states only its disabled step: the client's slider is a
    reasoning control, and a level it cannot honour there is a knob that does
    nothing.  Everything below is a provider-wire level, so it is worded through
    ``published_reasoning_levels`` and therefore carries ``ultra`` beside every
    ``max`` exactly as the Pi-facing listing does.
    """

    if capability is None:
        return
    if capability.supported_levels == ("none",):
        levels = ["none"]
    else:
        levels = published_reasoning_levels(capability.supported_levels)
    model["supported_reasoning_levels"] = [
        {
            "effort": level,
            "description": _REASONING_DESCRIPTIONS.get(level, f"{level} reasoning"),
        }
        for level in levels
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


def _apply_own_reasoning_top(model: dict[str, Any]) -> None:
    """Let a custom route's ``ultra`` mean its own hardest level.

    ``multi_agent_reasoning_effort`` is the effort Codex uses when a person
    picks ``ultra``: its resolver takes that field when the model supports it,
    then the model's ``max``, then its highest supported non-``ultra`` level.
    Some native profiles pin it lower on purpose -- ``gpt-6.1-sol`` ships
    ``xhigh``, so selecting Ultra on OpenAI's own model sends ``xhigh``, and
    measured here on the installed client (0.166.0-alpha.5.1, its own bundled
    catalog, ``model_reasoning_effort = "ultra"``) the request carried
    ``reasoning.effort: xhigh``.

    A relay route is not that model: its catalog states ``ultra`` above the
    hardest level the route maps, so the inherited pin would make the top tier
    think *less* than the ``max`` directly below it -- ``claude-opus-5-5`` sent
    ``xhigh`` for Ultra while Max sent ``max``. Dropping the pin leaves the
    client's documented fallback: its own ``max`` when the ladder states one,
    else its highest supported non-``ultra`` level, and ``proxy.reasoning``
    clamps that onto what the route actually maps.
    """

    model.pop("multi_agent_reasoning_effort", None)


def _apply_search_tool_capability(
    model: dict[str, Any],
    registry: ModelContextRegistry,
    name: str,
) -> None:
    """Keep the catalog aligned with the search tool Young Router can serve."""

    capability = registry.search_tool_capability_for(name)
    if capability is None:
        return
    if capability == SEARCH_TOOL_CAPABILITY_UNSUPPORTED:
        model["supports_search_tool"] = False
        model.pop("web_search_tool_type", None)
        return
    model["supports_search_tool"] = True
    if capability == SEARCH_TOOL_CAPABILITY_BRIDGE:
        # The proxy supplies a text-search function through the local search bridge when
        # a route cannot execute Codex's hosted search itself. Keep the native
        # profile's value: the Responses Lite client recognizes the built-in
        # web-search tool only when this optional field is present.
        model["web_search_tool_type"] = "text_and_image"


def _catalog_model(
    name: str,
    priority: int,
    registry: ModelContextRegistry,
    native_models: Sequence[Mapping[str, Any]],
    base_slug: str | None = None,
    native_profiles: Mapping[str, Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    native_model, exact_native_match = _native_profile_for_name(
        name, native_models, base_slug, native_profiles
    )
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
            _apply_own_reasoning_top(model)

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
    #
    # A generator states no window at all (``0``): it holds no conversation to
    # compact, and the catalogs that describe one state its limit as ``0``.  The
    # entry is still published -- ``/v1/models`` serves every route this app
    # serves -- but it is published without a number rather than with the
    # unknown-model placeholder, which would claim 500,000 tokens of memory for
    # a model that never reads a prompt.
    if context.context_window > 0:
        model["context_window"] = context.context_window
        model["max_context_window"] = context.max_context_window
        model["effective_context_window_percent"] = context.effective_context_window_percent
    else:
        model.pop("context_window", None)
        model.pop("max_context_window", None)
        model.pop("effective_context_window_percent", None)
    _apply_search_tool_capability(model, registry, name)
    # A custom route's thinking arrives as ``response.reasoning_summary_*``
    # frames, and the proxy delivers them only to a request that named a visible
    # summary.  Such a route inherits the base profile's ``none``, which the
    # client reads as "hide the thinking" -- so every route's block stayed empty
    # until the user found ``model_reasoning_summary`` in the client's own TOML
    # by hand.  The catalog is the only place this app can state the default, so
    # a route states one.  An exact native profile is the client's own model and
    # keeps whatever it declares: its thinking does not depend on this proxy.
    if not exact_native_match:
        model["default_reasoning_summary"] = DEFAULT_REASONING_SUMMARY
    # An exact native profile may contain Codex-only modes such as ``ultra``
    # that activate task delegation rather than map directly to a provider
    # reasoning effort, so its ladder stays the client's own.  Every other
    # entry states what its route actually maps -- and an image route states
    # only its disabled step whether or not it has a native namesake, because
    # the profile it inherited advertises thinking this route cannot do.
    if _route_is_image_model(registry, name):
        model["supported_reasoning_levels"] = [
            {
                "effort": "none",
                "description": _REASONING_DESCRIPTIONS["none"],
            }
        ]
        model["default_reasoning_level"] = "none"
    elif not exact_native_match:
        _apply_reasoning_capability(model, _route_reasoning_capability(registry, name))
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
    native_profiles = _native_profiles_by_name(resolved_native)
    # Codex's models-manager discards this array's order and sorts the picker by
    # ``priority`` ascending, so the number is the row's position in the list a
    # person reads.  ``names`` arrives in the one order every generated picker
    # uses -- ``catalog_names_from_editor`` answers it -- so the catalog
    # reproduces that order rather than inventing a second one.
    return {
        "models": [
            _with_required_messages_only(
                _without_duplicated_instructions(
                        _catalog_model(
                        name,
                        index + 1,
                        context_registry,
                        resolved_native,
                        base_slug,
                        native_profiles,
                    )
                )
            )
            for index, name in enumerate(names)
        ]
    }


#: The one message family a catalog entry must state.
#:
#: ``render_model_instructions`` reads only ``model_messages.instructions_template``
#: and answers an *empty* system prompt when it is absent (it logs a warning and
#: returns "").  Every other family -- approvals, permissions, collaboration
#: modes, auto review, multi agent, persistent instructions, token budget,
#: confirmation policies, apps, plugins -- resolves through
#: ``ResolvedMessage::new(catalog, bundled)`` or an ``unwrap_or`` default, so
#: leaving it out hands the client its own version-matched text.  Carrying those
#: 40 KB per entry only pins an older client's wording into every alias.
REQUIRED_MESSAGE_FIELD = "instructions_template"


def _with_required_messages_only(model: dict[str, Any]) -> dict[str, Any]:
    """Keep the one message family the client cannot default."""

    messages = model.get("model_messages")
    if not isinstance(messages, Mapping):
        return model
    template = messages.get(REQUIRED_MESSAGE_FIELD)
    if not isinstance(template, str) or not template.strip():
        # Nothing to keep and nothing to default: leave the entry as it came, so
        # an intentional override (or a diagnostic) is not silently removed.
        return model
    model["model_messages"] = {REQUIRED_MESSAGE_FIELD: template}
    return model


def _without_duplicated_instructions(model: dict[str, Any]) -> dict[str, Any]:
    """Drop the legacy mirror whenever the canonical template carries the text.

    No client this app supports needs the mirror: from 0.163 on the template is
    the carrier, and the server-side compat shim is what re-creates the mirror
    for anything older.  A model whose template is somehow absent keeps the
    field, so a hand-written entry still states its instructions once.
    """

    messages = model.get("model_messages")
    template = messages.get("instructions_template") if isinstance(messages, Mapping) else None
    if isinstance(template, str) and template.strip():
        model.pop(LEGACY_INSTRUCTIONS_FIELD, None)
    return model


def _served_catalog_limits() -> int:
    return MAX_SERVED_CATALOG_BYTES - SERVED_CATALOG_RESERVE_BYTES


def _slim_catalog_payload(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Drop the fields that only carry the client's own prompt text."""

    models = payload.get("models")
    if not isinstance(models, Sequence) or isinstance(models, (str, bytes, bytearray)):
        return dict(payload)
    slimmed: list[Any] = []
    for model in models:
        if not isinstance(model, Mapping):
            slimmed.append(model)
            continue
        slimmed.append({key: value for key, value in model.items() if key not in SERVED_CATALOG_OMIT_FIELDS})
    return {**payload, "models": slimmed}


def served_catalog_body(payload: Mapping[str, Any], *, pretty: bool = False) -> bytes:
    """Encode a catalog for HTTP delivery, inside the bound the client enforces.

    Codex rejects an explicitly configured catalog response above 1 MiB and
    then holds no models at all, because an explicit catalog replaces its
    bundled list instead of falling back to it.  The full entries are served
    while they fit; only a catalog that would be rejected loses
    ``model_messages``/``base_instructions``, where Codex uses its own bundled
    text.  A body that still does not fit is answered with ``b""`` so the
    caller can refuse the request rather than publish something the client
    will discard.
    """

    limit = _served_catalog_limits()

    def encode(value: Mapping[str, Any]) -> bytes:
        # ``pretty`` is for the person reading the address in a browser: the
        # client asks with ``client_version`` and gets the compact wire form,
        # where indentation would only spend the byte budget it is held to.
        if pretty:
            text = json.dumps(value, ensure_ascii=False, indent=2) + "\n"
        else:
            text = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
        return text.encode("utf-8")

    body = encode(payload)
    if len(body) <= limit:
        return body
    body = encode(_slim_catalog_payload(payload))
    return body if len(body) <= limit else b""


def catalog_is_usable(path: Path | str) -> bool:
    """Whether the managed catalog on disk is one Codex will load.

    Codex refuses its entire configuration when the catalog holds no model
    (``must contain at least one model``), so a file with an empty ``models``
    array is as unusable as a missing one.  Callers use this to tell a live
    view that is genuinely empty apart from a file that is simply broken: an
    empty live view must never be handed to the client, and an unusable file
    it already points at must not be left in place either.
    """

    names = catalog_model_names(path)
    return bool(names)


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
    minimal fallback profile, whose instructions are ``FALLBACK_INSTRUCTIONS``;
    one generated from a client carries that client's own prompt.  The
    distinction lets a refresh keep the better file when the client's bundled
    catalog is temporarily unreadable, for example while Codex is updating --
    so it is made on the prompt's *value*: both shapes are identical now that
    the legacy mirror is never written.
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
        template = messages.get("instructions_template") if isinstance(messages, Mapping) else None
        if isinstance(template, str) and template.strip() and template.strip() != FALLBACK_INSTRUCTIONS:
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
        existing = read_json(path, default=None)
        existing_models = existing.get("models") if isinstance(existing, Mapping) else None
        if not isinstance(existing_models, Sequence) or isinstance(
            existing_models, (str, bytes, bytearray)
        ):
            return False
        expected = catalog_payload(
            names,
            registry=registry,
            native_models=resolved_native,
            base_slug=base_slug,
        )
        if any(not isinstance(item, Mapping) for item in existing_models):
            return False
        if existing != expected and [
            item.get("slug") for item in existing_models
        ] != [item.get("slug") for item in expected["models"]]:
            return False
        existing_native = any(
            profile_has_instructions(item) for item in existing_models
        )
        expected_native = any(
            profile_has_instructions(item) for item in expected["models"]
        )
        if existing != expected:
            return expected_native and not existing_native
        return True
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
    "catalog_is_usable",
    "catalog_model_names",
    "catalog_names_from_editor",
    "catalog_payload",
    "legacy_managed_catalog_path",
    "managed_catalog_path",
    "previous_managed_catalog_path",
    "profile_has_instructions",
    "listing_mode_serves_generation",
    "route_serves_only_generation",
    "route_serves_only_images",
    "select_inherited_profile",
    "write_catalog",
]
