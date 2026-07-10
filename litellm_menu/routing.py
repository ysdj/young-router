from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_execution as _responses_execution_module
from . import responses_surfaces as _responses_surfaces_module
from . import state as _state_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    List,
    Optional,
    _CHAT_TOOL_NAME_PATTERN,
    _ATTEMPTED_UPSTREAM_URL_SURFACES_KEY,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_DEPLOYMENT_COOLDOWN_SURFACE,
    _CURRENT_UPSTREAM_URL_SURFACE_KEY,
    _CURRENT_SELECTED_DEPLOYMENT,
    _CURRENT_SELECTED_DEPLOYMENT_BOX,
    _DEPLOYMENT_COOLDOWNS,
    _DEPLOYMENT_COOLDOWN_DEFAULT_FAILURES,
    _DEPLOYMENT_COOLDOWN_DEFAULT_SECONDS,
    _DEPLOYMENT_COOLDOWN_FILE_ENV,
    _DEPLOYMENT_COOLDOWN_FAILURES_ENV,
    _DEPLOYMENT_COOLDOWN_FAILURE_RECORDED_ATTR,
    _DEPLOYMENT_COOLDOWN_LOCK,
    _DEPLOYMENT_COOLDOWN_SECONDS_ENV,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_LOCK,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_MAX,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_TTL_SECONDS,
    _LITELLM_MODEL_GROUP_FALLBACK_EXHAUSTED_MARKERS,
    _RESPONSES_IMAGE_INPUT_SUPPORT_KEY,
    _RECOVERY_INTERVAL_DEFAULT_SECONDS,
    _RECOVERY_INTERVAL_SECONDS_ENV,
    _RECOVERY_MAX_DEFAULT_SECONDS,
    _RECOVERY_MAX_SECONDS_ENV,
    _REQUEST_TIMEOUT_DEFAULT_SECONDS,
    _REQUEST_TIMEOUT_SECONDS_ENV,
    _ROUTE_RECOVERY_POLL_METADATA_KEY,
    _RouteRecoveryStreamResponse,
    _SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR,
    _SANITIZED_UPSTREAM_ROUTE_FAILURE_STATUS_CODE,
    _SESSION_ID_KEY_FRAGMENTS,
    _SESSION_NAME_KEY_FRAGMENTS,
    _STALL_TIMEOUT_DEFAULT_SECONDS,
    _STALL_TIMEOUT_SECONDS_ENV,
    _STREAM_ROUTE_EXHAUSTION_DEFAULT_RETRIES,
    _STREAM_ROUTE_EXHAUSTION_RETRY_AFTER_MAX_SECONDS,
    _SUPPORTED_UPSTREAM_URL_SURFACES_KEY,
    _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY,
    _SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY,
    _SUPPORTS_RESPONSES_WEB_SEARCH_KEY,
    _SUPPORTS_WEB_SEARCH_KEY,
    _TerminalFailedResponsesStreamResponse,
    _UPSTREAM_BALANCE_ERROR_MARKERS,
    _UPSTREAM_HTML_BAD_REQUEST_MARKERS,
    _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES,
    _UPSTREAM_TEMPORARY_ERROR_MARKERS,
    _SURFACE_TARGET_DEPLOYMENT_ID_KEY,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES,
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
    _WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY,
    _XHIGH_REASONING_EFFORT,
    asyncio,
    datetime,
    json,
    litellm,
    os,
    re,
    time,
    timezone,
)



def _event_time(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    return None


def _duration_ms(start_time: Any, end_time: Any) -> Optional[int]:
    if isinstance(start_time, datetime) and isinstance(end_time, datetime):
        try:
            return max(0, int((end_time - start_time).total_seconds() * 1000))
        except Exception:
            return None
    return None


def _stall_timeout_seconds() -> float:
    value = os.getenv(_STALL_TIMEOUT_SECONDS_ENV, "").strip()
    if not value:
        return _STALL_TIMEOUT_DEFAULT_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _STALL_TIMEOUT_DEFAULT_SECONDS
    return max(0.0, parsed)


def _request_timeout_seconds() -> float:
    value = os.getenv(_REQUEST_TIMEOUT_SECONDS_ENV, "").strip()
    if not value:
        return _REQUEST_TIMEOUT_DEFAULT_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _REQUEST_TIMEOUT_DEFAULT_SECONDS
    return max(0.0, parsed)


def _request_metadata_positive_float(
    request_data: Optional[dict],
    key: str,
) -> Optional[float]:
    if not isinstance(request_data, dict):
        return None
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(
            request_data,
            metadata_key,
        )
        if not isinstance(metadata, dict):
            continue
        parsed = _safe_float(metadata.get(key))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _stream_start_timeout_seconds_for_request(request_data: Optional[dict]) -> float:
    override = _request_metadata_positive_float(
        request_data,
        "route_recovery_attempt_timeout_seconds",
    )
    if override is not None:
        return override
    stall_timeout = _stall_timeout_seconds()
    request_timeout = _request_timeout_seconds()
    if stall_timeout <= 0:
        return request_timeout
    if request_timeout <= 0:
        return stall_timeout
    return min(stall_timeout, request_timeout)


def _stream_route_exhaustion_retries() -> int:
    return _STREAM_ROUTE_EXHAUSTION_DEFAULT_RETRIES


def _stream_route_exhaustion_retry_delay_seconds() -> float:
    return _recovery_interval_seconds()


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name, "").strip().lower()
    if not value:
        return default
    if value in {"1", "true", "yes", "on"}:
        return True
    if value in {"0", "false", "no", "off"}:
        return False
    return default


def _env_float_seconds(name: str, default: float, *, minimum: float = 0.0) -> float:
    value = os.getenv(name, "").strip()
    if not value:
        return default
    try:
        parsed = float(value)
    except ValueError:
        return default
    if parsed < minimum:
        return default
    return parsed


def _recovery_max_seconds() -> float:
    return _env_float_seconds(
        _RECOVERY_MAX_SECONDS_ENV,
        _RECOVERY_MAX_DEFAULT_SECONDS,
        minimum=0.0,
    )


def _external_web_search_started_request_key(request_kwargs: Optional[dict]) -> Optional[str]:
    request_id = _trace_request_id(request_kwargs)
    if isinstance(request_id, str) and request_id.strip():
        return request_id.strip()
    return None


def _prune_external_web_search_started_requests(now: Optional[float] = None) -> None:
    current_time = time.monotonic() if now is None else now
    expired = [
        key
        for key, started_at in _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS.items()
        if current_time - started_at > _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_TTL_SECONDS
    ]
    for key in expired:
        _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS.pop(key, None)
    overflow = len(_EXTERNAL_WEB_SEARCH_STARTED_REQUESTS) - _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_MAX
    if overflow <= 0:
        return
    oldest_keys = sorted(
        _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS,
        key=lambda key: _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS.get(key, 0.0),
    )[:overflow]
    for key in oldest_keys:
        _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS.pop(key, None)


def _mark_external_web_search_started_for_request(request_kwargs: Optional[dict]) -> None:
    if isinstance(request_kwargs, dict):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
        if metadata.get(_WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY) is not True:
            updated_metadata = metadata.copy()
            updated_metadata[_WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY] = True
            request_kwargs["litellm_metadata"] = updated_metadata
    key = _external_web_search_started_request_key(request_kwargs)
    if key is None:
        return
    now = time.monotonic()
    with _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_LOCK:
        _prune_external_web_search_started_requests(now)
        _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS[key] = now


def _request_has_started_external_web_search(request_kwargs: Optional[dict]) -> bool:
    if isinstance(request_kwargs, dict):
        for metadata_key in ("litellm_metadata", "metadata"):
            metadata = _image_generation_module._request_metadata_dict(request_kwargs, metadata_key) or {}
            if metadata.get(_WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY) is True:
                return True
    key = _external_web_search_started_request_key(request_kwargs)
    if key is None:
        return False
    now = time.monotonic()
    with _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_LOCK:
        _prune_external_web_search_started_requests(now)
        return key in _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS


def _should_block_external_web_search_original_recovery(request_kwargs: Optional[dict]) -> bool:
    if not _request_has_started_external_web_search(request_kwargs):
        return False
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, metadata_key) or {}
        search_results = metadata.get("external_web_search_search_results")
        has_search_results = isinstance(search_results, str) and bool(search_results.strip())
        has_completed_actions = bool(metadata.get("external_web_search_completed_actions"))
        if metadata.get("external_web_search_synthesis") is True:
            return False
        if metadata.get("external_web_search_continuation") is True:
            return False
        if has_search_results or has_completed_actions:
            return False
    input_text = str((request_kwargs or {}).get("input") or "")
    if "Retrieved evidence" in input_text or "Retrieved evidence observed so far" in input_text:
        return False
    if not _tools_module._request_has_web_search_tool(request_kwargs):
        return False
    return _streaming_module._request_is_responses_stream(request_kwargs)


def _recovery_max_seconds_for_request(request_data: Optional[dict]) -> float:
    override = _request_metadata_positive_float(
        request_data,
        "route_recovery_max_seconds",
    )
    if override is not None:
        return override
    return _recovery_max_seconds()


def _recovery_interval_seconds() -> float:
    return _env_float_seconds(
        _RECOVERY_INTERVAL_SECONDS_ENV,
        _RECOVERY_INTERVAL_DEFAULT_SECONDS,
        minimum=0.001,
    )


def _is_route_recovery_poll_payload(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs,
            metadata_key,
        )
        if isinstance(metadata, dict) and metadata.get(_ROUTE_RECOVERY_POLL_METADATA_KEY) is True:
            return True
    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        for metadata_key in ("litellm_metadata", "metadata"):
            metadata = litellm_params.get(metadata_key)
            if isinstance(metadata, dict) and metadata.get(_ROUTE_RECOVERY_POLL_METADATA_KEY) is True:
                return True
    return False


def _is_route_recovery_poll_error(exception: Exception) -> bool:
    if _is_context_size_error(exception):
        return False
    if _is_upstream_gateway_bad_request_error(exception):
        return False
    if _is_deployment_compatible_bad_request_error(exception):
        return False
    if _is_no_deployments_available_error(exception):
        return True
    if _exception_indicates_network_connectivity_error(exception):
        return True
    if _responses_execution_module._failed_deployment_id(exception):
        return True
    if _is_upstream_deployment_failover_error(exception):
        return True
    status_code = _exception_status_code(exception)
    if status_code == 429:
        return True
    if type(exception).__name__ in _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES:
        return True
    if status_code == 408:
        return True
    if status_code is not None and status_code >= 500:
        return True
    text = _exception_text(exception)
    return any(marker in text for marker in _UPSTREAM_TEMPORARY_ERROR_MARKERS)


def _should_return_route_recovery_stream(
    exception: Exception,
    request_kwargs: Optional[dict],
    router: Any = None,
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if _is_route_recovery_poll_payload(request_kwargs):
        return False
    if request_kwargs.get("stream") is not True:
        return False
    if not _streaming_module._request_is_responses_stream(request_kwargs):
        return False
    if not _is_route_recovery_poll_error(exception):
        return False
    if _recovery_max_seconds() <= 0:
        return False
    if (
        router is not None
        and not _is_sanitized_upstream_route_failure_error(exception)
        and _responses_execution_module._ordered_deployment_fallback_entry(router, exception, request_kwargs)
    ):
        return False
    return True


def _route_recovery_stream_response(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    return _RouteRecoveryStreamResponse(request_data, exception)


def _is_route_recovery_stream_response(response: Any) -> bool:
    return isinstance(response, _RouteRecoveryStreamResponse)


def _failed_responses_stream_response(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    return _TerminalFailedResponsesStreamResponse(request_data, exception)


def _is_failed_responses_stream_response(response: Any) -> bool:
    return isinstance(response, _TerminalFailedResponsesStreamResponse)


def _is_sanitized_upstream_route_failure_error(exception: Exception) -> bool:
    return bool(getattr(exception, _SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR, False))


def _is_terminal_responses_stream_failure_error(exception: Exception) -> bool:
    if _is_sanitized_upstream_route_failure_error(exception):
        return True
    return _should_sanitize_final_upstream_route_error(exception)


def _should_return_failed_responses_stream(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if _is_route_recovery_poll_payload(request_kwargs):
        return False
    if request_kwargs.get("stream") is not True:
        return False
    if not _streaming_module._request_is_responses_stream(request_kwargs):
        return False
    return _is_terminal_responses_stream_failure_error(exception)


def _deployment_cooldown_failure_threshold() -> int:
    value = os.getenv(_DEPLOYMENT_COOLDOWN_FAILURES_ENV, "").strip()
    if not value:
        return _DEPLOYMENT_COOLDOWN_DEFAULT_FAILURES
    try:
        parsed = int(value)
    except ValueError:
        return _DEPLOYMENT_COOLDOWN_DEFAULT_FAILURES
    return max(0, parsed)


def _deployment_cooldown_seconds() -> float:
    value = os.getenv(_DEPLOYMENT_COOLDOWN_SECONDS_ENV, "").strip()
    if not value:
        return _DEPLOYMENT_COOLDOWN_DEFAULT_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _DEPLOYMENT_COOLDOWN_DEFAULT_SECONDS
    return max(0.0, parsed)


def _deployment_cooldown_enabled() -> bool:
    return _deployment_cooldown_failure_threshold() > 0 and _deployment_cooldown_seconds() > 0


def _deployment_cooldown_file_path() -> Optional[str]:
    return _state_module._deployment_cooldown_file_path()


def _deployment_cooldown_state_map(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("schema_version", 1)
    cooldowns = payload.setdefault("cooldowns", {})
    if not isinstance(cooldowns, dict):
        cooldowns = {}
        payload["cooldowns"] = cooldowns
    return cooldowns


def _clean_deployment_cooldown_state(
    state: Any,
    *,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    cleaned = dict(state)
    try:
        cooldown_until = float(cleaned.get("cooldown_until") or 0.0)
    except (TypeError, ValueError):
        cooldown_until = 0.0
    if cooldown_until > 0 and now is not None and cooldown_until <= now:
        return None
    try:
        failures = int(cleaned.get("failures") or 0)
    except (TypeError, ValueError):
        failures = 0
    cleaned["failures"] = max(0, failures)
    cleaned["cooldown_until"] = cooldown_until
    return cleaned


def _sync_deployment_cooldowns_from_shared_locked(
    cooldowns: dict[str, Any],
    now: float,
) -> None:
    shared: dict[str, dict[str, Any]] = {}
    expired_keys: list[str] = []
    for cooldown_key, state in list(cooldowns.items()):
        cleaned = _clean_deployment_cooldown_state(state, now=now)
        if cleaned is None:
            expired_keys.append(cooldown_key)
            continue
        shared[cooldown_key] = cleaned
        if cleaned is not state:
            cooldowns[cooldown_key] = cleaned
    for cooldown_key in expired_keys:
        cooldowns.pop(cooldown_key, None)

    with _DEPLOYMENT_COOLDOWN_LOCK:
        _DEPLOYMENT_COOLDOWNS.clear()
        _DEPLOYMENT_COOLDOWNS.update({key: value.copy() for key, value in shared.items()})


def _deployment_cooldown_update_shared(callback: Any) -> Any:
    path = _deployment_cooldown_file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        cooldowns = _deployment_cooldown_state_map(payload)
        _sync_deployment_cooldowns_from_shared_locked(cooldowns, now)
        result = callback(cooldowns, now)
        _sync_deployment_cooldowns_from_shared_locked(cooldowns, now)
        return result, now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        return None


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _nested_dict(value: dict[str, Any], *keys: str) -> dict[str, Any]:
    current: Any = value
    for key in keys:
        if not isinstance(current, dict):
            return {}
        current = current.get(key)
    return current if isinstance(current, dict) else {}


def _safe_float(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _first_not_none(*values: Any) -> Any:
    for value in values:
        if value is not None:
            return value
    return None


def _usage_summary(response_obj: Any, request_kwargs: dict[str, Any]) -> dict[str, Any]:
    usage = getattr(response_obj, "usage", None)
    if not isinstance(usage, dict) and hasattr(usage, "model_dump"):
        try:
            usage = usage.model_dump()
        except Exception:
            usage = None
    if not isinstance(usage, dict) and isinstance(response_obj, dict):
        usage = response_obj.get("usage")
    if not isinstance(usage, dict):
        usage = _nested_dict(request_kwargs, "standard_logging_object", "response", "usage")

    result: dict[str, Any] = {}
    if isinstance(usage, dict):
        for source_key, target_key in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("total_tokens", "total_tokens"),
            ("input_tokens", "input_tokens"),
            ("output_tokens", "output_tokens"),
        ):
            value = usage.get(source_key)
            if isinstance(value, int):
                result[target_key] = value
    return result


def _response_type_summary(response_obj: Any) -> List[str]:
    seen: List[str] = []
    for item_type in _image_generation_module._response_types(response_obj):
        if item_type not in seen:
            seen.append(item_type)
        if len(seen) >= 12:
            break
    return seen


def _request_log_exception(request_kwargs: dict[str, Any], response_obj: Any) -> Optional[Exception]:
    exception = request_kwargs.get("exception")
    if isinstance(exception, Exception):
        return exception
    if isinstance(response_obj, Exception):
        return response_obj
    return None


def _request_log_error_summary(
    request_kwargs: dict[str, Any],
    response_obj: Any,
) -> dict[str, Any]:
    exception = _request_log_exception(request_kwargs, response_obj)
    standard = _as_dict(request_kwargs.get("standard_logging_object"))
    error: dict[str, Any] = {}

    if exception is not None:
        traced = _trace_exception(exception)
        if traced.get("class"):
            error["type"] = traced.get("class")
        if traced.get("status_code") is not None:
            error["status_code"] = traced.get("status_code")
        if traced.get("reason"):
            error["reason"] = traced.get("reason")
        if traced.get("failed_deployment_id"):
            error["failed_deployment_id"] = traced.get("failed_deployment_id")
        if traced.get("failed_deployment_route_key"):
            error["failed_route_key"] = traced.get("failed_deployment_route_key")
        if traced.get("failed_deployment_order") is not None:
            error["failed_deployment_order"] = traced.get("failed_deployment_order")

    for source_key, target_key in (
        ("error_type", "type"),
        ("error_status", "status_code"),
        ("error_code", "code"),
    ):
        value = standard.get(source_key)
        if value is not None and target_key not in error:
            error[target_key] = value

    if "type" not in error:
        error_type = request_kwargs.get("exception_type") or request_kwargs.get("error_type")
        if error_type is not None:
            error["type"] = _state_module._safe_log_text(error_type, limit=120)
    if "reason" not in error and error.get("status_code") is not None:
        error["reason"] = f"upstream-status-{error['status_code']}"

    return {key: value for key, value in error.items() if value not in (None, "")}


def _should_suppress_recent_failure_log(
    request_kwargs: Optional[dict],
    response_obj: Any,
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    exception = _request_log_exception(request_kwargs, response_obj)
    if exception is None:
        return False
    return _responses_surfaces_module._responses_chat_bridge_retry_kwargs(exception, request_kwargs, None) is not None


def _request_log_record(
    status: str,
    request_kwargs: Optional[dict],
    response_obj: Any = None,
    start_time: Any = None,
    end_time: Any = None,
) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    litellm_params = _as_dict(request_kwargs.get("litellm_params"))
    standard = _as_dict(request_kwargs.get("standard_logging_object"))
    model_info = _image_generation_module._request_model_info(request_kwargs)
    api_base = _image_generation_module._request_api_base(request_kwargs)
    response_cost = _safe_float(
        _first_not_none(request_kwargs.get("response_cost"), standard.get("response_cost"))
    )
    provider = _first_not_none(
        model_info.get("provider"),
        request_kwargs.get("custom_llm_provider"),
        standard.get("model_provider"),
    )
    upstream_model = _first_not_none(
        litellm_params.get("model"),
        model_info.get("model"),
        standard.get("model"),
        request_kwargs.get("model"),
    )
    api_key_name = _state_module._safe_log_text(model_info.get("api_key_name"), limit=120)
    route_key = _state_module._safe_log_text(
        _deployment_route_key_from_request(request_kwargs),
        limit=260,
    )

    record: dict[str, Any] = {
        "ts": _event_time(end_time) or datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "status": status,
        "duration_ms": _duration_ms(start_time, end_time),
        "call_type": _state_module._safe_log_text(request_kwargs.get("call_type"), limit=80),
        "model_group": _state_module._safe_log_text(_responses_execution_module._request_model_group(request_kwargs), limit=160),
        "deployment_id": _state_module._safe_log_text(_deployment_id_from_request(request_kwargs), limit=180),
        "deployment_token": _state_module._safe_log_text(_deployment_id_from_request(request_kwargs), limit=180),
        "route_key": route_key,
        "deployment_order": _deployment_order_from_request(request_kwargs),
        "provider": _state_module._safe_log_text(provider, limit=120),
        "api_key_name": api_key_name,
        "upstream_model": _state_module._safe_log_text(upstream_model, limit=180),
        "api_base_host": _state_module._safe_log_text(_image_generation_module._api_base_host(api_base), limit=180),
        "request_id": _state_module._safe_log_text(_trace_request_id(request_kwargs), limit=180),
        "session": _trace_session_context(request_kwargs),
        "tool_types": _trace_module._trace_tool_types(request_kwargs.get("tools")),
        "tool_names": _trace_module._trace_tool_names(request_kwargs.get("tools")),
        "tool_choice": _state_module._safe_log_text(request_kwargs.get("tool_choice"), limit=120),
        "has_web_search_tool": _tools_module._request_has_web_search_tool(request_kwargs),
        "has_image_generation_tool": _tools_module._request_has_image_generation_tool(request_kwargs),
        "has_image_input": _image_generation_module._request_has_image_input(request_kwargs),
        "cache_hit": request_kwargs.get("cache_hit"),
        "response_cost": response_cost,
        "usage": _usage_summary(response_obj, request_kwargs),
        "response_types": _response_type_summary(response_obj),
    }

    if status in {"failure", "stuck"}:
        record["error"] = _request_log_error_summary(request_kwargs, response_obj)
    if status == "stuck":
        stuck: dict[str, Any] = {}
        reason = _state_module._safe_log_text(request_kwargs.get("stuck_reason"), limit=120)
        if reason:
            stuck["reason"] = reason
        timeout = _safe_float(request_kwargs.get("stream_idle_timeout_seconds"))
        if timeout is not None:
            stuck["stream_idle_timeout_seconds"] = timeout
        timeout = _safe_float(request_kwargs.get("stream_start_timeout_seconds"))
        if timeout is not None:
            stuck["stream_start_timeout_seconds"] = timeout
        saw_chunk = request_kwargs.get("stream_saw_chunk")
        if isinstance(saw_chunk, bool):
            stuck["stream_saw_chunk"] = saw_chunk
        buffered_chunks = request_kwargs.get("stream_buffered_chunks")
        if isinstance(buffered_chunks, int):
            stuck["stream_buffered_chunks"] = buffered_chunks
        if stuck:
            record["stuck"] = stuck

    return {key: value for key, value in record.items() if value not in (None, "", [], {})}


def _deployment_route_key(
    *,
    model_group: Any = None,
    provider: Any,
    model: Any,
    api_base: Any = None,
    api_key_name: Any = None,
    order: Any = None,
) -> str:
    parts = []
    public_model = str(model_group or "").strip()
    if public_model:
        parts.append(f"model={public_model}")
    parts.extend([
        f"provider={str(provider or 'unknown-provider').strip() or 'unknown-provider'}",
        f"upstream={str(model or 'unknown-model').strip() or 'unknown-model'}",
    ])
    host = _image_generation_module._api_base_host(str(api_base or "").strip())
    if host:
        parts.append(f"host={host}")
    key_name = str(api_key_name or "").strip()
    if key_name:
        parts.append(f"key={key_name}")
    coerced_order = _coerce_order(order)
    if coerced_order is not None:
        parts.append(f"order={coerced_order}")
    return " / ".join(parts)


def _deployment_route_key_from_deployment(deployment: Any) -> Optional[str]:
    if not isinstance(deployment, dict):
        return None
    model_info = deployment.get("model_info")
    litellm_params = deployment.get("litellm_params")
    if not isinstance(model_info, dict):
        model_info = {}
    if not isinstance(litellm_params, dict):
        litellm_params = {}
    route_key = model_info.get("route_key")
    api_base = litellm_params.get("api_base")
    if not api_base and isinstance(route_key, str) and route_key.strip():
        return route_key
    model_group = _first_not_none(
        deployment.get("model_name"),
        deployment.get("model_group"),
        model_info.get("model_group"),
        model_info.get("model_name"),
    )
    return _deployment_route_key(
        model_group=model_group,
        provider=model_info.get("provider"),
        model=litellm_params.get("model") or model_info.get("model"),
        api_base=api_base,
        api_key_name=model_info.get("api_key_name"),
        order=_image_generation_module._deployment_order(deployment),
    )


def _deployment_cooldown_key(
    *,
    deployment_id: Optional[str],
    route_key: Optional[str],
) -> Optional[str]:
    if isinstance(deployment_id, str) and deployment_id.strip():
        return f"id:{deployment_id.strip()}"
    if isinstance(route_key, str) and route_key.strip():
        return f"route:{route_key.strip()}"
    return None


def _deployment_cooldown_keys(
    *,
    deployment_id: Optional[str],
    route_key: Optional[str],
) -> list[str]:
    if isinstance(deployment_id, str) and deployment_id.strip():
        return [f"id:{deployment_id.strip()}"]
    if isinstance(route_key, str) and route_key.strip():
        return [f"route:{route_key.strip()}"]
    return []


def _deployment_cooldown_surface(request_kwargs: Optional[dict]) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    return _request_current_upstream_surface(request_kwargs) or None


def _deployment_cooldown_request_for_current_surface() -> Optional[dict]:
    surface = _CURRENT_DEPLOYMENT_COOLDOWN_SURFACE.get()
    if surface:
        return {_CURRENT_UPSTREAM_URL_SURFACE_KEY: surface}
    return None


def _deployment_cooldown_keys_for_request(
    *,
    deployment_id: Optional[str],
    route_key: Optional[str],
    request_kwargs: Optional[dict],
) -> list[str]:
    keys = _deployment_cooldown_keys(
        deployment_id=deployment_id,
        route_key=route_key,
    )
    surface = _deployment_cooldown_surface(request_kwargs)
    if not surface:
        model_info = _image_generation_module._request_model_info(request_kwargs)
        raw_surfaces = model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
        surfaces = []
        if isinstance(raw_surfaces, list):
            for raw_surface in raw_surfaces:
                normalized = _normalized_deployment_surface(raw_surface)
                if normalized and normalized not in surfaces:
                    surfaces.append(normalized)
        if len(surfaces) == 1:
            surface = surfaces[0]
    if surface:
        return [f"{key}|surface:{surface}" for key in keys]
    return keys


def _deployment_cooldown_key_from_request(request_kwargs: Optional[dict]) -> Optional[str]:
    return _deployment_cooldown_key(
        deployment_id=_deployment_id_from_request(request_kwargs),
        route_key=_deployment_route_key_from_request(request_kwargs),
    )


def _deployment_cooldown_key_from_deployment(deployment: Any) -> Optional[str]:
    return _deployment_cooldown_key(
        deployment_id=_image_generation_module._deployment_id(deployment),
        route_key=_deployment_route_key_from_deployment(deployment),
    )


def _deployment_cooldown_keys_from_request(request_kwargs: Optional[dict]) -> list[str]:
    return _deployment_cooldown_keys_for_request(
        deployment_id=_deployment_id_from_request(request_kwargs),
        route_key=_deployment_route_key_from_request(request_kwargs),
        request_kwargs=request_kwargs,
    )


def _deployment_cooldown_keys_from_deployment(deployment: Any) -> list[str]:
    return _deployment_cooldown_keys(
        deployment_id=_image_generation_module._deployment_id(deployment),
        route_key=_deployment_route_key_from_deployment(deployment),
    )


def _deployment_cooldown_keys_from_deployment_for_request(
    deployment: Any,
    request_kwargs: Optional[dict],
) -> list[str]:
    return _deployment_cooldown_keys_for_request(
        deployment_id=_image_generation_module._deployment_id(deployment),
        route_key=_deployment_route_key_from_deployment(deployment),
        request_kwargs=request_kwargs,
    )

def _trace_deployment(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict):
        return {}
    model_info = value.get("model_info")
    litellm_params = value.get("litellm_params")
    if not isinstance(model_info, dict):
        model_info = {}
    if not isinstance(litellm_params, dict):
        litellm_params = {}
    order = _coerce_order(litellm_params.get("order")) or _coerce_order(model_info.get("order"))
    model = litellm_params.get("model")
    provider = model_info.get("provider")
    api_key_name = model_info.get("api_key_name")
    return {
        "id": model_info.get("id"),
        "token": model_info.get("id"),
        "provider": model_info.get("provider"),
        "api_key_name": api_key_name,
        "order": order,
        "model": model,
        "api_base": litellm_params.get("api_base"),
        "route_key": _deployment_route_key_from_deployment(value),
        "supports_responses_image_generation_tool": model_info.get(
            "supports_responses_image_generation_tool"
        ),
        "supported_upstream_url_surfaces": model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY),
        "supports_responses_image_input": model_info.get(_RESPONSES_IMAGE_INPUT_SUPPORT_KEY),
        "supports_responses_hosted_tools": model_info.get(
            _SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY
        ),
        "supports_responses_client_tools": model_info.get(
            _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY
        ),
        "supports_responses_web_search": model_info.get(_SUPPORTS_RESPONSES_WEB_SEARCH_KEY),
        "supports_web_search": model_info.get(_SUPPORTS_WEB_SEARCH_KEY),
        "supports_vision": model_info.get("supports_vision"),
    }


def _trace_deployments(deployments: Any) -> list[dict[str, Any]]:
    if not isinstance(deployments, list):
        return []
    return [_trace_deployment(deployment) for deployment in deployments]


def _normalized_deployment_surface(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = value.strip().lower()
    return text if text in {
        _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
        _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        _UPSTREAM_URL_SURFACE_ANTHROPIC,
    } else ""


def _normalized_request_surface(value: Any) -> str:
    surface = _normalized_deployment_surface(value)
    if surface in {
        _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
        _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
        _UPSTREAM_URL_SURFACE_ANTHROPIC,
    }:
        return surface
    return ""


def _request_current_upstream_surface(request_kwargs: Optional[dict]) -> str:
    if not isinstance(request_kwargs, dict):
        return ""
    surface = _normalized_request_surface(
        request_kwargs.get(_CURRENT_UPSTREAM_URL_SURFACE_KEY)
    )
    if surface:
        return surface
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs, metadata_key
        )
        if not isinstance(metadata, dict):
            continue
        surface = _normalized_request_surface(
            metadata.get(_CURRENT_UPSTREAM_URL_SURFACE_KEY)
        )
        if surface:
            return surface
    return ""


def _request_attempted_upstream_surfaces(request_kwargs: Optional[dict]) -> list[str]:
    if not isinstance(request_kwargs, dict):
        return []
    raw_values: Any = request_kwargs.get(_ATTEMPTED_UPSTREAM_URL_SURFACES_KEY)
    if raw_values is None:
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs, "litellm_metadata"
        ) or {}
        raw_values = metadata.get(_ATTEMPTED_UPSTREAM_URL_SURFACES_KEY)
    if not isinstance(raw_values, list):
        return []
    surfaces: list[str] = []
    for value in raw_values:
        surface = _normalized_request_surface(value)
        if surface and surface not in surfaces:
            surfaces.append(surface)
    return surfaces


def _request_surface_deployment_id(request_kwargs: Optional[dict]) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    value = request_kwargs.get(_UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY)
    if not isinstance(value, str) or not value.strip():
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs, "litellm_metadata"
        ) or {}
        value = metadata.get(_UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _set_request_surface_state(
    request_kwargs: dict,
    *,
    surface: str,
    attempted_surfaces: Optional[list[str]] = None,
    deployment_id: Optional[str] = None,
    target_deployment_id: Optional[str] = None,
) -> None:
    surface = _normalized_request_surface(surface)
    if not surface:
        return
    request_kwargs[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface
    metadata = _image_generation_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    updated_metadata = metadata.copy()
    updated_metadata[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface
    if isinstance(deployment_id, str) and deployment_id.strip():
        deployment_id = deployment_id.strip()
        request_kwargs[_UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY] = deployment_id
        updated_metadata[_UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY] = deployment_id
    if attempted_surfaces is not None:
        normalized_attempts: list[str] = []
        for value in attempted_surfaces:
            normalized = _normalized_request_surface(value)
            if normalized and normalized not in normalized_attempts:
                normalized_attempts.append(normalized)
        request_kwargs[_ATTEMPTED_UPSTREAM_URL_SURFACES_KEY] = normalized_attempts
        updated_metadata[_ATTEMPTED_UPSTREAM_URL_SURFACES_KEY] = normalized_attempts
    if isinstance(target_deployment_id, str) and target_deployment_id.strip():
        request_kwargs[_SURFACE_TARGET_DEPLOYMENT_ID_KEY] = target_deployment_id.strip()
        updated_metadata[_SURFACE_TARGET_DEPLOYMENT_ID_KEY] = target_deployment_id.strip()
    else:
        request_kwargs.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
        updated_metadata.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
    request_kwargs["litellm_metadata"] = updated_metadata


def _clear_request_surface_target(request_kwargs: Optional[dict]) -> None:
    if not isinstance(request_kwargs, dict):
        return
    request_kwargs.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
    metadata = _image_generation_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    )
    if isinstance(metadata, dict) and _SURFACE_TARGET_DEPLOYMENT_ID_KEY in metadata:
        updated_metadata = metadata.copy()
        updated_metadata.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
        request_kwargs["litellm_metadata"] = updated_metadata


def _request_surface_target_deployment_id(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    value = request_kwargs.get(_SURFACE_TARGET_DEPLOYMENT_ID_KEY)
    if not isinstance(value, str) or not value.strip():
        metadata = _image_generation_module._request_metadata_dict(
            request_kwargs, "litellm_metadata"
        ) or {}
        value = metadata.get(_SURFACE_TARGET_DEPLOYMENT_ID_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _deployment_supported_surface_modes(deployment: Any) -> list[str]:
    if not isinstance(deployment, dict):
        return []
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return []

    raw_modes: list[Any] = []
    supported = model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
    if isinstance(supported, list):
        raw_modes.extend(supported)


    modes: list[str] = []
    for raw_mode in raw_modes:
        mode = _normalized_deployment_surface(raw_mode)
        if mode and mode not in modes:
            modes.append(mode)
    return modes


def _deployment_has_surface_configuration(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    model_info = deployment.get("model_info")
    return bool(
        isinstance(model_info, dict)
        and isinstance(model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY), list)
        and model_info.get(_SUPPORTED_UPSTREAM_URL_SURFACES_KEY)
    )


def _deployment_primary_surface(deployment: Any) -> str:
    modes = _deployment_supported_surface_modes(deployment)
    return modes[0] if modes else ""


def _active_cooldown_state_for_key(
    cooldowns: dict[str, Any],
    cooldown_key: str,
    now: float,
) -> Optional[dict[str, Any]]:
    state = cooldowns.get(cooldown_key)
    if not isinstance(state, dict):
        return None
    cooldown_until = float(state.get("cooldown_until") or 0.0)
    if cooldown_until > now:
        return state
    if cooldown_until > 0:
        cooldowns.pop(cooldown_key, None)
    return None


def _first_available_deployment_surface(
    deployment: Any,
    cooldowns: dict[str, Any],
    now: float,
) -> str:
    base_keys = _deployment_cooldown_keys_from_deployment(deployment)
    for surface in _deployment_supported_surface_modes(deployment):
        if not any(
            _active_cooldown_state_for_key(
                cooldowns,
                f"{base_key}|surface:{surface}",
                now,
            )
            is not None
            for base_key in base_keys
        ):
            return surface
    return ""


def _first_available_surface_for_deployment(deployment: Any) -> str:
    if not _deployment_cooldown_enabled():
        return _deployment_primary_surface(deployment)

    def select(cooldowns: dict[str, Any], now: float) -> str:
        return _first_available_deployment_surface(deployment, cooldowns, now)

    result = _deployment_cooldown_update_shared(select)
    if isinstance(result, tuple) and isinstance(result[0], str):
        return result[0]
    with _DEPLOYMENT_COOLDOWN_LOCK:
        return select(_DEPLOYMENT_COOLDOWNS, time.time())


def _request_surface_for_deployment(
    request_kwargs: Optional[dict],
    deployment: Any,
) -> str:
    deployment_id = _image_generation_module._deployment_id(deployment)
    state_deployment_id = _request_surface_deployment_id(request_kwargs)
    target_deployment_id = _request_surface_target_deployment_id(request_kwargs)
    requested = _request_current_upstream_surface(request_kwargs)
    supported = _deployment_supported_surface_modes(deployment)
    if (
        requested
        and requested in supported
        and deployment_id
        and deployment_id in {state_deployment_id, target_deployment_id}
    ):
        return requested
    if requested and not supported and deployment_id == target_deployment_id:
        return requested
    return _first_available_surface_for_deployment(deployment)


def _surface_adapter_model(model: Any, surface: str) -> Any:
    if not isinstance(model, str) or not model.strip():
        return model
    upstream = model.strip()
    for prefix in (
        "openai/responses/",
        "anthropic/",
        "openai/",
    ):
        if upstream.startswith(prefix):
            upstream = upstream[len(prefix):]
            break
    if surface == _UPSTREAM_URL_SURFACE_ANTHROPIC:
        return f"anthropic/{upstream}"
    if surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        return f"openai/responses/{upstream}"
    return f"openai/{upstream}"


def _apply_surface_adapter_to_request(
    request_kwargs: dict,
    surface: str,
    upstream_model: Any,
) -> None:
    surface = _normalized_request_surface(surface)
    if not surface:
        return
    adapted_model = _surface_adapter_model(upstream_model, surface)
    request_kwargs["model"] = adapted_model
    request_kwargs["custom_llm_provider"] = (
        "anthropic" if surface == _UPSTREAM_URL_SURFACE_ANTHROPIC else "openai"
    )
    if surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        request_kwargs.pop("use_chat_completions_api", None)
    else:
        request_kwargs["use_chat_completions_api"] = True

    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        updated_params = litellm_params.copy()
        updated_params["model"] = adapted_model
        updated_params["custom_llm_provider"] = request_kwargs[
            "custom_llm_provider"
        ]
        request_kwargs["litellm_params"] = updated_params


def _next_upstream_surface_for_failed_deployment(
    router: Any,
    exception: Exception,
    request_kwargs: Optional[dict],
) -> Optional[tuple[str, str]]:
    if not isinstance(request_kwargs, dict):
        return None
    failed_id = (
        _responses_execution_module._failed_deployment_id(exception)
        or _deployment_id_from_request(request_kwargs)
    )
    if not failed_id:
        return None
    model_group = _responses_execution_module._request_model_group(request_kwargs)
    if not model_group:
        return None
    try:
        deployments = _router_configured_deployments(router, model_group)
    except Exception:
        return None
    deployment = next(
        (
            candidate
            for candidate in deployments
            if _image_generation_module._deployment_id(candidate) == failed_id
        ),
        None,
    )
    if deployment is None:
        return None
    surfaces = _deployment_supported_surface_modes(deployment)
    current = _request_current_upstream_surface(request_kwargs)
    attempted = _request_attempted_upstream_surfaces(request_kwargs)
    if current and current not in attempted:
        attempted.append(current)
    for surface in surfaces:
        if surface in attempted:
            continue
        probe_request = {_CURRENT_UPSTREAM_URL_SURFACE_KEY: surface}
        available, _cooled, _filtered = _with_active_deployment_cooldowns(
            [deployment],
            request_kwargs=probe_request,
        )
        if available:
            return surface, failed_id
    return None


def _deployment_prefers_responses_surface(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return False
    return (
        _deployment_primary_surface(deployment)
        == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    )


def _deployment_is_known_chat_bridge_surface(deployment: Any) -> bool:
    if not isinstance(deployment, dict):
        return False
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return False
    primary_mode = _deployment_primary_surface(deployment)
    if primary_mode in _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES:
        return True
    return False


def _request_has_responses_structured_tools(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    tools = request_kwargs.get("tools")
    if not isinstance(tools, list):
        return False
    structured_types = {
        "function",
        "custom",
        "namespace",
        "tool_search",
        "web_search",
        "web_search_preview",
        "image_generation",
    }
    return any(
        isinstance(tool, dict) and tool.get("type") in structured_types
        for tool in tools
    )


def _request_should_prefer_responses_surface(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if request_kwargs.get("use_chat_completions_api") is True:
        return False
    if not _image_generation_module._request_is_responses_api(request_kwargs):
        return False
    if _image_generation_module._request_has_codex_client_evidence(request_kwargs):
        return True
    return _request_has_responses_structured_tools(request_kwargs)


def _prefer_responses_surface_deployments(
    deployments: List[dict],
    request_kwargs: Optional[dict],
) -> tuple[List[dict], bool]:
    if not deployments or not _request_should_prefer_responses_surface(request_kwargs):
        return deployments, False

    responses_deployments = [
        deployment
        for deployment in deployments
        if _deployment_prefers_responses_surface(deployment)
    ]
    if not responses_deployments:
        return deployments, False

    filtered = [
        deployment
        for deployment in deployments
        if not _deployment_is_known_chat_bridge_surface(deployment)
    ]
    if not filtered or filtered == deployments:
        return deployments, False
    return filtered, True


def _selected_deployment_request_marker(deployment: Any) -> Optional[dict]:
    if not isinstance(deployment, dict):
        return None
    model_info = deployment.get("model_info")
    litellm_params = deployment.get("litellm_params")
    if not isinstance(model_info, dict):
        model_info = {}
    if not isinstance(litellm_params, dict):
        litellm_params = {}
    model_info = model_info.copy()
    for key in ("model_name", "model_group"):
        model_group = deployment.get(key)
        if isinstance(model_group, str) and model_group.strip():
            model_info.setdefault("model_group", model_group.strip())
    request_params = {
        key: value
        for key, value in litellm_params.items()
        if key in {"api_key"}
    }
    litellm_params = {
        key: value
        for key, value in litellm_params.items()
        if key in {"api_base", "api_version", "custom_llm_provider", "model", "order"}
    }
    deployment_id = model_info.get("id")
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        return None
    if model_info.get("order") is None and litellm_params.get("order") is not None:
        model_info["order"] = litellm_params.get("order")
    if model_info.get("model") is None and litellm_params.get("model") is not None:
        model_info["model"] = litellm_params.get("model")
    return {
        "model_info": model_info,
        "litellm_params": litellm_params,
        "request_params": request_params,
    }


def _remember_selected_deployment(
    deployment: Any,
    *,
    surface: str = "",
) -> None:
    marker = _selected_deployment_request_marker(deployment)
    if marker is None:
        return
    surface = _normalized_request_surface(surface)
    if surface:
        marker[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface
    _CURRENT_SELECTED_DEPLOYMENT.set(marker)
    selected_box = _CURRENT_SELECTED_DEPLOYMENT_BOX.get()
    if isinstance(selected_box, dict):
        selected_box["marker"] = marker


def _selected_deployment_marker_from_box(selected_box: Any = None) -> Optional[dict]:
    if isinstance(selected_box, dict):
        marker = selected_box.get("marker")
        if isinstance(marker, dict):
            return marker
    marker = _CURRENT_SELECTED_DEPLOYMENT.get()
    return marker if isinstance(marker, dict) else None


def _apply_selected_deployment_marker_to_request(
    request_kwargs: Optional[dict],
    marker: Any,
    *,
    update_top_level: bool = True,
) -> bool:
    if not isinstance(request_kwargs, dict) or not isinstance(marker, dict):
        return False
    model_info = marker.get("model_info")
    litellm_params = marker.get("litellm_params")
    request_params = marker.get("request_params")
    if not isinstance(model_info, dict) or not model_info:
        return False
    if not isinstance(litellm_params, dict):
        litellm_params = {}
    if not isinstance(request_params, dict):
        request_params = {}

    selected_model_info = model_info.copy()
    selected_litellm_params = litellm_params.copy()
    selected_surface = _normalized_request_surface(
        marker.get(_CURRENT_UPSTREAM_URL_SURFACE_KEY)
    )
    if update_top_level:
        request_kwargs["model_info"] = selected_model_info
        if selected_litellm_params:
            existing_litellm_params = request_kwargs.get("litellm_params")
            merged_litellm_params = (
                existing_litellm_params.copy()
                if isinstance(existing_litellm_params, dict)
                else {}
            )
            merged_litellm_params.update(selected_litellm_params)
            request_kwargs["litellm_params"] = merged_litellm_params

    litellm_metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    updated_metadata = litellm_metadata.copy()
    updated_metadata["model_info"] = selected_model_info
    api_base = selected_litellm_params.get("api_base")
    if isinstance(api_base, str) and api_base.strip():
        updated_metadata["api_base"] = api_base
    request_kwargs["litellm_metadata"] = updated_metadata
    if selected_surface:
        _set_request_surface_state(
            request_kwargs,
            surface=selected_surface,
            attempted_surfaces=_request_attempted_upstream_surfaces(request_kwargs),
            deployment_id=_deployment_id_from_request(request_kwargs),
            target_deployment_id=_request_surface_target_deployment_id(request_kwargs),
        )
    return True


def _apply_current_selected_deployment_to_request(
    request_kwargs: Optional[dict],
    *,
    selected_box: Any = None,
    update_top_level: bool = True,
) -> bool:
    return _apply_selected_deployment_marker_to_request(
        request_kwargs,
        _selected_deployment_marker_from_box(selected_box),
        update_top_level=update_top_level,
    )


def _remember_selected_deployment_for_request(
    request_kwargs: Optional[dict],
    deployment: Any,
) -> None:
    marker = _selected_deployment_request_marker(deployment)
    if marker is not None and isinstance(request_kwargs, dict):
        surface = _request_current_upstream_surface(request_kwargs)
        if surface:
            marker[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface
    _apply_selected_deployment_marker_to_request(
        request_kwargs,
        marker,
        update_top_level=True,
    )


def _trace_request_id(request_kwargs: Optional[dict]) -> Optional[str]:
    request_kwargs = request_kwargs or {}
    for key in ("request_id", "litellm_call_id", "call_id"):
        value = request_kwargs.get(key)
        if isinstance(value, str) and value.strip():
            return value
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = request_kwargs.get(metadata_key)
        if not isinstance(metadata, dict):
            continue
        for key in ("request_id", "litellm_call_id", "call_id"):
            value = metadata.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


def _normal_trace_key(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "_", str(value or "").strip().lower()).strip("_")


def _trace_session_context(request_kwargs: Optional[dict]) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    result: dict[str, Any] = {}

    def set_value(kind: str, key: str, value: Any, path: str) -> None:
        if kind in result:
            return
        if not isinstance(value, (str, int)):
            return
        text = _trace_module._sanitize_trace_text(str(value), limit=180)
        if not text:
            return
        result[kind] = text
        result[f"{kind}_key"] = key
        result[f"{kind}_path"] = path

    def visit(value: Any, path: str, depth: int = 0) -> None:
        if depth > 6 or (result.get("id") and result.get("name")):
            return
        if isinstance(value, dict):
            for key, nested in value.items():
                normal = _normal_trace_key(key)
                next_path = f"{path}.{key}" if path else str(key)
                if any(fragment in normal for fragment in _SESSION_ID_KEY_FRAGMENTS):
                    set_value("id", str(key), nested, next_path)
                if any(fragment in normal for fragment in _SESSION_NAME_KEY_FRAGMENTS):
                    set_value("name", str(key), nested, next_path)
                if isinstance(nested, (dict, list)):
                    visit(nested, next_path, depth + 1)
        elif isinstance(value, list):
            for index, nested in enumerate(value[:20]):
                if isinstance(nested, (dict, list)):
                    visit(nested, f"{path}[{index}]", depth + 1)

    visit(request_kwargs, "")
    return result


def _trace_exception(exception: Exception) -> dict[str, Any]:
    status_code = _exception_status_code(exception)
    text = _exception_text(exception)
    if _is_terminal_prompt_or_policy_error(exception):
        reason = "terminal-prompt-or-policy"
    elif _is_image_generation_tool_runtime_fallback_error(exception):
        reason = "image-generation-tool-runtime-fallback"
    elif _is_upstream_deployment_failover_error(exception):
        reason = "upstream-auth-or-balance"
    elif _is_upstream_gateway_bad_request_error(exception):
        reason = "upstream-gateway-bad-request"
    elif _is_responses_schema_unsupported_error(exception):
        reason = "responses-schema-unsupported"
    elif _is_image_parameter_or_capability_bad_request_error(exception):
        reason = "image-parameter-or-capability-bad-request"
    elif _is_deployment_compatible_bad_request_error(exception):
        reason = "upstream-compatible-bad-request"
    elif _exception_indicates_network_connectivity_error(exception):
        reason = "upstream-network-connectivity"
    elif status_code in (408, 429) or (status_code is not None and status_code >= 500):
        reason = f"upstream-status-{status_code}"
    elif type(exception).__name__ in _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES:
        reason = "upstream-temporary-class"
    elif any(marker in text for marker in _UPSTREAM_TEMPORARY_ERROR_MARKERS):
        reason = "upstream-temporary-text"
    else:
        reason = "other"
    return {
        "class": type(exception).__name__,
        "status_code": status_code,
        "reason": reason,
        "text": _trace_module._sanitize_trace_text(text),
        "failed_deployment_id": _responses_execution_module._failed_deployment_id(exception),
        "failed_deployment_route_key": _responses_execution_module._failed_deployment_route_key(exception),
        "failed_deployment_order": _responses_execution_module._failed_deployment_order(exception),
    }


def _coerce_order(value: Any) -> Optional[int]:
    if isinstance(value, int):
        return value
    if isinstance(value, str):
        if not value.strip():
            return 1
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _exception_status_code(exception: Exception) -> Optional[int]:
    status_code = getattr(exception, "status_code", None)
    if isinstance(status_code, int):
        return status_code
    response = getattr(exception, "response", None)
    response_status_code = getattr(response, "status_code", None)
    return response_status_code if isinstance(response_status_code, int) else None


def _exception_text(exception: Exception) -> str:
    parts = [str(exception)]
    for attr in ("message", "body", "litellm_debug_info"):
        value = getattr(exception, attr, None)
        if value is not None:
            parts.append(str(value))
    response = getattr(exception, "response", None)
    response_text = getattr(response, "text", None)
    if isinstance(response_text, str):
        parts.append(response_text)
    return "\n".join(parts).lower()


def _duration_unit_seconds(value: float, unit: Optional[str]) -> float:
    unit = (unit or "seconds").strip().lower()
    if unit in {"m", "min", "mins", "minute", "minutes"}:
        return value * 60.0
    return value


def _parse_retry_after_seconds(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)):
        seconds = float(value)
        return seconds if seconds >= 0 and seconds < float("inf") else None
    if not isinstance(value, str):
        return None
    text = value.strip().lower()
    if not text:
        return None
    try:
        seconds = float(text)
        return seconds if seconds >= 0 and seconds < float("inf") else None
    except ValueError:
        pass
    match = re.search(
        r"(?:retry-after|retry\s+after|retry\s+again\s+in|try\s+again\s+in)"
        r"\s*[:=]?\s*(\d+(?:\.\d+)?)\s*"
        r"(seconds?|secs?|s|minutes?|mins?|m)?",
        text,
    )
    if not match:
        return None
    seconds = _duration_unit_seconds(float(match.group(1)), match.group(2))
    return seconds if seconds >= 0 and seconds < float("inf") else None


def _header_retry_after_seconds(headers: Any) -> Optional[float]:
    if headers is None:
        return None
    values: list[Any] = []
    getter = getattr(headers, "get", None)
    if callable(getter):
        values.extend([getter("Retry-After"), getter("retry-after")])
    if isinstance(headers, dict):
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                values.append(value)
    for value in values:
        seconds = _parse_retry_after_seconds(value)
        if seconds is not None:
            return seconds
    return None


def _exception_retry_after_seconds(exception: Exception) -> Optional[float]:
    for attr in ("retry_after", "retry_after_seconds"):
        seconds = _parse_retry_after_seconds(getattr(exception, attr, None))
        if seconds is not None:
            return seconds
    for headers in (
        getattr(exception, "headers", None),
        getattr(getattr(exception, "response", None), "headers", None),
    ):
        seconds = _header_retry_after_seconds(headers)
        if seconds is not None:
            return seconds
    return _parse_retry_after_seconds(_exception_text(exception))


def _route_exhaustion_retry_delay_for_exception(
    exception: Exception,
    configured_delay_seconds: float,
) -> float:
    if configured_delay_seconds <= 0:
        return 0.0
    if _exception_status_code(exception) != 429:
        return configured_delay_seconds
    retry_after = _exception_retry_after_seconds(exception)
    if retry_after is None:
        return configured_delay_seconds
    capped_retry_after = min(retry_after, _STREAM_ROUTE_EXHAUSTION_RETRY_AFTER_MAX_SECONDS)
    return max(configured_delay_seconds, capped_retry_after)


def _is_upstream_deployment_failover_error(exception: Exception) -> bool:
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    status_code = _exception_status_code(exception)
    if status_code in (401, 403):
        return True
    if _is_ssl_verification_error(exception):
        return True
    text = _exception_text(exception)
    return any(marker in text for marker in _UPSTREAM_BALANCE_ERROR_MARKERS)


def _exception_indicates_network_connectivity_error(exception: Exception) -> bool:
    if _is_ssl_verification_error(exception):
        return False
    text = _exception_text(exception)
    exception_class = type(exception).__name__.lower()
    if exception_class in {
        "apiconnectionerror",
        "connecterror",
        "connectionerror",
        "networkerror",
    }:
        return True
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "api connection error",
            "apiconnectionerror",
            "connecterror",
            "cannot connect to host",
            "failed to connect",
            "connection refused",
            "connection reset",
            "connection aborted",
            "connection closed",
            "connection lost",
            "server disconnected",
            "network is unreachable",
            "network unreachable",
            "no route to host",
            "temporary failure in name resolution",
            "name or service not known",
            "nodename nor servname provided",
        )
    )


def _is_ssl_verification_error(exception: Exception) -> bool:
    text = _exception_text(exception)
    if not text:
        return False
    return any(
        marker in text
        for marker in (
            "ssl: certificate_verify_failed",
            "certificate_verify_failed",
            "ssl certificate verify failed",
            "certificate verify failed",
            "certificate verification failed",
            "ssl verification failed",
            "ssl verify failed",
            "sslcertverificationerror",
            "unable to get local issuer certificate",
            "self signed certificate",
            "self-signed certificate",
            "hostname mismatch",
            "certificate has expired",
            "tlsv1 alert unknown ca",
        )
    )


def _is_cloudflare_browser_signature_block_error(exception: Exception) -> bool:
    status_code = _exception_status_code(exception)
    text = _exception_text(exception)
    if status_code not in (400, 403) and "error 1010" not in text:
        return False
    return any(
        marker in text
        for marker in (
            "cloudflare error 1010",
            "error 1010",
            "browser's signature",
            "browser signature",
            "site owner has blocked access",
            "access based on your browser",
        )
    )


def _should_retry_with_browser_compatible_headers(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> bool:
    return (
        _is_cloudflare_browser_signature_block_error(exception)
        and not _image_generation_module._request_forces_browser_compatible_headers(request_kwargs)
    )


def _is_image_generation_tool_runtime_fallback_error(exception: Exception) -> bool:
    return getattr(exception, "image_generation_tool_runtime_fallback", False) is True


def _is_terminal_prompt_or_policy_error(exception: Exception) -> bool:
    status_code = _exception_status_code(exception)
    if status_code is not None and status_code < 400:
        return False
    text = _exception_text(exception)
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "content_policy",
            "content policy",
            "content-policy",
            "contentpolicy",
            "policy_violation",
            "policy violation",
            "safety policy",
            "safety system",
            "safety guidelines",
            "safety_violation",
            "prompt violates",
            "prompt violation",
            "violates our policy",
            "violates the policy",
            "violates policy",
            "violates safety",
            "violates content",
            "request violates",
            "input violates",
            "blocked by safety",
            "blocked for safety",
            "blocked due to safety",
            "blocked_content",
            "content moderation",
            "moderation blocked",
            "unsafe prompt",
            "unsafe content",
            "not allowed by policy",
            "disallowed content",
            "disallowed prompt",
            "responsible ai policy",
            "policy reasons",
        )
    ):
        return True
    return type(exception).__name__ in {
        "ContentPolicyViolationError",
        "ModerationError",
    }


def _is_image_parameter_or_capability_bad_request_error(exception: Exception) -> bool:
    if _exception_status_code(exception) not in (400, 422):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_responses_schema_unsupported_error(exception):
        return False
    text = _exception_text(exception)
    if not text:
        return False
    image_markers = (
        "image",
        "images/",
        "image_generation",
        "gpt-image",
        "dall-e",
        "output_format",
        "output format",
        "output_compression",
        "quality",
        "size",
        "resolution",
        "dimension",
        "aspect ratio",
    )
    if not any(marker in text for marker in image_markers):
        return False
    return any(
        marker in text
        for marker in (
            "unsupported tool",
            "unsupported tool type",
            "unsupported_tool",
            "tool not supported",
            "tool is not supported",
            "tool unsupported",
            "unknown tool",
            "invalid tool",
            "invalid tool type",
            "image_generation tool",
            "image generation tool",
            "image_generation_tool",
            "image generation is not available",
            "image_generation is not available",
            "image_generation not available",
            "invalid model name",
            "model not found",
            "requires an image model",
            "unsupported model",
            "not support",
            "not supported",
            "unsupported",
            "unknown parameter",
            "unrecognized parameter",
            "unsupported parameter",
            "unsupported value",
            "invalid parameter",
            "invalid_request_error",
            "bad_response_status_code",
            "invalid size",
            "unsupported size",
            "size must",
            "invalid quality",
            "unsupported quality",
            "invalid output_format",
            "unsupported output_format",
            "invalid output format",
            "unsupported output format",
            "invalid dimensions",
            "unsupported dimensions",
            "invalid aspect ratio",
            "unsupported aspect ratio",
            "expected one of",
            "must be one of",
            "should be one of",
            "valid values",
            "allowed values",
        )
    )


def _is_native_responses_web_search_unsupported_error(exception: Exception) -> bool:
    status_code = _exception_status_code(exception)
    if status_code is not None and status_code not in {400, 404, 422}:
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    text = _exception_text(exception)
    if not text:
        return False
    if not any(
        marker in text
        for marker in (
            "web_search",
            "web search",
            "web_search_preview",
            "hosted web search",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "unsupported",
            "not supported",
            "does not support",
            "not support",
            "unsupported tool",
            "invalid tool",
            "unknown tool",
            "unrecognized tool",
            "tool type",
            "invalid_request_error",
            "not found",
            "unrecognized",
            "unknown",
        )
    )


def _is_upstream_gateway_bad_request_error(exception: Exception) -> bool:
    if _exception_status_code(exception) != 400:
        return False
    text = _exception_text(exception)
    if not all(marker in text for marker in _UPSTREAM_HTML_BAD_REQUEST_MARKERS):
        return False
    if "openaiexception" in text or "openai exception" in text:
        return True
    return all(
        marker in text for marker in _LITELLM_MODEL_GROUP_FALLBACK_EXHAUSTED_MARKERS
    )


def _is_context_size_error(exception: Exception) -> bool:
    text = _exception_text(exception)
    if not text:
        return False
    if type(exception).__name__ == "ContextWindowExceededError":
        return True
    if "max_output_tokens" in text and not any(
        marker in text
        for marker in (
            "max_input_tokens",
            "input tokens",
            "prompt tokens",
            "context length",
            "context window",
            "maximum context",
        )
    ):
        return False
    if any(
        marker in text
        for marker in (
            "maximum context length",
            "context length exceeded",
            "context window exceeded",
            "exceeds the context window",
            "exceeded context window",
            "context length limit",
            "context window limit",
            "prompt is too long",
            "prompt too long",
            "input is too long",
            "input too long",
            "too many input tokens",
            "too many prompt tokens",
            "reduce the length of the input",
            "reduce your input",
            "tokens exceeds the model",
            "tokens exceed the model",
        )
    ):
        return True
    return bool(
        re.search(
            r"\b(?:input|prompt|context)\b.{0,80}\b(?:tokens?|length|window)\b.{0,80}\b(?:exceed|exceeds|exceeded|too long|larger than|greater than|maximum|limit)",
            text,
        )
        or re.search(
            r"\b(?:exceed|exceeds|exceeded|too long|larger than|greater than)\b.{0,80}\b(?:input|prompt|context)\b.{0,80}\b(?:tokens?|length|window)\b",
            text,
        )
    )


def _deployment_id_from_request(request_kwargs: Optional[dict]) -> Optional[str]:
    model_info = _image_generation_module._request_model_info(request_kwargs)
    deployment_id = model_info.get("id")
    if isinstance(deployment_id, str) and deployment_id.strip():
        return deployment_id
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, metadata_key)
        if not metadata:
            continue
        nested_model_info = metadata.get("model_info")
        if isinstance(nested_model_info, dict):
            deployment_id = nested_model_info.get("id")
            if isinstance(deployment_id, str) and deployment_id.strip():
                return deployment_id
    return None


def _deployment_route_key_from_request(request_kwargs: Optional[dict]) -> Optional[str]:
    request_kwargs = request_kwargs or {}
    model_info = _image_generation_module._request_model_info(request_kwargs)
    route_key = model_info.get("route_key")
    api_base = _image_generation_module._request_api_base(request_kwargs)
    if not api_base and isinstance(route_key, str) and route_key.strip():
        return route_key
    litellm_params = _as_dict(request_kwargs.get("litellm_params"))
    has_deployment_context = bool(
        model_info.get("id")
        or model_info.get("provider")
        or model_info.get("api_key_name")
        or model_info.get("order") is not None
        or litellm_params.get("model")
        or litellm_params.get("api_base")
        or request_kwargs.get("custom_llm_provider")
    )
    if not has_deployment_context:
        return None
    provider = _first_not_none(
        model_info.get("provider"),
        request_kwargs.get("custom_llm_provider"),
    )
    model_group = _first_not_none(
        model_info.get("model_group"),
        model_info.get("model_name"),
        _responses_execution_module._request_model_group(request_kwargs),
    )
    model = _first_not_none(
        litellm_params.get("model"),
        model_info.get("model"),
        request_kwargs.get("model"),
    )
    order = None
    if model_info.get("order") is not None or litellm_params.get("order") is not None:
        order = _deployment_order_from_request(request_kwargs)
    return _deployment_route_key(
        model_group=model_group,
        provider=provider,
        model=model,
        api_base=api_base,
        api_key_name=model_info.get("api_key_name"),
        order=order,
    )


def _order_from_route_key(route_key: Any) -> Optional[int]:
    if not isinstance(route_key, str):
        return None
    match = re.search(r"(?:^|/)\s*order\s*=\s*(\d+)\s*(?:/|$)", route_key)
    if match is None:
        return None
    return _coerce_order(match.group(1))


def _deployment_order_from_request(request_kwargs: Optional[dict]) -> Optional[int]:
    request_kwargs = request_kwargs or {}
    order = _coerce_order(request_kwargs.get("order"))
    if order is not None:
        return order
    has_deployment_context = False
    saw_defaultable_order = False
    saw_invalid_order = False
    for section_name in ("litellm_params", "model_info"):
        section = request_kwargs.get(section_name)
        if not isinstance(section, dict):
            continue
        has_deployment_context = has_deployment_context or any(
            section.get(key) is not None
            for key in ("id", "provider", "api_key_name", "model", "api_base")
        )
        if "order" not in section or section.get("order") is None:
            saw_defaultable_order = True
            order = _order_from_route_key(section.get("route_key"))
            if order is not None:
                return order
            continue
        order = _coerce_order(section.get("order"))
        if order is not None:
            return order
        saw_invalid_order = True
    model_info = _image_generation_module._request_model_info(request_kwargs)
    has_deployment_context = has_deployment_context or any(
        model_info.get(key) is not None
        for key in ("id", "provider", "api_key_name", "model", "api_base")
    )
    if "order" in model_info:
        order = _coerce_order(model_info.get("order"))
        if order is not None:
            return order
        saw_invalid_order = True
    route_key_order = _order_from_route_key(model_info.get("route_key"))
    if route_key_order is not None:
        return route_key_order
    target_order = _coerce_order(request_kwargs.get("_target_order"))
    if target_order is not None:
        return target_order
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, metadata_key)
        if not metadata:
            continue
        nested_model_info = metadata.get("model_info")
        if isinstance(nested_model_info, dict):
            has_deployment_context = has_deployment_context or any(
                nested_model_info.get(key) is not None
                for key in ("id", "provider", "api_key_name", "model", "api_base")
            )
            if "order" not in nested_model_info or nested_model_info.get("order") is None:
                saw_defaultable_order = True
                order = _order_from_route_key(nested_model_info.get("route_key"))
                if order is not None:
                    return order
                continue
            order = _coerce_order(nested_model_info.get("order"))
            if order is not None:
                return order
            saw_invalid_order = True
    if saw_invalid_order:
        return None
    if has_deployment_context or saw_defaultable_order:
        return 1
    return None


def _request_allows_failed_deployment_order(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if "order" in request_kwargs or "_target_order" in request_kwargs:
        return True
    if isinstance(request_kwargs.get("litellm_params"), dict):
        return True
    model_info = request_kwargs.get("model_info")
    if isinstance(model_info, dict) and "order" in model_info:
        return True
    if isinstance(model_info, dict) and _order_from_route_key(model_info.get("route_key")) is not None:
        return True
    request_model_info = _image_generation_module._request_model_info(request_kwargs)
    if isinstance(request_model_info, dict) and "order" in request_model_info:
        return True
    if isinstance(request_model_info, dict) and _order_from_route_key(request_model_info.get("route_key")) is not None:
        return True
    return False


def _mark_exception_for_deployment_failover(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    _apply_current_selected_deployment_to_request(request_kwargs)
    deployment_id = _deployment_id_from_request(request_kwargs)
    route_key = _deployment_route_key_from_request(request_kwargs)
    if deployment_id and not getattr(exception, "failed_deployment_id", None):
        try:
            exception.failed_deployment_id = deployment_id  # type: ignore[attr-defined]
        except Exception:
            pass
    if route_key and not getattr(exception, "failed_deployment_route_key", None):
        try:
            exception.failed_deployment_route_key = route_key  # type: ignore[attr-defined]
        except Exception:
            pass
    deployment_order = _deployment_order_from_request(request_kwargs)
    deployment_surface = _request_current_upstream_surface(request_kwargs)
    if deployment_surface and not getattr(
        exception, "failed_deployment_surface", None
    ):
        try:
            exception.failed_deployment_surface = deployment_surface  # type: ignore[attr-defined]
        except Exception:
            pass
    if (
        deployment_order is not None
        and _request_allows_failed_deployment_order(request_kwargs)
        and not getattr(exception, "failed_deployment_order", None)
    ):
        try:
            exception.failed_deployment_order = deployment_order  # type: ignore[attr-defined]
        except Exception:
            pass
    should_sync_exclusions = bool(
        not _is_local_stream_timeout_error(exception)
        and not _exception_indicates_timeout_or_long_wait(exception)
        and not _exception_indicates_network_connectivity_error(exception)
    )
    if not deployment_surface and should_sync_exclusions:
        _sync_failed_deployment_exclusions(
            request_kwargs, exception, deployment_id=deployment_id
        )
    elif deployment_surface and should_sync_exclusions and isinstance(request_kwargs, dict):
        existing_exclusions = _image_generation_module._request_excluded_deployment_ids(
            request_kwargs
        )
        if existing_exclusions:
            _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(existing_exclusions)
            try:
                exception.excluded_deployment_ids = sorted(existing_exclusions)  # type: ignore[attr-defined]
            except Exception:
                pass
    try:
        exception.num_retries = 0  # type: ignore[attr-defined]
    except Exception:
        pass
    _trace_module._route_trace(
        "deployment_failover_marked",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=deployment_id,
        deployment_token=deployment_id,
        route_key=route_key,
        deployment_order=deployment_order,
        request=_trace_module._trace_request_summary(request_kwargs),
        exception=_trace_exception(exception),
    )
    _record_deployment_failure_for_cooldown(exception, request_kwargs)


def _mark_exception_for_upstream_surface_failover(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    try:
        exception.upstream_surface_unsupported = True  # type: ignore[attr-defined]
    except Exception:
        pass
    _mark_exception_for_deployment_failover(exception, request_kwargs)


def _exception_excluded_deployment_ids(exception: Exception) -> set[str]:
    excluded = getattr(exception, "excluded_deployment_ids", None)
    if isinstance(excluded, (list, tuple, set)):
        return {item for item in excluded if isinstance(item, str)}
    return set()


def _sync_failed_deployment_exclusions(
    request_kwargs: Optional[dict],
    exception: Exception,
    *,
    deployment_id: Optional[str] = None,
) -> None:
    excluded_ids = set(_CURRENT_EXCLUDED_DEPLOYMENT_IDS.get() or ())
    excluded_ids.update(_exception_excluded_deployment_ids(exception))
    if isinstance(request_kwargs, dict):
        excluded_ids.update(_image_generation_module._request_excluded_deployment_ids(request_kwargs))
    failed_id = deployment_id or _responses_execution_module._failed_deployment_id(exception)
    request_model_info = _image_generation_module._request_model_info(request_kwargs)
    supported_surfaces = []
    raw_supported_surfaces = request_model_info.get(
        _SUPPORTED_UPSTREAM_URL_SURFACES_KEY
    )
    if isinstance(raw_supported_surfaces, list):
        for raw_surface in raw_supported_surfaces:
            surface = _normalized_deployment_surface(raw_surface)
            if surface and surface not in supported_surfaces:
                supported_surfaces.append(surface)
    attempted_surfaces = set(_request_attempted_upstream_surfaces(request_kwargs))
    surface_retry_pending = bool(
        failed_id
        and isinstance(request_kwargs, dict)
        and _request_current_upstream_surface(request_kwargs)
        and any(surface not in attempted_surfaces for surface in supported_surfaces)
    )
    if (
        failed_id
        and not surface_retry_pending
        and not _is_local_stream_timeout_error(exception)
        and not _should_retry_same_deployment_before_fallback(exception)
    ):
        excluded_ids.add(failed_id)
    if excluded_ids:
        if isinstance(request_kwargs, dict):
            request_kwargs["_excluded_deployment_ids"] = sorted(excluded_ids)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids)
        try:
            exception.excluded_deployment_ids = sorted(excluded_ids)  # type: ignore[attr-defined]
        except Exception:
            pass


def _is_priority_deployment_failover_error(exception: Exception) -> bool:
    if _is_context_size_error(exception):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_image_generation_tool_runtime_fallback_error(exception):
        return True
    if _is_upstream_surface_failover_error(exception):
        return True
    if getattr(exception, "responses_stream_incomplete", False):
        return True
    if _is_upstream_deployment_failover_error(exception):
        return True
    if _is_upstream_gateway_bad_request_error(exception):
        return True
    if _is_image_parameter_or_capability_bad_request_error(exception):
        return True
    if _is_deployment_compatible_bad_request_error(exception):
        return True
    if type(exception).__name__ in _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES:
        return True
    status_code = _exception_status_code(exception)
    if status_code in (408, 429):
        return True
    if status_code is not None and status_code >= 500:
        return True
    text = _exception_text(exception)
    return any(marker in text for marker in _UPSTREAM_TEMPORARY_ERROR_MARKERS)


def _request_duration_seconds(request_kwargs: Optional[dict]) -> Optional[float]:
    if not isinstance(request_kwargs, dict):
        return None
    candidates = [
        request_kwargs.get("duration_ms"),
        request_kwargs.get("response_ms"),
        request_kwargs.get("litellm_call_duration_ms"),
    ]
    standard = _as_dict(request_kwargs.get("standard_logging_object"))
    candidates.extend(
        [
            standard.get("duration_ms"),
            standard.get("response_ms"),
            standard.get("litellm_call_duration_ms"),
        ]
    )
    for value in candidates:
        seconds = _safe_float(value)
        if seconds is not None and seconds >= 0:
            return seconds / 1000.0
    return None


def _exception_indicates_timeout_or_long_wait(exception: Exception) -> bool:
    status_code = _exception_status_code(exception)
    if status_code in (408, 504):
        return True
    text = _exception_text(exception)
    if not text:
        return False
    if any(
        marker in text
        for marker in (
            "timeout",
            "timed out",
            "time out",
            "deadline exceeded",
            "deadline_exceeded",
            "upstream request timeout",
            "stream start timeout",
            "stream idle timeout",
            "without the first stream event",
            "without a new chunk",
            "all channels",
            "all upstreams",
            "所有渠道",
            "均失败",
            "超时",
        )
    ):
        return True
    if re.search(r"\b(?:after|within|in)\s+\d+(?:\.\d+)?\s*s\b", text):
        return True
    if re.search(r"\b\d+(?:\.\d+)?\s*(?:seconds?|secs?)\b", text) and any(
        marker in text for marker in ("wait", "waiting", "timeout", "timed out", "deadline")
    ):
        return True
    return False


def _should_count_deployment_failure_for_cooldown(
    exception: Exception,
    request_kwargs: Optional[dict] = None,
) -> bool:
    stream_start_timeout = _is_local_stream_start_timeout_error(exception)
    if _is_context_size_error(exception):
        return False
    if _is_sanitized_upstream_route_failure_error(exception):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_local_stream_timeout_error(exception) and not stream_start_timeout:
        return False
    if _is_no_deployments_available_error(exception):
        return False
    if _should_retry_same_deployment_before_fallback(exception):
        return False
    if _is_upstream_gateway_bad_request_error(exception):
        return False
    if _is_image_parameter_or_capability_bad_request_error(exception):
        return False
    if _is_deployment_compatible_bad_request_error(exception):
        return False
    if _exception_indicates_network_connectivity_error(exception):
        return False

    if _is_upstream_deployment_failover_error(exception):
        return True
    if _exception_indicates_timeout_or_long_wait(exception) and not stream_start_timeout:
        return False
    duration_seconds = _request_duration_seconds(request_kwargs)
    if duration_seconds is not None and duration_seconds >= 30.0 and not stream_start_timeout:
        return False
    if stream_start_timeout:
        return True
    if _is_upstream_surface_failover_error(exception):
        return True
    status_code = _exception_status_code(exception)
    if status_code is not None and status_code >= 500:
        return True
    return False


def _record_deployment_failure_for_cooldown(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    if not _deployment_cooldown_enabled():
        return
    if not _should_count_deployment_failure_for_cooldown(exception, request_kwargs):
        return
    if getattr(exception, _DEPLOYMENT_COOLDOWN_FAILURE_RECORDED_ATTR, False):
        return

    deployment_id = _responses_execution_module._failed_deployment_id(exception) or _deployment_id_from_request(request_kwargs)
    route_key = _responses_execution_module._failed_deployment_route_key(exception) or _deployment_route_key_from_request(
        request_kwargs
    )
    cooldown_keys = _deployment_cooldown_keys_for_request(
        deployment_id=deployment_id,
        route_key=route_key,
        request_kwargs=request_kwargs,
    )
    if not cooldown_keys:
        return

    try:
        setattr(exception, _DEPLOYMENT_COOLDOWN_FAILURE_RECORDED_ATTR, True)
    except Exception:
        pass

    threshold = _deployment_cooldown_failure_threshold()
    cooldown_seconds = _deployment_cooldown_seconds()

    def record(cooldowns: dict[str, Any], now: float) -> list[tuple[str, int, float]]:
        started: list[tuple[str, int, float]] = []
        for cooldown_key in cooldown_keys:
            state = cooldowns.get(cooldown_key)
            if not isinstance(state, dict):
                state = {}
                cooldowns[cooldown_key] = state
            else:
                existing_cooldown_until = float(state.get("cooldown_until") or 0.0)
                if existing_cooldown_until > 0 and existing_cooldown_until <= now:
                    state["failures"] = 0
                    state["cooldown_until"] = 0.0

            failures = int(state.get("failures") or 0) + 1
            state["failures"] = failures
            state["last_failure_at"] = now
            state["deployment_id"] = deployment_id
            state["route_key"] = route_key

            if failures >= threshold:
                cooldown_until = now + cooldown_seconds
                previous_until = float(state.get("cooldown_until") or 0.0)
                state["cooldown_until"] = cooldown_until
                if cooldown_until > previous_until:
                    started.append((cooldown_key, failures, cooldown_until))
        return started

    result = _deployment_cooldown_update_shared(record)
    if isinstance(result, tuple) and isinstance(result[0], list):
        started_entries = result[0]
        now = result[1]
    else:
        now = time.time()
        with _DEPLOYMENT_COOLDOWN_LOCK:
            started_entries = record(_DEPLOYMENT_COOLDOWNS, now)

    if started_entries:
        first_cooldown_key, first_failures, first_cooldown_until = started_entries[0]
        _trace_module._route_trace(
            "deployment_cooldown_started",
            request_id=_trace_request_id(request_kwargs),
            session=_trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=deployment_id,
            route_key=route_key,
            cooldown_key=first_cooldown_key,
            cooldown_keys=cooldown_keys,
            cooldown_started_keys=[entry[0] for entry in started_entries],
            failures=first_failures,
            failure_threshold=threshold,
            cooldown_seconds=cooldown_seconds,
            cooldown_remaining_seconds=round(max(0.0, first_cooldown_until - now), 3),
            exception=_trace_exception(exception),
        )


def _record_deployment_success_for_cooldown(request_kwargs: Optional[dict]) -> None:
    cooldown_keys = _deployment_cooldown_keys_from_request(request_kwargs)
    if not cooldown_keys:
        return

    def clear(cooldowns: dict[str, Any], _now: float) -> list[dict[str, Any]]:
        cleared: list[dict[str, Any]] = []
        for cooldown_key in cooldown_keys:
            state = cooldowns.pop(cooldown_key, None)
            if isinstance(state, dict) and state:
                state = state.copy()
                state["cooldown_key"] = cooldown_key
                cleared.append(state)
        return cleared

    result = _deployment_cooldown_update_shared(clear)
    if isinstance(result, tuple) and isinstance(result[0], list):
        cleared_states = result[0]
    else:
        with _DEPLOYMENT_COOLDOWN_LOCK:
            cleared_states = clear(_DEPLOYMENT_COOLDOWNS, time.time())
    if not cleared_states:
        return
    state = cleared_states[0]

    _trace_module._route_trace(
        "deployment_cooldown_cleared",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=state.get("deployment_id") or _deployment_id_from_request(request_kwargs),
        route_key=state.get("route_key") or _deployment_route_key_from_request(request_kwargs),
        cooldown_key=state.get("cooldown_key"),
        cooldown_keys=[entry.get("cooldown_key") for entry in cleared_states],
        failures=state.get("failures"),
    )


def _deployment_cooldown_trace_entry(
    deployment: dict,
    state: dict[str, Any],
    now: float,
    *,
    cooldown_key: Optional[str] = None,
) -> dict[str, Any]:
    entry = _trace_deployment(deployment)
    entry["cooldown_key"] = cooldown_key or _deployment_cooldown_key_from_deployment(deployment)
    entry["cooldown_failures"] = state.get("failures")
    entry["cooldown_remaining_seconds"] = round(
        max(0.0, float(state.get("cooldown_until") or 0.0) - now),
        3,
    )
    return entry


def _with_active_deployment_cooldowns(
    deployments: List[dict],
    *,
    request_kwargs: Optional[dict] = None,
) -> tuple[List[dict], list[dict[str, Any]], bool]:
    if not deployments or not _deployment_cooldown_enabled():
        return deployments, [], False

    def filter_active(cooldowns: dict[str, Any], now: float) -> tuple[List[dict], list[dict[str, Any]], bool]:
        available: list[dict] = []
        cooled: list[dict[str, Any]] = []
        for deployment in deployments:
            requested_surface = _deployment_cooldown_surface(request_kwargs)
            if requested_surface is not None:
                cooldown_keys = _deployment_cooldown_keys_from_deployment_for_request(
                    deployment,
                    request_kwargs,
                )
                active_cooldown = next(
                    (
                        (cooldown_key, state)
                        for cooldown_key in cooldown_keys
                        if (
                            state := _active_cooldown_state_for_key(
                                cooldowns, cooldown_key, now
                            )
                        )
                        is not None
                    ),
                    None,
                )
            else:
                active_cooldown = None
                configured_surfaces = _deployment_supported_surface_modes(
                    deployment
                )
                if configured_surfaces:
                    available_surface = _first_available_deployment_surface(
                        deployment, cooldowns, now
                    )
                else:
                    available_surface = ""
                    for cooldown_key in _deployment_cooldown_keys_from_deployment(
                        deployment
                    ):
                        state = _active_cooldown_state_for_key(
                            cooldowns, cooldown_key, now
                        )
                        if state is not None:
                            active_cooldown = (cooldown_key, state)
                            break
                if configured_surfaces and not available_surface:
                    base_keys = _deployment_cooldown_keys_from_deployment(deployment)
                    for surface in configured_surfaces:
                        for base_key in base_keys:
                            cooldown_key = f"{base_key}|surface:{surface}"
                            state = _active_cooldown_state_for_key(
                                cooldowns, cooldown_key, now
                            )
                            if state is not None:
                                active_cooldown = (cooldown_key, state)
                                break
                        if active_cooldown is not None:
                            break
            if active_cooldown is not None:
                cooldown_key, state = active_cooldown
                cooled.append(_deployment_cooldown_trace_entry(deployment, state, now, cooldown_key=cooldown_key))
                continue
            available.append(deployment)

        if cooled:
            return available, cooled, True
        return deployments, [], False

    result = _deployment_cooldown_update_shared(filter_active)
    if isinstance(result, tuple) and isinstance(result[0], tuple):
        return result[0]
    with _DEPLOYMENT_COOLDOWN_LOCK:
        return filter_active(_DEPLOYMENT_COOLDOWNS, time.time())


def _router_configured_deployments(
    router: Any,
    model_name: str,
    *,
    team_id: Any = None,
) -> List[dict]:
    getter = getattr(router, "_get_all_deployments", None)
    if not callable(getter):
        return []
    original_getter = getattr(getter, "_original_get_all_deployments", None)
    if original_getter is None:
        original_getter = getattr(
            getattr(getter, "__func__", None),
            "_original_get_all_deployments",
            None,
        )

    if callable(original_getter):
        if getattr(original_getter, "__self__", None) is not None:
            deployments = original_getter(model_name=model_name, team_id=team_id)
        else:
            deployments = original_getter(router, model_name=model_name, team_id=team_id)
    else:
        token = _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(None)
        try:
            deployments = getter(model_name=model_name, team_id=team_id)
        finally:
            _CURRENT_EXCLUDED_DEPLOYMENT_IDS.reset(token)

    if isinstance(deployments, list):
        return deployments
    if deployments is None:
        return []
    try:
        return list(deployments)
    except Exception:
        return []


def _is_no_deployments_available_error(exception: BaseException) -> bool:
    text = _exception_text(exception) if isinstance(exception, Exception) else str(exception).lower()
    if "no deployments available" in text:
        return True
    if "no healthy deployment" in text:
        return True
    if "available model group fallbacks=none" in text and "deployment" in text:
        return True
    return type(exception).__name__ == "RouterRateLimitError" and "deployment" in text


def _exception_body_reason(exception: Exception) -> Optional[str]:
    body = getattr(exception, "body", None)
    if isinstance(body, dict):
        reason = body.get("reason")
        if isinstance(reason, str) and reason.strip():
            return reason
    return None


def _exception_body(exception: Exception) -> dict:
    body = getattr(exception, "body", None)
    return body if isinstance(body, dict) else {}


def _is_local_stream_timeout_error(exception: Exception) -> bool:
    return _exception_body_reason(exception) in {
        "stream_idle_timeout",
        "stream_start_timeout",
    }


def _is_local_stream_start_timeout_error(exception: Exception) -> bool:
    if _exception_body_reason(exception) != "stream_start_timeout":
        return False
    body = _exception_body(exception)
    if body.get("saw_chunk") is True:
        return False
    buffered_chunks = _safe_float(body.get("buffered_chunks"))
    if buffered_chunks is not None and buffered_chunks > 0:
        return False
    return True


def _is_constrained_no_deployments_error(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> bool:
    if not _is_no_deployments_available_error(exception):
        return False
    if not isinstance(request_kwargs, dict):
        return False
    return (
        _image_generation_module._request_target_order(request_kwargs) is not None
        and bool(_image_generation_module._request_excluded_deployment_ids(request_kwargs))
    )


def _mark_no_deployments_for_order_exhaustion(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    if not _is_no_deployments_available_error(exception):
        return
    target_order = _image_generation_module._request_target_order(request_kwargs)
    if target_order is None:
        return
    if _responses_execution_module._failed_deployment_order(exception) is None:
        try:
            exception.failed_deployment_order = target_order  # type: ignore[attr-defined]
        except Exception:
            pass
    _sync_failed_deployment_exclusions(request_kwargs, exception)


def _raise_retryable_stream_disconnect(
    request_data: dict,
    *,
    original_exception: Exception,
    fallback_exception: Optional[Exception],
) -> None:
    trigger_exception = fallback_exception or original_exception
    _trace_module._route_trace(
        "retryable_stream_disconnect",
        request_id=_trace_request_id(request_data),
        session=_trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        original_exception=_trace_exception(original_exception),
        exception=_trace_exception(trigger_exception),
    )
    raise asyncio.CancelledError(
        "retryable upstream route exhaustion before any stream chunk"
    ) from trigger_exception


def _should_sanitize_final_upstream_route_error(exception: Exception) -> bool:
    if _is_sanitized_upstream_route_failure_error(exception):
        return False
    if _is_context_size_error(exception):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_image_generation_tool_runtime_fallback_error(exception):
        return True
    if _is_upstream_deployment_failover_error(exception):
        return True
    if _exception_indicates_network_connectivity_error(exception):
        return True
    if _is_no_deployments_available_error(exception):
        return True
    if _is_upstream_gateway_bad_request_error(exception):
        return True
    if _is_image_parameter_or_capability_bad_request_error(exception):
        return True
    if _is_deployment_compatible_bad_request_error(exception):
        return True
    status_code = _exception_status_code(exception)
    if status_code in (408, 429):
        return True
    if status_code is not None and status_code >= 500:
        return True
    if type(exception).__name__ in _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES:
        return True
    text = _exception_text(exception)
    return any(marker in text for marker in _UPSTREAM_TEMPORARY_ERROR_MARKERS)


def _should_retry_final_upstream_route_error(
    exception: Exception,
    request_kwargs: Optional[dict] = None,
) -> bool:
    if _is_local_stream_timeout_error(exception):
        return False
    if _is_context_size_error(exception):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _exception_indicates_network_connectivity_error(exception):
        return True
    if _is_upstream_deployment_failover_error(exception):
        return False
    if _is_deployment_compatible_bad_request_error(exception):
        return False
    if _is_constrained_no_deployments_error(exception, request_kwargs):
        return False
    return _should_sanitize_final_upstream_route_error(exception)


def _should_retry_same_deployment_before_fallback(exception: Exception) -> bool:
    if _is_local_stream_timeout_error(exception):
        return False
    if _is_context_size_error(exception):
        return False
    if _exception_indicates_timeout_or_long_wait(exception):
        return True
    if _exception_indicates_network_connectivity_error(exception):
        return True
    if _exception_status_code(exception) == 429:
        return True
    text = _exception_text(exception)
    return any(
        marker in text
        for marker in (
            "high demand",
            "selected model is at capacity",
            "model is at capacity",
            "server is at capacity",
            "service is at capacity",
            "capacity reached",
            "concurrency limit",
            "rate limit",
            "retry later",
            "try again later",
            "too many requests",
        )
    )


async def _sleep_before_final_route_retry(
    model: Optional[str],
    exception: Exception,
    request_kwargs: dict,
    *,
    attempt: int,
    max_retries: int,
    configured_delay_seconds: float,
) -> None:
    delay_seconds = _route_exhaustion_retry_delay_for_exception(
        exception,
        configured_delay_seconds,
    )
    _trace_module._route_trace(
        "final_route_retry",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        model_group=model or _responses_execution_module._request_model_group(request_kwargs),
        retry_attempt=attempt,
        max_retries=max_retries,
        retry_delay_seconds=delay_seconds,
        configured_retry_delay_seconds=configured_delay_seconds,
        exception=_trace_exception(exception),
    )
    no_deployments_available = _is_no_deployments_available_error(exception)
    _streaming_module._reset_route_exhaustion_retry_state(
        request_kwargs,
        exception,
        preserve_failed_deployment=(
            not no_deployments_available
            and not _should_retry_same_deployment_before_fallback(exception)
        ),
        preserve_existing_exclusions=no_deployments_available,
    )
    if delay_seconds > 0:
        await asyncio.sleep(delay_seconds)


def _sanitized_upstream_route_failure_message(
    model: Optional[str],
    exception: Exception,
    request_kwargs: Optional[dict],
) -> str:
    model_group = model or _responses_execution_module._request_model_group(request_kwargs) or "requested model"
    status_code = _exception_status_code(exception)
    if _is_upstream_deployment_failover_error(exception):
        reason = "upstream auth or balance error"
    elif status_code == 429:
        reason = "upstream rate limit"
    elif status_code == 408:
        reason = "upstream timeout"
    elif _exception_indicates_network_connectivity_error(exception):
        reason = "temporary network connectivity error"
    elif _is_upstream_gateway_bad_request_error(exception):
        reason = "temporary upstream gateway bad request"
    elif _is_deployment_compatible_bad_request_error(exception):
        reason = "upstream request compatibility error"
    elif status_code is not None and status_code >= 500:
        reason = "temporary upstream server error"
    elif _is_no_deployments_available_error(exception):
        reason = "no healthy upstream route"
    else:
        reason = "temporary upstream error"
    prefix = (
        "Upstream route failure"
        if _is_upstream_deployment_failover_error(exception)
        else "Temporary upstream route failure"
    )
    return (
        f"{prefix} for {model_group} ({reason}) "
        "after LiteLLM fallback retries. Retry later or choose another model route."
    )


def _sanitized_upstream_route_exception(
    model: Optional[str],
    exception: Exception,
    request_kwargs: Optional[dict],
) -> Exception:
    message = _sanitized_upstream_route_failure_message(model, exception, request_kwargs)
    model_group = model or _responses_execution_module._request_model_group(request_kwargs) or ""
    error_cls = getattr(
        litellm,
        "ServiceUnavailableError",
        getattr(litellm, "InternalServerError", RuntimeError),
    )
    try:
        sanitized = error_cls(
            message=message,
            llm_provider="litellm-menu",
            model=model_group,
        )
    except TypeError:
        try:
            sanitized = error_cls(
                message=message,
                model=model_group,
                llm_provider="litellm-menu",
            )
        except TypeError:
            sanitized = RuntimeError(message)
    try:
        setattr(sanitized, _SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR, True)
    except Exception:
        pass
    try:
        sanitized.status_code = _SANITIZED_UPSTREAM_ROUTE_FAILURE_STATUS_CODE  # type: ignore[attr-defined]
    except Exception:
        pass
    for attr in (
        "failed_deployment_id",
        "failed_deployment_route_key",
        "failed_deployment_order",
        "excluded_deployment_ids",
        "num_retries",
        "max_retries",
    ):
        value = getattr(exception, attr, None)
        if value is None:
            continue
        try:
            setattr(sanitized, attr, value)
        except Exception:
            pass
    try:
        sanitized.original_exception_class = type(exception).__name__  # type: ignore[attr-defined]
    except Exception:
        pass
    return sanitized


def _raise_sanitized_upstream_route_failure(
    model: Optional[str],
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    sanitized = _sanitized_upstream_route_exception(model, exception, request_kwargs)
    _trace_module._route_trace(
        "sanitized_upstream_route_failure",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        model_group=model or _responses_execution_module._request_model_group(request_kwargs),
        excluded_deployment_ids=(request_kwargs or {}).get("_excluded_deployment_ids"),
        exception=_trace_exception(exception),
        client_message=_sanitized_upstream_route_failure_message(
            model,
            exception,
            request_kwargs,
        ),
    )
    raise sanitized from exception


def _is_upstream_surface_failover_error(exception: Exception) -> bool:
    return bool(
        getattr(exception, "upstream_surface_unsupported", False)
        and _responses_execution_module._failed_deployment_id(exception)
    )


def _is_current_upstream_surface_incompatible_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    current_surface = _request_current_upstream_surface(request_kwargs)
    if not current_surface:
        current_surface = _request_current_upstream_surface(outer_request_kwargs)
    if not current_surface:
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False

    status_code = _exception_status_code(exception)
    if status_code in {404, 405}:
        return True
    if (
        current_surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
        and _is_responses_schema_unsupported_error(exception)
    ):
        return True
    if status_code not in {400, 422}:
        return False

    text = _exception_text(exception)
    endpoint_markers = (
        "endpoint not found",
        "unknown endpoint",
        "unsupported endpoint",
        "endpoint is not supported",
        "method not allowed",
        "unsupported api protocol",
        "unsupported protocol",
    )
    if any(marker in text for marker in endpoint_markers):
        return True
    if current_surface == _UPSTREAM_URL_SURFACE_ANTHROPIC:
        return any(
            marker in text
            for marker in (
                "messages api is not supported",
                "messages endpoint is not supported",
                "anthropic messages is not supported",
            )
        )
    if current_surface == _UPSTREAM_URL_SURFACE_OPENAI_CHAT:
        return any(
            marker in text
            for marker in (
                "chat completions api is not supported",
                "chat completions endpoint is not supported",
                "chat/completions is not supported",
            )
        )
    return False


def _is_responses_endpoint_not_found_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> bool:
    if not (
        _image_generation_module._request_is_responses_api(request_kwargs)
        or _image_generation_module._request_is_responses_api(outer_request_kwargs)
    ):
        return False
    if _exception_status_code(exception) != 404:
        return False
    text = _exception_text(exception)
    return "not found" in text


def _is_deployment_compatible_bad_request_error(exception: Exception) -> bool:
    if _exception_status_code(exception) != 400:
        return False
    if _is_responses_schema_unsupported_error(exception):
        return False
    if _is_upstream_gateway_bad_request_error(exception):
        return True
    text = _exception_text(exception)
    if "openaiexception" not in text and "openai exception" not in text:
        return False
    if "bad_response_status_code" in text:
        return not any(
            marker in text
            for marker in (
                "authentication",
                "api key",
                "permission",
                "policy",
                "content_policy",
                "content policy",
                "insufficient_quota",
                "quota",
            )
        )
    if "invalid_request_error" not in text:
        return False
    if any(
        marker in text
        for marker in (
            "authentication",
            "api key",
            "permission",
            "policy",
            "content_policy",
            "content policy",
            "insufficient_quota",
            "quota",
        )
    ):
        return False
    return True


def _is_responses_schema_unsupported_error(exception: Exception) -> bool:
    if _exception_status_code(exception) not in (400, 422):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    text = _exception_text(exception)
    if not text:
        return False
    if not any(
        marker in text
        for marker in (
            "invalid responses api request",
            "invalid_prompt",
            "responses api request",
        )
    ):
        return False
    return any(
        marker in text
        for marker in (
            "invalid_union",
            "invalid_type",
            "invalid_value",
            "invalid input: expected",
            "expected string, received array",
            "expected array, received undefined",
            "expected object, received",
            "expected string",
            "expected array",
        )
    )


def _is_xhigh_reasoning_unsupported_error(exception: Exception) -> bool:
    if _exception_status_code(exception) != 400:
        return False
    text = _exception_text(exception)
    if _XHIGH_REASONING_EFFORT not in text:
        return False
    if any(
        re.search(pattern, text)
        for pattern in (
            r"\bxhigh\b[^.\n]{0,80}\b(?:not supported|unsupported|not allowed)\b",
            r"\b(?:not support|does not support|not supported|unsupported|not allowed)\b[^.\n]{0,80}\bxhigh\b",
            r"不支持\s*xhigh",
            r"xhigh\s*不支持",
        )
    ):
        return True
    if (
        any(
            marker in text
            for marker in (
                "valid levels",
                "valid values",
                "supported values",
                "input should be",
                "expected",
                "only supports",
                "must be one of",
                "should be one of",
                "allowed values",
                "只支持",
            )
        )
        and all(
            re.search(rf"(?<![a-z0-9_]){level}(?![a-z0-9_])", text)
            for level in ("low", "medium", "high")
        )
    ):
        return True
    return False


def _valid_chat_tool_name(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    name = value.strip()
    if not _CHAT_TOOL_NAME_PATTERN.match(name):
        return None
    return name
