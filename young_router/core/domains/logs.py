"""Bounded, redacted log views owned by the Python Core."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
import re
import stat
import threading
import time
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

from ..log_tabs import LOG_TABS
from ..persistence import PersistenceError, atomic_write_json, read_json
from ..security import REDACT_TEXT
from ...log_rotation import (
    LOG_BACKUP_SEGMENTS_ENV,
    LOG_MAX_BYTES_ENV,
    MIN_LOG_MAX_BYTES,
    append_bounded_log,
)

try:
    import fcntl
except ImportError:  # pragma: no cover - Windows does not provide POSIX flock.
    fcntl = None


DOMAIN_NAME = "logs"
MAX_READ_BYTES = 16 * 1024 * 1024
# The local IPC transport caps one JSON message at 4 MiB. Leave headroom for
# the response envelope while still returning the newest useful log rows.
MAX_VIEW_BYTES = 3 * 1024 * 1024
DEFAULT_LINES = 10_000
MAX_LINES = 100_000
MAX_FILTER_CHARS = 256
RECOVERY_HEARTBEAT_TTL_SECONDS = 45.0
MENU_ACTIONS = frozenset(
    {
        "open-providers-models",
        "open-runtime-settings",
        "open-codex-settings",
        "open-claude-settings",
        "open-relay-accounts",
        "open-data-management",
        "open-logs",
        "toggle-autostart",
        "service-start",
        "service-stop",
        "service-restart",
        "service-reload",
        "service-health",
        "set-language-system",
        "set-language-en",
        "set-language-zh-Hans",
    }
)
ANSI_CONTROL_SEQUENCE = re.compile(r"\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))")
SERVICE_TIMESTAMP_PREFIX = re.compile(r"^(?:\[[^]]+\]\s*)+")
# A service row is one detail line; anything longer would be cut in the table
# anyway.  The repeat suffix has to fit inside the same bound.
MAX_TEXT_LOG_CHARS = 512
# One burst of identical console lines is one event: the managed proxy runs
# one LiteLLM worker per configured worker and reports its lifecycle per
# worker, and a client probes one route many times a minute.  The window keeps
# a repeat hours later its own row instead of folding it into an old burst.
SERVICE_REPEAT_WINDOW_SECONDS = 60.0
SERVICE_BANNER_HEADLINE = "litellm: proxy initialized with config"
# Console art, a client address, a worker PID, and LiteLLM's own clock are the
# only parts of a repeated line that differ between copies.
SERVICE_ART = re.compile(r"[█▀▄▌▐░▒▓╔╗╚╝║═╠╣╦╩╬┌┐└┘├┤┬┴┼─]")
SERVICE_BARE_MODEL = re.compile(r"[A-Za-z0-9_.\-]{1,128}")
SERVICE_EVENT_VOLATILE = re.compile(
    r"\d{2}:\d{2}:\d{2}\s+-\s+|\[\d{1,7}\]|\b\d{1,3}(?:\.\d{1,3}){3}:\d{1,5}\b"
)
# Every console line starts with this app's own ``[timestamp]`` marker, so a
# second marker inside a line is the head of the next record: the workers share
# one log and a partial write lets two of them interleave.
SERVICE_EMBEDDED_TIMESTAMP = re.compile(r"\[\d{4}-\d{2}-\d{2}T[^\]]+\]")
LEADING_TIMESTAMP = re.compile(
    r"^\[(?P<bracket>[^\]]+)\]\s*|^(?:Updated\s+)?(?P<iso>\d{4}-\d{2}-\d{2}[T ][^\s]+)\s*"
)
TRACEBACK_START = re.compile(r"Traceback \(most recent call last\):", re.IGNORECASE)
TRACEBACK_TERMINAL = re.compile(
    r"^(?:[A-Za-z_][\w.]*(?:Error|Exception|Interrupt|Exit)|Error|Exception):\s*",
    re.IGNORECASE,
)


class LogsDomainError(ValueError):
    """A source-safe log operation error."""


def _runtime_root(value: Path | str | None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "YOUNG_ROUTER_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".young-router"


def _safe_scalar(value: object, limit: int = 160) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return REDACT_TEXT(value)[:limit]
    return None


def _public_model_from_route_key(value: object) -> str:
    """Extract the full ``model=`` value up to the next route-key part.

    Route keys join parts with `` / ``.  A model value such as
    ``openai/gpt-5.6-sol`` legitimately contains slashes, so capture up to
    the next `` / name=`` separator instead of stopping at the first ``/``
    (which truncated prefixed models to a bare ``openai``).
    """

    if not isinstance(value, str):
        return ""
    match = re.search(
        r"(?:^|/)\s*model\s*=\s*(.*?)\s*(?=/\s*[A-Za-z_]+\s*=|$)", value
    )
    return match.group(1).strip() if match else ""


def _matches_upstream_model(public_model: object, upstream_model: object) -> bool:
    if not isinstance(public_model, str) or not isinstance(upstream_model, str):
        return False
    public_name = public_model.strip()
    upstream_name = upstream_model.strip()
    if not public_name or not upstream_name:
        return False
    _, separator, unprefixed_name = upstream_name.partition("/")
    return public_name == upstream_name or (
        bool(separator) and public_name == unprefixed_name
    )


def _configured_public_models_from(
    configured: Mapping[str, Mapping[str, str]],
) -> dict[str, str]:
    return {
        deployment_id: deployment["public_model"]
        for deployment_id, deployment in configured.items()
        if deployment["public_model"]
    }


def _normalized_order(value: object) -> str:
    text = _string_record_value(value)
    if not text:
        return ""
    try:
        return repr(float(text))
    except ValueError:
        return ""


def _request_route_identity(record: Mapping[str, Any]) -> tuple[str, str, str, str]:
    """Route identity every request row carries, independent of its id."""

    route_key = record.get("route_key")
    provider = _string_record_value(record.get("provider")) or _route_key_value(
        route_key, "provider"
    )
    upstream_model = _string_record_value(
        record.get("upstream_model")
    ) or _route_key_value(route_key, "upstream")
    api_key_name = _string_record_value(record.get("api_key_name")) or _route_key_value(
        route_key, "key"
    )
    return (
        provider.casefold(),
        _upstream_model_name(upstream_model),
        api_key_name,
        _normalized_order(_route_key_value(route_key, "order")),
    )


def _configured_route_public_models_from(
    configured: Mapping[str, Mapping[str, str]],
) -> tuple[dict[tuple[str, str, str, str], str], dict[tuple[str, str, str], str]]:
    """Index configured public names by the route a request actually used.

    LiteLLM replaces a deployment id with a per-call hash for client-side
    credential and dynamic routes, so the id alone cannot always resolve the
    configured public model.  The route identity -- provider, upstream model,
    key name, order -- survives that rewrite.  The second index drops the order
    for rows whose recorded order differs from the staged default, and routes
    that map to more than one configured public name stay unresolved instead of
    guessing.
    """

    by_route: dict[tuple[str, str, str, str], str] = {}
    by_route_without_order: dict[tuple[str, str, str], str] = {}
    ambiguous_routes: set[tuple[str, str, str, str]] = set()
    ambiguous_route_prefixes: set[tuple[str, str, str]] = set()
    for deployment in configured.values():
        public_model = deployment.get("public_model", "")
        provider = deployment.get("provider", "")
        upstream_model = deployment.get("upstream_model", "")
        if not (public_model and provider and upstream_model):
            continue
        route_prefix = (provider.casefold(), upstream_model, deployment.get("api_key_name", ""))
        route = (*route_prefix, _normalized_order(deployment.get("order")))
        known = by_route.get(route)
        if known is not None and known != public_model:
            ambiguous_routes.add(route)
        else:
            by_route[route] = public_model
        known_prefix = by_route_without_order.get(route_prefix)
        if known_prefix is not None and known_prefix != public_model:
            ambiguous_route_prefixes.add(route_prefix)
        else:
            by_route_without_order[route_prefix] = public_model
    for route in ambiguous_routes:
        by_route.pop(route, None)
    for route_prefix in ambiguous_route_prefixes:
        by_route_without_order.pop(route_prefix, None)
    return by_route, by_route_without_order


def _configured_route_public_model(
    configured_route_models: object,
    record: Mapping[str, Any],
) -> str:
    """Resolve a public model from the route identity of one request row."""

    if not (
        isinstance(configured_route_models, tuple)
        and len(configured_route_models) == 2
    ):
        return ""
    by_route, by_route_without_order = configured_route_models
    if not isinstance(by_route, Mapping) or not isinstance(by_route_without_order, Mapping):
        return ""
    provider, upstream_model, api_key_name, order = _request_route_identity(record)
    return (
        by_route.get((provider, upstream_model, api_key_name, order))
        or by_route_without_order.get((provider, upstream_model, api_key_name))
        or ""
    )


def _string_record_value(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _mapping_value(value: object) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _route_key_value(value: object, name: str) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(rf"(?:^|/)\s*{re.escape(name)}\s*=\s*(.*?)\s*(?=/\s*[A-Za-z_]+\s*=|$)", value)
    return match.group(1).strip() if match else ""


def _upstream_model_name(value: object) -> str:
    model = _string_record_value(value)
    return model.partition("/")[2] or model


_CONFIGURATION_CACHE_LOCK = threading.Lock()
_CONFIGURATION_CACHE: dict[str, tuple[tuple[object, ...], dict[str, dict[str, str]]]] = {}


def _configuration_signature(config_path: Path) -> tuple[object, ...]:
    """Cheap identity for one configuration file revision."""

    try:
        details = config_path.stat()
    except OSError:
        return (str(config_path), None, None, None, None)
    return (
        str(config_path),
        details.st_dev,
        details.st_ino,
        details.st_mtime_ns,
        details.st_size,
    )


def _configured_deployments(config_path: Path) -> dict[str, dict[str, str]]:
    """Configured deployment metadata for the request and route projections.

    Every log refresh needs this index, and re-parsing the staged YAML
    document on each append cost more than projecting the rows themselves, so
    one revision is parsed once and reused until the file changes.  The
    returned mapping is shared: callers only ever read it.
    """

    signature = _configuration_signature(config_path)
    key = str(config_path)
    with _CONFIGURATION_CACHE_LOCK:
        cached = _CONFIGURATION_CACHE.get(key)
        if cached is not None and cached[0] == signature:
            return cached[1]
    configured = _load_configured_deployments(config_path)
    with _CONFIGURATION_CACHE_LOCK:
        _CONFIGURATION_CACHE[key] = (signature, configured)
    return configured


def _load_configured_deployments(config_path: Path) -> dict[str, dict[str, str]]:
    from ...config import load as config_load

    try:
        document = config_load.config_document_from_path(config_path)
        payload = config_load.load_config_document(document)
    except (OSError, TypeError, ValueError):
        return {}

    configured: dict[str, dict[str, str]] = {}
    providers = payload.get("providers")
    if not isinstance(providers, list):
        return configured
    for provider in providers:
        if not isinstance(provider, Mapping):
            continue
        provider_name = _string_record_value(provider.get("name"))
        models = provider.get("models")
        if not isinstance(models, list):
            continue
        for model in models:
            if not isinstance(model, Mapping):
                continue
            deployment_id = _string_record_value(model.get("deployment_id"))
            if not deployment_id:
                continue
            configured[deployment_id] = {
                "public_model": _string_record_value(model.get("model_name")),
                "upstream_model": _upstream_model_name(model.get("litellm_model")),
                "provider": _string_record_value(model.get("provider")) or provider_name,
                "api_key_name": _string_record_value(model.get("api_key_name")),
                "order": _string_record_value(model.get("order"))
                or _string_record_value(model.get("effective_order")),
            }
    return configured


def _trace_value(value: object, limit: int = 160) -> str:
    if value is None or isinstance(value, bool):
        return ""
    if isinstance(value, (int, float)):
        return str(value)
    if isinstance(value, str):
        return REDACT_TEXT(value)[:limit]
    if isinstance(value, Mapping):
        return ", ".join(
            f"{key}={_trace_value(item, limit=80)}"
            for key, item in value.items()
            if _trace_value(item, limit=80)
        )[:limit]
    if isinstance(value, list):
        return ", ".join(
            item for item in (_trace_value(item, limit=80) for item in value) if item
        )[:limit]
    return ""


def _trace_count(value: object) -> int | None:
    return len(value) if isinstance(value, list) else None


def _trace_bool(value: object) -> str:
    return str(value).lower() if isinstance(value, bool) else ""


def _trace_detail_parts(*parts: tuple[str, object, str]) -> str:
    details: list[str] = []
    for name, value, suffix in parts:
        text = _trace_value(value)
        if text:
            details.append(f"{name}={text}{suffix}")
    return " · ".join(details)


def _trace_reason(raw: Mapping[str, Any], exception: Mapping[str, Any]) -> str:
    return _string_record_value(
        exception.get("reason")
        or raw.get("invalid_reason")
        or raw.get("preemptive_reason")
        or raw.get("reason")
    )


def _trace_detail(raw: Mapping[str, Any]) -> str:
    event = _string_record_value(raw.get("event"))
    request = _mapping_value(raw.get("request"))
    interface = _mapping_value(request.get("interface"))
    deployment = _mapping_value(raw.get("deployment"))
    exception = _mapping_value(raw.get("exception") or raw.get("error"))
    reason = _trace_reason(raw, exception)
    excluded = _trace_count(raw.get("excluded_deployment_ids"))

    if event == "filter_deployments":
        return _trace_detail_parts(
            ("candidates", _trace_count(raw.get("after_constraints")), ""),
            ("selected", _trace_count(raw.get("selected_candidates")), ""),
            ("excluded", excluded, ""),
        )

    if event == "deployment_failover_marked":
        return _trace_detail_parts(
            (
                "failed_order",
                exception.get("failed_deployment_order") or raw.get("deployment_order"),
                "",
            ),
            ("reason", reason, ""),
        )

    if event == "selected_deployment":
        selected_order = deployment.get("order")
        if selected_order is None:
            selected_order = raw.get("target_order")
        return _trace_detail_parts(
            ("order", selected_order, ""),
            (
                "protocol",
                interface.get("effective_upstream_surface")
                or interface.get("client_surface")
                or interface.get("requested_endpoint"),
                "",
            ),
            ("stream", _trace_bool(interface.get("stream")), ""),
        )

    if event in {
        "same_deployment_protocol_fallback_available",
        "protocol_fallback_cache_hit",
        "protocol_fallback_success",
        "protocol_fallback_cache_cleared",
    }:
        return _trace_detail_parts(
            ("from_protocol", raw.get("failed_surface") or raw.get("from_surface"), ""),
            ("fallback_protocol", raw.get("fallback_surface"), ""),
            ("ttl", raw.get("ttl_seconds"), "s"),
            ("remaining", raw.get("remaining_seconds"), "s"),
        )

    if event == "generic_fallback_helper_start":
        return _trace_detail_parts(
            (
                "protocol",
                interface.get("effective_upstream_surface")
                or interface.get("client_surface")
                or interface.get("requested_endpoint"),
                "",
            ),
            ("stream", _trace_bool(interface.get("stream")), ""),
            ("order", raw.get("target_order"), ""),
            ("excluded", excluded, ""),
        )

    if event == "browser_compatible_headers_retry_start":
        return _trace_detail_parts(
            ("order", raw.get("target_order"), ""),
            ("excluded", excluded, ""),
        )

    if event == "generic_fallback_helper_error":
        return _trace_detail_parts(
            ("retry", raw.get("retry_attempt"), ""),
            ("order", raw.get("target_order"), ""),
            ("excluded", excluded, ""),
            ("reason", reason, ""),
        )

    if event in {
        "dsh_vision_router_fallback_start",
        "dsh_vision_router_fallback_retry_start",
        "dsh_vision_router_fallback_error",
    }:
        return _trace_detail_parts(
            ("order", raw.get("target_order"), ""),
            ("excluded", excluded, ""),
            ("reason", reason, ""),
        )

    if event == "fallback_deployment_cooldown_filter":
        return _trace_detail_parts(
            ("cooling", _trace_count(raw.get("cooldown_deployments")), ""),
            ("all_cooled", _trace_bool(raw.get("cooldown_all_candidates")), ""),
        )

    if event == "next_order_fallback_available":
        return _trace_detail_parts(
            ("failed_order", raw.get("failed_order"), ""),
            ("next_order", raw.get("target_order"), ""),
            ("candidates", _trace_count(raw.get("candidates")), ""),
            ("excluded", excluded, ""),
        )

    if event == "final_order_fallback_retry_start":
        return _trace_detail_parts(
            ("next_order", raw.get("target_order"), ""),
            ("excluded", excluded, ""),
            ("reason", reason, ""),
        )

    if event.startswith("external_web_search_bridge_chat_tool_"):
        return _trace_detail_parts(
            ("phase", raw.get("phase"), ""),
            ("reason", reason, ""),
        )

    if event == "external_web_search_bridge_actions_executed":
        return _trace_detail_parts(
            ("round", raw.get("round"), ""),
            ("actions", _trace_count(raw.get("actions")), ""),
            ("sources", raw.get("source_url_count"), ""),
            ("evidence", raw.get("evidence_chars"), ""),
        )

    if event == "external_web_search_bridge_continuation_start":
        return _trace_detail_parts(
            ("round", raw.get("round"), ""),
            ("queries", _trace_count(raw.get("queries")), ""),
            ("evidence", raw.get("evidence_chars"), ""),
            ("continuation_evidence", raw.get("continuation_evidence_chars"), ""),
            ("input", raw.get("continuation_input_chars"), ""),
            ("output_limit", raw.get("continuation_max_output_tokens"), ""),
        )

    if event == "external_web_search_bridge_continuation_done":
        return _trace_detail_parts(
            ("round", raw.get("round"), ""),
            ("queries", _trace_count(raw.get("queries")), ""),
            ("next_actions", _trace_count(raw.get("next_actions")), ""),
            ("next_queries", _trace_count(raw.get("next_queries")), ""),
        )

    if event == "external_web_search_bridge_continuation_error":
        return _trace_detail_parts(
            ("round", raw.get("round"), ""),
            ("queries", _trace_count(raw.get("queries")), ""),
            ("reason", reason, ""),
        )

    if event == "external_web_search_bridge_empty_continuation_synthesis":
        return _trace_detail_parts(
            ("round", raw.get("round"), ""),
            ("queries", _trace_count(raw.get("queries")), ""),
        )

    if event in {
        "external_web_search_bridge_synthesis_start",
        "external_web_search_bridge_synthesis_done",
        "external_web_search_bridge_synthesis_error",
        "external_web_search_bridge_final_invalid",
    }:
        return _trace_detail_parts(
            ("queries", _trace_count(raw.get("queries")), ""),
            ("sources", raw.get("source_url_count"), ""),
            ("reason", reason, ""),
        )

    if event in {
        "external_web_search_bridge_synthesis_chat_start",
        "external_web_search_bridge_synthesis_chat_done",
    }:
        return _trace_detail_parts(("phase", "synthesis", ""))

    if event.startswith("external_web_search_bridge_"):
        return _trace_detail_parts(
            ("phase", raw.get("phase"), ""),
            ("round", raw.get("round"), ""),
            ("queries", _trace_count(raw.get("queries")), ""),
            ("sources", raw.get("source_url_count"), ""),
            ("retry", raw.get("retry_attempt"), ""),
            ("max_retries", raw.get("max_retries"), ""),
            ("retry_delay", raw.get("retry_delay_seconds"), "s"),
            ("reason", reason, ""),
        )

    if event.startswith("responses_chat_bridge_preemptive") or event == "responses_chat_bridge_preemptive":
        return _trace_detail_parts(("reason", reason, ""))

    if event == "deployment_cooldown_started":
        return _trace_detail_parts(
            ("cooldown", raw.get("cooldown_remaining_seconds") or raw.get("cooldown_seconds"), "s"),
            ("failures", raw.get("failures"), ""),
            ("threshold", raw.get("failure_threshold"), ""),
            ("reason", reason, ""),
        )

    if event == "stream_start_timeout":
        return _trace_detail_parts(
            ("timeout", raw.get("start_seconds"), "s"),
            ("buffered", raw.get("buffered_chunks"), ""),
            ("saw_chunk", _trace_bool(raw.get("saw_chunk")), ""),
        )

    return _trace_detail_parts(
        ("round", raw.get("round"), ""),
        ("order", raw.get("target_order"), ""),
        ("service_tier", raw.get("service_tier"), ""),
        ("cooldown", raw.get("cooldown_remaining_seconds"), "s"),
        ("reason", reason, ""),
    )


def _whole_seconds(value: object) -> int | None:
    try:
        seconds = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(seconds):
        return None
    return max(0, math.ceil(seconds))


def _recovery_cooldown_seconds(raw: Mapping[str, Any], *, now: float | None = None) -> int | None:
    cooldown_until = _timestamp_number(raw.get("cooldown_until"))
    if cooldown_until is not None:
        return _whole_seconds(cooldown_until - (time.time() if now is None else now))
    return _whole_seconds(raw.get("cooldown_remaining_seconds"))


def _recovery_detail(raw: Mapping[str, Any], *, now: float | None = None) -> str:
    details: list[str] = []
    for value, key, suffix in (
        (_trace_value(raw.get("attempt")), "attempt", ""),
        (_trace_value(_whole_seconds(raw.get("attempt_timeout_seconds"))), "timeout", "s"),
        (_trace_value(_recovery_cooldown_seconds(raw, now=now)), "cooldown", "s"),
        (_trace_value(_whole_seconds(raw.get("poll_interval_seconds"))), "retry", "s"),
    ):
        if value:
            details.append(f"{key}={value}{suffix}")
    diagnostic = _mapping_value(raw.get("diagnostic"))
    reason = _string_record_value(diagnostic.get("kind"))
    if reason:
        details.append(f"reason={reason}")
    return " · ".join(details)


def _recovery_candidate(raw: Mapping[str, Any]) -> Mapping[str, Any]:
    candidates = raw.get("cooldown_deployments")
    if not isinstance(candidates, list):
        return {}
    model_group = _string_record_value(raw.get("model_group"))
    for candidate in candidates:
        if not isinstance(candidate, Mapping):
            continue
        if _public_model_from_route_key(candidate.get("route_key")) == model_group:
            return candidate
    return next(
        (candidate for candidate in candidates if isinstance(candidate, Mapping)),
        {},
    )


def _safe_recovery_record(
    raw: Mapping[str, Any],
    configured_deployments: Mapping[str, Mapping[str, str]],
    *,
    now: float | None = None,
) -> dict[str, Any]:
    request = _mapping_value(raw.get("request"))
    exception = _mapping_value(raw.get("exception") or raw.get("error"))
    route_key = (
        raw.get("route_key")
        or request.get("route_key")
        or exception.get("failed_deployment_route_key")
        or exception.get("failed_route_key")
    )
    candidate = _recovery_candidate(raw) if not route_key else {}
    if not route_key:
        route_key = candidate.get("route_key")
    deployment_id = _string_record_value(
        raw.get("deployment_id")
        or request.get("deployment_id")
        or exception.get("failed_deployment_id")
        or candidate.get("id")
    )
    configured = configured_deployments.get(deployment_id, {})
    route_public_model = _public_model_from_route_key(route_key)
    route_upstream_model = _route_key_value(route_key, "upstream")
    public_model = (
        # The configured public model is authoritative for a known
        # deployment.  Route keys can carry a prefixed litellm model
        # (``openai/gpt-5.6-sol``) whose bare form is the public name.
        configured.get("public_model", "")
        or route_public_model
        or _string_record_value(raw.get("public_model"))
        or _string_record_value(raw.get("model_group"))
        or _string_record_value(request.get("public_model"))
        or _string_record_value(request.get("model_group"))
    )
    upstream_model = _upstream_model_name(
        route_upstream_model
        or configured.get("upstream_model", "")
        or candidate.get("model")
        or raw.get("upstream_model")
        or request.get("upstream_model")
        or exception.get("upstream_model")
    )
    provider = (
        _route_key_value(route_key, "provider")
        or configured.get("provider", "")
        or _string_record_value(candidate.get("provider"))
        or _string_record_value(raw.get("provider"))
        or _string_record_value(request.get("provider"))
        or _string_record_value(exception.get("provider"))
    )
    api_key_name = (
        _route_key_value(route_key, "key")
        or configured.get("api_key_name", "")
        or _string_record_value(candidate.get("api_key_name"))
        or _string_record_value(raw.get("api_key_name"))
        or _string_record_value(request.get("api_key_name"))
    )
    timestamp = next(
        (
            raw.get(key)
            for key in (
                "ts",
                "timestamp",
                "time",
                "created_at",
                "updated_at",
                "checked_at",
                "heartbeat_at",
                "started_at",
            )
            if raw.get(key) not in (None, "")
        ),
        None,
    )
    result: dict[str, Any] = {
        "timestamp": _safe_scalar(timestamp),
        "public_model": _safe_scalar(public_model),
        "upstream_model": _safe_scalar(upstream_model),
        "provider": _safe_scalar(provider),
        "api_key_name": _safe_scalar(api_key_name),
        "status": _safe_scalar(raw.get("status")),
        "detail": _safe_scalar(_recovery_detail(raw, now=now), limit=260),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_cooldown_record(
    raw: Mapping[str, Any],
    cooldown_key: str,
    configured_deployments: Mapping[str, Mapping[str, str]],
    *,
    remaining_seconds: float,
    now: float,
) -> dict[str, Any]:
    """Project one live deployment cooldown into the recovery log schema."""
    timestamp = raw.get("last_failure_at") or raw.get("updated_at") or raw.get("cooldown_until")
    projected = _safe_recovery_record(
        {
            **dict(raw),
            "status": "cooldown",
            "timestamp": timestamp,
            "cooldown_remaining_seconds": _whole_seconds(remaining_seconds),
        },
        configured_deployments,
        now=now,
    )
    failures = raw.get("failures")
    try:
        failure_count = int(failures or 0)
    except (TypeError, ValueError):
        failure_count = 0
    details = [value for value in (projected.get("detail"), f"failures={failure_count}" if failure_count > 0 else "") if value]
    if details:
        projected["detail"] = " · ".join(details)
    if not projected:
        return {
            "timestamp": _safe_scalar(timestamp),
            "status": "cooldown",
            "detail": _safe_scalar(f"cooldown={_whole_seconds(remaining_seconds) or 0}s"),
        }
    return projected


def _safe_route_identity(
    route_key: object,
    *,
    deployment_id: object = "",
    public_model: object = "",
    upstream_model: object = "",
    provider: object = "",
    order: object = None,
    configured_public_models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    route_public_model = _public_model_from_route_key(route_key)
    deployment_text = _string_record_value(deployment_id)
    configured_public_model = (
        configured_public_models.get(deployment_text)
        if configured_public_models and deployment_text
        else ""
    )
    result = {
        "deployment_id": _safe_scalar(deployment_text),
        "public_model": _safe_scalar(
            configured_public_model or route_public_model or _string_record_value(public_model)
        ),
        "upstream_model": _safe_scalar(
            _upstream_model_name(_route_key_value(route_key, "upstream") or upstream_model)
        ),
        "provider": _safe_scalar(_route_key_value(route_key, "provider") or provider),
        "order": _safe_scalar(_route_key_value(route_key, "order") or order),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_route_candidates(
    raw: Mapping[str, Any],
    *,
    configured_public_models: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    seen: set[str] = set()
    for field in (
        "candidates",
        "after_constraints",
        "selected_candidates",
        "cooldown_deployments",
    ):
        values = raw.get(field)
        if not isinstance(values, list):
            continue
        for value in values:
            if not isinstance(value, Mapping):
                continue
            route_key = value.get("route_key")
            identity = _safe_route_identity(
                route_key,
                deployment_id=value.get("id") or value.get("deployment_id"),
                public_model=value.get("public_model") or value.get("model_group"),
                upstream_model=value.get("model") or value.get("upstream_model"),
                provider=value.get("provider"),
                order=value.get("order"),
                configured_public_models=configured_public_models,
            )
            identity_key = json.dumps(identity, ensure_ascii=False, sort_keys=True)
            if not identity or identity_key in seen:
                continue
            seen.add(identity_key)
            candidates.append(identity)
            if len(candidates) >= 24:
                return candidates
    return candidates


def _safe_route_trace_record(
    raw: Mapping[str, Any],
    configured_public_models: Mapping[str, str] | None = None,
    configured_route_models: object = None,
) -> dict[str, Any]:
    request = _mapping_value(raw.get("request"))
    deployment = _mapping_value(raw.get("deployment"))
    peer_entry = _mapping_value(raw.get("peer_entry"))
    exception = _mapping_value(raw.get("exception") or raw.get("error"))
    selected_route_key = (
        raw.get("route_key")
        or deployment.get("route_key")
        or request.get("route_key")
        or peer_entry.get("route_key")
    )
    selected_deployment_id = (
        raw.get("deployment_id")
        or deployment.get("id")
        or request.get("deployment_id")
        or peer_entry.get("id")
    )
    failed_route_key = (
        exception.get("failed_deployment_route_key")
        or exception.get("failed_route_key")
        or raw.get("failed_route_key")
    )
    failed_deployment_id = (
        exception.get("failed_deployment_id")
        or raw.get("failed_deployment_id")
    )
    route_key = selected_route_key or failed_route_key
    deployment_id = selected_deployment_id or failed_deployment_id
    route_public_model = _public_model_from_route_key(route_key)
    public_model = (
        route_public_model
        or _string_record_value(raw.get("public_model"))
        or _string_record_value(raw.get("model_group"))
        or _string_record_value(request.get("model_group"))
    )
    configured_public_model = (
        configured_public_models.get(deployment_id.strip())
        if configured_public_models
        and isinstance(deployment_id, str)
        and deployment_id.strip()
        else None
    )
    upstream_model = _upstream_model_name(
        _route_key_value(route_key, "upstream")
        or raw.get("upstream_model")
        or deployment.get("model")
        or request.get("upstream_model")
        or exception.get("upstream_model")
    )
    provider = (
        _route_key_value(route_key, "provider")
        or _string_record_value(raw.get("provider"))
        or _string_record_value(deployment.get("provider"))
        or _string_record_value(request.get("provider"))
        or _string_record_value(exception.get("provider"))
    )
    if configured_public_model is None and configured_route_models:
        configured_public_model = (
            _configured_route_public_model(
                configured_route_models,
                {
                    "provider": provider,
                    "upstream_model": upstream_model,
                    "api_key_name": _route_key_value(route_key, "key")
                    or _string_record_value(raw.get("api_key_name"))
                    or _string_record_value(deployment.get("api_key_name"))
                    or _string_record_value(request.get("api_key_name")),
                    "route_key": route_key,
                },
            )
            or None
        )
    if configured_public_model and (
        not route_public_model
        or _matches_upstream_model(
            route_public_model, _route_key_value(route_key, "upstream")
        )
    ):
        public_model = configured_public_model
    status = _string_record_value(raw.get("status") or raw.get("result"))
    if not status and (raw.get("exception") is not None or raw.get("error") is not None):
        status = "error"
    timestamp = next(
        (
            raw.get(key)
            for key in (
                "ts",
                "timestamp",
                "time",
                "created_at",
                "updated_at",
                "checked_at",
                "heartbeat_at",
                "started_at",
            )
            if raw.get(key) not in (None, "")
        ),
        None,
    )
    result: dict[str, Any] = {
        "timestamp": _safe_scalar(timestamp),
        "event": _safe_scalar(raw.get("event")),
        "public_model": _safe_scalar(public_model),
        "upstream_model": _safe_scalar(upstream_model),
        "provider": _safe_scalar(provider),
        "status": _safe_scalar(status),
        "detail": _safe_scalar(_trace_detail(raw), limit=260),
    }
    session = _mapping_value(raw.get("session"))
    request_id = raw.get("request_id") or request.get("request_id")
    session_id = raw.get("session_id") or session.get("id")
    deployment_order = raw.get("deployment_order")
    if deployment_order is None:
        deployment_order = deployment.get("order")
    if deployment_order is None:
        deployment_order = peer_entry.get("order")
    route_identity = (
        _safe_route_identity(
            selected_route_key,
            deployment_id=selected_deployment_id,
            public_model=public_model,
            upstream_model=upstream_model or peer_entry.get("model"),
            provider=provider or peer_entry.get("provider"),
            order=deployment_order,
            configured_public_models=configured_public_models,
        )
        if selected_route_key or selected_deployment_id or deployment or peer_entry
        else {}
    )
    failed_identity = (
        _safe_route_identity(
            failed_route_key,
            deployment_id=failed_deployment_id,
            public_model=public_model,
            upstream_model=exception.get("upstream_model"),
            provider=exception.get("provider"),
            order=exception.get("failed_deployment_order"),
            configured_public_models=configured_public_models,
        )
        if failed_route_key or failed_deployment_id
        else {}
    )
    safe_fields = {
        "request_id": _safe_scalar(request_id),
        "session_id": _safe_scalar(session_id),
        "deployment_id": _safe_scalar(deployment_id),
        "deployment_order": _safe_scalar(deployment_order),
        "target_order": _safe_scalar(raw.get("target_order")),
        "route": route_identity,
        "failed_route": failed_identity,
        "candidate_routes": _safe_route_candidates(
            raw,
            configured_public_models=configured_public_models,
        ),
    }
    result.update({key: value for key, value in safe_fields.items() if value not in (None, "", {}, [])})
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_request_record(
    raw: Mapping[str, Any],
    configured_public_models: Mapping[str, str] | None = None,
    configured_route_models: object = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "ts",
        "timestamp",
        "time",
        "created_at",
        "updated_at",
        "checked_at",
        "heartbeat_at",
        "started_at",
        "request_id",
        "status",
        "model_group",
        "public_model",
        "provider",
        "api_key_name",
        "upstream_model",
        "duration_ms",
        "route_key",
        "routing_state",
    ):
        if key in raw:
            result[key] = _safe_scalar(raw[key])
    route_public_model = _public_model_from_route_key(result.get("route_key"))
    if route_public_model:
        result["public_model"] = route_public_model
    route_upstream_model = _route_key_value(result.get("route_key"), "upstream")
    if route_upstream_model:
        result["upstream_model"] = _safe_scalar(route_upstream_model)
    route_provider = _route_key_value(result.get("route_key"), "provider")
    if route_provider:
        result["provider"] = _safe_scalar(route_provider)
    route_api_key_name = _route_key_value(result.get("route_key"), "key")
    if route_api_key_name:
        result["api_key_name"] = _safe_scalar(route_api_key_name)
    if (
        result.get("routing_state") in (None, "")
        and not _string_record_value(result.get("route_key"))
        and not _string_record_value(result.get("provider"))
        and _string_record_value(result.get("status")) in {"failure", "failed", "error", "stuck"}
    ):
        result["routing_state"] = "unselected"
    deployment_id = raw.get("deployment_id")
    configured_public_model = (
        configured_public_models.get(deployment_id.strip())
        if configured_public_models
        and isinstance(deployment_id, str)
        and deployment_id.strip()
        else None
    )
    if configured_public_model is None and configured_route_models:
        # A dynamic id (client-side credentials, rebuilt fallback kwargs) is
        # not in the configuration; the route identity still is.
        configured_public_model = (
            _configured_route_public_model(configured_route_models, result) or None
        )
    if configured_public_model and (
        not route_public_model
        or _matches_upstream_model(
            route_public_model,
            result.get("upstream_model"),
        )
    ):
        result["public_model"] = _safe_scalar(configured_public_model)
    usage = raw.get("usage")
    if isinstance(usage, Mapping):
        # Relays report either input/output or prompt/completion pairs.  Keep
        # both spellings under one canonical pair so the UI can always show
        # sent/received tokens.
        projected_usage: dict[str, Any] = {}
        for raw_key, projected_key in (
            ("input_tokens", "input_tokens"),
            ("prompt_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
            ("completion_tokens", "output_tokens"),
            ("total_tokens", "total_tokens"),
        ):
            value = usage.get(raw_key)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                projected_usage[projected_key] = value
        result["usage"] = projected_usage
    error = raw.get("error")
    if isinstance(error, Mapping):
        projected_error = {
            key: value
            for key in (
                "status_code",
                "code",
                "type",
                "reason",
                "failed_deployment_id",
                "failed_route_key",
                "failed_deployment_order",
            )
            if (value := _safe_scalar(error.get(key), limit=260)) not in (None, "")
        }
        result["error"] = projected_error or {"reason": "request-failed"}
    elif error not in (None, ""):
        result["error"] = {"reason": "request-failed"}
    aborted = raw.get("aborted")
    if isinstance(aborted, Mapping):
        # Keep the runtime's own reason for a closed attempt (client
        # disconnect, service restart) instead of showing a bare status.
        projected_aborted = {
            key: value
            for key in ("reason",)
            if (value := _safe_scalar(aborted.get(key), limit=160)) not in (None, "")
        }
        if projected_aborted:
            result["aborted"] = projected_aborted
    return result


_REQUEST_PENDING_STATUSES = frozenset({"pending", "sending"})
# A request attempt that stops progressing without a terminal callback (client
# disconnect, failover teardown, worker exit) must not stay "sending" forever.
# The runtime closes those rows itself; this window is the viewer's fallback for
# rows written by an earlier runtime or by a process that died mid-flight.
_REQUEST_PROGRESS_STATUSES = _REQUEST_PENDING_STATUSES | {"stream"}
REQUEST_STALE_SECONDS_KEY = "YOUNG_ROUTER_LOG_REQUEST_STALE_SECONDS"
DEFAULT_REQUEST_STALE_SECONDS = 900.0
MAX_REQUEST_STALE_SECONDS = 86400.0


def _line_byte_size(line: str) -> int:
    return len(line.encode("utf-8")) + 1


def _request_stale_seconds_value(value: object) -> float:
    try:
        parsed = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return DEFAULT_REQUEST_STALE_SECONDS
    if not math.isfinite(parsed) or parsed <= 0:
        return 0.0
    return min(parsed, MAX_REQUEST_STALE_SECONDS)


def _close_stale_request_record(
    record: dict[str, Any],
    *,
    stale_seconds: float,
    now: float,
) -> dict[str, Any]:
    """Project an unfinished row that stopped progressing as interrupted."""

    if stale_seconds <= 0:
        return record
    if _request_record_status(record) not in _REQUEST_PROGRESS_STATUSES:
        return record
    observed = _timestamp_number(
        record.get("ts")
        or record.get("timestamp")
        or record.get("started_at")
        or record.get("updated_at")
    )
    if observed is None or now - observed < stale_seconds:
        return record
    settled = dict(record)
    settled["status"] = "aborted"
    settled["aborted"] = {"reason": "stale"}
    return settled


def _request_projection_key(record: Mapping[str, Any]) -> str:
    return _string_record_value(record.get("request_id"))


def _request_record_status(record: Mapping[str, Any]) -> str:
    return _string_record_value(record.get("status")).casefold()


def _should_replace_request_record(
    existing: Mapping[str, Any],
    incoming: Mapping[str, Any],
) -> bool:
    incoming_status = _request_record_status(incoming)
    if incoming_status in _REQUEST_PENDING_STATUSES:
        # A new deployment attempt can follow an earlier failed attempt while
        # retaining the same request id. Surface that retry as active again.
        return True
    existing_status = _request_record_status(existing)
    if incoming_status == "stream" and existing_status not in {
        *_REQUEST_PENDING_STATUSES,
        "stream",
    }:
        # Ignore an out-of-order non-terminal stream callback after a terminal
        # callback. A subsequent pending record still starts a visible retry.
        return False
    return True


def _collapse_request_records(records: list[object]) -> list[object]:
    """Keep one row per request while allowing progress to reach a terminal state."""
    projected: dict[str, tuple[int, dict[str, Any]]] = {}
    standalone: list[tuple[int, dict[str, Any]]] = []
    for index, record in enumerate(records):
        if not isinstance(record, Mapping):
            continue
        value = dict(record)
        request_id = _request_projection_key(value)
        if not request_id:
            standalone.append((index, value))
            continue
        existing = projected.get(request_id)
        if existing is not None and not _should_replace_request_record(existing[1], value):
            continue
        projected[request_id] = (existing[0] if existing else index, value)
    result = list(projected.values()) + standalone
    return [record for _index, record in sorted(result, key=lambda x: x[0])]


def _is_service_noise(line: str) -> bool:
    message = SERVICE_TIMESTAMP_PREFIX.sub("", line).strip()
    if not message:
        return True
    lowered = message.casefold()
    if (
        "github.com/berriai/litellm/issues/new" in lowered
        or lowered.startswith("thank you for using litellm")
        or lowered.startswith("give feedback / get help")
        or lowered.startswith(SERVICE_BANNER_HEADLINE)
    ):
        return True
    if message.startswith("#") and message.endswith("#"):
        return True
    if re.fullmatch(r"[#_|/\\=+* .:-]+", message):
        return True
    # The startup banner draws itself in block characters before it lists the
    # configured models.
    if SERVICE_ART.search(message):
        return True
    # LiteLLM prints configured model identifiers as bare banner lines, with a
    # provider prefix or as the configured name alone. Real service events
    # carry a severity, PID, timestamp, or explanatory text.
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+", message):
        return True
    if SERVICE_BARE_MODEL.fullmatch(message):
        return True
    return False


def _timestamp_number(value: object) -> float | None:
    """Return a comparable timestamp without making log parsing strict."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if not math.isfinite(number):
            return None
        return number / 1000 if abs(number) >= 100_000_000_000 else number
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if re.fullmatch(r"[-+]?\d+(?:\.\d+)?", text):
        number = float(text)
        if not math.isfinite(number):
            return None
        return number / 1000 if abs(number) >= 100_000_000_000 else number
    if text.endswith("Z"):
        text = f"{text[:-1]}+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except (TypeError, ValueError, OverflowError):
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    try:
        return parsed.timestamp()
    except (OverflowError, OSError, ValueError):
        return None


def active_recovery_states(value: object, *, now: float | None = None) -> list[Mapping[str, Any]]:
    """Return route-recovery entries that still have a live heartbeat."""
    if not isinstance(value, Mapping):
        return []
    current = time.time() if now is None else now
    active: list[Mapping[str, Any]] = []
    for state in value.values():
        if not isinstance(state, Mapping):
            continue
        heartbeat = next(
            (
                parsed
                for key in (
                    "heartbeat_at",
                    "updated_at",
                    "timestamp",
                    "time",
                    "ts",
                    "created_at",
                    "checked_at",
                    "started_at",
                )
                if (parsed := _timestamp_number(state.get(key))) is not None
            ),
            None,
        )
        if heartbeat is None or current - heartbeat > RECOVERY_HEARTBEAT_TTL_SECONDS:
            continue
        active.append(state)
    return active


def active_cooldown_states(
    value: object,
    *,
    now: float | None = None,
) -> list[tuple[str, Mapping[str, Any]]]:
    """Return deployment cooldown entries whose expiry is still in the future."""
    if not isinstance(value, Mapping):
        return []
    current = time.time() if now is None else now
    active: list[tuple[str, Mapping[str, Any]]] = []
    for key, state in value.items():
        if not isinstance(state, Mapping):
            continue
        try:
            cooldown_until = float(state.get("cooldown_until") or 0)
        except (TypeError, ValueError):
            continue
        if cooldown_until > current:
            active.append((str(key), state))
    return active


def _leading_timestamp(line: str) -> str:
    match = LEADING_TIMESTAMP.match(line.strip())
    if not match:
        return ""
    candidate = match.group("bracket") or match.group("iso") or ""
    return candidate if _timestamp_number(candidate) is not None else ""


def _record_timestamp(record: object) -> float | None:
    if isinstance(record, Mapping):
        for key in (
            "ts",
            "timestamp",
            "time",
            "created_at",
            "updated_at",
            "checked_at",
            "heartbeat_at",
            "started_at",
        ):
            value = _timestamp_number(record.get(key))
            if value is not None:
                return value
        return None
    if isinstance(record, str):
        return _timestamp_number(_leading_timestamp(record))
    return None


def _sort_records_by_time(records: list[object]) -> list[object]:
    """Keep every view chronological while preserving source order for ties."""
    decorated = [(_record_timestamp(record), index, record) for index, record in enumerate(records)]
    decorated.sort(
        key=lambda item: (
            item[0] is None,
            item[0] if item[0] is not None else 0.0,
            item[1],
        )
    )
    return [record for _timestamp, _index, record in decorated]


def _service_continuation(line: str) -> str:
    """Remove a repeated timestamp from a traceback continuation line."""
    match = LEADING_TIMESTAMP.match(line.strip())
    if not match:
        return line.strip()
    start = match.end()
    return line[start:].strip()


def _looks_like_traceback_continuation(line: str) -> bool:
    payload = _service_continuation(line)
    return bool(
        re.match(
            r"^(?:File \"|self\.|raise\b|return\b|if\b|elif\b|else\b|for\b|while\b|try\b|except\b|finally\b|[\^~]+$)",
            payload.strip(),
            re.IGNORECASE,
        )
    )


def _service_payload_without_level(line: str) -> str:
    payload = SERVICE_TIMESTAMP_PREFIX.sub("", line).strip()
    return re.sub(
        r"^(?:\[[A-Z]+\]|(?:DEBUG|INFO|WARNING|ERROR|CRITICAL):)\s*",
        "",
        payload,
        flags=re.IGNORECASE,
    ).strip()


def _service_event_identity(line: str) -> str:
    """The parts of a console line that make it the event the reader sees.

    One worker's copy of a line differs from another's only by its PID, by the
    client address uvicorn logs beside it, and by the clock LiteLLM prints at
    the head of its own records.  Two lines that share an identity are copies
    of one event, not two events.
    """

    payload = SERVICE_TIMESTAMP_PREFIX.sub("", line).strip()
    return SERVICE_EVENT_VOLATILE.sub("#", payload)


def _service_repeat_row(line: str, count: int) -> str:
    """One row for a repeated event, bounded like any other service row."""

    if count <= 1:
        return line
    suffix = f" (×{count})"
    return f"{line[: MAX_TEXT_LOG_CHARS - len(suffix)]}{suffix}"


def _collapse_service_repeats(lines: list[str]) -> list[str]:
    """Fold one burst of identical service lines into one row.

    A sixteen-worker proxy reports its uvicorn lifecycle once per worker, so a
    single start wrote sixteen copies of every line, and a client that probes
    the endpoint repeats one access line many times a minute. Each burst is
    one event, and the count is what shows whether every worker arrived, so
    the row states it instead of the copies.  Workers write concurrently, so
    their copies interleave with each other's: a burst is every line with the
    same identity inside the window, wherever it sits, and the row stays at
    the burst's first line.
    """

    collapsed: list[str] = []
    bursts: dict[str, int] = {}
    counts: list[int] = []
    stamps: list[float | None] = []

    for raw in lines:
        line = raw.strip()
        if not line:
            continue
        identity = _service_event_identity(line)
        moment = _record_timestamp(line)
        index = bursts.get(identity)
        if index is not None:
            last = stamps[index]
            if moment is None or last is None or abs(moment - last) <= SERVICE_REPEAT_WINDOW_SECONDS:
                counts[index] += 1
                if moment is not None:
                    stamps[index] = moment
                continue
        bursts[identity] = len(collapsed)
        collapsed.append(line)
        counts.append(1)
        stamps.append(moment)
    return [
        _service_repeat_row(line, count)
        for line, count in zip(collapsed, counts)
    ]


def _split_merged_service_lines(lines: list[str]) -> list[str]:
    """Restore records that two proxy workers wrote into one line.

    The workers share one log file and a partial write lets their output
    interleave, so one console line can carry the head of the next record
    glued to its tail.  Both records keep their own timestamp marker; split
    there so each is projected (or dropped) as the line it was.
    """

    expanded: list[str] = []
    for raw in lines:
        # A route-trace record is one JSON document whose preview may quote a
        # timestamp of its own; it is dropped whole, never split.
        if "litellm_route_trace" in raw:
            expanded.append(raw)
            continue
        stripped = raw.strip()
        match = SERVICE_TIMESTAMP_PREFIX.match(stripped)
        if match is None:
            expanded.append(raw)
            continue
        payload = stripped[match.end() :]
        if SERVICE_EMBEDDED_TIMESTAMP.search(payload) is None:
            expanded.append(raw)
            continue
        stamp = stripped[: match.end()].strip().split("]", 1)[0] + "]"
        for part in SERVICE_EMBEDDED_TIMESTAMP.split(payload):
            part = part.strip()
            if part:
                expanded.append(f"{stamp} {part}")
    return expanded


def _group_service_lines(lines: list[str]) -> list[str]:
    """Collapse one logical service event (including traceback lines) to one row."""
    grouped: list[str] = []
    current: list[str] = []
    current_timestamp = ""
    in_traceback = False

    def flush() -> None:
        nonlocal current, current_timestamp, in_traceback
        if current:
            grouped.append(" | ".join(part for part in current if part))
        current = []
        current_timestamp = ""
        in_traceback = False

    for raw in _split_merged_service_lines(lines):
        plain = ANSI_CONTROL_SEQUENCE.sub("", raw).strip()
        # These two cheap checks have to stay ahead of ``REDACT_TEXT``, which
        # runs several regexes over every line: the blanks, the startup
        # banner, and the route-trace records are most of a service log, and
        # the trace records alone are the bulk of its bytes.
        if not plain or "litellm_route_trace" in plain or _is_service_noise(plain):
            continue
        line = REDACT_TEXT(plain)
        timestamp = _leading_timestamp(line)
        same_timestamp = bool(
            current
            and timestamp
            and current_timestamp
            and timestamp == current_timestamp
        )
        payload = _service_continuation(line) if same_timestamp else line
        payload_without_prefix = _service_payload_without_level(payload)
        continuation = bool(current) and in_traceback and (
            not timestamp
            or same_timestamp
            or _looks_like_traceback_continuation(payload_without_prefix)
        )
        if continuation:
            current.append(payload)
            if in_traceback and TRACEBACK_TERMINAL.match(payload_without_prefix):
                in_traceback = False
            continue
        flush()
        current = [line]
        current_timestamp = timestamp
        in_traceback = bool(TRACEBACK_START.search(payload_without_prefix))
    flush()
    return grouped


class LogsDomain:
    name = DOMAIN_NAME

    def __init__(
        self,
        runtime_root: Path | str | None = None,
        *,
        config_path: Path | str | None = None,
        online_usage_reader: object | None = None,
        runtime_settings_path: Path | str | None = None,
        maximum_lines: int = DEFAULT_LINES,
        maximum_read_bytes: int = MAX_READ_BYTES,
    ) -> None:
        self.root = _runtime_root(runtime_root)
        self.config_path = Path(config_path).expanduser() if config_path else self.root / "config.yaml"
        self._online_usage_reader = online_usage_reader
        if runtime_settings_path is None:
            from ._shared import _default_runtime_settings_path

            self.runtime_settings_path = _default_runtime_settings_path()
        else:
            self.runtime_settings_path = Path(runtime_settings_path).expanduser()
        self._runtime_settings_signature: tuple[int, int] | None = None
        self._runtime_line_limit = max(1, min(int(maximum_lines), MAX_LINES))
        self._request_stale_seconds = DEFAULT_REQUEST_STALE_SECONDS
        # The Core writes the menu-action log itself, so it does not inherit
        # the configured runtime settings through the proxy environment.
        # Cache the values here and pass them to the rotation writer.
        self._runtime_log_max_bytes: int | None = None
        self._runtime_log_backup_segments: int | None = None
        self._online_usage_records: list[str] = []
        self._online_usage_refreshed = False
        self._online_usage_revision = 0
        runtime = self.root / ".litellm-runtime"
        self._recovery_path = Path(
            os.environ.get("YOUNG_ROUTER_ROUTE_RECOVERY_STATE_FILE", runtime / "route-recovery-state.json")
        ).expanduser()
        self._cooldowns_path = Path(
            os.environ.get("YOUNG_ROUTER_DEPLOYMENT_COOLDOWN_FILE", runtime / "deployment-cooldowns.json")
        ).expanduser()
        self.maximum_lines = max(1, min(int(maximum_lines), MAX_LINES))
        self.maximum_read_bytes = max(4096, min(int(maximum_read_bytes), MAX_READ_BYTES))
        self.revision = 0
        self._paused: set[str] = set()
        self._cleared: set[str] = set()
        self._clear_cursors: dict[str, tuple[object, ...]] = {}
        self._filters: dict[str, str] = {}
        self._limits: dict[str, int] = {}
        self._source_signatures: dict[str, tuple[object, ...]] = {}
        self._view_revisions: dict[str, int] = {tab: 0 for tab in LOG_TABS}
        self._tabs: dict[str, dict[str, Any]] = {
            tab: self._empty_tab(tab) for tab in LOG_TABS
        }
        # The request log is appended many times per turn while its tab polls
        # once a second.  Reparse and reproject only what an append added, and
        # keep the already projected window for the lines before it.
        self._request_tail_lock = threading.Lock()
        self._request_tail: dict[str, Any] = {}

    def _project_request_line(
        self,
        line: str,
        configured_public_models: Mapping[str, str],
        configured_route_models: object,
    ) -> dict[str, Any] | None:
        if not line.strip():
            return None
        try:
            parsed = json.loads(line)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(parsed, Mapping):
            return None
        return _safe_request_record(
            parsed,
            configured_public_models,
            configured_route_models,
        )

    @staticmethod
    def _request_tail_boundary_reached(path: Path, offset: int) -> bool:
        """Whether a cached read ended exactly on a line boundary.

        A read that stopped inside a line cannot be extended: the partial line
        is already part of the cached window, so the tail is rebuilt from the
        file instead of resumed.
        """

        if offset <= 0:
            return True
        try:
            with path.open("rb") as handle:
                handle.seek(offset - 1)
                return handle.read(1) == b"\n"
        except OSError:
            return False

    def _read_appended_lines(self, path: Path, offset: int) -> tuple[list[str], int]:
        """Read the complete lines appended after ``offset``.

        A line still being written stays for the next refresh: it can never
        project a record, and resuming exactly at the last newline keeps the
        cached window aligned with the file.
        """

        with path.open("rb") as handle:
            handle.seek(offset)
            data = handle.read(self.maximum_read_bytes)
        end = data.rfind(b"\n")
        if end < 0:
            return [], 0
        consumed = end + 1
        return data[:consumed].decode("utf-8", errors="replace").splitlines(), consumed

    def _trim_request_tail(
        self,
        entries: list[tuple[dict[str, Any] | None, int]],
        line_limit: int,
    ) -> tuple[list[tuple[dict[str, Any] | None, int]], int]:
        total = sum(size for _record, size in entries)
        start = 0
        while start < len(entries) and (
            len(entries) - start > line_limit or total > self.maximum_read_bytes
        ):
            total -= entries[start][1]
            start += 1
        return (entries[start:], total) if start else (entries, total)

    def _requests_records(
        self,
        path: Path,
        configured_public_models: Mapping[str, str],
        configured_route_models: object,
    ) -> list[dict[str, Any]]:
        """Project the newest request lines, reusing the parsed tail."""

        line_limit = self._line_limit("requests")
        signature = _configuration_signature(self.config_path)
        try:
            details = path.lstat()
        except OSError:
            details = None
        regular = (
            details is not None
            and not stat.S_ISLNK(details.st_mode)
            and stat.S_ISREG(details.st_mode)
        )
        with self._request_tail_lock:
            cache = self._request_tail
            offset = cache.get("offset")
            usable = (
                regular
                and cache.get("dev") == details.st_dev
                and cache.get("ino") == details.st_ino
                and cache.get("signature") == signature
                and isinstance(offset, int)
                and 0 <= offset <= details.st_size
                and cache.get("line_limit", 0) >= line_limit
                and details.st_size - offset <= self.maximum_read_bytes
                and self._request_tail_boundary_reached(path, offset)
            )
            if usable:
                entries = list(cache.get("entries") or [])
                if details.st_size > offset:
                    appended, consumed = self._read_appended_lines(path, offset)
                    if consumed > 0:
                        for line in appended:
                            entries.append(
                                (
                                    self._project_request_line(
                                        line,
                                        configured_public_models,
                                        configured_route_models,
                                    ),
                                    _line_byte_size(line),
                                )
                            )
                        entries, total = self._trim_request_tail(entries, line_limit)
                        cache.update(
                            {
                                "entries": entries,
                                "bytes": total,
                                "offset": offset + consumed,
                                "line_limit": line_limit,
                            }
                        )
                return [record for record, _size in entries if record is not None]
            self._request_tail = {}
        lines = self._read_lines(path, line_limit=line_limit)
        entries = [
            (
                self._project_request_line(
                    line,
                    configured_public_models,
                    configured_route_models,
                ),
                _line_byte_size(line),
            )
            for line in lines
        ]
        entries, total = self._trim_request_tail(entries, line_limit)
        if regular:
            with self._request_tail_lock:
                self._request_tail = {
                    "dev": details.st_dev,
                    "ino": details.st_ino,
                    "offset": details.st_size,
                    "line_limit": line_limit,
                    "signature": signature,
                    "bytes": total,
                    "entries": entries,
                }
        return [record for record, _size in entries if record is not None]

    def _empty_tab(self, tab: str) -> dict[str, Any]:
        return {
            "tab": tab,
            "available": False,
            "paused": tab in self._paused,
            "line_count": 0,
            "records": [],
            "filter": self._filters.get(tab, ""),
            "limit": self._line_limit(tab),
        }

    def _refresh_runtime_settings(self) -> None:
        """Reload cached Runtime Settings values when their file changes."""

        try:
            details = self.runtime_settings_path.stat()
            signature = (details.st_mtime_ns, details.st_size)
        except OSError:
            signature = (-1, -1)
        if signature == self._runtime_settings_signature:
            return
        self._runtime_settings_signature = signature
        try:
            from ..runtime_settings_io import load_specs, read_settings_file

            values = read_settings_file(self.runtime_settings_path, load_specs())
            configured = int(values.get("YOUNG_ROUTER_LOG_VIEW_LIMIT", DEFAULT_LINES))
            self._runtime_line_limit = max(1, min(configured, MAX_LINES))
            self._request_stale_seconds = _request_stale_seconds_value(
                values.get(REQUEST_STALE_SECONDS_KEY, DEFAULT_REQUEST_STALE_SECONDS)
            )
            # ``read_settings_file`` reports an ``mb`` kind in megabytes.
            megabytes = float(values.get(LOG_MAX_BYTES_ENV, "") or "")
            self._runtime_log_max_bytes = max(MIN_LOG_MAX_BYTES, round(megabytes * 1024 * 1024))
            raw_segments = str(values.get(LOG_BACKUP_SEGMENTS_ENV, "") or "").strip()
            self._runtime_log_backup_segments = int(raw_segments) if raw_segments else None
        except (OSError, TypeError, ValueError):
            self._runtime_line_limit = self.maximum_lines
            self._request_stale_seconds = DEFAULT_REQUEST_STALE_SECONDS
            self._runtime_log_max_bytes = None
            self._runtime_log_backup_segments = None

    def _line_limit(self, tab: str) -> int:
        self._refresh_runtime_settings()
        return self._limits.get(tab, self._runtime_line_limit)

    def _path(self, tab: str) -> Path | None:
        names = {
            "requests": "recent-requests.jsonl",
            "service": "server.log",
            "actions": "actions.log",
            "route-trace": "server.log",
        }
        if tab == "recovery":
            return self._recovery_path
        name = names.get(tab)
        return self.root / name if name else None

    @staticmethod
    def _file_cursor(path: Path) -> tuple[object, ...]:
        try:
            details = path.lstat()
        except OSError:
            return (None, None, 0, 0)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return (None, None, 0, 0)
        return (details.st_dev, details.st_ino, details.st_mtime_ns, details.st_size)

    def _source_cursor(self, tab: str) -> tuple[object, ...]:
        if tab == "online-usage":
            return ("online", self._online_usage_revision, len(self._online_usage_records))
        if tab == "recovery":
            return (
                "state",
                self._file_cursor(self._recovery_path),
                self._file_cursor(self._cooldowns_path),
            )
        path = self._path(tab)
        if path is None:
            return ("none",)
        try:
            details = path.lstat()
        except OSError:
            return ("file", None, None, 0, 0)
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            return ("file", None, None, 0, 0)
        return (
            "file",
            details.st_dev,
            details.st_ino,
            details.st_mtime_ns,
            details.st_size,
        )

    def _read_lines(
        self,
        path: Path,
        *,
        complete_document: bool = False,
        line_limit: int | None = None,
        all_lines: bool = False,
    ) -> list[str]:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LogsDomainError("Log source is unavailable")
        try:
            with path.open("rb") as handle:
                if details.st_size > self.maximum_read_bytes:
                    if complete_document:
                        return []
                    handle.seek(-self.maximum_read_bytes, os.SEEK_END)
                    handle.readline()
                data = handle.read(self.maximum_read_bytes)
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None
        lines = data.decode("utf-8", errors="replace").splitlines()
        if complete_document or all_lines:
            return lines
        if line_limit is None:
            self._refresh_runtime_settings()
            line_limit = self._runtime_line_limit
        return lines[-line_limit:]

    def _read_current_and_previous_lines(self, path: Path) -> list[str]:
        """Read the bounded current and immediately previous log segments.

        The proxy keeps the previous segment at ``.1`` when it rotates the
        service log. Allocate the shared read budget newest-first, then return
        the segments in chronological file order.
        """
        remaining = self.maximum_read_bytes
        segments: list[list[str]] = []
        for source in (path, path.with_name(f"{path.name}.1")):
            if remaining <= 0:
                break
            try:
                details = source.lstat()
            except FileNotFoundError:
                continue
            except OSError:
                raise LogsDomainError("Log source is unavailable") from None
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise LogsDomainError("Log source is unavailable")
            read_bytes = min(details.st_size, remaining)
            if read_bytes == 0:
                segments.append([])
                continue
            try:
                with source.open("rb") as handle:
                    start = details.st_size - read_bytes
                    if start > 0:
                        handle.seek(start - 1)
                        if handle.read(1) != b"\n":
                            handle.readline()
                    data = handle.read(read_bytes)
            except OSError:
                raise LogsDomainError("Log source is unavailable") from None
            segments.append(data.decode("utf-8", errors="replace").splitlines())
            remaining -= read_bytes
        return [line for segment in reversed(segments) for line in segment]

    def _read_lines_since(
        self,
        path: Path,
        offset: int,
        *,
        line_limit: int | None = None,
    ) -> list[str]:
        try:
            details = path.lstat()
        except FileNotFoundError:
            return []
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise LogsDomainError("Log source is unavailable")
        if offset >= details.st_size:
            return []
        try:
            with path.open("rb") as handle:
                start = offset
                if details.st_size - offset > self.maximum_read_bytes:
                    start = details.st_size - self.maximum_read_bytes
                if start > 0:
                    handle.seek(start - 1)
                    if handle.read(1) != b"\n":
                        handle.readline()
                data = handle.read(self.maximum_read_bytes)
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None
        lines = data.decode("utf-8", errors="replace").splitlines()
        return lines if line_limit is None else lines[-line_limit:]

    def _lines_for_cleared_file(
        self,
        tab: str,
        path: Path,
        *,
        line_limit: int | None = None,
        all_lines: bool = False,
    ) -> list[str]:
        cursor = self._clear_cursors.get(tab)
        if not cursor or cursor[0] != "file":
            return self._read_lines(path, line_limit=line_limit, all_lines=all_lines)
        current = self._source_cursor(tab)
        if current[0] != "file":
            return []
        same_file = current[1:3] == cursor[1:3]
        current_mtime = current[3]
        current_size = current[4]
        baseline_mtime = cursor[3]
        baseline_size = cursor[4]
        if not same_file or current_size < baseline_size:
            return self._read_lines(path, line_limit=line_limit, all_lines=all_lines)
        if current_size == baseline_size and current_mtime == baseline_mtime:
            return []
        if current_size == baseline_size:
            return self._read_lines(path, line_limit=line_limit, all_lines=all_lines)
        return self._read_lines_since(
            path,
            baseline_size,
            line_limit=None if all_lines else line_limit,
        )

    def _discard_clear(self, tab: str) -> None:
        self._cleared.discard(tab)
        self._clear_cursors.pop(tab, None)

    def _clear_recovery_and_cooldowns(self) -> None:
        """Clear recovery and deployment cooldown state without other caches."""

        def clear_collection(path: Path, key: str) -> None:
            lock_descriptor: int | None = None
            try:
                path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                if fcntl is not None:
                    lock_path = Path(f"{path}.lock")
                    lock_descriptor = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
                    try:
                        os.chmod(lock_path, 0o600)
                    except OSError:
                        pass
                    fcntl.flock(lock_descriptor, fcntl.LOCK_EX)
                payload = read_json(path, default={})
                payload["schema_version"] = 1
                payload[key] = {}
                atomic_write_json(path, payload)
            finally:
                if lock_descriptor is not None:
                    try:
                        fcntl.flock(lock_descriptor, fcntl.LOCK_UN)
                    finally:
                        os.close(lock_descriptor)

        try:
            clear_collection(self._recovery_path, "recoveries")
            clear_collection(self._cooldowns_path, "cooldowns")
        except (OSError, PersistenceError) as exc:
            raise LogsDomainError("Recovery and cooldown state could not be cleared") from exc

    def _record_menu_action(self, value: object) -> None:
        action = value if isinstance(value, str) else ""
        if action.startswith("open-logs?tab="):
            tab = action.partition("=")[2]
            valid = tab in LOG_TABS
        else:
            valid = action in MENU_ACTIONS
        if not valid:
            raise LogsDomainError("Menu action is invalid")
        path = self._path("actions")
        if path is None:
            raise LogsDomainError("Log source is unavailable")
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        line = f"[{stamp}] [INFO] {action}\n".encode("utf-8")
        try:
            self._refresh_runtime_settings()
            append_bounded_log(
                str(path),
                line,
                maximum_bytes=self._runtime_log_max_bytes,
                backup_segments=self._runtime_log_backup_segments,
            )
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None

    def _records(self, tab: str) -> tuple[bool, list[object]]:
        if tab == "online-usage":
            records: list[object] = list(self._online_usage_records)
            cursor = self._clear_cursors.get(tab) if tab in self._cleared else None
            if cursor and cursor[0] == "online":
                current = self._source_cursor(tab)
                if current[0] == "online" and current[1] == cursor[1] and current[2] >= cursor[2]:
                    records = records[int(cursor[2]) :]
            needle = self._filters.get(tab, "").casefold()
            if needle:
                records = [record for record in records if needle in str(record).casefold()]
            return self._online_usage_refreshed, _sort_records_by_time(records)[-self._line_limit(tab) :]
        path = self._path(tab)
        if path is None:
            return False, []
        if tab == "requests" and tab not in self._cleared:
            configured_deployments = _configured_deployments(self.config_path)
            records = self._requests_records(
                path,
                _configured_public_models_from(configured_deployments),
                _configured_route_public_models_from(configured_deployments),
            )
            records = _collapse_request_records(records)
            self._refresh_runtime_settings()
            stale_seconds = self._request_stale_seconds
            if stale_seconds > 0:
                now = time.time()
                records = [
                    _close_stale_request_record(
                        record,
                        stale_seconds=stale_seconds,
                        now=now,
                    )
                    for record in records
                ]
            needle = self._filters.get(tab, "").casefold()
            if needle:
                records = [
                    record
                    for record in records
                    if needle
                    in json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
                ]
            return path.exists(), _sort_records_by_time(records)[-self._line_limit(tab) :]
        if tab == "recovery":
            source_cursor = self._source_cursor(tab)
            clear_cursor = self._clear_cursors.get(tab)
            if tab in self._cleared and clear_cursor == source_cursor:
                return self._recovery_path.is_file() or self._cooldowns_path.is_file(), []
            recovery_lines = self._read_lines(self._recovery_path, complete_document=True)
            cooldown_lines = self._read_lines(self._cooldowns_path, complete_document=True)
            try:
                recovery_payload = json.loads("\n".join(recovery_lines))
            except (TypeError, json.JSONDecodeError):
                recovery_payload = {}
            try:
                cooldown_payload = json.loads("\n".join(cooldown_lines))
            except (TypeError, json.JSONDecodeError):
                cooldown_payload = {}
            configured_deployments = _configured_deployments(self.config_path)
            now = time.time()
            records: list[object] = []
            recoveries = recovery_payload.get("recoveries") if isinstance(recovery_payload, Mapping) else None
            for value in active_recovery_states(recoveries, now=now):
                projected = _safe_recovery_record(value, configured_deployments, now=now)
                if projected:
                    records.append(projected)
            cooldowns = cooldown_payload.get("cooldowns") if isinstance(cooldown_payload, Mapping) else None
            for cooldown_key, value in active_cooldown_states(cooldowns, now=now):
                remaining = max(0.0, float(value.get("cooldown_until") or 0) - now)
                projected = _safe_cooldown_record(
                    value,
                    cooldown_key,
                    configured_deployments,
                    remaining_seconds=remaining,
                    now=now,
                )
                if projected:
                    records.append(projected)
            needle = self._filters.get(tab, "").casefold()
            if needle:
                records = [
                    record
                    for record in records
                    if needle in json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
                ]
            return (
                self._recovery_path.is_file() or self._cooldowns_path.is_file(),
                _sort_records_by_time(records)[-self._line_limit(tab) :],
            )
        if tab == "route-trace":
            # Route traces are sparse relative to the ordinary proxy output.
            # Filter across both retained rotation segments before applying the
            # view limit, otherwise an old trace disappears as soon as enough
            # non-trace service output is written.
            lines = (
                self._lines_for_cleared_file(tab, path, all_lines=True)
                if tab in self._cleared
                else self._read_current_and_previous_lines(path)
            )
            lines = [line for line in lines if "litellm_route_trace" in line]
        else:
            line_limit = (
                min(MAX_LINES, self._line_limit(tab) * 4)
                if tab == "service"
                else self._line_limit(tab)
            )
            lines = (
                self._lines_for_cleared_file(tab, path, line_limit=line_limit)
                if tab in self._cleared
                else self._read_lines(path, line_limit=line_limit)
            )
        if tab == "service":
            # ``_group_service_lines`` already drops the route trace, the
            # startup banner, and the console noise; the repeats left behind
            # are the per-worker copies of one event.
            lines = _collapse_service_repeats(_group_service_lines(lines))
        configured_deployments = (
            _configured_deployments(self.config_path)
            if tab in {"requests", "route-trace"}
            else {}
        )
        configured_public_models = _configured_public_models_from(configured_deployments)
        configured_route_models = _configured_route_public_models_from(
            configured_deployments
        )
        records: list[object] = []
        for line in lines:
            if not line.strip():
                continue
            parsed: object | None = None
            if tab in {"requests", "route-trace"}:
                try:
                    source = line
                    if tab == "route-trace":
                        source = line.split("litellm_route_trace ", 1)[-1]
                    parsed = json.loads(source)
                except (TypeError, json.JSONDecodeError):
                    parsed = None
            if isinstance(parsed, Mapping):
                if tab == "route-trace" and not any(
                    parsed.get(key) not in (None, "")
                    for key in (
                        "ts",
                        "timestamp",
                        "time",
                        "created_at",
                        "updated_at",
                        "checked_at",
                        "heartbeat_at",
                        "started_at",
                    )
                ):
                    timestamp = _leading_timestamp(line)
                    if timestamp:
                        parsed = {**parsed, "timestamp": timestamp}
                record = (
                    _safe_request_record(
                        parsed,
                        configured_public_models,
                        configured_route_models,
                    )
                    if tab == "requests"
                    else _safe_route_trace_record(
                        parsed,
                        configured_public_models,
                        configured_route_models,
                    )
                )
                if record:
                    records.append(record)
            elif tab not in {"requests", "route-trace"}:
                records.append(
                    REDACT_TEXT(ANSI_CONTROL_SEQUENCE.sub("", line))[:MAX_TEXT_LOG_CHARS]
                )
        if tab == "requests":
            records = _collapse_request_records(records)
            self._refresh_runtime_settings()
            stale_seconds = self._request_stale_seconds
            if stale_seconds > 0:
                now = time.time()
                records = [
                    _close_stale_request_record(
                        record,
                        stale_seconds=stale_seconds,
                        now=now,
                    )
                    for record in records
                ]
        needle = self._filters.get(tab, "").casefold()
        if needle:
            records = [
                record
                for record in records
                if needle in json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
            ]
        available = path.exists() or (
            tab == "route-trace" and path.with_name(f"{path.name}.1").exists()
        )
        return available, _sort_records_by_time(records)[-self._line_limit(tab) :]

    def _source_signature(self, tab: str) -> tuple[object, ...]:
        if tab == "recovery":
            source: tuple[object, ...] = (*self._source_cursor(tab), int(time.time()))
            return (
                *source,
                self._filters.get(tab, ""),
                self._line_limit(tab),
                tab in self._cleared,
            )
        path = self._path(tab)
        if path is None:
            source = self._source_cursor(tab)[1:]
        elif tab == "route-trace":
            source = (
                self._file_cursor(path.with_name(f"{path.name}.1")),
                self._file_cursor(path),
            )
        else:
            try:
                details = path.stat()
                source = (details.st_mtime_ns, details.st_size)
            except OSError:
                source = (-1, -1)
        return (
            *source,
            self._filters.get(tab, ""),
            self._line_limit(tab),
            tab in self._cleared,
        )

    def _refresh(self, tab: str, *, force: bool = False) -> None:
        if tab in self._paused:
            previous = dict(
                self._tabs.get(
                    tab,
                    self._empty_tab(tab),
                )
            )
            previous["paused"] = True
            previous["filter"] = self._filters.get(tab, "")
            if tab in self._cleared:
                previous["records"] = []
                previous["line_count"] = 0
            self._store_tab(tab, self._fit_view_records(previous))
            return
        signature = self._source_signature(tab)
        if not force and self._source_signatures.get(tab) == signature:
            return
        available, records = self._records(tab)
        self._store_tab(tab, self._fit_view_records({
            "tab": tab,
            "available": available,
            "paused": tab in self._paused,
            "line_count": len(records),
            "records": records,
            "filter": self._filters.get(tab, ""),
            "limit": self._line_limit(tab),
        }))
        self._source_signatures[tab] = signature

    def _store_tab(self, tab: str, value: dict[str, Any]) -> None:
        if self._tabs.get(tab) == value:
            return
        self._tabs[tab] = value
        self._view_revisions[tab] += 1

    @staticmethod
    def _fit_view_records(value: dict[str, Any]) -> dict[str, Any]:
        records = value.get("records")
        if not isinstance(records, list) or not records:
            return value
        encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) <= MAX_VIEW_BYTES:
            return value
        metadata = dict(value)
        metadata["records"] = []
        metadata_size = len(json.dumps(metadata, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        budget = max(0, MAX_VIEW_BYTES - metadata_size - 2)
        selected: list[object] = []
        used = 0
        for record in reversed(records):
            record_size = len(json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
            extra = record_size + (1 if selected else 0)
            if used + extra > budget:
                break
            selected.append(record)
            used += extra
        metadata["records"] = list(reversed(selected))
        metadata["line_count"] = len(metadata["records"])
        return metadata

    def snapshot(self) -> dict[str, Any]:
        return {
            "domain": self.name,
            "revision": self.revision,
            "tabs": {
                tab: {
                    key: value
                    for key, value in self._tabs[tab].items()
                    if key != "records"
                }
                for tab in LOG_TABS
            },
        }

    def draft_state(self) -> object:
        return {}

    def view(self, tab: str, known_revision: int | None = None) -> dict[str, Any]:
        if tab not in LOG_TABS:
            raise LogsDomainError("Log tab is invalid")
        self._refresh(tab)
        revision = self._view_revisions[tab]
        changed = known_revision != revision
        return {
            "changed": changed,
            "revision": revision,
            "log": dict(self._tabs[tab]) if changed else None,
        }

    def dispatch(self, action: str, payload: object | None = None) -> dict[str, Any]:
        data = dict(payload) if isinstance(payload, Mapping) else {}
        tab = data.get("tab")
        if tab not in LOG_TABS:
            raise LogsDomainError("Log tab is invalid")
        operation = action.removeprefix("logs.").replace("-", "_")
        if tab == "requests":
            # A view action can change what the tab shows (filter, limit,
            # pause, clear) — never serve it from a window parsed under the
            # previous intent.
            with self._request_tail_lock:
                self._request_tail = {}
        if operation == "record_menu_action":
            if tab != "actions":
                raise LogsDomainError("Log tab is invalid")
            self._record_menu_action(data.get("menu_action"))
        elif operation == "pause":
            self._paused.add(str(tab))
        elif operation == "resume":
            self._paused.discard(str(tab))
            self._discard_clear(str(tab))
        elif operation == "clear":
            # Clear only the view. Core never deletes diagnostic files without
            # a separate, explicit destructive operation.
            self._cleared.add(str(tab))
            self._clear_cursors[str(tab)] = self._source_cursor(str(tab))
        elif operation == "clear_recovery_and_cooldowns":
            if tab != "recovery":
                raise LogsDomainError("Log tab is invalid")
            self._clear_recovery_and_cooldowns()
            self._discard_clear("recovery")
        elif operation in {"set_filter", "filter"}:
            value = data.get("filter", data.get("query", ""))
            # The bound is the same 256 the IPC contract states: a filter is
            # text, and counting its bytes refused a filter the schema had just
            # accepted (86 CJK characters) with a message the pane could not
            # translate.
            if not isinstance(value, str) or len(value) > MAX_FILTER_CHARS:
                raise LogsDomainError("Log filter is invalid")
            self._filters[str(tab)] = value.strip()
            self._discard_clear(str(tab))
        elif operation in {"set_limit", "limit"}:
            value = data.get("limit")
            if type(value) is not int or not 1 <= value <= MAX_LINES:
                raise LogsDomainError("Log line limit is invalid")
            self._limits[str(tab)] = value
            self._discard_clear(str(tab))
        elif operation in {"refresh", "reload", "refresh_online_usage"}:
            self._discard_clear(str(tab))
            if tab == "online-usage":
                try:
                    reader = self._online_usage_reader
                    if reader is None:
                        from ..operations import OnlineUsageReader

                        reader = OnlineUsageReader(self.config_path)
                    refresh = getattr(reader, "refresh", None)
                    values = refresh() if callable(refresh) else []
                    self._online_usage_records = [
                        REDACT_TEXT(str(value))[:512]
                        for value in values
                        if isinstance(value, str) and value.strip()
                    ][-self._line_limit(str(tab)) :]
                except Exception:
                    self._online_usage_records = ["Online usage logs are unavailable."]
                self._online_usage_refreshed = True
                self._online_usage_revision += 1
        else:
            raise LogsDomainError("Log action is unavailable")
        self.revision += 1
        self._refresh(str(tab), force=True)
        return self.snapshot()

    def validate(self, payload: object | None = None) -> dict[str, Any]:
        return {"valid": True, "errors": []}

    def apply(self, payload: object | None = None) -> dict[str, Any]:
        return {"applied": True, **self.snapshot()}

    def reload(self) -> dict[str, Any]:
        self._cleared.clear()
        self._clear_cursors.clear()
        self._source_signatures.clear()
        with self._request_tail_lock:
            self._request_tail = {}
        self._view_revisions = {
            tab: self._view_revisions.get(tab, 0) + 1 for tab in LOG_TABS
        }
        self._tabs = {tab: self._empty_tab(tab) for tab in LOG_TABS}
        self.revision += 1
        return self.snapshot()


__all__ = ["DOMAIN_NAME", "LOG_TABS", "LogsDomain", "LogsDomainError"]
