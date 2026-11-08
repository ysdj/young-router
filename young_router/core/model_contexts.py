"""Context-window metadata for the managed Codex model catalog.

The catalog is a Codex-facing policy surface, not a copy of LiteLLM's public
pricing table.  A route can expose a public alias while pointing at a
provider-qualified model, so metadata is resolved from the configured route
first.  A small bundled registry keeps the feature useful offline; the
registry can refresh from public upstream metadata and persist the latest
records privately for the next run.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass
import re
import threading
import time
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any
import urllib.request
from urllib.parse import urlparse

from ..browser_identity import browser_request_headers
from ..public_names import public_name_key
from ..values import positive_int as _positive_int
from .persistence import PersistenceError, atomic_write_json, read_json


UNKNOWN_MODEL_CONTEXT_WINDOW = 272_000
DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT = 95

# How far an upstream's stated window must sit from an *inferred* one before the
# inference is read as wrong rather than conservative.  Provider copies of one
# model state ceilings a few percent apart (1,000,000 beside 1,048,576), and that
# is the same window said twice; a copy stating a quarter of it is not the same
# window at all.  Deliberately far from 1.05 and far from 2: this answers "is this
# a different window", never "which of two nearby numbers wins".
_OUTLIER_RATIO = 3.0


def _same_window(left: object, right: object) -> bool:
    """Whether two stated windows are the same window said by two providers.

    Provider copies of one model state ceilings a few percent apart --
    1,000,000 beside 1,048,576 is the same model's window as two services
    publish it -- so equality would read that as disagreement.  ``_OUTLIER_RATIO``
    is the same line the contradiction test draws; this uses it in the other
    direction so the two rules cannot drift apart.
    """

    one = _positive_int(left)
    other = _positive_int(right)
    if one is None or other is None:
        return False
    return max(one, other) / min(one, other) < _OUTLIER_RATIO
DEFAULT_MODEL_CONTEXT_REFRESH_HOURS = 6
MODEL_CONTEXT_CACHE_FILE_NAME = "model-contexts.json"
# Cache spellings retired inside the Codex home.  The cache is this app's
# private metadata, so current releases keep it under the runtime root (never
# in a client directory) and migrate these copies once, newest spelling first.
PREVIOUS_MODEL_CONTEXT_CACHE_FILE_NAMES = (
    "young-router-model-contexts.json",
    "litellm-menu-model-contexts.json",
)
MODEL_CONTEXT_SOURCES = (
    "https://pi.dev/api/models",
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json",
    "https://models.dev/models.json",
)
_PI_SOURCE = MODEL_CONTEXT_SOURCES[0]
_CODEX_SOURCE = MODEL_CONTEXT_SOURCES[1]
SEARCH_TOOL_CAPABILITY_NATIVE = "native"
SEARCH_TOOL_CAPABILITY_BRIDGE = "bridge"
SEARCH_TOOL_CAPABILITY_UNSUPPORTED = "unsupported"
_SOURCE_PRIORITIES = {
    _PI_SOURCE: 40,
    _CODEX_SOURCE: 30,
    MODEL_CONTEXT_SOURCES[2]: 20,
}
_UPSTREAM_MAX_BYTES = 8 * 1024 * 1024
_PI_REASONING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_CODEX_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")
# A published listing -- Pi's ``supported_efforts`` and this app's Codex catalog
# alike -- tops every reasoning ladder out at ``max`` and states ``ultra`` beside
# it.  ``ultra`` is the tier Codex's own catalog places above ``max`` (maximum
# reasoning plus task delegation), and pi.dev's ladder stops at ``max``, so the
# level never reaches a route's provider-wire capability and would otherwise
# vanish from any route that can serve the hardest thinking.
#
# ``max`` is published the same way, and for the same reason: it is the top of
# the client's dial, not a claim about one provider's table.  Codex resolves the
# request effort itself -- ``reasoning_effort_for_request`` in ``codex-rs/core/
# src/client.rs`` maps ``ultra`` onto the model's ``multi_agent_reasoning_effort``,
# else its ``max``, else its highest supported non-``ultra`` level (its own
# ``ultra_reasoning_uses_highest_non_ultra`` test adds ``ultra`` to a model that
# lists no ``max`` and expects ``xhigh`` on the wire) -- so a route can state the
# top tier without serving a provider tier by that name.  This proxy clamps a
# requested ``max``/``ultra`` down to the highest level the route maps
# (``proxy.reasoning._clamp_reasoning_level``; the Google surface also folds
# ``max`` onto ``high`` in ``_GOOGLE_THINKING_LEVEL_BY_EFFORT``), so the knob
# selects the hardest thinking a route actually has instead of sending a value
# its provider would refuse.
_ULTRA_REASONING_LEVEL = "ultra"
_TOP_REASONING_LEVEL = "max"


def published_reasoning_levels(
    levels: Sequence[str],
    *,
    pi_spelling: bool = False,
) -> list[str]:
    """Return reasoning levels the way a listing must state them.

    ``pi_spelling`` renders this app's Codex-side ``none`` as Pi's ``off``: the
    array is read by a Pi client, whose ladder names the disabled step ``off``
    and has no member called ``none``.

    A route that can think at all tops out at ``max`` and ``ultra``: the top of
    the client's dial is what a person picks for the hardest work, and every
    route behind this proxy answers that pick with the hardest level it maps.
    The one exception is a ladder that states nothing but the disabled step (an
    image route), which keeps it alone -- a slider with two tiers it cannot
    honour is worse than no slider.
    """

    published: list[str] = []
    for level in levels:
        rendered = "off" if pi_spelling and level == "none" else level
        if rendered not in published:
            published.append(rendered)
    if any(level != "none" for level in levels) and _TOP_REASONING_LEVEL not in published:
        published.append(_TOP_REASONING_LEVEL)
    if _TOP_REASONING_LEVEL in published and _ULTRA_REASONING_LEVEL not in published:
        published.insert(published.index(_TOP_REASONING_LEVEL) + 1, _ULTRA_REASONING_LEVEL)
    return published


@dataclass(frozen=True)
class ContextRecord:
    context_window: int
    max_context_window: int
    effective_context_window_percent: int = DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT


@dataclass(frozen=True)
class ReasoningCapability:
    """Codex-facing reasoning levels plus Pi's provider-wire mapping."""

    reasoning: bool | None
    supported_levels: tuple[str, ...]
    default_level: str | None
    thinking_level_map: Mapping[str, str | None] | None = None


def _pi_reasoning_level(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = value.strip().casefold()
    if normalized == "none":
        normalized = "off"
    return normalized if normalized in _PI_REASONING_LEVELS else None


def _codex_reasoning_level(value: object) -> str | None:
    pi_level = _pi_reasoning_level(value)
    if pi_level is None:
        return None
    return "none" if pi_level == "off" else pi_level


def _thinking_level_map(value: object) -> dict[str, str | None] | None:
    if not isinstance(value, Mapping):
        return None
    result: dict[str, str | None] = {}
    for raw_level, raw_mapped in value.items():
        level = _pi_reasoning_level(raw_level)
        if level is None or (raw_mapped is not None and not isinstance(raw_mapped, str)):
            continue
        mapped = raw_mapped.strip() if isinstance(raw_mapped, str) else None
        if mapped or raw_mapped is None:
            result[level] = mapped
    return result


def _supported_levels_from_payload(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes, bytearray)):
        return ()
    supported: set[str] = set()
    for item in value:
        raw_level = item.get("effort") if isinstance(item, Mapping) else item
        level = _codex_reasoning_level(raw_level)
        if level is not None:
            supported.add(level)
    return tuple(level for level in _CODEX_REASONING_LEVELS if level in supported)


def _reasoning_fields_from_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    reasoning = value.get("reasoning")
    if isinstance(reasoning, bool):
        fields["reasoning"] = reasoning

    raw_map = value.get("thinking_level_map")
    if raw_map is None and "thinkingLevelMap" in value:
        raw_map = value.get("thinkingLevelMap")
    thinking_map = _thinking_level_map(raw_map)
    if thinking_map is not None:
        fields["thinking_level_map"] = thinking_map

    supported = _supported_levels_from_payload(
        value.get("supported_reasoning_levels")
        if "supported_reasoning_levels" in value
        else value.get("supportedReasoningLevels")
    )
    if supported:
        fields["supported_reasoning_levels"] = list(supported)

    default_level = _codex_reasoning_level(
        value.get("default_reasoning_level")
        if "default_reasoning_level" in value
        else value.get("defaultReasoningLevel")
    )
    if default_level is not None:
        fields["default_reasoning_level"] = default_level
    return fields


def _image_model_metadata_from_payload(value: Mapping[str, Any]) -> dict[str, Any]:
    """Extract the bounded public image-model contract from a catalog record.

    A cache entry carries the contract the normalizer wrote, and the fields it
    was derived from (``modalities``, ``architecture``) are not part of it, so
    a reload returns that stored contract rather than deriving it again from
    fields the cache no longer holds.
    """

    stored = value.get("image_model_metadata")
    if isinstance(stored, Mapping):
        return _image_metadata_contract(stored)
    architecture = value.get("architecture")
    architecture = architecture if isinstance(architecture, Mapping) else {}
    modalities = value.get("modalities")
    modalities = modalities if isinstance(modalities, Mapping) else {}
    output_modalities = architecture.get("output_modalities", modalities.get("output"))
    if not isinstance(output_modalities, list):
        output_modalities = []
    model_type = str(value.get("type") or "").strip().lower()
    is_image = (
        model_type == "image"
        or "image" in {str(item).strip().lower() for item in output_modalities}
        or "image" in str(architecture.get("modality") or "").lower().split("->")[-1]
    )
    if not is_image:
        return {}
    return _image_metadata_contract(value)


def _image_metadata_contract(value: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize one image-model contract, whether derived or already stored.

    A derived contract reads its modalities from ``architecture``/``modalities``;
    a stored one states ``input_modalities``/``output_modalities`` itself, and
    that statement is the one a reload must return.
    """

    architecture = value.get("architecture")
    architecture = architecture if isinstance(architecture, Mapping) else {}
    modalities = value.get("modalities")
    modalities = modalities if isinstance(modalities, Mapping) else {}
    input_modalities = value.get("input_modalities", architecture.get("input_modalities", modalities.get("input")))
    output_modalities = value.get("output_modalities", architecture.get("output_modalities", modalities.get("output")))
    if not isinstance(input_modalities, list):
        input_modalities = []
    if not isinstance(output_modalities, list):
        output_modalities = []
    metadata: dict[str, Any] = {"type": "image"}
    for key, items in (("input_modalities", input_modalities), ("output_modalities", output_modalities)):
        normalized = [
            str(item).strip()
            for item in items[:16]
            if isinstance(item, str) and item.strip()
        ]
        if normalized:
            metadata[key] = normalized
    supported = value.get("supported_parameters")
    if isinstance(supported, list):
        normalized = list(dict.fromkeys(
            str(item).strip()
            for item in supported[:128]
            if isinstance(item, str) and item.strip()
        ))
        if normalized:
            metadata["supported_parameters"] = normalized
    defaults = value.get("default_parameters")
    if isinstance(defaults, Mapping):
        safe_defaults = {
            str(key).strip(): item
            for key, item in list(defaults.items())[:64]
            if isinstance(key, str)
            and key.strip()
            and (isinstance(item, (str, int, float, bool)) or item is None)
        }
        if safe_defaults:
            metadata["default_parameters"] = safe_defaults
    for key in ("id", "canonical_slug", "canonical_model_id", "name", "last_updated"):
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            metadata[key] = item.strip()
    input_limits = value.get("inputLimits")
    if isinstance(input_limits, Mapping):
        safe_limits: dict[str, Any] = {}
        for key, item in list(input_limits.items())[:16]:
            if isinstance(key, str) and key.strip() and isinstance(item, (str, int, float, bool)):
                safe_limits[key.strip()] = item
        if safe_limits:
            metadata["input_limits"] = safe_limits
    return metadata


def _record(
    context_window: int,
    max_context_window: int | None = None,
    *,
    source: str = "bundled",
    priority: int = 10,
) -> dict[str, Any]:
    context = int(context_window)
    maximum = max(context, int(max_context_window or context))
    return {
        "context_window": context,
        "max_context_window": maximum,
        "effective_context_window_percent": DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
        "source": source,
        "priority": priority,
    }


# These are the policy values used when the process is offline.  Codex's
# native agent profiles intentionally use 272k even though some API surfaces
# advertise a larger hard limit; the effective 95% display is therefore about
# 258k, matching the Codex UI.  Other current Young Router routes use the
# provider limits as their starting policy and can be refreshed below.
_BUNDLED_RECORDS: dict[str, dict[str, Any]] = {
    "gpt-5.6": _record(272_000, priority=25),
    "gpt-5.6-sol": _record(272_000, priority=25),
    "gpt-5.6-terra": _record(272_000, priority=25),
    "gpt-5.6-luna": _record(272_000, priority=25),
    "gpt-5.5": _record(272_000, priority=25),
    "gpt-5.2": _record(272_000, priority=25),
    "gemini-3.1-pro-preview": _record(1_048_576),
    "gemini-3.1-flash-lite": _record(1_048_576),
    "gemini-3.1-flash-lite-preview": _record(1_048_576),
    "gemini-3.1-flash-image": _record(65_536),
    "claude-fable-5": _record(1_000_000),
    "claude-sonnet-5": _record(1_000_000),
    "claude-opus-5": _record(1_000_000),
    "glm-5.3": _record(1_000_000),
    # ``glm-5.3-flash`` is served by relays that only think at the top of the
    # dial, and the ladder the registry would otherwise answer with is a
    # third party's: no record states this route, so a suffix match against
    # other providers' ``.../glm-5.3-flash`` entries is what ``high`` came
    # from.  Measured 2026-11-07 on the native Responses wire with tools, both
    # deployments of the group (api.sisct2.xyz, new.youyangai.top):
    # ``effort=high`` produced a reasoning item on 1 of 16 short turns and
    # ``effort=max`` on 19 of 21 -- and a turn without one reported
    # ``output_tokens_details.reasoning_tokens: 0``, so the upstream answered
    # without thinking and nothing was dropped on this side.  ``high`` on this
    # route is therefore stated as what it really selects.  ``low`` is kept
    # because it is where a request that asked for no thinking lands: every
    # level above it maps to ``max``, and ``off``/``minimal`` clamp onto the
    # only tier that turns thinking off.
    "glm-5.3-flash": {
        **_record(1_000_000, priority=45),
        "reasoning": True,
        "thinking_level_map": {
            "off": None,
            "minimal": None,
            "low": "low",
            "medium": None,
            "high": "max",
            "xhigh": None,
            "max": "max",
        },
    },
    "kimi-k3": _record(262_144, 1_048_576, priority=25),
    "k3-256k": _record(262_144, priority=25),
    "gpt-image-2": _record(65_536),
}
_BUNDLED_ALIASES = {
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
}

_GPT_FAMILY_MODEL_RE = re.compile(r"^gpt-[0-9]", re.IGNORECASE)


def gpt_family_model_name(value: object) -> bool:
    """Return whether a model name belongs to the GPT family.

    The digit after the ``gpt-`` prefix keeps this narrow: ``gpt-5.6-sol``
    and ``gpt-4.1`` match, while image families such as ``gpt-image-2`` do
    not. Keep this aligned with ``_codex_gpt_family_model_name`` in
    ``young_router.proxy.responses_request``, which owns the request-time rule.
    """

    if not isinstance(value, str):
        return False
    return bool(_GPT_FAMILY_MODEL_RE.match(value.strip()))


def _api_base_host(value: object) -> str:
    if not isinstance(value, str):
        return ""
    parsed = urlparse(value.strip())
    return (parsed.hostname or "").strip().casefold()


def _record_from_payload(value: object, *, source: str, priority: int) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    limits = value.get("limit")
    limits = limits if isinstance(limits, Mapping) else {}
    context = (
        _positive_int(value.get("context_window"))
        or _positive_int(value.get("contextWindow"))
        or _positive_int(limits.get("context"))
    )
    maximum = _positive_int(value.get("max_context_window"))
    if maximum is None:
        maximum = _positive_int(value.get("max_input_tokens")) or _positive_int(limits.get("input"))
    if context is None:
        context = maximum
    image_metadata = _image_model_metadata_from_payload(value)
    context_known = context is not None
    if context is None and not image_metadata:
        return None
    # A generator with no stated window has no window to publish.  The public
    # catalogs state ``limit.context: 0`` for ``gpt-image-2.5-flare`` and its
    # siblings -- they are cogs with no conversation to hold -- and folding
    # that into the unknown-model placeholder published 272,000 as if pi.dev
    # had said it, on the pane's 公开上下文 and in the catalog Codex compacts
    # against.  ``0`` is the whole statement here, so the record keeps it and
    # every surface reads "no window" instead of a number nobody declared.
    # A text record that states nothing still falls back, exactly as before.
    if image_metadata and not context_known:
        return {
            "context_window": 0,
            "max_context_window": 0,
            "effective_context_window_percent": DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
            "source": source,
            "priority": 0,
            "image_model_metadata": image_metadata,
            **_reasoning_fields_from_payload(value),
        }
    context = context or UNKNOWN_MODEL_CONTEXT_WINDOW
    maximum = max(context, maximum or context)
    percent = _positive_int(value.get("effective_context_window_percent"))
    if percent is None or percent > 100:
        percent = DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT
    auto_compact = _positive_int(value.get("auto_compact_token_limit"))
    result: dict[str, Any] = {
        "context_window": context,
        "max_context_window": maximum,
        "effective_context_window_percent": percent,
        "source": source,
        "priority": priority if context_known else 0,
    }
    if auto_compact is not None:
        result["auto_compact_token_limit"] = auto_compact
    if image_metadata:
        result["image_model_metadata"] = image_metadata
    result.update(_reasoning_fields_from_payload(value))
    return result


def _pi_source_records(payload: object, *, source: str, priority: int) -> dict[str, dict[str, Any]]:
    """Extract Pi's provider-qualified agent profiles without conflating routes."""

    result: dict[str, dict[str, Any]] = {}
    if not isinstance(payload, Mapping):
        return result
    for raw_provider, models in payload.items():
        if not isinstance(raw_provider, str) or not raw_provider.strip():
            continue
        if not isinstance(models, Mapping):
            continue
        provider = raw_provider.strip().casefold()
        for item in models.values():
            if not isinstance(item, Mapping):
                continue
            model_id = item.get("id")
            if not isinstance(model_id, str) or not model_id.strip():
                continue
            record = _record_from_payload(item, source=source, priority=priority)
            if record is not None:
                result[f"{provider}/{model_id.strip().casefold()}"] = record
    return result


def _source_records(payload: object, *, source: str, priority: int) -> dict[str, dict[str, Any]]:
    """Extract provider-neutral or Codex model records from an upstream JSON document."""

    if source == _PI_SOURCE:
        return _pi_source_records(payload, source=source, priority=priority)
    result: dict[str, dict[str, Any]] = {}
    if isinstance(payload, Mapping):
        # models.dev/models.json is a flat ``provider/model`` map.
        # Accept the nested provider form as well for compatible mirrors.
        nested_models = any(
            isinstance(value, Mapping) and isinstance(value.get("models"), Mapping)
            for value in payload.values()
        )
        if nested_models:
            for raw_provider, provider in payload.items():
                if not isinstance(raw_provider, str) or not isinstance(provider, Mapping):
                    continue
                models = provider.get("models")
                if not isinstance(models, Mapping):
                    continue
                for raw_id, item in models.items():
                    if not isinstance(raw_id, str) or not isinstance(item, Mapping):
                        continue
                    model_id = str(item.get("id") or raw_id).strip()
                    record = _record_from_payload(item, source=source, priority=priority)
                    if record is not None:
                        result[f"{raw_provider.strip().casefold()}/{model_id.casefold()}"] = record
            return result
        if isinstance(payload.get("models"), Sequence) and not isinstance(payload.get("models"), (str, bytes, bytearray)):
            for item in payload["models"]:
                if not isinstance(item, Mapping):
                    continue
                model_id = item.get("slug") or item.get("id") or item.get("model")
                if not isinstance(model_id, str) or not model_id.strip():
                    continue
                record = _record_from_payload(item, source=source, priority=priority)
                if record is not None:
                    result[model_id.strip().casefold()] = record
            return result
        for raw_id, item in payload.items():
            if not isinstance(raw_id, str) or not raw_id.strip():
                continue
            record = _record_from_payload(item, source=source, priority=priority)
            if record is not None:
                result[raw_id.strip().casefold()] = record
    return result


def _merge_records(target: dict[str, dict[str, Any]], incoming: Mapping[str, Mapping[str, Any]]) -> None:
    for raw_id, record in incoming.items():
        model_id = str(raw_id).strip().casefold()
        if not model_id:
            continue
        current = target.get(model_id)
        priority = _positive_int(record.get("priority")) or 0
        current_priority = _positive_int(current.get("priority")) if isinstance(current, Mapping) else None
        if current is None:
            target[model_id] = dict(record)
            continue

        merged = dict(current)
        if priority >= (current_priority or 0) and record.get("context_known", True):
            merged.update(record)

        # A hard-limit source can win the context fields while carrying no
        # provider-specific reasoning policy. Keep the highest-priority value
        # for each Pi/Codex reasoning field independently.
        for key in (
            "reasoning",
            "thinking_level_map",
            "supported_reasoning_levels",
            "default_reasoning_level",
        ):
            if key in record and (
                key not in current or priority >= (current_priority or 0)
            ):
                merged[key] = record[key]
        current_image = current.get("image_model_metadata") if isinstance(current, Mapping) else None
        incoming_image = record.get("image_model_metadata")
        if isinstance(incoming_image, Mapping):
            merged_image = dict(current_image) if isinstance(current_image, Mapping) else {}
            for key, value in incoming_image.items():
                merged_image.setdefault(key, value)
            if merged_image:
                merged["image_model_metadata"] = merged_image
        target[model_id] = merged


def _candidate_ids(value: str) -> list[str]:
    raw = value.strip().casefold()
    if not raw:
        return []
    for separator in ("@",):
        if separator in raw:
            raw = raw.split(separator, 1)[0]
    if raw.startswith("models/"):
        raw = raw[7:]
    candidates: list[str] = [raw]
    if "." in raw and "/" not in raw:
        candidates.append(raw.split(".", 1)[1])
    parts = raw.split("/")
    for index in range(1, len(parts)):
        candidates.append("/".join(parts[index:]))
    if parts:
        candidates.append(parts[-1])
    # Bare provider aliases are common in LiteLLM model parameters.
    if raw.startswith("openai."):
        candidates.append(raw[7:])
    unique: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        candidate = candidate.strip()
        if candidate and candidate not in seen:
            seen.add(candidate)
            unique.append(candidate)
    return unique


def _image_generator_keys(model_id: object) -> list[str]:
    """The identity forms one model id is remembered under.

    Deliberately *not* ``_candidate_ids``: that name list exists to find catalog
    profiles and includes a version split (``gpt-5.6`` -> ``6``) for Bedrock's
    ``provider.alias`` spelling, which would file ``gpt-image-2.6-flare`` under
    ``6-flare`` as well.  A probe finding is a statement about one model, so it
    is remembered under its own spelling with and without the adapter prefix.
    """

    if not isinstance(model_id, str):
        return []
    raw = model_id.strip().casefold()
    if not raw:
        return []
    keys = [raw]
    if "/" in raw:
        stripped = raw.split("/", 1)[1].strip()
        if stripped:
            keys.append(stripped)
    return list(dict.fromkeys(keys))


def _context_window_policy(record: Mapping[str, Any]) -> tuple[int, int, int]:
    context = int(record["context_window"])
    maximum = int(record.get("max_context_window") or context)
    percent = int(
        record.get(
            "effective_context_window_percent",
            DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
        )
    )
    return context, maximum, percent


def _suffix_match_keys(records: Mapping[str, Mapping[str, Any]], candidates: Sequence[str]) -> set[str]:
    """The record keys that end with one of the candidates.

    The decision is the same comparison the plain scan made: the key's last
    path segment equals a candidate, or the key ends with ``/<candidate>``.
    Every key that can satisfy either test has a last segment equal to a
    candidate or to a candidate's own last segment, so the run only looks at
    those keys instead of at every record.  The record's own tail index is
    reused when the mapping carries one, because a snapshot resolves the same
    hundreds of records once per public model.
    """

    if isinstance(records, RecordLookupIndex):
        return _suffix_match_keys_from_tails(records.record_tail_index(), candidates)
    grouped: dict[str, list[str]] = {}
    for key in records:
        grouped.setdefault(key.rsplit("/", 1)[-1], []).append(key)
    return _suffix_match_keys_from_tails(
        {tail: tuple(keys) for tail, keys in grouped.items()}, candidates
    )


def _suffix_match_keys_from_tails(
    tails: Mapping[str, Sequence[str]], candidates: Sequence[str]
) -> set[str]:
    """The keys ending with a candidate, given keys grouped by their tail."""

    matched: set[str] = set()
    for candidate in candidates:
        # The key's own tail names the candidate.
        matched.update(tails.get(candidate, ()))
        # The candidate is a path suffix of the key; a multi-segment candidate
        # keeps the same tail, so the same group answers this test.
        for key in tails.get(candidate.rsplit("/", 1)[-1], ()):
            if key.endswith(f"/{candidate}"):
                matched.add(key)
    return matched


class RecordLookupIndex(dict):
    """The record map plus a tail index `_lookup` reuses across calls.

    Subclassing the mapping keeps every existing reader and writer of
    ``registry._records`` working, while the index is rebuilt whenever the
    records themselves are replaced.
    """

    tail_index: dict[str, tuple[str, ...]] | None = None

    def record_tail_index(self) -> dict[str, tuple[str, ...]]:
        if self.tail_index is None:
            grouped: dict[str, list[str]] = {}
            for key in self:
                grouped.setdefault(key.rsplit("/", 1)[-1], []).append(key)
            self.tail_index = {tail: tuple(keys) for tail, keys in grouped.items()}
        return self.tail_index

    def invalidate_tail_index(self) -> None:
        self.tail_index = None


#: The fields a record states about a route's thinking, as opposed to its
#: context window.  They travel together because a ladder is one statement:
#: taking them from different provider copies produces a surface no provider
#: describes (``_consensus_reasoning`` is the merge that keeps them honest).
_REASONING_RECORD_KEYS = (
    "reasoning",
    "thinking_level_map",
    "supported_reasoning_levels",
    "default_reasoning_level",
)


def _consensus_reasoning(records: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Merge several provider copies of one model into the ladder they share.

    The registry's keys are provider-qualified, so one public model served by
    many providers has many records.  A route asks for the *model*, and a relay
    deployment spells it ``openai/deepseek-v4.1-flash`` -- a wire prefix that
    names no catalog provider -- so the suffix match below has several copies
    and no way to tell which one describes this route.

    Reading the first copy in the cache made that copy's restrictions the
    route's own: the copy that came first for ``deepseek-v4.1-flash`` mapped
    ``low``/``medium`` to ``null``, so the listing offered ``none, high, max``
    for a model whose own vendor (pi.dev's ``deepseek/deepseek-flash``, the same
    "DeepSeek V4.1 Flash") serves ``low``, ``high`` and ``max``.  The answer
    also moved whenever a cache refresh reordered the records.

    The copies are independent statements about the same model, so a level is
    part of the model's ladder when a strict majority of the copies that state
    a level map serve it, carrying the value most of them map it to.  A level
    the majority does not serve is stated as unsupported, so the merged record
    answers exactly the ladder a listing publishes.  Every value here comes
    from a record; nothing is inferred from the model's name or family.
    """

    capabilities = [
        capability
        for capability in (_reasoning_capability(record) for record in records)
        if capability is not None
    ]
    if not capabilities:
        return {}

    agreed: dict[str, Any] = {}
    reasoning_values = {capability.reasoning for capability in capabilities}
    if len(reasoning_values) == 1:
        reasoning = reasoning_values.pop()
        if reasoning is not None:
            agreed["reasoning"] = reasoning

    mapped = [capability for capability in capabilities if capability.thinking_level_map]
    declared = [
        capability
        for capability in capabilities
        if not capability.thinking_level_map and capability.supported_levels
    ]
    if not mapped:
        # No copy states a level map; the ones that state a level array are the
        # only statements there are, and they merge the same way.
        counts = Counter(
            level for capability in declared for level in capability.supported_levels
        )
        levels = [
            level
            for level in _CODEX_REASONING_LEVELS
            if counts[level] * 2 > len(declared)
        ]
        if levels:
            agreed["supported_reasoning_levels"] = levels
        return agreed

    ladder: dict[str, Any] = {}
    served_any = False
    for level in _CODEX_REASONING_LEVELS:
        serving = [capability for capability in mapped if level in capability.supported_levels]
        if len(serving) * 2 <= len(mapped):
            ladder[level] = None
            continue
        served_any = True
        values = [
            (capability.thinking_level_map or {}).get(level) for capability in serving
        ]
        stated = [value for value in values if value is not None]
        if stated:
            ladder[level] = Counter(stated).most_common(1)[0][0]
        # A served level no copy states a value for stays unstated: that is how
        # a record says the level is passed through as the client sent it.
    if not served_any:
        # Every copy disagrees about every level.  Stating an empty ladder would
        # publish a reasoning control with no steps, so the caller keeps the
        # record it already selected rather than this merge.
        return {}
    agreed["thinking_level_map"] = ladder
    return agreed


def _lookup(records: Mapping[str, Mapping[str, Any]], model_id: str) -> dict[str, Any] | None:
    candidates = _candidate_ids(model_id)
    if not candidates:
        return None
    direct_matches: list[tuple[int, int, dict[str, Any]]] = []
    for index, candidate in enumerate(candidates):
        direct = records.get(candidate)
        if isinstance(direct, Mapping):
            direct_matches.append((_positive_int(direct.get("priority")) or 0, -index, dict(direct)))
    direct_match = max(direct_matches, key=lambda item: (item[0], item[1])) if direct_matches else None
    suffix_keys = _suffix_match_keys(records, candidates)
    # Walk the records in their own order so the selected record for a tie is
    # the same one the ordered scan used to produce.
    suffix_matches: list[tuple[int, dict[str, Any]]] = []
    for key, value in records.items():
        if key not in suffix_keys or not isinstance(value, Mapping):
            continue
        suffix_matches.append((_positive_int(value.get("priority")) or 0, dict(value)))
    if suffix_matches:
        top_priority = max(item[0] for item in suffix_matches)
        if direct_match is not None:
            direct_source = direct_match[2].get("source")
            # An exact Codex catalog entry is already a provider-neutral
            # policy decision.  Only let provider-qualified profiles replace
            # the low-confidence bundled alias that caused the lookup to
            # miss an upstream profile in the first place.
            if direct_source != "bundled" or direct_match[0] >= top_priority:
                return direct_match[2]
        top_records = [item[1] for item in suffix_matches if item[0] == top_priority]
        # Provider-qualified copies of the same public model often share a
        # family while advertising slightly different windows. Falling through
        # to the unknown default hides a known 1M-class model as 258k.
        context, maximum, percent = min(_context_window_policy(record) for record in top_records)
        selected = dict(top_records[0])
        selected["context_window"] = context
        selected["max_context_window"] = maximum
        selected["effective_context_window_percent"] = percent
        consensus = _consensus_reasoning(top_records)
        for key in _REASONING_RECORD_KEYS:
            selected.pop(key, None)
        selected.update(consensus)
        return selected
    return direct_match[2] if direct_match is not None else None


def _default_reasoning_level(
    supported_levels: Sequence[str],
    preferred: object = None,
) -> str | None:
    supported = tuple(supported_levels)
    preferred_level = _codex_reasoning_level(preferred)
    if preferred_level in supported:
        return preferred_level
    for candidate in ("medium", "low", "minimal", "high", "xhigh", "max", "none"):
        if candidate in supported:
            return candidate
    return None


def _reasoning_capability(record: Mapping[str, Any]) -> ReasoningCapability | None:
    has_reasoning_metadata = any(
        key in record
        for key in (
            "reasoning",
            "thinking_level_map",
            "supported_reasoning_levels",
            "default_reasoning_level",
        )
    )
    if not has_reasoning_metadata:
        return None

    reasoning = record.get("reasoning")
    reasoning = reasoning if isinstance(reasoning, bool) else None
    raw_map = record.get("thinking_level_map")
    thinking_map = _thinking_level_map(raw_map) if isinstance(raw_map, Mapping) else None

    if reasoning is False:
        supported = ("none",)
    elif thinking_map is not None:
        supported_pi: list[str] = []
        for level in _PI_REASONING_LEVELS:
            mapped = thinking_map.get(level)
            if mapped is None and level in thinking_map:
                continue
            if level in {"xhigh", "max"} and level not in thinking_map:
                continue
            supported_pi.append(level)
        supported = tuple("none" if level == "off" else level for level in supported_pi)
    else:
        declared = _supported_levels_from_payload(record.get("supported_reasoning_levels"))
        if declared:
            supported = declared
        elif reasoning is True:
            # Pi's default contract exposes the standard levels through high;
            # xhigh/max require explicit map entries.
            supported = _CODEX_REASONING_LEVELS[:5]
        else:
            return None

    codex_map = (
        {
            ("none" if level == "off" else level): mapped
            for level, mapped in thinking_map.items()
        }
        if thinking_map is not None
        else None
    )
    return ReasoningCapability(
        reasoning=reasoning,
        supported_levels=supported,
        default_level=_default_reasoning_level(
            supported,
            record.get("default_reasoning_level"),
        ),
        thinking_level_map=codex_map,
    )


def _litellm_record(model_ids: Sequence[str]) -> dict[str, Any] | None:
    # LiteLLM's provider registry is large and is not needed for the normal
    # bundled-context path. Import it only for an unknown model that actually
    # needs a provider fallback lookup.
    import litellm

    for model_id in model_ids:
        for candidate in _candidate_ids(model_id):
            value = litellm.model_cost.get(candidate)
            if not isinstance(value, Mapping):
                continue
            # ``max_tokens`` is LiteLLM's own legacy fallback for the window, as
            # its cost map's ``sample_spec`` entry states: an entry that carries
            # only it has stated one number, not an output ceiling beside an
            # unknown window.  Read as an output cap it would be exactly the
            # context/max-output confusion, and the entries that state both keep
            # ``max_input_tokens`` first, which is the larger, real window.
            context = _positive_int(value.get("max_input_tokens")) or _positive_int(value.get("max_tokens"))
            if context is not None:
                return _record(context, source="litellm", priority=15)
    return None


#: LiteLLM's mode for a model that generates images and answers no text turn.
#: Its cost map is the one table that states a modality for a name no public
#: catalog covers yet: ``gpt-image-2.6-flare`` is in it as ``image_generation``
#: while pi.dev and models.dev have nothing under that spelling, and a route
#: whose name is new is exactly the one a picker must not offer as a chat model.
_IMAGE_GENERATION_MODE = "image_generation"


def litellm_mode(model_ids: Sequence[str] | str) -> str | None:
    """The mode LiteLLM's cost map states for one of these ids, if it states one.

    Read from ``litellm.model_cost`` rather than through ``get_model_info``:
    that function raises for a name the map does not carry, and the question
    here is only whether the table speaks about this model at all.  The import
    is the same one ``_litellm_record`` already pays for, and it is lazy so a
    Core that resolves known profiles never imports the runtime.

    A single name is accepted as well as a list: a string is a sequence of
    characters, and iterating one here asked the cost map about ``g``, ``p``,
    ``t``... and answered ``None`` for a model it does state.
    """

    if isinstance(model_ids, str):
        model_ids = [model_ids]
    try:
        import litellm
    except Exception:
        return None
    cost_map = getattr(litellm, "model_cost", None)
    if not isinstance(cost_map, Mapping):
        return None
    for model_id in model_ids:
        for candidate in _candidate_ids(model_id):
            value = cost_map.get(candidate)
            if isinstance(value, Mapping) and isinstance(value.get("mode"), str):
                mode = value["mode"].strip().casefold()
                if mode:
                    return mode
    return None


def _litellm_states_image_generation(model_ids: Sequence[str] | str) -> bool:
    """Whether LiteLLM's cost map calls one of these ids an image generator."""

    return litellm_mode(model_ids) == _IMAGE_GENERATION_MODE


_INSTALLED_LITELLM_VERSION: list[str | None] = []


def _installed_litellm_version() -> str | None:
    """Which LiteLLM the fallback lookup would answer from, without importing it.

    Read from the installed distribution's metadata, so the answer costs a file
    read instead of the runtime this module exists to keep out of the Core's
    startup.
    """

    if _INSTALLED_LITELLM_VERSION:
        return _INSTALLED_LITELLM_VERSION[0]
    version_value: str | None = None
    try:
        from importlib.metadata import version

        version_value = version("litellm")
    except Exception:
        version_value = None
    _INSTALLED_LITELLM_VERSION.append(version_value)
    return version_value


#: Serializes remote context-source refreshes across registries.  The fetch
#: runs without Core's store lock, so this is what keeps two background lanes
#: from spending the same eight-second ceiling twice.
_CONTEXT_REFRESH_LOCK = threading.Lock()


class ModelContextRegistry:
    """Resolve model context policy and periodically refresh its metadata."""

    def __init__(
        self,
        *,
        runtime_config_path: Path | str | None = None,
        runtime_settings_path: Path | str | None = None,
        cache_path: Path | str | None = None,
        legacy_cache_paths: Sequence[Path | str] | None = None,
        refresh_enabled: bool | None = None,
        fetcher: Callable[[str], object] | None = None,
        clock: Callable[[], float] | None = None,
        stated_windows: Callable[[], Mapping[str, int]] | None = None,
    ) -> None:
        self.runtime_config_path = Path(runtime_config_path).expanduser() if runtime_config_path else None
        self.runtime_settings_path = Path(runtime_settings_path).expanduser() if runtime_settings_path else None
        self.cache_path = Path(cache_path).expanduser() if cache_path else None
        self.legacy_cache_paths = tuple(
            Path(path).expanduser() for path in (legacy_cache_paths or ())
        )
        self.refresh_enabled = bool(runtime_settings_path) if refresh_enabled is None else bool(refresh_enabled)
        self._fetcher = fetcher or self._fetch_json
        self._clock = clock or time.time
        self._records: dict[str, dict[str, Any]] = RecordLookupIndex()
        self._cache_loaded = False
        # Identity of the cache file this instance's records came from.  The
        # cache has more than one writer -- this registry's own refresh, and the
        # one a refresh-less reader like a pane snapshot uses -- so records read
        # once would keep answering from a cache another writer has already
        # replaced.  That is how a public model added after a refresh was drawn
        # with the unknown-model window while pi.dev named its profile: the
        # pane's registry had read the file before the profile landed and never
        # looked again.
        self._cache_file_identity: tuple[int, int, int] | None = None
        # Every catalog entry resolves the same runtime configuration (route
        # deployments, model ids, search capability).  Parsing that document
        # once per file generation replaces the per-model re-reads that made a
        # single catalog build parse a 33 KB YAML three dozen times.
        self._route_index_key: tuple[int, int, int] | None = None
        self._route_index_cache: dict[str, list[dict[str, Any]]] = {}
        self._cache_fetched_at: float | None = None
        # The windows the routes' own upstreams stated, as the caller that
        # performed those catalog reads holds them (an id, a number).  The
        # number is the upstream's, so it is never written into the
        # configuration: a stale reading must not outlive the read that produced
        # it.  It answers one question and one question only -- the window of a
        # model this registry cannot resolve at all (``_stated_contexts``).  That
        # is what keeps a catalog's generic fallback from overriding a profile
        # the registry does know, which is the defect of 2026-11-04.
        self._stated_windows: Callable[[], Mapping[str, int]] | None = stated_windows
        # The readings themselves, as the shared cache holds them.  In-memory
        # sources are not enough: the managed catalog is written by a domain
        # that never performed a catalog read, so the number has to travel
        # through the cache both domains open.
        self._stated: dict[str, int] = {}
        # Which model ids a probe measured as served by the Images wire, and
        # which it measured as a chat model.  The same travelling problem as the
        # windows above, for the same reason: no table carries a name that is
        # newer than the release this app bundles.
        self._image_generators: dict[str, bool] = {}
        # Answers the provider-registry fallback already gave: a model id mapped
        # to its record, or to None when LiteLLM does not know it either.  Kept
        # in the same cache as the fetched profiles so a start that resolves the
        # same routes does not import the runtime to repeat itself.
        self._provider_lookups: dict[str, dict[str, Any] | None] = {}
        self._last_refresh_attempt: float | None = None
        # Remote sources are fetched outside the Core lock.  The refresh lock
        # lives at module scope so it is not part of the instance state that a
        # transaction checkpoint deep-copies; it only keeps two refresh lanes
        # from doing the same fetch twice.  Readers never take it: the completed
        # records are swapped in as one assignment.
        self._runtime_values_key: tuple[int, int] | None = None
        self._runtime_values_cache: tuple[int, int] | None = None

    @staticmethod
    def _fetch_json(url: str) -> object:
        request = urllib.request.Request(
            url,
            headers=browser_request_headers(),
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = response.read(_UPSTREAM_MAX_BYTES + 1)
        if len(payload) > _UPSTREAM_MAX_BYTES:
            raise ValueError("upstream model metadata is too large")
        import json

        return json.loads(payload.decode("utf-8"))

    def _remove_legacy_caches(self) -> None:
        """Drop retired in-Codex-home copies once the private cache is current."""

        for legacy in self.legacy_cache_paths:
            if self.cache_path is not None and legacy == self.cache_path:
                continue
            try:
                legacy.unlink()
            except OSError:
                pass

    def _migrate_legacy_cache(self, payload: Mapping[str, Any]) -> None:
        """Copy a retired in-Codex-home cache to the runtime root once."""

        if self.cache_path is None:
            return
        try:
            atomic_write_json(self.cache_path, dict(payload))
        except PersistenceError:
            return
        self._remove_legacy_caches()

    def _read_cache(self) -> Mapping[str, Any]:
        """Read the private cache, importing a retired Codex-home copy once."""

        if self.cache_path is not None:
            try:
                payload = read_json(self.cache_path, default={})
            except PersistenceError:
                payload = {}
            if isinstance(payload, Mapping) and payload.get("records"):
                self._remove_legacy_caches()
                return payload
        for legacy in self.legacy_cache_paths:
            if self.cache_path is not None and legacy == self.cache_path:
                continue
            try:
                payload = read_json(legacy, default={})
            except PersistenceError:
                continue
            if isinstance(payload, Mapping) and payload.get("records"):
                self._migrate_legacy_cache(payload)
                return payload
        return {}

    def _load_provider_lookups(self, payload: Mapping[str, Any]) -> None:
        """Restore earlier fallback answers, unless LiteLLM itself changed."""

        section = payload.get("provider_lookups")
        if not isinstance(section, Mapping):
            return
        installed = _installed_litellm_version()
        if installed is None or section.get("litellm_version") != installed:
            return
        cached = section.get("records")
        if not isinstance(cached, Mapping):
            return
        self._provider_lookups = {
            model_id: dict(record) if isinstance(record, Mapping) else None
            for model_id, record in cached.items()
            if isinstance(model_id, str)
        }

    def stated_windows(self) -> dict[str, int]:
        """The windows catalog reads have reported, as the shared cache holds them.

        Written by the side that performed the read and read by every other
        answer, so the pane and the managed catalog resolve one number instead
        of one of them falling back to the unknown-model placeholder.  It is a
        *reading*: nothing here is a declaration, and the resolution rule that
        decides when it may answer lives in ``_stated_contexts``.
        """

        self._sync_cache()
        return dict(self._stated)

    def remember_stated_windows(self, windows: Mapping[str, int]) -> None:
        """Record the windows catalog reads reported.

        Merged into what the cache already holds rather than replacing it: a
        read answers for one provider's catalog, and the next read is of
        another's.  Only the ids this read actually stated are rewritten, so an
        account whose roster dropped a model keeps the other accounts' entries.
        """

        stated = {
            name: int(window)
            for name, window in windows.items()
            if isinstance(name, str)
            and name.strip()
            and isinstance(window, int)
            and not isinstance(window, bool)
            and window > 0
        }
        if not stated:
            return
        self._sync_cache()
        if all(self._stated.get(name) == window for name, window in stated.items()):
            return
        self._stated.update(stated)
        self._write_cache()

    def _load_stated_windows(self, payload: Mapping[str, Any]) -> None:
        """Restore the upstream readings another writer stored."""

        section = payload.get("stated_windows")
        if not isinstance(section, Mapping):
            return
        self._stated = {
            name: int(window)
            for name, window in section.items()
            if isinstance(name, str)
            and isinstance(window, int)
            and not isinstance(window, bool)
            and window > 0
        }

    def _load_image_generators(self, payload: Mapping[str, Any]) -> None:
        """Restore which model ids a probe measured as image generators."""

        section = payload.get("image_generators")
        if not isinstance(section, Mapping):
            return
        self._image_generators = {
            name: bool(value)
            for name, value in section.items()
            if isinstance(name, str) and isinstance(value, bool)
        }

    def _cache_payload(self) -> dict[str, Any]:
        """Everything the private cache holds, written by one place.

        The stated windows are merged with whatever the file already holds, and
        that merge is what makes two writers one cache.  This registry is not
        the only one in the process -- the pane's domain and the managed
        catalog's each own one -- and a writer that has not loaded the file yet
        otherwise replaced the other's readings with the empty map it holds,
        which is how a catalog read's 960,000 became the placeholder again
        between one pane refresh and the next catalog write.
        """

        return {
            "fetched_at": self._cache_fetched_at,
            "records": self._records,
            "provider_lookups": {
                "litellm_version": _installed_litellm_version(),
                "records": self._provider_lookups,
            },
            # The windows upstreams stated, keyed by their own model id.  The
            # pane's domain is the only one that performs those reads, and the
            # managed catalog is written by the other; without this they answer
            # one model with two numbers.
            "stated_windows": self._merged_stated_windows(),
            # What a probe measured, merged for the same reason the windows are:
            # the two domains open this file and only one of them ever probes.
            "image_generators": self._merged_image_generators(),
        }

    def _merged_stated_windows(self) -> dict[str, int]:
        """This registry's readings merged over the ones the file already holds."""

        stored = self._read_cache().get("stated_windows")
        if not isinstance(stored, Mapping):
            return dict(self._stated)
        merged = {
            name: int(window)
            for name, window in stored.items()
            if isinstance(name, str)
            and isinstance(window, int)
            and not isinstance(window, bool)
            and window > 0
        }
        # This instance's own readings are the newer of the two, and its key set
        # is authoritative for what it just read: a model whose entry lost its
        # window must not be answered from a copy of the entry before the loss.
        merged.update(self._stated)
        return merged

    def _merged_image_generators(self) -> dict[str, bool]:
        """This registry's findings merged over the ones the file already holds."""

        stored = self._read_cache().get("image_generators")
        if not isinstance(stored, Mapping):
            return dict(self._image_generators)
        merged = {
            name: bool(value)
            for name, value in stored.items()
            if isinstance(name, str) and isinstance(value, bool)
        }
        merged.update(self._image_generators)
        return merged

    def _write_cache(self) -> None:
        if self.cache_path is None:
            return
        try:
            atomic_write_json(self.cache_path, self._cache_payload())
            # This instance's records are now what the file states, so the
            # next lookup must not read the write back as someone else's.
            self._cache_file_identity = self._cache_file_state()
            self._remove_legacy_caches()
        except PersistenceError:
            pass

    def _resolve_provider_record(self, model_id: str) -> dict[str, Any] | None:
        """Answer the provider-registry fallback without paying for it twice.

        ``_litellm_record`` imports the complete LiteLLM runtime to read one
        registry, and this lookup runs while the Core is starting: every route
        whose model the bundled profiles do not know used to wait for that
        import before the settings windows could be drawn.  The answer -- the
        record, or the fact that LiteLLM does not know the model either -- is
        written beside the fetched profiles and dropped when the installed
        LiteLLM changes, because that is what the answer depends on.
        """

        if model_id in self._provider_lookups:
            cached = self._provider_lookups[model_id]
            return dict(cached) if isinstance(cached, Mapping) else None
        record = _litellm_record([model_id])
        if _installed_litellm_version() is not None:
            self._provider_lookups[model_id] = dict(record) if record is not None else None
            self._write_cache()
        return record

    def _cache_file_state(self) -> tuple[int, int, int] | None:
        """The identity of the cache file as it stands on disk right now.

        ``None`` means no cache file is configured or none can be read.  The
        inode belongs to the identity because the cache is written atomically:
        a replacement arrives as a new file, and one whose size happens to
        match the previous copy must still be noticed.
        """

        if self.cache_path is None:
            return None
        try:
            details = self.cache_path.stat()
        except OSError:
            return None
        return (details.st_ino, details.st_mtime_ns, details.st_size)

    def _adopt_external_cache_write(self) -> None:
        """Re-read the cache when another writer has replaced it.

        This instance's records describe one version of that file; once a
        different one lands, answering from the copy in hand reports windows
        the file no longer states.  The provider-registry fallbacks memoized
        from that copy go with it, because they answer for the same snapshot.
        A cache that has disappeared keeps the records already in hand: the
        file going away is not evidence that a resolved window became unknown,
        and the bundled fallback may not stand in for it.

        This instance's own ``_write_cache`` refreshes the recorded identity, so
        the file it just wrote is never adopted as someone else's.
        """

        if not self._cache_loaded:
            return
        identity = self._cache_file_state()
        if identity is None or identity == self._cache_file_identity:
            return
        # Drop the copy this instance holds; ``_sync_cache`` reads the new one.
        self._cache_loaded = False
        self._provider_lookups = {}
        # The readings go with it.  They are as much that file's content as the
        # profiles are, and a copy kept across a replacement answered a window
        # the file no longer states.
        self._stated = {}
        self._image_generators = {}

    def _sync_cache(self) -> None:
        """Answer from the cache file as it stands, adopting another's write.

        Every read path needs both halves.  The adopt only invalidates the copy
        in hand, so the read beside it is the one that reloads; neither step
        can be dropped, and neither repeats the other's work.
        """

        self._adopt_external_cache_write()
        self._load_cache()

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        self._cache_file_identity = self._cache_file_state()
        self._records = RecordLookupIndex({key: dict(value) for key, value in _BUNDLED_RECORDS.items()})
        if self.cache_path is None:
            return
        payload = self._read_cache()
        fetched_at = payload.get("fetched_at")
        if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
            self._cache_fetched_at = float(fetched_at)
        self._load_provider_lookups(payload)
        self._load_stated_windows(payload)
        self._load_image_generators(payload)
        cached = payload.get("records")
        if not isinstance(cached, Mapping):
            return
        self._records.invalidate_tail_index()
        cache_has_pi_profiles = any(
            isinstance(record, Mapping) and record.get("source") == _PI_SOURCE
            for record in cached.values()
        )
        parsed = _source_records(cached, source="cache", priority=1)
        # Preserve the source and priority carried by a valid cache entry.
        for model_id, raw in cached.items():
            if not isinstance(model_id, str) or not isinstance(raw, Mapping):
                continue
            record = _record_from_payload(raw, source=str(raw.get("source", "cache")), priority=_positive_int(raw.get("priority")) or 1)
            if record is not None:
                parsed[model_id.casefold()] = record
        _merge_records(self._records, parsed)
        # The pre-Pi cache contains only hard-limit sources.  Do not make an
        # installed upgrade wait for its previous refresh interval
        # before adopting provider-specific agent profiles.
        if self._cache_fetched_at is not None and not cache_has_pi_profiles:
            self._cache_fetched_at = None

    def _runtime_values(self) -> tuple[int, int]:
        unknown = UNKNOWN_MODEL_CONTEXT_WINDOW
        refresh_hours = DEFAULT_MODEL_CONTEXT_REFRESH_HOURS
        if self.runtime_settings_path is None:
            return unknown, refresh_hours
        # Every catalog entry and pane snapshot asks for these two values, so
        # re-parse the settings file only when it actually changed; a live
        # edit still lands on the next lookup.
        try:
            details = self.runtime_settings_path.stat()
            key: tuple[int, int] | None = (details.st_mtime_ns, details.st_size)
        except OSError:
            key = None
        if key is not None and key == self._runtime_values_key and self._runtime_values_cache is not None:
            return self._runtime_values_cache
        resolved: tuple[int, int] | None = None
        try:
            from .runtime_settings_io import load_specs, read_settings_file

            values = read_settings_file(self.runtime_settings_path, load_specs())
            unknown = int(values.get("YOUNG_ROUTER_UNKNOWN_MODEL_CONTEXT_WINDOW", unknown))
            refresh_hours = int(values.get("YOUNG_ROUTER_MODEL_CONTEXT_REFRESH_HOURS", refresh_hours))
            resolved = (max(1, unknown), max(0, refresh_hours))
        except Exception:
            # A malformed settings file is reported by the Runtime Settings
            # domain; catalog generation still has its bundled policy.
            pass
        if resolved is None:
            return max(1, unknown), max(0, refresh_hours)
        self._runtime_values_key = key
        self._runtime_values_cache = resolved
        return resolved

    def _route_index(self) -> dict[str, list[dict[str, Any]]]:
        """Return the parsed ``model_list`` of the provider config, by name.

        The index is keyed by the configuration file's (inode, mtime, size)
        identity so an edited file is picked up immediately while repeated
        lookups inside one catalog build reuse a single parse.  That identity
        is sufficient because every writer this app owns replaces the document
        atomically: a change always arrives as a new inode, even when a coarse
        timestamp cannot tell two writes apart.

        The key is the name itself (``public_name_key``), not a folded form of
        it: the name is the group, and the import already stated the one
        spelling every route of it carries.  Keying on a folded name merges two
        models the Router serves separately, so each group reads the other's
        route list and can report a window none of its own routes stated.
        """

        if self.runtime_config_path is None:
            return {}
        text: str | None = None
        try:
            details = self.runtime_config_path.stat()
            key: tuple[int, int, int] | None = (
                details.st_ino,
                details.st_mtime_ns,
                details.st_size,
            )
        except OSError:
            key = None
        if key is not None and key == self._route_index_key:
            # The identity moved with the bytes: an inode, a nanosecond mtime
            # and a size that all match the copy this index was built from.  A
            # re-read here proved the same thing and cost the read it was meant
            # to save, once per lookup.
            return self._route_index_cache
        index: dict[str, list[dict[str, Any]]] = {}
        try:
            if text is None:
                text = self.runtime_config_path.read_text(encoding="utf-8")
            from ..config.schema import safe_load_yaml_text

            data = safe_load_yaml_text(text, self.runtime_config_path.name)
        except Exception:
            data = None
        if isinstance(data, Mapping) and isinstance(data.get("model_list"), list):
            for deployment in data["model_list"]:
                if not isinstance(deployment, Mapping):
                    continue
                if not isinstance(deployment.get("litellm_params"), Mapping):
                    continue
                name = public_name_key(deployment.get("model_name", ""))
                if name:
                    index.setdefault(name, []).append(dict(deployment))
        self._route_index_key = key
        self._route_index_cache = index
        return index

    def _load_route_deployments(self, public_name: str) -> list[dict[str, Any]]:
        """Return full deployment mappings for every route of a public model."""

        deployments = self._route_index().get(public_name_key(public_name))
        if not deployments:
            return []
        # Copy per call like the previous per-call parse did, so a caller can
        # never mutate the cached deployment mappings.
        return [dict(deployment) for deployment in deployments]

    def search_tool_capability_for(self, public_name: str) -> str | None:
        """Resolve how this public model can search the web."""

        deployments = self._load_route_deployments(public_name)
        if not deployments:
            return None
        native_search_possible = False
        function_bridge_possible = False
        for deployment in deployments:
            params = deployment.get("litellm_params")
            if not isinstance(params, Mapping):
                continue
            info = deployment.get("model_info")
            info = info if isinstance(info, Mapping) else {}
            explicit_native_false = any(
                info.get(key) is False
                for key in ("supports_responses_web_search", "supports_web_search")
            )
            provider = params.get("custom_llm_provider")
            identified_native_route = (
                isinstance(provider, str)
                and provider.strip().casefold() in {"openai", "chatgpt"}
            ) or _api_base_host(params.get("api_base")) == "api.openai.com"
            if (
                not explicit_native_false
                and (
                    identified_native_route
                    or gpt_family_model_name(public_name)
                )
            ):
                native_search_possible = True
            if info.get("supports_responses_function_tools") is not False:
                function_bridge_possible = True
        if native_search_possible:
            return SEARCH_TOOL_CAPABILITY_NATIVE
        if function_bridge_possible:
            return SEARCH_TOOL_CAPABILITY_BRIDGE
        return SEARCH_TOOL_CAPABILITY_UNSUPPORTED

    def route_model_ids(self, public_name: str) -> list[str]:
        """The deployment model ids one public route fans out to, in order.

        Empty means the name is not a route in this configuration, which is a
        different answer from "a route with no deployments": callers reading
        per-route metadata need both and must not substitute the public name
        for the first.
        """

        return self._load_route_model_ids(public_name)

    def _load_route_model_ids(self, public_name: str) -> list[str]:
        if self.runtime_config_path is None:
            return []
        result: list[str] = []
        for deployment in self._load_route_deployments(public_name):
            params = deployment.get("litellm_params")
            if not isinstance(params, Mapping):
                continue
            model_id = params.get("model")
            if isinstance(model_id, str) and model_id.strip():
                normalized_id = model_id.strip()
                provider = params.get("custom_llm_provider")
                if (
                    isinstance(provider, str)
                    and provider.strip()
                    and not normalized_id.casefold().startswith(
                        f"{provider.strip().casefold()}/"
                    )
                ):
                    normalized_id = f"{provider.strip()}/{normalized_id}"
                result.append(normalized_id)
        return result

    def refresh_if_due(self, *, force: bool = False, sync_cache: bool = True) -> bool:
        """Refresh the public context sources, without holding a reader's lock.

        The fetch is a network round trip to public metadata (up to eight
        seconds per source).  It used to run under Core's store lock, so the
        first window refresh after the cache aged out waited for the internet.
        The new records are merged into a private copy and swapped in with one
        assignment: a reader sees the old map or the new one, never a half-merged
        one, and never waits for the fetch.  ``sync_cache`` is for a caller that
        already holds Core's lock and therefore cannot adopt the cache file
        here; the background lane calls this outside the lock.
        """

        if sync_cache:
            self._sync_cache()
        if not self.refresh_enabled:
            return False
        _, refresh_hours = self._runtime_values()
        if refresh_hours <= 0:
            return False
        now = float(self._clock())
        if not force and self._cache_fetched_at is not None and now - self._cache_fetched_at < refresh_hours * 3600:
            return False
        if not force and self._last_refresh_attempt is not None and now - self._last_refresh_attempt < 300:
            return False
        with _CONTEXT_REFRESH_LOCK:
            # A second lane may have finished while this one waited.
            now = float(self._clock())
            if not force and self._cache_fetched_at is not None and now - self._cache_fetched_at < refresh_hours * 3600:
                return False
            self._last_refresh_attempt = now
            incoming_all: dict[str, dict[str, Any]] = {}
            for source in MODEL_CONTEXT_SOURCES:
                priority = _SOURCE_PRIORITIES[source]
                try:
                    payload = self._fetcher(source)
                    incoming = _source_records(payload, source=source, priority=priority)
                except Exception:
                    continue
                if incoming:
                    _merge_records(incoming_all, incoming)
            if not incoming_all:
                return False
            merged = RecordLookupIndex(
                {key: dict(value) for key, value in self._records.items()}
            )
            _merge_records(merged, incoming_all)
            self._records = merged
            self._cache_fetched_at = now
            self._write_cache()
            return True

    @staticmethod
    def _deployment_context(deployment: Mapping[str, Any]) -> int | None:
        """The window the *user* declared for one route, if they declared one."""

        model_info = deployment.get("model_info")
        if not isinstance(model_info, Mapping):
            return None
        return _positive_int(model_info.get("max_input_tokens"))

    def _wire_context(self, deployment: Mapping[str, Any]) -> int | None:
        """The window this route's own upstream stated for the wire it calls.

        The deployment names the wire as ``openai/<id>``; every catalog read
        records it as the bare ``<id>`` the upstream publishes, so the adapter
        prefix is the only part removed.

        Two sources answer the same question, and the live one is asked first:
        the caller's own in-memory readings (a pane that just refreshed), then
        the shared cache another writer filled (the managed catalog's side, or
        this process before it restarted).

        The id is matched by identity, not by spelling.  A catalog states
        ``minimax-m3.1-flash`` while a route of the same group may address
        ``MiniMax-M3.1-Flash``, and an exact lookup left that route with no
        reading at all -- which read as "this upstream states nothing" and made
        the group look as if only half of it had answered.
        """

        params = deployment.get("litellm_params")
        if not isinstance(params, Mapping):
            return None
        wire = str(params.get("model") or "").strip()
        if "/" in wire:
            wire = wire.split("/", 1)[1].strip()
        if not wire:
            return None
        key = wire.casefold()
        for source in (self._live_stated(), self._stated):
            if not isinstance(source, Mapping):
                continue
            window = _positive_int(source.get(wire))
            if window is not None:
                return window
            for name, value in source.items():
                if isinstance(name, str) and name.casefold() == key:
                    window = _positive_int(value)
                    if window is not None:
                        return window
        return None

    def _live_stated(self) -> Mapping[str, int] | None:
        """The caller's own catalog readings, when it has any to hand over."""

        if self._stated_windows is None:
            return None
        try:
            live = self._stated_windows()
        except Exception:
            return None
        return live if isinstance(live, Mapping) else None

    def _stated_contexts(self, public_name: str) -> list[int]:
        """The windows this group's routes' upstreams stated, where the inference is not trusted.

        A route's own catalog is the authority for the window that route will
        accept, so it answers in two cases, and the rule is the same in both:
        the registry has no profile at all, or the window the registry inferred
        is contradicted by the upstream that serves the route.

        No profile at all.  ``hy4free``/``hy4std`` are in no bundled profile, no
        pi.dev record and no LiteLLM entry, and the WorkBuddy catalog states
        960,000 for both.  The unknown-model placeholder was published instead,
        on every surface at once -- the pane's 公开上下文, the 推荐上下文 beside it,
        and the catalog Codex compacts against -- so a 960,000-token model was
        compacted at 272,000.

        A contradicted inference.  A bare public name that no provider-qualified
        record states exactly is resolved by suffix match over every provider's
        copy of the id, and the copies disagree.  ``minimax-m3.1-flash`` has
        seven copies at 1,000,000 or 1,048,576 and one -- nvidia's -- at
        262,144, and ``_lookup`` takes the smallest of the top-priority set, so
        the whole group was published at a quarter of what its upstream states.
        A lone outlier among agreeing copies is what a maintenance slip looks
        like, and the upstream serving this route is the evidence that settles
        it.  Where the copies agree, the inference is taken at its word: the
        disagreement rule must not turn a catalog's guess into the answer.

        What is deliberately *not* here is the case of 2026-11-04: the free
        lane publishes its generic fallback 131,072 for an id its table does not
        carry, against a real 1,048,576.  There the copies agree on 1,048,576 and
        the route's own upstream states something far smaller, so the registry
        keeps its answer and the reading stays a reading.
        """

        contexts: list[int] = []
        for deployment in self._load_route_deployments(public_name):
            stated = self._wire_context(deployment)
            if stated is None:
                continue
            params = deployment.get("litellm_params")
            model_id = str(params.get("model") or "").strip() if isinstance(params, Mapping) else ""
            if not model_id:
                continue
            resolved = self._resolve_context_record(model_id)
            if resolved is not None and not self._inference_is_contradicted(model_id, stated):
                continue
            contexts.append(stated)
        return contexts

    def _inference_is_contradicted(
        self,
        model_id: str,
        stated: int,
    ) -> bool:
        """Whether an upstream's number shows a suffix-match inference to be wrong.

        The question is never "which of two numbers wins"; it is "did the
        inference pick a copy the upstream does not agree with?".  So both sides
        are read, and the upstream's number has to match what most of the
        provider copies state:

        * ``minimax-m3.1-flash`` -- seven copies at 1,000,000 or 1,048,576, one
          (nvidia's) at 262,144, and the upstream states 1,000,000.  The
          inference took the outlier; the upstream agrees with the majority.  A
          contradicted inference.
        * ``pulse-alpha-free`` -- one copy at 1,048,576 and the lane states its
          generic fallback 131,072.  The upstream agrees with *nobody*, so what
          it states is a reading to report in its own row and not a window to
          publish.  This is the 2026-11-04 incident and the rule keeps it.

        A record that names the id exactly is a profile this app looked up on
        purpose, so it is never overridden either -- only a suffix match is.
        """

        if self._states_id_exactly(model_id):
            return False
        resolved = _lookup(self._records, model_id)
        context = int(resolved.get("context_window") or 0) if resolved else 0
        if context <= 0 or stated <= 0:
            return False
        larger, smaller = max(context, stated), min(context, stated)
        if larger / smaller < _OUTLIER_RATIO:
            # The two sit inside one family of numbers; the inference is not an
            # outlier, so there is nothing to contradict.
            return False
        # The upstream has to agree with some other copy of the same id, so this
        # asks whether a copy states its number rather than counting copies here
        # -- ``_lookup`` already owns the question of what the registry knows.
        keys = _suffix_match_keys(self._records, _candidate_ids(model_id))
        return any(
            _same_window(self._records[key]["context_window"], stated)
            for key in keys
            if key in self._records
            and isinstance(self._records.get(key), Mapping)
            and int(self._records[key]["context_window"]) != context
        )

    def _states_id_exactly(self, model_id: str) -> bool:
        """Whether a record names this id, rather than matching it by suffix."""

        return any(candidate in self._records for candidate in _candidate_ids(model_id))

    def _custom_contexts(self, public_name: str) -> list[int]:
        """Collect the windows the *user* declared on the routes of one group.

        The group takes the smallest of them, because a group is only as usable
        as its narrowest route.

        A window an upstream advertises for a model is deliberately not one of
        these.  It is a *reading*: the model detail reports it in its own row
        (``upstream_context_window``), it changes whenever that upstream's
        catalog is read again, and nothing here decides it.  Letting a reading
        stand in for the declaration made the two numbers of one route disagree:
        the pane proposed the advertised number while the managed catalog -- the
        only surface that actually hands a window to a client -- published the
        registry's, so ``pulse-alpha-free`` read 131,072 in the pane (its lane
        states its fallback for an id its table does not know) and 1,048,576 in
        the catalog written the same minute.
        """

        contexts: list[int] = []
        for deployment in self._load_route_deployments(public_name):
            declared = self._deployment_context(deployment)
            if declared is not None:
                contexts.append(declared)
        return contexts

    def _resolved_route_records(self, public_name: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        for model_id in self._load_route_model_ids(public_name):
            record = self._resolve_context_record(model_id)
            if record is not None:
                records.append(record)
        if not records:
            record = self._resolve_context_record(public_name)
            if record is not None:
                records.append(record)
        return records

    @staticmethod
    def _context_record_from_records(
        records: Sequence[Mapping[str, Any]],
        *,
        unknown: int,
    ) -> ContextRecord:
        if not records:
            return ContextRecord(
                context_window=unknown,
                max_context_window=unknown,
                effective_context_window_percent=DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
            )
        windows = [int(record["context_window"]) for record in records]
        # A zero is a statement, not a missing value: the catalog said this
        # generator holds no conversation, and the group it belongs to answers
        # that.  Substituting ``unknown`` here would hand the placeholder back
        # to the one surface that resolved the record most precisely.
        window = min(windows) if all(value > 0 for value in windows) else 0
        if window <= 0:
            return ContextRecord(
                context_window=0,
                max_context_window=0,
                effective_context_window_percent=DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
            )
        return ContextRecord(
            context_window=window,
            max_context_window=min(int(record["max_context_window"]) for record in records),
            effective_context_window_percent=min(int(record.get("effective_context_window_percent", DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT)) for record in records),
        )

    def default_record_for(self, public_name: str) -> ContextRecord:
        """Resolve registry metadata while ignoring the user's own limits.

        The model inspector shows this window as the value a public model
        falls back to when the user has not customized it; ``record_for`` is
        the effective policy the managed catalog writes.

        A name no profile resolves has no recommendation to offer, so the
        answer in that case is the one its routes' own catalogs stated
        (``_stated_contexts``) rather than the unknown-model placeholder.  A name
        whose inferred window an upstream contradicts takes the upstream's too:
        the inference is a suffix match over provider copies that disagreed, and
        the route's own catalog is the one piece of evidence about the route.
        Both surfaces read this one answer, which is what keeps the pane's field
        and the recommendation beside it from naming two different numbers for a
        model nothing knows.
        """

        self._sync_cache()
        unknown, _ = self._runtime_values()
        records = self._resolved_route_records(public_name)
        if not records and self._public_name_generates_images(public_name):
            # A generator no profile describes does not fall back to the
            # unknown-model placeholder; that placeholder is a guess about a
            # conversation, and this route has none.  The managed catalog and
            # the pane then state "no window" instead of 272,000 tokens of
            # memory for a model that never reads a prompt.
            return ContextRecord(
                context_window=0,
                max_context_window=0,
                effective_context_window_percent=DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
            )
        resolved = self._context_record_from_records(
            records,
            unknown=unknown,
        )
        stated = self._stated_contexts(public_name)
        if not stated:
            return resolved
        window = min(stated)
        return ContextRecord(
            context_window=window,
            max_context_window=window,
            effective_context_window_percent=DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
        )

    def _public_name_generates_images(self, public_name: str) -> bool:
        """Whether this route's model is one the app can state serves images.

        Asked of the route's wire names, and of the public name beside them: a
        group spelled one way may address an upstream under another.  The
        catalog's modality, LiteLLM's mode and a past probe all answer here, so
        the pane and the managed catalog agree with whatever the probe last
        measured on a name no bundled table carries yet.
        """

        names = [
            str(deployment.get("litellm_params", {}).get("model") or "")
            if isinstance(deployment.get("litellm_params"), Mapping)
            else ""
            for deployment in self._load_route_deployments(public_name)
        ]
        names = [name.strip() for name in names if name.strip()]
        names.append(public_name)
        try:
            return self.states_image_generation(names)
        except Exception:
            return False

    def record_for(self, public_name: str) -> ContextRecord:
        """Resolve one public model's context policy.

        An explicitly configured window wins over registry metadata: the user
        knows the relay in front of the route, so a public model whose routes
        declare ``model_info.max_input_tokens`` keeps that value even when a
        better-known upstream profile resolves a different window.  The group
        reports the smallest declared value, the same safe direction every
        inferred lookup already takes.
        """

        resolved = self.default_record_for(public_name)
        contexts = self._custom_contexts(public_name)
        if not contexts:
            return resolved
        context = min(contexts)
        return ContextRecord(
            context_window=context,
            max_context_window=context,
            effective_context_window_percent=DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT,
        )

    def _resolve_context_record(self, model_id: str) -> dict[str, Any] | None:
        record = _lookup(self._records, model_id)
        if record is None:
            for candidate in [model_id.casefold(), *_candidate_ids(model_id)]:
                alias = _BUNDLED_ALIASES.get(candidate)
                if alias:
                    record = _lookup(self._records, alias)
                    if record is not None:
                        break
        if record is None:
            record = self._resolve_provider_record(model_id)
        return record

    def image_metadata_for_model_id(
        self,
        model_id: str,
        *,
        refresh: bool = False,
    ) -> dict[str, Any] | None:
        """Return public image metadata, optionally refreshing public catalogs."""

        self._sync_cache()
        if refresh:
            self.refresh_if_due()
        candidates = _candidate_ids(model_id)
        matches: list[tuple[int, int, dict[str, Any]]] = []
        for index, (record_id, record) in enumerate(self._records.items()):
            if not isinstance(record, Mapping) or not isinstance(record.get("image_model_metadata"), Mapping):
                continue
            if any(
                record_id == candidate
                or record_id.endswith(f"/{candidate}")
                for candidate in candidates
            ):
                matches.append((_positive_int(record.get("priority")) or 0, -index, dict(record)))
        if not matches:
            return None
        metadata = max(matches, key=lambda item: (item[0], item[1]))[2].get("image_model_metadata")
        return dict(metadata) if isinstance(metadata, Mapping) else None

    def states_image_generation(self, model_ids: Sequence[str]) -> bool:
        """Whether this registry can say these ids are image generators.

        Three statements settle the question, in the order they are trusted:
        the public catalogs' modality, LiteLLM's cost-map ``mode``, and finally
        this app's own probe result (``remember_image_generator``), which is the
        only one that can name a model no table has caught up with yet.

        A name none of the three carries keeps the historical answer
        (``False``): an unreadable table is not evidence that a route generates
        images.
        """

        if any(self.image_metadata_for_model_id(model_id) for model_id in model_ids):
            return True
        if _litellm_states_image_generation(model_ids):
            return True
        return self.is_remembered_image_generator(model_ids)

    def is_remembered_image_generator(self, model_ids: Sequence[str]) -> bool:
        """Whether a probe already established that one of these ids is a generator.

        Kept beside the stated windows for the same reason: the probe is the
        only surface that ever asks the Images question, and the pane and the
        managed catalog are written by domains that never perform it.  A
        reading, not a declaration -- the id keys it, exactly as a stated
        window does.
        """

        self._sync_cache()
        for model_id in model_ids:
            for key in _image_generator_keys(model_id):
                if self._image_generators.get(key) is True:
                    return True
        return False

    def remember_image_generator(self, model_ids: Sequence[str], *, generator: bool = True) -> None:
        """Record that a probe found these ids served (or not) by the Images wire.

        Only an answered Images endpoint records here, so this is evidence and
        not a guess; ``generator=False`` records the negative a chat-answering
        route already proved, which is what keeps a model that later appears in
        no table from being re-asked.
        """

        recorded = {
            key: bool(generator)
            for model_id in model_ids
            for key in _image_generator_keys(model_id)
        }
        if not recorded:
            return
        self._sync_cache()
        if all(self._image_generators.get(name) is value for name, value in recorded.items()):
            return
        self._image_generators.update(recorded)
        self._write_cache()

    def image_metadata_for_model_ids(
        self,
        model_ids: Sequence[str],
        *,
        refresh: bool = False,
    ) -> dict[str, dict[str, Any]]:
        """Resolve image metadata for concrete route model ids."""

        self._sync_cache()
        if refresh:
            self.refresh_if_due()
        resolved: dict[str, dict[str, Any]] = {}
        for model_id in model_ids:
            metadata = self.image_metadata_for_model_id(model_id)
            if metadata:
                resolved[str(model_id).strip()] = metadata
        return resolved

    def reasoning_for_model_id(self, model_id: str) -> ReasoningCapability | None:
        """Return Pi/Codex reasoning policy for one concrete deployment model."""

        self._sync_cache()
        normalized_id = model_id.strip().casefold()
        if "@" in normalized_id:
            normalized_id = normalized_id.split("@", 1)[0]
        record = self._records.get(normalized_id)
        if record is None and "/" not in normalized_id:
            record = _lookup(self._records, normalized_id)
        if record is None and "/" in normalized_id:
            # A deployment states its model with the upstream's own prefix
            # (``openai/glm-5.3-flash``), and a provider-qualified id is
            # deliberately never fuzzy-matched here: inheriting whichever
            # catalogue entry ends in the same name is how a relay route was
            # handed another provider's ladder.  A policy this app declares for
            # the model itself is not that kind of guess, so the declared name
            # answers for its prefixed spellings too.  Without this the listing
            # published a ladder (through the bare-name fallback in
            # ``proxy.reasoning._model_listing_efforts``) that the request path
            # could never apply, because every deployment id in the group was
            # prefixed.
            record = _BUNDLED_RECORDS.get(normalized_id.rsplit("/", 1)[-1])
        if record is None:
            alias = _BUNDLED_ALIASES.get(normalized_id)
            if alias:
                record = _lookup(self._records, alias)
        return _reasoning_capability(record) if record is not None else None

    def reasoning_for(self, public_name: str) -> ReasoningCapability | None:
        """Return the common safe reasoning surface for a public route group."""

        route_ids = self._load_route_model_ids(public_name)
        model_ids = route_ids or [public_name]
        capabilities = [self.reasoning_for_model_id(model_id) for model_id in model_ids]
        if not capabilities or any(capability is None for capability in capabilities):
            return None
        resolved = [capability for capability in capabilities if capability is not None]
        common = set(resolved[0].supported_levels)
        for capability in resolved[1:]:
            common.intersection_update(capability.supported_levels)
        supported = tuple(level for level in _CODEX_REASONING_LEVELS if level in common)
        if not supported:
            return None

        reasoning_values = {capability.reasoning for capability in resolved}
        reasoning = reasoning_values.pop() if len(reasoning_values) == 1 else None
        maps = [capability.thinking_level_map for capability in resolved]
        common_map = maps[0] if all(mapping == maps[0] for mapping in maps[1:]) else None
        preferred_defaults = [
            capability.default_level
            for capability in resolved
            if capability.default_level in supported
        ]
        preferred = preferred_defaults[0] if preferred_defaults else None
        return ReasoningCapability(
            reasoning=reasoning,
            supported_levels=supported,
            default_level=_default_reasoning_level(supported, preferred),
            thinking_level_map=dict(common_map) if common_map is not None else None,
        )


def default_context_cache_path(runtime_root: Path | str) -> Path:
    """Return the private cache path under the app's own runtime root."""

    return Path(runtime_root).expanduser() / MODEL_CONTEXT_CACHE_FILE_NAME


def legacy_context_cache_paths(codex_home: Path | str) -> tuple[Path, ...]:
    """Return retired in-Codex-home cache copies, newest spelling first."""

    home = Path(codex_home).expanduser()
    return tuple(home / name for name in PREVIOUS_MODEL_CONTEXT_CACHE_FILE_NAMES)


__all__ = [
    "ContextRecord",
    "DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT",
    "DEFAULT_MODEL_CONTEXT_REFRESH_HOURS",
    "MODEL_CONTEXT_CACHE_FILE_NAME",
    "MODEL_CONTEXT_SOURCES",
    "PREVIOUS_MODEL_CONTEXT_CACHE_FILE_NAMES",
    "ModelContextRegistry",
    "ReasoningCapability",
    "SEARCH_TOOL_CAPABILITY_BRIDGE",
    "SEARCH_TOOL_CAPABILITY_NATIVE",
    "SEARCH_TOOL_CAPABILITY_UNSUPPORTED",
    "UNKNOWN_MODEL_CONTEXT_WINDOW",
    "default_context_cache_path",
    "gpt_family_model_name",
    "legacy_context_cache_paths",
    "published_reasoning_levels",
]
