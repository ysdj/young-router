"""Context-window metadata for the managed Codex model catalog.

The catalog is a Codex-facing policy surface, not a copy of LiteLLM's public
pricing table.  A route can expose a public alias while pointing at a
provider-qualified model, so metadata is resolved from the configured route
first.  A small bundled registry keeps the feature useful offline; the
registry can refresh from public upstream metadata and persist the latest
records privately for the next run.
"""

from __future__ import annotations

from dataclasses import dataclass
import time
from pathlib import Path
from collections.abc import Callable, Mapping, Sequence
from typing import Any
import urllib.request

from .persistence import PersistenceError, atomic_write_json, read_json


UNKNOWN_MODEL_CONTEXT_WINDOW = 272_000
DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT = 95
DEFAULT_MODEL_CONTEXT_REFRESH_HOURS = 6
MODEL_CONTEXT_CACHE_FILE_NAME = "litellm-menu-model-contexts.json"
MODEL_CONTEXT_SOURCES = (
    "https://pi.dev/api/models",
    "https://raw.githubusercontent.com/openai/codex/main/codex-rs/models-manager/models.json",
    "https://models.dev/models.json",
)
_PI_SOURCE = MODEL_CONTEXT_SOURCES[0]
_CODEX_SOURCE = MODEL_CONTEXT_SOURCES[1]
_SOURCE_PRIORITIES = {
    _PI_SOURCE: 40,
    _CODEX_SOURCE: 30,
    MODEL_CONTEXT_SOURCES[2]: 20,
}
_UPSTREAM_MAX_BYTES = 8 * 1024 * 1024
_PI_REASONING_LEVELS = ("off", "minimal", "low", "medium", "high", "xhigh", "max")
_CODEX_REASONING_LEVELS = ("none", "minimal", "low", "medium", "high", "xhigh", "max")


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
# 258k, matching the Codex UI.  Other current LiteLLM Menu routes use the
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
    "kimi-k3": _record(262_144, 1_048_576, priority=25),
    "k3-256k": _record(262_144, priority=25),
    "gpt-image-2": _record(65_536),
}
_BUNDLED_ALIASES = {
    "gemini-3.1-pro": "gemini-3.1-pro-preview",
}


def _positive_int(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, float) and value.is_integer() and value > 0:
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        parsed = int(value.strip())
        return parsed if parsed > 0 else None
    return None


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
    if context is None:
        return None
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
        "priority": priority,
    }
    if auto_compact is not None:
        result["auto_compact_token_limit"] = auto_compact
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
        if isinstance(payload.get("models"), Sequence) and not isinstance(payload.get("models"), (str, bytes, bytearray)):
            items = payload["models"]
            for item in items:
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
        if priority >= (current_priority or 0):
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


def _lookup(records: Mapping[str, Mapping[str, Any]], model_id: str) -> dict[str, Any] | None:
    direct_matches: list[tuple[int, int, dict[str, Any]]] = []
    for index, candidate in enumerate(_candidate_ids(model_id)):
        direct = records.get(candidate)
        if isinstance(direct, Mapping):
            direct_matches.append((_positive_int(direct.get("priority")) or 0, -index, dict(direct)))
    direct_match = max(direct_matches, key=lambda item: (item[0], item[1])) if direct_matches else None
    suffix_matches: list[tuple[int, dict[str, Any]]] = []
    candidates = set(_candidate_ids(model_id))
    for key, value in records.items():
        if not isinstance(value, Mapping):
            continue
        tail = key.rsplit("/", 1)[-1]
        if tail in candidates or any(key.endswith(f"/{candidate}") for candidate in candidates):
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
            context = _positive_int(value.get("max_input_tokens")) or _positive_int(value.get("max_tokens"))
            if context is not None:
                return _record(context, source="litellm", priority=15)
    return None


class ModelContextRegistry:
    """Resolve model context policy and periodically refresh its metadata."""

    def __init__(
        self,
        *,
        runtime_config_path: Path | str | None = None,
        runtime_settings_path: Path | str | None = None,
        cache_path: Path | str | None = None,
        refresh_enabled: bool | None = None,
        fetcher: Callable[[str], object] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self.runtime_config_path = Path(runtime_config_path).expanduser() if runtime_config_path else None
        self.runtime_settings_path = Path(runtime_settings_path).expanduser() if runtime_settings_path else None
        self.cache_path = Path(cache_path).expanduser() if cache_path else None
        self.refresh_enabled = bool(runtime_settings_path) if refresh_enabled is None else bool(refresh_enabled)
        self._fetcher = fetcher or self._fetch_json
        self._clock = clock or time.time
        self._records: dict[str, dict[str, Any]] = {}
        self._cache_loaded = False
        self._cache_fetched_at: float | None = None
        self._last_refresh_attempt: float | None = None

    @staticmethod
    def _fetch_json(url: str) -> object:
        request = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "LiteLLM-Menu model-context updater"},
        )
        with urllib.request.urlopen(request, timeout=8) as response:
            payload = response.read(_UPSTREAM_MAX_BYTES + 1)
        if len(payload) > _UPSTREAM_MAX_BYTES:
            raise ValueError("upstream model metadata is too large")
        import json

        return json.loads(payload.decode("utf-8"))

    def _load_cache(self) -> None:
        if self._cache_loaded:
            return
        self._cache_loaded = True
        self._records = {key: dict(value) for key, value in _BUNDLED_RECORDS.items()}
        if self.cache_path is None:
            return
        try:
            payload = read_json(self.cache_path, default={})
        except PersistenceError:
            return
        fetched_at = payload.get("fetched_at")
        if isinstance(fetched_at, (int, float)) and not isinstance(fetched_at, bool):
            self._cache_fetched_at = float(fetched_at)
        cached = payload.get("records")
        if not isinstance(cached, Mapping):
            return
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
        try:
            from runtime_settings_io import load_specs, read_settings_file

            values = read_settings_file(self.runtime_settings_path, load_specs())
            unknown = int(values.get("LITELLM_MENU_UNKNOWN_MODEL_CONTEXT_WINDOW", unknown))
            refresh_hours = int(values.get("LITELLM_MENU_MODEL_CONTEXT_REFRESH_HOURS", refresh_hours))
        except Exception:
            # A malformed settings file is reported by the Runtime Settings
            # domain; catalog generation still has its bundled policy.
            pass
        return max(1, unknown), max(0, refresh_hours)

    def _load_route_model_ids(self, public_name: str) -> list[str]:
        if self.runtime_config_path is None:
            return []
        try:
            text = self.runtime_config_path.read_text(encoding="utf-8")
            from config_editor_core.schema import safe_load_yaml_text

            data = safe_load_yaml_text(text, self.runtime_config_path.name)
        except Exception:
            return []
        if not isinstance(data, Mapping) or not isinstance(data.get("model_list"), list):
            return []
        result: list[str] = []
        for deployment in data["model_list"]:
            if not isinstance(deployment, Mapping) or str(deployment.get("model_name", "")).strip() != public_name:
                continue
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

    def refresh_if_due(self, *, force: bool = False) -> bool:
        self._load_cache()
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
        self._last_refresh_attempt = now
        refreshed = False
        for source in MODEL_CONTEXT_SOURCES:
            priority = _SOURCE_PRIORITIES[source]
            try:
                payload = self._fetcher(source)
                incoming = _source_records(payload, source=source, priority=priority)
            except Exception:
                continue
            if incoming:
                _merge_records(self._records, incoming)
                refreshed = True
        if not refreshed:
            return False
        self._cache_fetched_at = now
        if self.cache_path is not None:
            try:
                atomic_write_json(
                    self.cache_path,
                    {"fetched_at": now, "records": self._records},
                )
            except PersistenceError:
                pass
        return True

    def record_for(self, public_name: str) -> ContextRecord:
        self._load_cache()
        route_ids = self._load_route_model_ids(public_name)
        unknown, _ = self._runtime_values()
        records: list[dict[str, Any]] = []
        for model_id in route_ids:
            record = self._resolve_context_record(model_id)
            if record is not None:
                records.append(record)
        if not records:
            record = self._resolve_context_record(public_name)
            if record is not None:
                records.append(record)
        if records:
            return ContextRecord(
                context_window=min(int(record["context_window"]) for record in records),
                max_context_window=min(int(record["max_context_window"]) for record in records),
                effective_context_window_percent=min(int(record.get("effective_context_window_percent", DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT)) for record in records),
            )
        return ContextRecord(
            context_window=unknown,
            max_context_window=unknown,
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
            record = _litellm_record([model_id])
        return record

    def reasoning_for_model_id(self, model_id: str) -> ReasoningCapability | None:
        """Return Pi/Codex reasoning policy for one concrete deployment model."""

        self._load_cache()
        normalized_id = model_id.strip().casefold()
        if "@" in normalized_id:
            normalized_id = normalized_id.split("@", 1)[0]
        record = self._records.get(normalized_id)
        if record is None and "/" not in normalized_id:
            record = _lookup(self._records, normalized_id)
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


def default_context_cache_path(codex_home: Path | str) -> Path:
    return Path(codex_home).expanduser() / MODEL_CONTEXT_CACHE_FILE_NAME


__all__ = [
    "ContextRecord",
    "DEFAULT_EFFECTIVE_CONTEXT_WINDOW_PERCENT",
    "DEFAULT_MODEL_CONTEXT_REFRESH_HOURS",
    "MODEL_CONTEXT_CACHE_FILE_NAME",
    "MODEL_CONTEXT_SOURCES",
    "ModelContextRegistry",
    "ReasoningCapability",
    "UNKNOWN_MODEL_CONTEXT_WINDOW",
    "default_context_cache_path",
]
