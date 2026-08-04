"""Bounded, redacted log views owned by the Python Core."""

from __future__ import annotations

import json
import os
from pathlib import Path
import re
import stat
from datetime import datetime, timezone
from collections.abc import Mapping
from typing import Any

from ..security import REDACT_TEXT
from ...log_rotation import append_bounded_log


DOMAIN_NAME = "logs"
LOG_TABS = (
    "requests",
    "service",
    "menu",
    "route-trace",
    "recovery",
    "online-usage",
)
MAX_READ_BYTES = 16 * 1024 * 1024
# The local IPC transport caps one JSON message at 4 MiB. Leave headroom for
# the response envelope while still returning the newest useful log rows.
MAX_VIEW_BYTES = 3 * 1024 * 1024
DEFAULT_LINES = 10_000
MAX_LINES = 100_000
MAX_FILTER_BYTES = 256
MENU_ACTIONS = frozenset(
    {
        "open-providers-models",
        "open-runtime-settings",
        "open-codex-settings",
        "open-claude-settings",
        "open-relay-accounts",
        "open-webdav-settings",
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


class LogsDomainError(ValueError):
    """A source-safe log operation error."""


def _runtime_root(value: Path | str | None) -> Path:
    if value is not None:
        return Path(value).expanduser()
    configured = os.environ.get("LITELLM_RUNTIME_ROOT", "").strip() or os.environ.get(
        "LITELLM_MENU_HOME", ""
    ).strip()
    return Path(configured).expanduser() if configured else Path.home() / ".litellm-menu"


def _safe_scalar(value: object, limit: int = 160) -> object:
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return REDACT_TEXT(value)[:limit]
    return None


def _public_model_from_route_key(value: object) -> str:
    if not isinstance(value, str):
        return ""
    match = re.search(r"(?:^|/)\s*model\s*=\s*([^/]+?)\s*(?=/|$)", value)
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


def _configured_public_models(config_path: Path) -> dict[str, str]:
    return {
        deployment_id: deployment["public_model"]
        for deployment_id, deployment in _configured_deployments(config_path).items()
        if deployment["public_model"]
    }


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


def _configured_deployments(config_path: Path) -> dict[str, dict[str, str]]:
    from config_editor_core import load as config_load

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


def _recovery_detail(raw: Mapping[str, Any]) -> str:
    details: list[str] = []
    for source, key, suffix in (
        (raw.get("attempt"), "attempt", ""),
        (raw.get("attempt_timeout_seconds"), "timeout", "s"),
        (raw.get("cooldown_remaining_seconds"), "cooldown", "s"),
        (raw.get("poll_interval_seconds"), "retry", "s"),
    ):
        value = _trace_value(source)
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
        route_public_model
        or configured.get("public_model", "")
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
    result: dict[str, Any] = {
        "timestamp": _safe_scalar(raw.get("updated_at") or raw.get("heartbeat_at") or raw.get("started_at")),
        "public_model": _safe_scalar(public_model),
        "upstream_model": _safe_scalar(upstream_model),
        "provider": _safe_scalar(provider),
        "api_key_name": _safe_scalar(api_key_name),
        "status": _safe_scalar(raw.get("status")),
        "detail": _safe_scalar(_recovery_detail(raw), limit=260),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_route_trace_record(
    raw: Mapping[str, Any],
    configured_public_models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    request = _mapping_value(raw.get("request"))
    deployment = _mapping_value(raw.get("deployment"))
    exception = _mapping_value(raw.get("exception") or raw.get("error"))
    route_key = (
        raw.get("route_key")
        or deployment.get("route_key")
        or request.get("route_key")
        or exception.get("failed_deployment_route_key")
        or exception.get("failed_route_key")
    )
    deployment_id = (
        raw.get("deployment_id")
        or deployment.get("id")
        or request.get("deployment_id")
        or exception.get("failed_deployment_id")
    )
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
    if configured_public_model and not route_public_model:
        public_model = configured_public_model
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
    status = _string_record_value(raw.get("status") or raw.get("result"))
    if not status and (raw.get("exception") is not None or raw.get("error") is not None):
        status = "error"
    result: dict[str, Any] = {
        "timestamp": _safe_scalar(raw.get("timestamp") or raw.get("ts")),
        "event": _safe_scalar(raw.get("event")),
        "public_model": _safe_scalar(public_model),
        "upstream_model": _safe_scalar(upstream_model),
        "provider": _safe_scalar(provider),
        "status": _safe_scalar(status),
        "detail": _safe_scalar(_trace_detail(raw), limit=260),
    }
    return {key: value for key, value in result.items() if value not in (None, "")}


def _safe_request_record(
    raw: Mapping[str, Any],
    configured_public_models: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key in (
        "ts",
        "timestamp",
        "status",
        "model_group",
        "public_model",
        "provider",
        "api_key_name",
        "upstream_model",
        "duration_ms",
        "route_key",
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
    deployment_id = raw.get("deployment_id")
    configured_public_model = (
        configured_public_models.get(deployment_id.strip())
        if configured_public_models
        and isinstance(deployment_id, str)
        and deployment_id.strip()
        else None
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
        result["usage"] = {
            key: value
            for key in ("input_tokens", "output_tokens", "total_tokens")
            if isinstance((value := usage.get(key)), (int, float)) and not isinstance(value, bool)
        }
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
    return result


def _is_service_noise(line: str) -> bool:
    message = SERVICE_TIMESTAMP_PREFIX.sub("", line).strip()
    if not message:
        return True
    lowered = message.casefold()
    if (
        "github.com/berriai/litellm/issues/new" in lowered
        or lowered.startswith("thank you for using litellm")
        or lowered.startswith("give feedback / get help")
    ):
        return True
    if message.startswith("#") and message.endswith("#"):
        return True
    if re.fullmatch(r"[#_|/\\=+* .:-]+", message):
        return True
    # LiteLLM prints configured model identifiers as bare banner lines. Real
    # service events carry a severity, PID, timestamp, or explanatory text.
    if re.fullmatch(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.:/-]+", message):
        return True
    return False


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
        self._online_usage_records: list[str] = []
        self._online_usage_refreshed = False
        self.maximum_lines = max(1, min(int(maximum_lines), MAX_LINES))
        self.maximum_read_bytes = max(4096, min(int(maximum_read_bytes), MAX_READ_BYTES))
        self.revision = 0
        self._paused: set[str] = set()
        self._cleared: set[str] = set()
        self._filters: dict[str, str] = {}
        self._limits: dict[str, int] = {}
        self._source_signatures: dict[str, tuple[object, ...]] = {}
        self._view_revisions: dict[str, int] = {tab: 0 for tab in LOG_TABS}
        self._tabs: dict[str, dict[str, Any]] = {
            tab: self._empty_tab(tab) for tab in LOG_TABS
        }

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

    def _default_line_limit(self) -> int:
        try:
            details = self.runtime_settings_path.stat()
            signature = (details.st_mtime_ns, details.st_size)
        except OSError:
            signature = (-1, -1)
        if signature == self._runtime_settings_signature:
            return self._runtime_line_limit
        self._runtime_settings_signature = signature
        try:
            from runtime_settings_io import load_specs, read_settings_file

            values = read_settings_file(self.runtime_settings_path, load_specs())
            configured = int(values.get("LITELLM_MENU_LOG_VIEW_LIMIT", DEFAULT_LINES))
            self._runtime_line_limit = max(1, min(configured, MAX_LINES))
        except (OSError, TypeError, ValueError):
            self._runtime_line_limit = self.maximum_lines
        return self._runtime_line_limit

    def _line_limit(self, tab: str) -> int:
        return self._limits.get(tab, self._default_line_limit())

    def _path(self, tab: str) -> Path | None:
        names = {
            "requests": "recent-requests.jsonl",
            "service": "menu-server.log",
            "menu": "menu-actions.log",
            "route-trace": "menu-server.log",
            "recovery": ".litellm-runtime/route-recovery-state.json",
        }
        name = names.get(tab)
        return self.root / name if name else None

    def _read_lines(
        self,
        path: Path,
        *,
        complete_document: bool = False,
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
        return lines if complete_document else lines[-(line_limit or self._default_line_limit()) :]

    def _record_menu_action(self, value: object) -> None:
        action = value if isinstance(value, str) else ""
        if action.startswith("open-logs?tab="):
            tab = action.partition("=")[2]
            valid = tab in LOG_TABS
        else:
            valid = action in MENU_ACTIONS
        if not valid:
            raise LogsDomainError("Menu action is invalid")
        path = self._path("menu")
        if path is None:
            raise LogsDomainError("Log source is unavailable")
        stamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
        line = f"[{stamp}] [INFO] {action}\n".encode("utf-8")
        try:
            append_bounded_log(str(path), line)
        except OSError:
            raise LogsDomainError("Log source is unavailable") from None

    def _records(self, tab: str) -> tuple[bool, list[object]]:
        if tab == "online-usage":
            records: list[object] = list(self._online_usage_records)
            needle = self._filters.get(tab, "").casefold()
            if needle:
                records = [record for record in records if needle in str(record).casefold()]
            return self._online_usage_refreshed, records[-self._line_limit(tab) :]
        path = self._path(tab)
        if path is None:
            return False, []
        lines = self._read_lines(
            path,
            complete_document=tab == "recovery",
            line_limit=self._line_limit(tab),
        )
        if tab == "recovery":
            records: list[object] = []
            try:
                payload = json.loads("\n".join(lines))
            except (TypeError, json.JSONDecodeError):
                payload = {}
            recoveries = payload.get("recoveries") if isinstance(payload, Mapping) else None
            configured_deployments = _configured_deployments(self.config_path)
            if isinstance(recoveries, Mapping):
                records = [
                    projected
                    for value in recoveries.values()
                    if isinstance(value, Mapping)
                    and (projected := _safe_recovery_record(value, configured_deployments))
                ]
            needle = self._filters.get(tab, "").casefold()
            if needle:
                records = [
                    record
                    for record in records
                    if needle in json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
                ]
            return path.exists(), records[-self._line_limit(tab) :]
        if tab == "route-trace":
            lines = [line for line in lines if "litellm_route_trace" in line]
        elif tab == "service":
            lines = [
                line
                for line in lines
                if "litellm_route_trace" not in line and not _is_service_noise(line)
            ]
        configured_public_models = (
            _configured_public_models(self.config_path)
            if tab in {"requests", "route-trace"}
            else {}
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
                record = (
                    _safe_request_record(parsed, configured_public_models)
                    if tab == "requests"
                    else _safe_route_trace_record(parsed, configured_public_models)
                )
                if record:
                    records.append(record)
            elif tab not in {"requests", "route-trace"}:
                records.append(REDACT_TEXT(ANSI_CONTROL_SEQUENCE.sub("", line))[:512])
        needle = self._filters.get(tab, "").casefold()
        if needle:
            records = [
                record
                for record in records
                if needle in json.dumps(record, ensure_ascii=False, sort_keys=True).casefold()
            ]
        return path.exists(), records[-self._line_limit(tab) :]

    def _source_signature(self, tab: str) -> tuple[object, ...]:
        path = self._path(tab)
        if path is None:
            source: tuple[object, ...] = (
                self._online_usage_refreshed,
                len(self._online_usage_records),
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
        if tab in self._cleared:
            records = []
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
        if operation == "record_menu_action":
            if tab != "menu":
                raise LogsDomainError("Log tab is invalid")
            self._record_menu_action(data.get("menu_action"))
            self._cleared.discard("menu")
        elif operation == "pause":
            self._paused.add(str(tab))
        elif operation == "resume":
            self._paused.discard(str(tab))
            self._cleared.discard(str(tab))
        elif operation == "clear":
            # Clear only the view. Core never deletes diagnostic files without
            # a separate, explicit destructive operation.
            self._cleared.add(str(tab))
        elif operation in {"set_filter", "filter"}:
            value = data.get("filter", data.get("query", ""))
            if not isinstance(value, str) or len(value.encode("utf-8")) > MAX_FILTER_BYTES:
                raise LogsDomainError("Log filter is invalid")
            self._filters[str(tab)] = value.strip()
            self._cleared.discard(str(tab))
        elif operation in {"set_limit", "limit"}:
            value = data.get("limit")
            if type(value) is not int or not 1 <= value <= MAX_LINES:
                raise LogsDomainError("Log line limit is invalid")
            self._limits[str(tab)] = value
            self._cleared.discard(str(tab))
        elif operation in {"refresh", "reload", "refresh_online_usage"}:
            self._cleared.discard(str(tab))
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
        self._source_signatures.clear()
        self._view_revisions = {
            tab: self._view_revisions.get(tab, 0) + 1 for tab in LOG_TABS
        }
        self._tabs = {tab: self._empty_tab(tab) for tab in LOG_TABS}
        self.revision += 1
        return self.snapshot()


__all__ = ["DOMAIN_NAME", "LOG_TABS", "LogsDomain", "LogsDomainError"]
