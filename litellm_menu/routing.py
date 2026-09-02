from __future__ import annotations

from . import responses_output as _responses_output_module
from . import image_inputs as _image_inputs_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import api_base as _api_base_module
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
    _CODEX_COMPACTION_STREAM_START_TIMEOUT_DEFAULT_SECONDS,
    _CODEX_COMPACTION_STREAM_START_TIMEOUT_SECONDS_ENV,
    _CODEX_COMPACTION_CAPABILITIES,
    _CODEX_COMPACTION_CAPABILITY_DEFAULT_TTL_SECONDS,
    _CODEX_COMPACTION_CAPABILITY_LOCK,
    _CODEX_COMPACTION_CAPABILITY_PROBE_METADATA_KEY,
    _CODEX_COMPACTION_CAPABILITY_TTL_SECONDS_ENV,
    _CODEX_COMPACTION_CAPABILITY_UNSUPPORTED_ATTR,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_SURFACE_TARGET_DEPLOYMENT_ID,
    _CURRENT_UPSTREAM_URL_SURFACE_KEY,
    _CURRENT_SELECTED_DEPLOYMENT,
    _CURRENT_SELECTED_DEPLOYMENT_BOX,
    _DEPLOYMENT_COOLDOWNS,
    _DEPLOYMENT_COOLDOWN_COMPACTION_DEFAULT_ENABLED,
    _DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV,
    _DEPLOYMENT_COOLDOWN_DEFAULT_FAILURES,
    _DEPLOYMENT_COOLDOWN_DEFAULT_SECONDS,
    _DEPLOYMENT_COOLDOWN_FILE_ENV,
    _DEPLOYMENT_COOLDOWN_FAILURES_ENV,
    _DEPLOYMENT_COOLDOWN_FAILURE_RECORDED_ATTR,
    _DEPLOYMENT_COOLDOWN_LOCK,
    _DEPLOYMENT_COOLDOWN_ORDINARY_DEFAULT_ENABLED,
    _DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV,
    _DEPLOYMENT_COOLDOWN_SECONDS_ENV,
    _IMAGE_GENERATION_TOOL_ALL_UNSUPPORTED_ATTR,
    _IMAGE_GENERATION_TOOL_CAPABILITY_UNSUPPORTED_ATTR,
    _IMAGE_GENERATION_TOOL_UNSUPPORTED,
    _IMAGE_GENERATION_TOOL_UNSUPPORTED_DEFAULT_TTL_SECONDS,
    _IMAGE_GENERATION_TOOL_UNSUPPORTED_LOCK,
    _IMAGE_GENERATION_TOOL_UNSUPPORTED_METADATA_KEY,
    _IMAGE_GENERATION_TOOL_UNSUPPORTED_TTL_SECONDS_ENV,
    _WEB_SEARCH_TOOL_UNSUPPORTED,
    _WEB_SEARCH_TOOL_CAPABILITY_UNSUPPORTED_ATTR,
    _WEB_SEARCH_TOOL_UNSUPPORTED_CACHE_HIT_KEY,
    _WEB_SEARCH_TOOL_UNSUPPORTED_DEFAULT_TTL_SECONDS,
    _WEB_SEARCH_TOOL_UNSUPPORTED_LOCK,
    _WEB_SEARCH_TOOL_UNSUPPORTED_METADATA_KEY,
    _WEB_SEARCH_TOOL_UNSUPPORTED_TTL_SECONDS_ENV,
    _HOSTED_WEB_SEARCH_TOOL_TYPES,
    _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES,
    _PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY,
    _PROTOCOL_FALLBACK_CACHE_HIT_KEY,
    _PROTOCOL_FALLBACK_DEFAULT_TTL_SECONDS,
    _PROTOCOL_FALLBACK_FAILURE_RECORDED_ATTR,
    _PROTOCOL_FALLBACK_FAILURE_RECORDED_KEY,
    _PROTOCOL_FALLBACK_FROM_SURFACE_KEY,
    _PROTOCOL_FALLBACK_LOCK,
    _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
    _PROTOCOL_FALLBACKS,
    _PROTOCOL_FALLBACK_TTL_SECONDS_ENV,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_LOCK,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_MAX,
    _EXTERNAL_WEB_SEARCH_STARTED_REQUESTS_TTL_SECONDS,
    _LITELLM_MODEL_GROUP_FALLBACK_EXHAUSTED_MARKERS,
    _RESPONSES_IMAGE_INPUT_SUPPORT_KEY,
    _UPSTREAM_PROTOCOL_MODE_FALLBACK,
    _UPSTREAM_PROTOCOL_MODE_FIXED,
    _UPSTREAM_PROTOCOL_MODE_KEY,
    _RECOVERY_INTERVAL_DEFAULT_SECONDS,
    _RECOVERY_INTERVAL_SECONDS_ENV,
    _RECOVERY_POLICY_BALANCE_ENV,
    _RECOVERY_POLICY_COOLDOWN,
    _RECOVERY_POLICY_ERROR,
    _RECOVERY_POLICY_NETWORK_ENV,
    _RECOVERY_POLICY_RATE_LIMIT_ENV,
    _RECOVERY_POLICY_REQUEST_ERROR_ENV,
    _RECOVERY_POLICY_SERVER_ENV,
    _RECOVERY_POLICY_STREAM_IDLE_TIMEOUT_ENV,
    _RECOVERY_POLICY_STREAM_START_TIMEOUT_ENV,
    _RECOVERY_POLICY_VALUES,
    _RECOVERY_POLICY_RECOVERY,
    _RECOVERY_MAX_DEFAULT_SECONDS,
    _RECOVERY_MAX_SECONDS_ENV,
    _REQUEST_TIMEOUT_DEFAULT_SECONDS,
    _REQUEST_TIMEOUT_SECONDS_ENV,
    _ROUTE_RECOVERY_POLL_METADATA_KEY,
    _ROUTE_FAILURE_POLICY_ATTR,
    _RouteOrder,
    _SAME_DEPLOYMENT_RETRY_EXHAUSTED_ATTR,
    _RouteRecoveryStreamResponse,
    _SANITIZED_UPSTREAM_ROUTE_FAILURE_ATTR,
    _SANITIZED_UPSTREAM_ROUTE_FAILURE_POLICY_ATTR,
    _SANITIZED_UPSTREAM_ROUTE_FAILURE_STATUS_CODE,
    _SAME_DEPLOYMENT_RETRIES_DEFAULT,
    _SAME_DEPLOYMENT_RETRIES_ENV,
    _SESSION_ID_KEY_FRAGMENTS,
    _SESSION_NAME_KEY_FRAGMENTS,
    _STALL_TIMEOUT_DEFAULT_SECONDS,
    _STALL_TIMEOUT_SECONDS_ENV,
    _STREAM_START_TIMEOUT_DEFAULT_SECONDS,
    _STREAM_START_TIMEOUT_SECONDS_ENV,
    _STREAM_ROUTE_EXHAUSTION_DEFAULT_RETRIES,
    _STREAM_ROUTE_EXHAUSTION_RETRY_AFTER_MAX_SECONDS,
    _SUPPORTS_RESPONSES_CLIENT_TOOLS_KEY,
    _SUPPORTS_RESPONSES_HOSTED_TOOLS_KEY,
    _SUPPORTS_RESPONSES_WEB_SEARCH_KEY,
    _SUPPORTS_WEB_SEARCH_KEY,
    _SURFACE_TARGET_DEPLOYMENT_ID_KEY,
    _TerminalFailedResponsesStreamResponse,
    _UPSTREAM_BALANCE_ERROR_MARKERS,
    _UPSTREAM_HTML_BAD_REQUEST_MARKERS,
    _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES,
    _UPSTREAM_TEMPORARY_ERROR_MARKERS,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _UPSTREAM_URL_SURFACE_DEPLOYMENT_ID_KEY,
    _UPSTREAM_URL_SURFACE_KEY,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_CHAT_BRIDGE_VALUES,
    _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _WEB_SEARCH_EXTERNAL_STARTED_METADATA_KEY,
    _XHIGH_REASONING_EFFORT,
    asyncio,
    datetime,
    json,
    litellm,
    math,
    os,
    re,
    time,
    threading,
    timezone,
)


_FIRST_STREAM_OUTPUT_TIME_KEY = "_litellm_menu_first_stream_output_time"
_REQUEST_STARTED_TIME_KEY = "_litellm_menu_request_started_time"
_FIRST_STREAM_OUTPUT_TIMES: dict[str, tuple[float, datetime]] = {}
_REQUEST_STARTED_TIMES: dict[str, tuple[float, datetime]] = {}
_FIRST_STREAM_OUTPUT_TIMES_LOCK = threading.Lock()
_FIRST_STREAM_OUTPUT_TIMES_MAX = 4096
_FIRST_STREAM_OUTPUT_TIMES_TTL_SECONDS = 600.0


class _SelectedDeploymentMarkerStream:
    """Carry the route selected before a streaming iterator is consumed."""

    def __init__(self, response: Any, marker: dict[str, Any]) -> None:
        self._response = response
        self._iterator = response.__aiter__()
        self._litellm_menu_selected_deployment_marker = marker

    def __aiter__(self) -> AsyncIterator[Any]:
        return self

    async def __anext__(self) -> Any:
        return await self._iterator.__anext__()

    async def aclose(self) -> None:
        close = getattr(self._iterator, "aclose", None)
        if not callable(close):
            return
        result = close()
        if hasattr(result, "__await__"):
            await result


def _wrap_response_with_selected_deployment_marker(
    response: Any,
    marker: Any,
) -> Any:
    if not isinstance(marker, dict) or not callable(getattr(response, "__aiter__", None)):
        return response
    if isinstance(
        response,
        (_RouteRecoveryStreamResponse, _TerminalFailedResponsesStreamResponse),
    ):
        return response
    if _selected_deployment_marker_from_response(response) is not None:
        return response
    return _SelectedDeploymentMarkerStream(response, marker)


def _selected_deployment_marker_from_response(response: Any) -> Optional[dict[str, Any]]:
    marker = getattr(response, "_litellm_menu_selected_deployment_marker", None)
    return marker if isinstance(marker, dict) else None


def _merge_request_routing_state_into_selected_deployment_marker(
    marker: Any,
    request_kwargs: Optional[dict],
) -> None:
    if not isinstance(marker, dict) or not isinstance(request_kwargs, dict):
        return
    excluded_ids = _responses_request_module._request_excluded_deployment_ids(marker)
    excluded_ids.update(
        _responses_request_module._request_excluded_deployment_ids(request_kwargs)
    )
    if excluded_ids:
        marker["_excluded_deployment_ids"] = sorted(excluded_ids)


def _remember_request_time(
    store: dict[str, tuple[float, datetime]],
    request_id: str,
    observed_at: datetime,
    *,
    replace: bool = True,
) -> None:
    now = time.monotonic()
    with _FIRST_STREAM_OUTPUT_TIMES_LOCK:
        stale_before = now - _FIRST_STREAM_OUTPUT_TIMES_TTL_SECONDS
        for key, (recorded_at, _) in list(store.items()):
            if recorded_at < stale_before:
                store.pop(key, None)
        if len(store) >= _FIRST_STREAM_OUTPUT_TIMES_MAX:
            oldest = min(store, key=lambda key: store[key][0])
            store.pop(oldest, None)
        if not replace and request_id in store:
            return
        store[request_id] = (now, observed_at)


def _record_request_started_time(request_kwargs: Optional[dict]) -> None:
    if not isinstance(request_kwargs, dict):
        return
    started_at = request_kwargs.get(_REQUEST_STARTED_TIME_KEY)
    if not isinstance(started_at, datetime):
        # Keep the timestamp in the bounded correlation store instead of
        # adding a datetime-valued private kwarg to LiteLLM's provider call.
        # Anthropic-compatible handlers serialize unknown kwargs directly and
        # reject that otherwise internal value before reaching the upstream.
        started_at = datetime.now(timezone.utc)
    request_id = _trace_request_id(request_kwargs)
    if request_id and isinstance(started_at, datetime):
        _remember_request_time(
            _REQUEST_STARTED_TIMES,
            request_id,
            started_at,
            replace=False,
        )


def _record_first_stream_output_time(
    request_kwargs: Optional[dict],
    observed_at: Optional[datetime] = None,
) -> None:
    if not isinstance(request_kwargs, dict):
        return
    if observed_at is None:
        observed_at = request_kwargs.get(_FIRST_STREAM_OUTPUT_TIME_KEY)
    if not isinstance(observed_at, datetime):
        return
    request_id = _trace_request_id(request_kwargs)
    if not request_id:
        return
    _remember_request_time(_FIRST_STREAM_OUTPUT_TIMES, request_id, observed_at)


def _correlated_request_time(
    request_kwargs: Optional[dict],
    key: str,
    store: dict[str, tuple[float, datetime]],
) -> Optional[datetime]:
    if not isinstance(request_kwargs, dict):
        return None
    direct = request_kwargs.get(key)
    if isinstance(direct, datetime):
        return direct
    request_id = _trace_request_id(request_kwargs)
    if not request_id:
        return None
    now = time.monotonic()
    with _FIRST_STREAM_OUTPUT_TIMES_LOCK:
        recorded = store.get(request_id)
        if recorded is None:
            return None
        recorded_at, observed_at = recorded
        if now - recorded_at > _FIRST_STREAM_OUTPUT_TIMES_TTL_SECONDS:
            store.pop(request_id, None)
            return None
        return observed_at


def _first_stream_output_time(request_kwargs: Optional[dict]) -> Optional[datetime]:
    return _correlated_request_time(
        request_kwargs,
        _FIRST_STREAM_OUTPUT_TIME_KEY,
        _FIRST_STREAM_OUTPUT_TIMES,
    )


def _request_started_time(request_kwargs: Optional[dict]) -> Optional[datetime]:
    return _correlated_request_time(
        request_kwargs,
        _REQUEST_STARTED_TIME_KEY,
        _REQUEST_STARTED_TIMES,
    )



def _event_time(value: Any) -> Optional[str]:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            # LiteLLM's callback can hand us a naive local wall-clock value.
            # Treating that as UTC makes every request look offset from the
            # user's actual completion time in the native log viewer.
            value = value.astimezone()
        return (
            value.astimezone(timezone.utc)
            .replace(microsecond=0)
            .isoformat()
            .replace("+00:00", "Z")
        )
    return None


def _duration_ms(start_time: Any, end_time: Any) -> Optional[int]:
    if isinstance(start_time, datetime) and isinstance(end_time, datetime):
        try:
            return max(0, int((end_time - start_time).total_seconds() * 1000))
        except Exception:
            return None
    return None


def _completion_start_time(
    request_kwargs: Optional[dict],
    end_time: Any = None,
) -> tuple[Optional[datetime], Optional[str]]:
    if not isinstance(request_kwargs, dict):
        return None, None
    raw_completion_start_time = _first_stream_output_time(request_kwargs)
    source = "stream_output_observed"
    if raw_completion_start_time is None:
        raw_completion_start_time = request_kwargs.get("completion_start_time")
        source = "litellm_completion_start_time"
    if raw_completion_start_time is None:
        standard = _as_dict(request_kwargs.get("standard_logging_object"))
        raw_completion_start_time = _first_not_none(
            standard.get("completionStartTime"),
            standard.get("completion_start_time"),
        )
        source = "litellm_standard_logging"
    completion_start_time = raw_completion_start_time
    if isinstance(completion_start_time, (int, float)):
        try:
            completion_start_time = datetime.fromtimestamp(
                float(completion_start_time),
                tz=timezone.utc,
            )
        except (OverflowError, OSError, ValueError):
            return None, None
    elif isinstance(completion_start_time, str):
        try:
            completion_start_time = datetime.fromisoformat(
                completion_start_time.replace("Z", "+00:00")
            )
        except ValueError:
            return None, None
    if not isinstance(completion_start_time, datetime):
        return None, None
    if completion_start_time.tzinfo is None:
        completion_start_time = completion_start_time.replace(tzinfo=timezone.utc)
    normalized_end_time = end_time
    if isinstance(normalized_end_time, datetime):
        if normalized_end_time.tzinfo is None:
            normalized_end_time = normalized_end_time.replace(tzinfo=timezone.utc)
        if source != "stream_output_observed" and completion_start_time == normalized_end_time:
            # LiteLLM fills a missing completion_start_time with end_time. Keep
            # that distinguishable from an actually observed first token.
            return None, None
    return completion_start_time, source


def _time_to_first_token_ms(
    request_kwargs: Optional[dict],
    start_time: Any,
    end_time: Any = None,
) -> Optional[int]:
    completion_start_time, source = _completion_start_time(request_kwargs, end_time)
    if source is None or completion_start_time is None:
        return None
    observed_request_start = _request_started_time(request_kwargs)
    if observed_request_start is not None:
        start_time = observed_request_start
    if not isinstance(start_time, datetime):
        return None
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    try:
        return max(0, int((completion_start_time - start_time).total_seconds() * 1000))
    except Exception:
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


def _stream_start_timeout_seconds() -> float:
    return _env_float_seconds(
        _STREAM_START_TIMEOUT_SECONDS_ENV,
        _STREAM_START_TIMEOUT_DEFAULT_SECONDS,
        minimum=0.0,
    )


def _codex_compaction_stream_start_timeout_seconds() -> float:
    return _env_float_seconds(
        _CODEX_COMPACTION_STREAM_START_TIMEOUT_SECONDS_ENV,
        _CODEX_COMPACTION_STREAM_START_TIMEOUT_DEFAULT_SECONDS,
        minimum=0.0,
    )


def _request_metadata_positive_float(
    request_data: Optional[dict],
    key: str,
) -> Optional[float]:
    if not isinstance(request_data, dict):
        return None
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(
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
    stream_start_timeout = _stream_start_timeout_seconds()
    request_timeout = _request_timeout_seconds()
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        compaction_timeout = _codex_compaction_stream_start_timeout_seconds()
        if request_timeout <= 0:
            return compaction_timeout
        if compaction_timeout <= 0:
            return request_timeout
        return min(compaction_timeout, request_timeout)
    if stream_start_timeout <= 0:
        return request_timeout
    if request_timeout <= 0:
        return stream_start_timeout
    return min(stream_start_timeout, request_timeout)


def _stream_idle_timeout_seconds_for_request(request_data: Optional[dict]) -> float:
    """Return the local gap budget for the active streaming request.

    Structured Codex compaction emits a bookkeeping event before the upstream
    produces its opaque encrypted compaction item.  That gap can legitimately
    exceed the ordinary stream-idle cap, so use the compaction start budget for
    the post-first-event wait as well.  An explicit zero stall timeout keeps its
    existing meaning and disables the local idle cap.
    """
    stall_timeout = _stall_timeout_seconds()
    if stall_timeout <= 0:
        return 0.0
    if not _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        return stall_timeout
    return max(stall_timeout, _stream_start_timeout_seconds_for_request(request_data))


def _stream_route_exhaustion_retries() -> int:
    return _STREAM_ROUTE_EXHAUSTION_DEFAULT_RETRIES


def _same_deployment_retries() -> int:
    value = os.getenv(_SAME_DEPLOYMENT_RETRIES_ENV, "").strip()
    if not value:
        return _SAME_DEPLOYMENT_RETRIES_DEFAULT
    try:
        return max(0, min(20, int(value)))
    except ValueError:
        return _SAME_DEPLOYMENT_RETRIES_DEFAULT


def _same_deployment_retry_exhausted(exception: Exception) -> bool:
    return bool(getattr(exception, _SAME_DEPLOYMENT_RETRY_EXHAUSTED_ATTR, False))


def _mark_same_deployment_retry_exhausted(exception: Exception) -> None:
    try:
        setattr(exception, _SAME_DEPLOYMENT_RETRY_EXHAUSTED_ATTR, True)
    except Exception:
        pass


def _same_deployment_retry_pending(exception: Exception) -> bool:
    """Whether this failure may stay on its selected deployment.

    A class can be recoverable without being entitled to another attempt on
    the same route: the explicit same-route budget owns that decision. Once
    it is exhausted, all fallback paths must advance.
    """
    return (
        _recovery_policy_for_exception(exception) != _RECOVERY_POLICY_ERROR
        and not _same_deployment_retry_exhausted(exception)
    )


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


def _recovery_policy_setting(name: str, default: str) -> str:
    value = os.getenv(name, "").strip().lower()
    return value if value in _RECOVERY_POLICY_VALUES else default


def _recovery_policy_for_exception(exception: Exception) -> str:
    """Classify an exhausted route failure before deciding to wait or cool down.

    This deliberately does not use a failed deployment id as evidence that an
    error is transient. A deployment-specific 400 is still deterministic until
    a protocol/input compatibility bridge fixes it.
    """
    if _is_image_generation_all_deployments_unsupported_error(exception):
        return _RECOVERY_POLICY_ERROR
    preserved = getattr(exception, _SANITIZED_UPSTREAM_ROUTE_FAILURE_POLICY_ATTR, None)
    if preserved in _RECOVERY_POLICY_VALUES:
        return preserved
    preserved = getattr(exception, _ROUTE_FAILURE_POLICY_ATTR, None)
    if preserved in _RECOVERY_POLICY_VALUES:
        return preserved

    if _is_local_stream_start_timeout_error(exception):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_STREAM_START_TIMEOUT_ENV,
            _RECOVERY_POLICY_COOLDOWN,
        )
    if _is_local_stream_timeout_error(exception):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_STREAM_IDLE_TIMEOUT_ENV,
            _RECOVERY_POLICY_RECOVERY,
        )

    status_code = _exception_status_code(exception)
    text = _exception_text(exception)
    if status_code == 402 or any(
        marker in text for marker in _UPSTREAM_BALANCE_ERROR_MARKERS
    ):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_BALANCE_ENV,
            _RECOVERY_POLICY_COOLDOWN,
        )
    if _is_no_deployments_available_error(exception):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_SERVER_ENV,
            _RECOVERY_POLICY_COOLDOWN,
        )
    if (
        _is_context_size_error(exception)
        or _is_terminal_prompt_or_policy_error(exception)
        or _is_ssl_verification_error(exception)
        or _is_upstream_model_not_found_error(exception)
        or _is_responses_schema_unsupported_error(exception)
        or _is_upstream_gateway_bad_request_error(exception)
        or _is_image_parameter_or_capability_bad_request_error(exception)
        or _is_deployment_compatible_bad_request_error(exception)
        or status_code in {400, 401, 403, 404, 405, 409, 422}
    ):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_REQUEST_ERROR_ENV,
            _RECOVERY_POLICY_ERROR,
        )

    if status_code is None and getattr(exception, "stream_incomplete", False):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_SERVER_ENV,
            _RECOVERY_POLICY_COOLDOWN,
        )

    if _is_network_recovery_exception(exception):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_NETWORK_ENV,
            _RECOVERY_POLICY_RECOVERY,
        )

    if status_code == 429 or any(
        marker in text for marker in _UPSTREAM_TEMPORARY_ERROR_MARKERS
    ):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_RATE_LIMIT_ENV,
            _RECOVERY_POLICY_RECOVERY,
        )

    if (
        status_code == 408
        or (status_code is not None and status_code >= 500)
        or type(exception).__name__ in _UPSTREAM_TEMPORARY_ERROR_CLASS_NAMES
    ):
        return _recovery_policy_setting(
            _RECOVERY_POLICY_SERVER_ENV,
            _RECOVERY_POLICY_COOLDOWN,
        )

    return _recovery_policy_setting(
        _RECOVERY_POLICY_REQUEST_ERROR_ENV,
        _RECOVERY_POLICY_ERROR,
    )


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
        metadata = _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
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
            metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key) or {}
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
        metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key) or {}
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
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        # A compaction request carries the complete signed/encrypted thread
        # history. Replaying it in the shared long-running recovery poll can
        # leave Codex stuck in "compacting context" for hours after an
        # upstream rejection. The normal bounded router fallback still runs;
        # only the cross-route recovery window is disabled.
        return 0.0
    override = _request_metadata_positive_float(
        request_data,
        "route_recovery_max_seconds",
    )
    configured_max_seconds = (
        override if override is not None else _recovery_max_seconds()
    )
    return configured_max_seconds


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
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs,
            metadata_key,
        ) or {}
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
    return _recovery_policy_for_exception(exception) != _RECOVERY_POLICY_ERROR


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
    # Route recovery is a routing decision.  The stream adapter selects the
    # wire method later, but the recovery/cooldown policy must not depend on
    # the client-facing protocol.
    if not _streaming_module._request_supports_streaming_error_fallback(
        request_kwargs
    ):
        return False
    if not _is_route_recovery_poll_error(exception):
        return False
    if _recovery_max_seconds_for_request(request_kwargs) <= 0:
        return False
    if (
        router is not None
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
    return _recovery_policy_for_exception(exception) == _RECOVERY_POLICY_ERROR


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


def _deployment_cooldown_recording_enabled_for_request(
    request_kwargs: Optional[dict],
) -> bool:
    """Whether this request class may add failures to the shared cooldown pool."""
    if _responses_request_module._request_has_structured_codex_compaction(
        request_kwargs
    ):
        return _env_bool(
            _DEPLOYMENT_COOLDOWN_COMPACTION_ENABLED_ENV,
            _DEPLOYMENT_COOLDOWN_COMPACTION_DEFAULT_ENABLED,
        )
    return _env_bool(
        _DEPLOYMENT_COOLDOWN_ORDINARY_ENABLED_ENV,
        _DEPLOYMENT_COOLDOWN_ORDINARY_DEFAULT_ENABLED,
    )


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


_CODEX_COMPACTION_CAPABILITY_PROBE_TIMEOUT_SECONDS = 15.0
# A single shared lease prevents all sixteen proxy workers from discovering an
# expired entry at once and sending the same probe concurrently.  It outlives
# the request timeout by a small margin so a suspended worker cannot leave the
# route unprotected forever.
_CODEX_COMPACTION_CAPABILITY_PROBE_LEASE_SECONDS = 20.0
_CODEX_COMPACTION_CAPABILITY_PROBE_WAIT_SECONDS = 0.05
_CODEX_COMPACTION_CAPABILITY_STATUSES = frozenset({"supported", "unsupported"})
_CODEX_COMPACTION_CAPABILITY_STATE_STATUSES = (
    _CODEX_COMPACTION_CAPABILITY_STATUSES | {"probing"}
)


class CodexCompactionCapabilityUnsupportedError(RuntimeError):
    """A route cannot satisfy the encrypted remote-compaction contract."""

    code = "upstream_compaction_unsupported"

    def __init__(
        self,
        *,
        deployment_id: Optional[str],
        route_key: Optional[str],
    ) -> None:
        message = (
            "The selected upstream route does not support encrypted Responses "
            "compaction. Codex should use its local context-checkpoint summary fallback."
        )
        super().__init__(message)
        self.status_code = 422
        self.failed_deployment_id = deployment_id
        self.failed_deployment_route_key = route_key
        self.body = {
            "error": {
                "type": "invalid_request_error",
                "code": self.code,
                "message": message,
            }
        }
        setattr(self, _CODEX_COMPACTION_CAPABILITY_UNSUPPORTED_ATTR, True)


def _is_codex_compaction_capability_unsupported_error(
    exception: Any,
) -> bool:
    if not isinstance(exception, Exception):
        return False
    if getattr(exception, _CODEX_COMPACTION_CAPABILITY_UNSUPPORTED_ATTR, False) is True:
        return True
    body = getattr(exception, "body", None)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, dict) and error.get("code") == "upstream_compaction_unsupported":
            return True
    text = _exception_text(exception).lower()
    return "upstream_compaction_unsupported" in text


def _codex_compaction_capability_ttl_seconds() -> float:
    value = os.getenv(_CODEX_COMPACTION_CAPABILITY_TTL_SECONDS_ENV, "").strip()
    if not value:
        return _CODEX_COMPACTION_CAPABILITY_DEFAULT_TTL_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _CODEX_COMPACTION_CAPABILITY_DEFAULT_TTL_SECONDS
    if not math.isfinite(parsed):
        return _CODEX_COMPACTION_CAPABILITY_DEFAULT_TTL_SECONDS
    return max(0.0, parsed)


def _is_codex_compaction_capability_probe(
    request_kwargs: Optional[dict],
) -> bool:
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs, metadata_key
        )
        if isinstance(metadata, dict) and metadata.get(
            _CODEX_COMPACTION_CAPABILITY_PROBE_METADATA_KEY
        ) is True:
            return True
    return False


def _request_uses_third_party_codex_compaction_route(
    request_kwargs: Optional[dict],
) -> bool:
    """Whether an encrypted compaction needs a one-time upstream probe.

    The official OpenAI Responses origin already defines the encrypted
    compaction protocol. Every other explicitly configured upstream origin is
    capability-probed instead of trusting a provider/model label.
    """

    if (
        not _responses_request_module._request_has_structured_codex_compaction(
            request_kwargs
        )
        or _is_codex_compaction_capability_probe(request_kwargs)
    ):
        return False
    host = _responses_request_module._api_base_host(
        _responses_request_module._request_api_base(request_kwargs)
    )
    if host:
        return host != "api.openai.com"
    model_info = _request_context_module._request_model_info(request_kwargs)
    provider = model_info.get("provider") or (request_kwargs or {}).get(
        "custom_llm_provider"
    )
    return isinstance(provider, str) and provider.strip().lower() not in {
        "",
        "openai",
        "chatgpt",
    }


def _codex_compaction_capability_cache_key(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    deployment_key = _deployment_cooldown_key_from_request(request_kwargs)
    return f"{deployment_key}|surface:openai/responses" if deployment_key else None


def _codex_compaction_capability_state_map(
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload.setdefault("schema_version", 1)
    states = payload.setdefault("codex_compaction_capabilities", {})
    if not isinstance(states, dict):
        states = {}
        payload["codex_compaction_capabilities"] = states
    return states


def _clean_codex_compaction_capability_state(
    state: Any,
    *,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    status = state.get("status")
    if status not in _CODEX_COMPACTION_CAPABILITY_STATE_STATUSES:
        return None
    try:
        expires_at = float(state.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        return None
    if (
        not math.isfinite(expires_at)
        or expires_at <= 0
        or (now is not None and expires_at <= now)
    ):
        return None
    cleaned = dict(state)
    cleaned["status"] = status
    cleaned["expires_at"] = expires_at
    try:
        detected_at = float(cleaned.get("detected_at") or 0.0)
        cleaned["detected_at"] = detected_at if math.isfinite(detected_at) else 0.0
    except (TypeError, ValueError):
        cleaned["detected_at"] = 0.0
    return cleaned


def _sync_codex_compaction_capabilities_from_shared_locked(
    states: dict[str, Any],
    now: float,
) -> None:
    shared: dict[str, dict[str, Any]] = {}
    for cache_key, state in list(states.items()):
        cleaned = _clean_codex_compaction_capability_state(state, now=now)
        if cleaned is None:
            states.pop(cache_key, None)
            continue
        shared[cache_key] = cleaned
        if cleaned is not state:
            states[cache_key] = cleaned
    with _CODEX_COMPACTION_CAPABILITY_LOCK:
        _CODEX_COMPACTION_CAPABILITIES.clear()
        _CODEX_COMPACTION_CAPABILITIES.update(
            {key: value.copy() for key, value in shared.items()}
        )


def _codex_compaction_capability_update_shared(callback: Any) -> Any:
    path = _deployment_cooldown_file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        states = _codex_compaction_capability_state_map(payload)
        _sync_codex_compaction_capabilities_from_shared_locked(states, now)
        result = callback(states, now)
        _sync_codex_compaction_capabilities_from_shared_locked(states, now)
        return result, now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        return None


def _cached_codex_compaction_capability_status(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    ttl = _codex_compaction_capability_ttl_seconds()
    cache_key = _codex_compaction_capability_cache_key(request_kwargs)
    if ttl <= 0 or not cache_key:
        return None

    def read(states: dict[str, Any], now: float) -> Optional[str]:
        state = _clean_codex_compaction_capability_state(
            states.get(cache_key), now=now
        )
        return str(state.get("status")) if state is not None else None

    result = _codex_compaction_capability_update_shared(read)
    if isinstance(result, tuple) and result[0] in _CODEX_COMPACTION_CAPABILITY_STATUSES:
        return result[0]
    with _CODEX_COMPACTION_CAPABILITY_LOCK:
        state = _clean_codex_compaction_capability_state(
            _CODEX_COMPACTION_CAPABILITIES.get(cache_key), now=time.time()
        )
    return (
        str(state.get("status"))
        if state is not None
        and state.get("status") in _CODEX_COMPACTION_CAPABILITY_STATUSES
        else None
    )


def _codex_compaction_capability_claim_probe(
    request_kwargs: Optional[dict],
) -> tuple[str, Optional[str]]:
    """Atomically claim a tiny probe, or report a final/shared in-flight state.

    The JSON state file is already locked across proxy workers for deployment
    cooldowns, so use the same critical section rather than adding a second
    process-local asyncio lock that would duplicate probes under uvicorn
    workers.
    """

    ttl = _codex_compaction_capability_ttl_seconds()
    cache_key = _codex_compaction_capability_cache_key(request_kwargs)
    if ttl <= 0 or not cache_key:
        return "probe", None
    deployment_id = _deployment_id_from_request(request_kwargs)
    route_key = _deployment_route_key_from_request(request_kwargs)
    lease_id = f"{os.getpid()}:{time.time_ns()}"

    def claim(states: dict[str, Any], now: float) -> tuple[str, Optional[str]]:
        state = _clean_codex_compaction_capability_state(
            states.get(cache_key), now=now
        )
        if state is not None:
            status = str(state.get("status"))
            if status in _CODEX_COMPACTION_CAPABILITY_STATUSES:
                return "cached", status
            if status == "probing":
                return "waiting", None
        states[cache_key] = {
            "status": "probing",
            "lease_id": lease_id,
            "deployment_id": deployment_id,
            "route_key": route_key,
            "detected_at": now,
            "expires_at": now + _CODEX_COMPACTION_CAPABILITY_PROBE_LEASE_SECONDS,
        }
        return "probe", lease_id

    result = _codex_compaction_capability_update_shared(claim)
    if (
        isinstance(result, tuple)
        and isinstance(result[0], tuple)
        and len(result[0]) == 2
        and result[0][0] in {"cached", "waiting", "probe"}
    ):
        return result[0]

    # The Core test harness and unusual read-only runtime roots have no shared
    # file. Keep the same lease semantics per process in that case.
    now = time.time()
    with _CODEX_COMPACTION_CAPABILITY_LOCK:
        state = _clean_codex_compaction_capability_state(
            _CODEX_COMPACTION_CAPABILITIES.get(cache_key), now=now
        )
        if state is not None:
            status = str(state.get("status"))
            if status in _CODEX_COMPACTION_CAPABILITY_STATUSES:
                return "cached", status
            if status == "probing":
                return "waiting", None
        _CODEX_COMPACTION_CAPABILITIES[cache_key] = {
            "status": "probing",
            "lease_id": lease_id,
            "deployment_id": deployment_id,
            "route_key": route_key,
            "detected_at": now,
            "expires_at": now + _CODEX_COMPACTION_CAPABILITY_PROBE_LEASE_SECONDS,
        }
    return "probe", lease_id


def _record_codex_compaction_capability(
    request_kwargs: Optional[dict],
    status: str,
    *,
    lease_id: Optional[str] = None,
) -> None:
    if status not in _CODEX_COMPACTION_CAPABILITY_STATUSES:
        return
    cache_key = _codex_compaction_capability_cache_key(request_kwargs)
    ttl = _codex_compaction_capability_ttl_seconds()
    if not cache_key or ttl <= 0:
        return
    deployment_id = _deployment_id_from_request(request_kwargs)
    route_key = _deployment_route_key_from_request(request_kwargs)
    now = time.time()
    expires_at = now + ttl

    def record(states: dict[str, Any], _now: float) -> bool:
        active = _clean_codex_compaction_capability_state(
            states.get(cache_key), now=_now
        )
        if lease_id is not None and (
            active is None
            or active.get("status") != "probing"
            or active.get("lease_id") != lease_id
        ):
            return False
        states[cache_key] = {
            "status": status,
            "deployment_id": deployment_id,
            "route_key": route_key,
            "detected_at": now,
            "expires_at": expires_at,
        }
        return True

    result = _codex_compaction_capability_update_shared(record)
    recorded = bool(isinstance(result, tuple) and result[0] is True)
    if result is None:
        with _CODEX_COMPACTION_CAPABILITY_LOCK:
            active = _clean_codex_compaction_capability_state(
                _CODEX_COMPACTION_CAPABILITIES.get(cache_key), now=now
            )
            if lease_id is not None and (
                active is None
                or active.get("status") != "probing"
                or active.get("lease_id") != lease_id
            ):
                return
            _CODEX_COMPACTION_CAPABILITIES[cache_key] = {
                "status": status,
                "deployment_id": deployment_id,
                "route_key": route_key,
                "detected_at": now,
                "expires_at": expires_at,
            }
            recorded = True
    if not recorded:
        return
    _trace_module._route_trace(
        "codex_compaction_capability_recorded",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=deployment_id,
        route_key=route_key,
        status=status,
        ttl_seconds=ttl,
        expires_at=expires_at,
    )


def _release_codex_compaction_capability_probe(
    request_kwargs: Optional[dict],
    lease_id: Optional[str],
) -> None:
    if not lease_id:
        return
    cache_key = _codex_compaction_capability_cache_key(request_kwargs)
    if not cache_key:
        return

    def release(states: dict[str, Any], now: float) -> None:
        active = _clean_codex_compaction_capability_state(
            states.get(cache_key), now=now
        )
        if (
            active is not None
            and active.get("status") == "probing"
            and active.get("lease_id") == lease_id
        ):
            states.pop(cache_key, None)

    result = _codex_compaction_capability_update_shared(release)
    if result is not None:
        return
    with _CODEX_COMPACTION_CAPABILITY_LOCK:
        active = _clean_codex_compaction_capability_state(
            _CODEX_COMPACTION_CAPABILITIES.get(cache_key), now=time.time()
        )
        if (
            active is not None
            and active.get("status") == "probing"
            and active.get("lease_id") == lease_id
        ):
            _CODEX_COMPACTION_CAPABILITIES.pop(cache_key, None)


def _codex_compaction_probe_payload(request_kwargs: dict) -> Optional[dict]:
    model = _request_public_model(request_kwargs)
    if not isinstance(model, str) or not model.strip():
        return None
    payload: dict[str, Any] = {
        "model": model,
        "input": [
            {
                "type": "compaction_trigger",
                "id": "litellm-menu-compaction-capability-probe",
            }
        ],
        "stream": True,
        "tools": [],
        "client_metadata": {
            "x-codex-turn-metadata": '{"request_kind":"compaction"}',
        },
        "litellm_metadata": {
            _CODEX_COMPACTION_CAPABILITY_PROBE_METADATA_KEY: True,
        },
    }
    deployment_id = _deployment_id_from_request(request_kwargs)
    if deployment_id:
        payload[_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY] = [deployment_id]
    target_order = _deployment_order_from_request(request_kwargs)
    if target_order is not None:
        payload["_target_order"] = target_order
    return payload


def _codex_compaction_probe_failure_status(value: Any) -> str:
    if isinstance(value, Exception):
        status_code = _exception_status_code(value)
        if status_code in {404, 405, 415, 422}:
            return "unsupported"
        text = _exception_text(value).lower()
    else:
        payload = _streaming_module._stream_chunk_dump(value)
        response = payload.get("response") if isinstance(payload, dict) else None
        error = response.get("error") if isinstance(response, dict) else None
        if not isinstance(error, dict):
            return "unknown"
        status_code = error.get("status_code")
        if status_code in {404, 405, 415, 422}:
            return "unsupported"
        text = " ".join(
            str(error.get(key) or "") for key in ("code", "type", "message")
        ).lower()
    return (
        "unsupported"
        if any(
            marker in text
            for marker in (
                "compaction",
                "unsupported",
                "not support",
                "invalid input",
                "invalid request",
                "unknown field",
            )
        )
        else "unknown"
    )


async def _probe_codex_compaction_capability(request_kwargs: dict) -> str:
    payload = _codex_compaction_probe_payload(request_kwargs)
    if payload is None:
        return "unknown"
    try:
        from litellm.proxy.proxy_server import llm_router

        if llm_router is None or not hasattr(llm_router, "aresponses"):
            return "unknown"

        async def consume() -> str:
            response = await llm_router.aresponses(**payload)
            if not hasattr(response, "__aiter__"):
                response_payload = _streaming_module._stream_chunk_dump(response)
                output = response_payload.get("output") if isinstance(response_payload, dict) else None
                return (
                    "supported"
                    if _streaming_module._codex_compaction_output_is_valid(output)
                    else "unsupported"
                )
            async for chunk in response:
                dumped = _streaming_module._stream_chunk_dump(chunk)
                if not isinstance(dumped, dict):
                    continue
                event_type = dumped.get("type")
                if event_type == "response.failed":
                    return _codex_compaction_probe_failure_status(chunk)
                if event_type != "response.completed":
                    continue
                completed = dumped.get("response")
                output = completed.get("output") if isinstance(completed, dict) else None
                return (
                    "supported"
                    if _streaming_module._codex_compaction_output_is_valid(output)
                    else "unsupported"
                )
            return "unknown"

        return await asyncio.wait_for(
            consume(), timeout=_CODEX_COMPACTION_CAPABILITY_PROBE_TIMEOUT_SECONDS
        )
    except asyncio.TimeoutError:
        return "unknown"
    except Exception as exc:
        return _codex_compaction_probe_failure_status(exc)


async def _ensure_codex_compaction_capability(request_kwargs: Optional[dict]) -> str:
    """Probe an unverified third-party route before sending signed history.

    A cached unsupported result raises before the full encrypted history is
    forwarded. A LiteLLM model selection enables Codex's native checkpoint
    summary path; the proxy never forges or rewrites encrypted compaction
    items.
    """

    if not _request_uses_third_party_codex_compaction_route(request_kwargs):
        return "skipped"
    if not isinstance(request_kwargs, dict):
        return "skipped"
    while True:
        action, value = _codex_compaction_capability_claim_probe(request_kwargs)
        if action == "cached":
            status = value or "unknown"
            _trace_module._route_trace(
                "codex_compaction_capability_cache_hit",
                request_id=_trace_request_id(request_kwargs),
                session=_trace_session_context(request_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs),
                deployment_id=_deployment_id_from_request(request_kwargs),
                route_key=_deployment_route_key_from_request(request_kwargs),
                status=status,
            )
            break
        if action == "waiting":
            await asyncio.sleep(_CODEX_COMPACTION_CAPABILITY_PROBE_WAIT_SECONDS)
            continue
        lease_id = value
        status = await _probe_codex_compaction_capability(request_kwargs)
        if status in _CODEX_COMPACTION_CAPABILITY_STATUSES:
            _record_codex_compaction_capability(
                request_kwargs, status, lease_id=lease_id
            )
        else:
            _release_codex_compaction_capability_probe(request_kwargs, lease_id)
        break
    if status == "unsupported":
        raise CodexCompactionCapabilityUnsupportedError(
            deployment_id=_deployment_id_from_request(request_kwargs),
            route_key=_deployment_route_key_from_request(request_kwargs),
        )
    return status


def _image_generation_tool_unsupported_ttl_seconds() -> float:
    value = os.getenv(_IMAGE_GENERATION_TOOL_UNSUPPORTED_TTL_SECONDS_ENV, "").strip()
    if not value:
        return _IMAGE_GENERATION_TOOL_UNSUPPORTED_DEFAULT_TTL_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _IMAGE_GENERATION_TOOL_UNSUPPORTED_DEFAULT_TTL_SECONDS
    return max(0.0, parsed)


def _image_generation_tool_unsupported_state_map(
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload.setdefault("schema_version", 1)
    states = payload.setdefault("image_tool_unsupported", {})
    if not isinstance(states, dict):
        states = {}
        payload["image_tool_unsupported"] = states
    return states


def _clean_image_generation_tool_unsupported_state(
    state: Any,
    *,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    try:
        expires_at = float(state.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if expires_at <= 0 or (now is not None and expires_at <= now):
        return None
    cleaned = dict(state)
    cleaned["expires_at"] = expires_at
    try:
        cleaned["detected_at"] = float(cleaned.get("detected_at") or 0.0)
    except (TypeError, ValueError):
        cleaned["detected_at"] = 0.0
    return cleaned


def _sync_image_generation_tool_unsupported_from_shared_locked(
    states: dict[str, Any],
    now: float,
) -> None:
    shared: dict[str, dict[str, Any]] = {}
    for cache_key, state in list(states.items()):
        cleaned = _clean_image_generation_tool_unsupported_state(state, now=now)
        if cleaned is None:
            states.pop(cache_key, None)
            continue
        shared[cache_key] = cleaned
        if cleaned is not state:
            states[cache_key] = cleaned
    with _IMAGE_GENERATION_TOOL_UNSUPPORTED_LOCK:
        _IMAGE_GENERATION_TOOL_UNSUPPORTED.clear()
        _IMAGE_GENERATION_TOOL_UNSUPPORTED.update(
            {key: value.copy() for key, value in shared.items()}
        )


def _image_generation_tool_unsupported_update_shared(callback: Any) -> Any:
    path = _deployment_cooldown_file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        states = _image_generation_tool_unsupported_state_map(payload)
        _sync_image_generation_tool_unsupported_from_shared_locked(states, now)
        result = callback(states, now)
        _sync_image_generation_tool_unsupported_from_shared_locked(states, now)
        return result, now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        return None


def _image_generation_tool_unsupported_key_for_deployment(
    deployment: Any,
) -> Optional[str]:
    return _deployment_cooldown_key_from_deployment(deployment)


def _image_generation_tool_unsupported_metadata_keys(
    request_kwargs: Optional[dict],
) -> set[str]:
    keys: set[str] = set()
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs,
            metadata_key,
        ) or {}
        values = metadata.get(_IMAGE_GENERATION_TOOL_UNSUPPORTED_METADATA_KEY)
        if isinstance(values, str) and values.strip():
            keys.add(values.strip())
        elif isinstance(values, list):
            keys.update(
                value.strip()
                for value in values
                if isinstance(value, str) and value.strip()
            )
    return keys


def _image_generation_tool_unsupported_cached_keys() -> set[str]:
    now = time.time()

    def read(states: dict[str, Any], current_time: float) -> set[str]:
        return {
            cache_key
            for cache_key, state in states.items()
            if _clean_image_generation_tool_unsupported_state(
                state,
                now=current_time,
            )
            is not None
        }

    result = _image_generation_tool_unsupported_update_shared(read)
    if isinstance(result, tuple) and isinstance(result[0], set):
        return result[0]
    with _IMAGE_GENERATION_TOOL_UNSUPPORTED_LOCK:
        return {
            cache_key
            for cache_key, state in _IMAGE_GENERATION_TOOL_UNSUPPORTED.items()
            if _clean_image_generation_tool_unsupported_state(state, now=now)
            is not None
        }


def _with_active_image_generation_tool_unsupported(
    deployments: List[dict],
    *,
    request_kwargs: Optional[dict] = None,
) -> tuple[List[dict], list[dict[str, Any]], bool]:
    if (
        not deployments
        or not _tools_module._request_has_image_generation_tool(request_kwargs)
        or _image_generation_tool_unsupported_ttl_seconds() <= 0
    ):
        return deployments, [], False

    def filter_active(
        states: dict[str, Any],
        now: float,
    ) -> tuple[List[dict], list[dict[str, Any]], bool]:
        available: list[dict] = []
        unsupported: list[dict[str, Any]] = []
        for deployment in deployments:
            cache_key = _image_generation_tool_unsupported_key_for_deployment(
                deployment
            )
            state = (
                _clean_image_generation_tool_unsupported_state(
                    states.get(cache_key),
                    now=now,
                )
                if cache_key
                else None
            )
            if state is None:
                available.append(deployment)
                continue
            trace_entry = _trace_deployment(deployment)
            trace_entry["image_tool_unsupported_key"] = cache_key
            trace_entry["image_tool_unsupported_remaining_seconds"] = round(
                max(0.0, float(state.get("expires_at") or 0.0) - now),
                3,
            )
            trace_entry["image_tool_unsupported_expires_at"] = round(
                float(state.get("expires_at") or 0.0),
                3,
            )
            unsupported.append(trace_entry)
        return available, unsupported, bool(unsupported)

    result = _image_generation_tool_unsupported_update_shared(filter_active)
    if isinstance(result, tuple) and isinstance(result[0], tuple):
        return result[0]
    with _IMAGE_GENERATION_TOOL_UNSUPPORTED_LOCK:
        return filter_active(_IMAGE_GENERATION_TOOL_UNSUPPORTED, time.time())


def _record_image_generation_tool_unsupported(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if not _tools_module._request_has_image_generation_tool(request_kwargs):
        return False
    if not _is_image_generation_tool_capability_error(exception):
        return False
    deployment_id = (
        _deployment_id_from_request(request_kwargs)
        or _responses_execution_module._failed_deployment_id(exception)
    )
    route_key = (
        _deployment_route_key_from_request(request_kwargs)
        or _responses_execution_module._failed_deployment_route_key(exception)
    )
    cache_key = _deployment_cooldown_key(
        deployment_id=deployment_id,
        route_key=route_key,
    )
    if not cache_key:
        return False

    metadata = (
        _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        )
        or {}
    )
    unsupported_keys = _image_generation_tool_unsupported_metadata_keys(request_kwargs)
    unsupported_keys.add(cache_key)
    updated_metadata = metadata.copy()
    updated_metadata[_IMAGE_GENERATION_TOOL_UNSUPPORTED_METADATA_KEY] = sorted(
        unsupported_keys
    )
    request_kwargs["litellm_metadata"] = updated_metadata
    try:
        setattr(exception, _IMAGE_GENERATION_TOOL_CAPABILITY_UNSUPPORTED_ATTR, True)
    except Exception:
        pass

    ttl = _image_generation_tool_unsupported_ttl_seconds()
    if ttl <= 0:
        return True
    now = time.time()
    expires_at = now + ttl
    def record(states: dict[str, Any], _now: float) -> None:
        states[cache_key] = {
            "deployment_id": deployment_id,
            "route_key": route_key,
            "detected_at": now,
            "expires_at": expires_at,
        }

    result = _image_generation_tool_unsupported_update_shared(record)
    if result is None:
        with _IMAGE_GENERATION_TOOL_UNSUPPORTED_LOCK:
            _IMAGE_GENERATION_TOOL_UNSUPPORTED[cache_key] = {
                "deployment_id": deployment_id,
                "route_key": route_key,
                "detected_at": now,
                "expires_at": expires_at,
            }
    _trace_module._route_trace(
        "image_generation_tool_unsupported_recorded",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        deployment_id=deployment_id,
        route_key=route_key,
        ttl_seconds=ttl,
        expires_at=expires_at,
        exception=_trace_exception(exception),
    )
    return True


def _web_search_tool_unsupported_ttl_seconds() -> float:
    value = os.getenv(_WEB_SEARCH_TOOL_UNSUPPORTED_TTL_SECONDS_ENV, "").strip()
    if not value:
        return _WEB_SEARCH_TOOL_UNSUPPORTED_DEFAULT_TTL_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _WEB_SEARCH_TOOL_UNSUPPORTED_DEFAULT_TTL_SECONDS
    return max(0.0, parsed)


def _web_search_tool_unsupported_state_map(
    payload: dict[str, Any],
) -> dict[str, Any]:
    payload.setdefault("schema_version", 1)
    states = payload.setdefault("web_search_tool_unsupported", {})
    if not isinstance(states, dict):
        states = {}
        payload["web_search_tool_unsupported"] = states
    return states


def _clean_web_search_tool_unsupported_state(
    state: Any,
    *,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    if state.get("status") not in {None, "unsupported"}:
        return None
    try:
        expires_at = float(state.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if expires_at <= 0 or (now is not None and expires_at <= now):
        return None
    cleaned = dict(state)
    cleaned["status"] = "unsupported"
    cleaned["expires_at"] = expires_at
    try:
        cleaned["detected_at"] = float(cleaned.get("detected_at") or 0.0)
    except (TypeError, ValueError):
        cleaned["detected_at"] = 0.0
    return cleaned


def _sync_web_search_tool_unsupported_from_shared_locked(
    states: dict[str, Any],
    now: float,
) -> None:
    shared: dict[str, dict[str, Any]] = {}
    for cache_key, state in list(states.items()):
        cleaned = _clean_web_search_tool_unsupported_state(state, now=now)
        if cleaned is None:
            states.pop(cache_key, None)
            continue
        shared[cache_key] = cleaned
        if cleaned is not state:
            states[cache_key] = cleaned
    with _WEB_SEARCH_TOOL_UNSUPPORTED_LOCK:
        _WEB_SEARCH_TOOL_UNSUPPORTED.clear()
        _WEB_SEARCH_TOOL_UNSUPPORTED.update(
            {key: value.copy() for key, value in shared.items()}
        )


def _web_search_tool_unsupported_update_shared(callback: Any) -> Any:
    path = _deployment_cooldown_file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        states = _web_search_tool_unsupported_state_map(payload)
        _sync_web_search_tool_unsupported_from_shared_locked(states, now)
        result = callback(states, now)
        _sync_web_search_tool_unsupported_from_shared_locked(states, now)
        return result, now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        return None


def _web_search_tool_unsupported_family(request_kwargs: Optional[dict]) -> str:
    families: set[str] = set()
    if not isinstance(request_kwargs, dict):
        return "unknown"
    if "web_search_options" in request_kwargs:
        families.add("hosted")
    tools = request_kwargs.get("tools")
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            tool_type = tool.get("type")
            if tool_type in _HOSTED_WEB_SEARCH_TOOL_TYPES:
                families.add("hosted")
            elif tool_type in _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES:
                families.add("provider_native")
    families.update(
        _web_search_tool_unsupported_input_families(request_kwargs.get("input"))
    )
    if not families:
        return "unknown"
    return "+".join(
        family for family in ("hosted", "provider_native") if family in families
    )


def _web_search_tool_unsupported_input_families(
    value: Any,
    *,
    depth: int = 0,
) -> set[str]:
    """Find hosted/provider-native declarations lifted into Responses input.

    Responses clients can carry ``additional_tools`` or a
    ``tool_search_output`` item in ``input`` instead of the top-level
    ``tools`` array. Keep the probe key aligned with the tool planner without
    treating arbitrary user text as a capability declaration.
    """
    if depth > 8:
        return set()
    if isinstance(value, list):
        families: set[str] = set()
        for item in value:
            families.update(
                _web_search_tool_unsupported_input_families(item, depth=depth + 1)
            )
        return families
    if not isinstance(value, dict):
        return set()
    families: set[str] = set()
    tool_type = value.get("type")
    if tool_type in _HOSTED_WEB_SEARCH_TOOL_TYPES:
        families.add("hosted")
    elif tool_type in _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES:
        families.add("provider_native")
    for key in ("tools", "input", "items", "output"):
        nested = value.get(key)
        if isinstance(nested, (dict, list)):
            families.update(
                _web_search_tool_unsupported_input_families(
                    nested, depth=depth + 1
                )
            )
    return families


def _web_search_tool_unsupported_request_has_search(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    return (
        _request_contains_hosted_web_search_tool(request_kwargs)
        or bool(_web_search_tool_unsupported_input_families(request_kwargs.get("input")))
        or "web_search_options" in request_kwargs
    )


def _web_search_tool_unsupported_surface(request_kwargs: Optional[dict]) -> str:
    if not isinstance(request_kwargs, dict):
        return _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    surface = _request_current_upstream_surface(request_kwargs)
    if not surface:
        model_info = _request_context_module._request_model_info(request_kwargs)
        surface = _normalized_request_surface(
            model_info.get(_UPSTREAM_URL_SURFACE_KEY)
        )
    if not surface:
        surface = _request_client_surface(request_kwargs)
    return surface or _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES


def _web_search_tool_unsupported_cache_key(
    request_kwargs: Optional[dict],
    exception: Optional[Exception] = None,
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    deployment_id = _deployment_id_from_request(request_kwargs)
    route_key = _deployment_route_key_from_request(request_kwargs)
    if exception is not None:
        deployment_id = deployment_id or _responses_execution_module._failed_deployment_id(
            exception
        )
        route_key = route_key or _responses_execution_module._failed_deployment_route_key(
            exception
        )
    deployment_key = _deployment_cooldown_key(
        deployment_id=deployment_id,
        route_key=route_key,
    )
    if not deployment_key:
        return None
    return (
        f"{deployment_key}|surface:{_web_search_tool_unsupported_surface(request_kwargs)}"
        f"|family:{_web_search_tool_unsupported_family(request_kwargs)}"
    )


def _set_web_search_tool_unsupported_request_state(
    request_kwargs: Optional[dict],
    *,
    state: dict[str, Any],
    cache_key: str,
    cache_hit: bool,
) -> None:
    if not isinstance(request_kwargs, dict):
        return
    if cache_hit:
        request_kwargs[_WEB_SEARCH_TOOL_UNSUPPORTED_CACHE_HIT_KEY] = True
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    updated_metadata = metadata.copy()
    updated_metadata[_WEB_SEARCH_TOOL_UNSUPPORTED_METADATA_KEY] = {
        "status": "unsupported",
        "cache_key": cache_key,
        "detected_at": state.get("detected_at"),
        "expires_at": state.get("expires_at"),
    }
    updated_metadata[_WEB_SEARCH_TOOL_UNSUPPORTED_CACHE_HIT_KEY] = bool(cache_hit)
    request_kwargs["litellm_metadata"] = updated_metadata


def _web_search_tool_unsupported_cached(
    request_kwargs: Optional[dict],
) -> Optional[dict[str, Any]]:
    if (
        not isinstance(request_kwargs, dict)
        or _web_search_tool_unsupported_ttl_seconds() <= 0
        or not _web_search_tool_unsupported_request_has_search(request_kwargs)
    ):
        return None
    cache_key = _web_search_tool_unsupported_cache_key(request_kwargs)
    if not cache_key:
        return None

    def read(states: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
        state = _clean_web_search_tool_unsupported_state(
            states.get(cache_key),
            now=now,
        )
        if state is None:
            states.pop(cache_key, None)
            return None
        return state

    result = _web_search_tool_unsupported_update_shared(read)
    state = result[0] if isinstance(result, tuple) else None
    if state is None:
        with _WEB_SEARCH_TOOL_UNSUPPORTED_LOCK:
            state = _clean_web_search_tool_unsupported_state(
                _WEB_SEARCH_TOOL_UNSUPPORTED.get(cache_key),
                now=time.time(),
            )
            if state is None:
                _WEB_SEARCH_TOOL_UNSUPPORTED.pop(cache_key, None)
    if not isinstance(state, dict):
        return None
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    already_marked = (
        metadata.get(_WEB_SEARCH_TOOL_UNSUPPORTED_CACHE_HIT_KEY) is True
        and isinstance(metadata.get(_WEB_SEARCH_TOOL_UNSUPPORTED_METADATA_KEY), dict)
        and metadata[_WEB_SEARCH_TOOL_UNSUPPORTED_METADATA_KEY].get("cache_key")
        == cache_key
    )
    _set_web_search_tool_unsupported_request_state(
        request_kwargs,
        state=state,
        cache_key=cache_key,
        cache_hit=True,
    )
    if not already_marked:
        _trace_module._route_trace(
            "web_search_tool_unsupported_cache_hit",
            request_id=_trace_request_id(request_kwargs),
            session=_trace_session_context(request_kwargs),
            deployment_id=_deployment_id_from_request(request_kwargs),
            route_key=_deployment_route_key_from_request(request_kwargs),
            cache_key=cache_key,
            expires_at=state.get("expires_at"),
            remaining_seconds=round(
                max(0.0, float(state.get("expires_at") or 0.0) - time.time()),
                3,
            ),
            request=_trace_module._trace_request_summary(request_kwargs),
        )
    return state


def _record_web_search_tool_unsupported(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if getattr(exception, _WEB_SEARCH_TOOL_CAPABILITY_UNSUPPORTED_ATTR, False):
        return True
    candidate = None
    for request in (request_kwargs, outer_request_kwargs):
        if not isinstance(request, dict):
            continue
        if not _web_search_tool_unsupported_request_has_search(request):
            continue
        if not _is_native_responses_web_search_unsupported_error(exception, request):
            continue
        if (
            _is_responses_endpoint_not_found_error(
                exception,
                request,
                outer_request_kwargs,
            )
            and not any(
                marker in _exception_text(exception)
                for marker in ("web_search", "web search")
            )
        ):
            # A bare Responses 404 means the whole protocol surface is absent;
            # protocol fallback owns that signal, not the web-search probe.
            continue
        candidate = request
        break
    if candidate is None:
        return False
    deployment_id = _deployment_id_from_request(candidate) or (
        _responses_execution_module._failed_deployment_id(exception)
    )
    route_key = _deployment_route_key_from_request(candidate) or (
        _responses_execution_module._failed_deployment_route_key(exception)
    )
    cache_key = _deployment_cooldown_key(
        deployment_id=deployment_id,
        route_key=route_key,
    )
    if cache_key:
        cache_key = (
            f"{cache_key}|surface:{_web_search_tool_unsupported_surface(candidate)}"
            f"|family:{_web_search_tool_unsupported_family(candidate)}"
        )
    if not cache_key:
        return False
    try:
        setattr(exception, _WEB_SEARCH_TOOL_CAPABILITY_UNSUPPORTED_ATTR, True)
    except Exception:
        pass
    ttl = _web_search_tool_unsupported_ttl_seconds()
    now = time.time()
    expires_at = now + ttl
    state = {
        "status": "unsupported",
        "deployment_id": deployment_id,
        "route_key": route_key,
        "surface": _web_search_tool_unsupported_surface(candidate),
        "family": _web_search_tool_unsupported_family(candidate),
        "detected_at": now,
        "expires_at": expires_at,
    }
    _set_web_search_tool_unsupported_request_state(
        candidate,
        state=state,
        cache_key=cache_key,
        cache_hit=False,
    )
    if ttl <= 0:
        return True

    def record(states: dict[str, Any], _now: float) -> None:
        states[cache_key] = state.copy()

    result = _web_search_tool_unsupported_update_shared(record)
    if result is None:
        with _WEB_SEARCH_TOOL_UNSUPPORTED_LOCK:
            _WEB_SEARCH_TOOL_UNSUPPORTED[cache_key] = state.copy()
    _trace_module._route_trace(
        "web_search_tool_unsupported_recorded",
        request_id=_trace_request_id(candidate),
        session=_trace_session_context(candidate),
        model_group=_responses_execution_module._request_model_group(candidate),
        deployment_id=state.get("deployment_id"),
        route_key=state.get("route_key"),
        surface=state.get("surface"),
        family=state.get("family"),
        cache_key=cache_key,
        ttl_seconds=ttl,
        expires_at=expires_at,
        exception=_trace_exception(exception),
    )
    return True


def _image_generation_tool_all_deployments_unsupported(
    deployments: List[dict],
    request_kwargs: Optional[dict],
) -> bool:
    if (
        not deployments
        or not _tools_module._request_has_image_generation_tool(request_kwargs)
    ):
        return False
    candidate_keys = {
        cache_key
        for cache_key in (
            _image_generation_tool_unsupported_key_for_deployment(deployment)
            for deployment in deployments
        )
        if cache_key
    }
    if not candidate_keys:
        return False
    unsupported_keys = _image_generation_tool_unsupported_metadata_keys(request_kwargs)
    if _image_generation_tool_unsupported_ttl_seconds() > 0:
        unsupported_keys.update(_image_generation_tool_unsupported_cached_keys())
    return candidate_keys.issubset(unsupported_keys)


def _protocol_fallback_ttl_seconds() -> float:
    value = os.getenv(_PROTOCOL_FALLBACK_TTL_SECONDS_ENV, "").strip()
    if not value:
        return _PROTOCOL_FALLBACK_DEFAULT_TTL_SECONDS
    try:
        parsed = float(value)
    except ValueError:
        return _PROTOCOL_FALLBACK_DEFAULT_TTL_SECONDS
    return max(0.0, parsed)


def _protocol_fallback_state_map(payload: dict[str, Any]) -> dict[str, Any]:
    payload.setdefault("schema_version", 1)
    fallbacks = payload.setdefault("protocol_fallbacks", {})
    if not isinstance(fallbacks, dict):
        fallbacks = {}
        payload["protocol_fallbacks"] = fallbacks
    return fallbacks


def _clean_protocol_fallback_state(
    state: Any,
    *,
    now: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    if not isinstance(state, dict):
        return None
    fallback_surface = _normalized_request_surface(state.get("fallback_surface"))
    client_surface = _normalized_request_surface(state.get("client_surface"))
    from_surface = _normalized_request_surface(state.get("from_surface"))
    if not fallback_surface or not client_surface or not from_surface:
        return None
    try:
        expires_at = float(state.get("expires_at") or 0.0)
    except (TypeError, ValueError):
        expires_at = 0.0
    if expires_at <= 0 or (now is not None and expires_at <= now):
        return None
    cleaned = dict(state)
    cleaned["fallback_surface"] = fallback_surface
    cleaned["client_surface"] = client_surface
    cleaned["from_surface"] = from_surface
    cleaned["expires_at"] = expires_at
    return cleaned


def _sync_protocol_fallbacks_from_shared_locked(
    fallbacks: dict[str, Any],
    now: float,
) -> None:
    shared: dict[str, dict[str, Any]] = {}
    expired_keys: list[str] = []
    for cache_key, state in list(fallbacks.items()):
        cleaned = _clean_protocol_fallback_state(state, now=now)
        if cleaned is None:
            expired_keys.append(cache_key)
            continue
        shared[cache_key] = cleaned
        if cleaned is not state:
            fallbacks[cache_key] = cleaned
    for cache_key in expired_keys:
        fallbacks.pop(cache_key, None)
    with _PROTOCOL_FALLBACK_LOCK:
        _PROTOCOL_FALLBACKS.clear()
        _PROTOCOL_FALLBACKS.update({key: value.copy() for key, value in shared.items()})


def _protocol_fallback_update_shared(callback: Any) -> Any:
    path = _deployment_cooldown_file_path()
    if not path:
        return None

    def update(payload: dict[str, Any]) -> Any:
        now = time.time()
        fallbacks = _protocol_fallback_state_map(payload)
        _sync_protocol_fallbacks_from_shared_locked(fallbacks, now)
        result = callback(fallbacks, now)
        _sync_protocol_fallbacks_from_shared_locked(fallbacks, now)
        return result, now

    try:
        return _state_module._locked_json_state_update(path, update)
    except OSError:
        return None


def _protocol_fallback_cache_key(
    deployment_id: Optional[str],
    client_surface: str,
) -> Optional[str]:
    if not isinstance(deployment_id, str) or not deployment_id.strip():
        return None
    normalized_client = _normalized_request_surface(client_surface)
    if not normalized_client:
        return None
    return f"{deployment_id.strip()}|{normalized_client}"


def _set_protocol_fallback_request_state(
    request_kwargs: Optional[dict],
    *,
    from_surface: str,
    client_surface: str,
    cache_hit: bool,
) -> None:
    if not isinstance(request_kwargs, dict):
        return
    normalized_from = _normalized_request_surface(from_surface)
    normalized_client = _normalized_request_surface(client_surface)
    if not normalized_from or not normalized_client:
        return
    request_kwargs[_PROTOCOL_FALLBACK_FROM_SURFACE_KEY] = normalized_from
    request_kwargs[_PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY] = normalized_client
    request_kwargs[_PROTOCOL_FALLBACK_CACHE_HIT_KEY] = bool(cache_hit)
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    updated_metadata = metadata.copy()
    updated_metadata[_PROTOCOL_FALLBACK_FROM_SURFACE_KEY] = normalized_from
    updated_metadata[_PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY] = normalized_client
    updated_metadata[_PROTOCOL_FALLBACK_CACHE_HIT_KEY] = bool(cache_hit)
    if _protocol_fallback_relax_tool_choice(request_kwargs):
        request_kwargs[_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY] = True
        updated_metadata[_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY] = True
    request_kwargs["litellm_metadata"] = updated_metadata


def _mark_protocol_fallback_relax_tool_choice(
    request_kwargs: Optional[dict],
) -> None:
    """Carry an explicit upstream request to use automatic tool selection.

    Some endpoints reject a named/required tool choice even though the
    protocol itself is usable.  The flag is set only after that concrete
    error and is consumed when the configured alternate surface is applied.
    """

    if not isinstance(request_kwargs, dict):
        return
    request_kwargs[_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY] = True
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    updated_metadata = metadata.copy()
    updated_metadata[_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY] = True
    request_kwargs["litellm_metadata"] = updated_metadata


def _protocol_fallback_relax_tool_choice(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    if request_kwargs.get(_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY) is True:
        return True
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    ) or {}
    return metadata.get(_PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY) is True


def _protocol_fallback_failure_recorded(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    marker = request_kwargs.get(_PROTOCOL_FALLBACK_FAILURE_RECORDED_KEY)
    if marker is True:
        return True
    current_deployment_id = _deployment_id_from_request(request_kwargs)
    current_route_key = _deployment_route_key_from_request(request_kwargs)
    if isinstance(marker, dict):
        marker_deployment_id = marker.get("deployment_id")
        marker_route_key = marker.get("route_key")
        if (
            isinstance(marker_deployment_id, str)
            and marker_deployment_id
            and marker_deployment_id == current_deployment_id
        ):
            return True
        if (
            isinstance(marker_route_key, str)
            and marker_route_key
            and marker_route_key == current_route_key
        ):
            return True
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs,
        "litellm_metadata",
    ) or {}
    metadata_marker = metadata.get(_PROTOCOL_FALLBACK_FAILURE_RECORDED_KEY)
    if metadata_marker is True:
        return True
    if isinstance(metadata_marker, dict):
        return (
            metadata_marker.get("deployment_id") == current_deployment_id
            and isinstance(current_deployment_id, str)
            and bool(current_deployment_id)
        ) or (
            metadata_marker.get("route_key") == current_route_key
            and isinstance(current_route_key, str)
            and bool(current_route_key)
        )
    return False


def _clear_protocol_fallback_request_state(request_kwargs: Optional[dict]) -> None:
    if not isinstance(request_kwargs, dict):
        return
    for key in (
        _PROTOCOL_FALLBACK_FROM_SURFACE_KEY,
        _PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY,
        _PROTOCOL_FALLBACK_CACHE_HIT_KEY,
        _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
    ):
        request_kwargs.pop(key, None)
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    )
    if isinstance(metadata, dict):
        updated_metadata = metadata.copy()
        changed = False
        for key in (
            _PROTOCOL_FALLBACK_FROM_SURFACE_KEY,
            _PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY,
            _PROTOCOL_FALLBACK_CACHE_HIT_KEY,
            _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
        ):
            if key in updated_metadata:
                updated_metadata.pop(key, None)
                changed = True
        if changed:
            request_kwargs["litellm_metadata"] = updated_metadata


def _protocol_fallback_cached_surface(
    request_kwargs: Optional[dict],
    deployment: Any,
) -> str:
    if _protocol_fallback_ttl_seconds() <= 0:
        return ""
    if _deployment_protocol_mode(deployment) != _UPSTREAM_PROTOCOL_MODE_FALLBACK:
        return ""
    deployment_id = _responses_request_module._deployment_id(deployment)
    client_surface = _request_client_surface(request_kwargs)
    configured_surface = _deployment_surface(deployment)
    cache_key = _protocol_fallback_cache_key(deployment_id, client_surface)
    if not cache_key or not configured_surface:
        return ""

    def read(fallbacks: dict[str, Any], now: float) -> Optional[dict[str, Any]]:
        state = _clean_protocol_fallback_state(fallbacks.get(cache_key), now=now)
        if state is None:
            fallbacks.pop(cache_key, None)
            return None
        if state.get("fallback_surface") != configured_surface:
            fallbacks.pop(cache_key, None)
            return None
        return state

    result = _protocol_fallback_update_shared(read)
    state = result[0] if isinstance(result, tuple) else None
    if state is None:
        with _PROTOCOL_FALLBACK_LOCK:
            state = _clean_protocol_fallback_state(
                _PROTOCOL_FALLBACKS.get(cache_key), now=time.time()
            )
    if not isinstance(state, dict):
        return ""
    fallback_surface = _normalized_request_surface(state.get("fallback_surface"))
    if not fallback_surface:
        return ""
    _set_protocol_fallback_request_state(
        request_kwargs,
        from_surface=state.get("from_surface") or client_surface,
        client_surface=state.get("client_surface") or client_surface,
        cache_hit=True,
    )
    if state.get("relax_tool_choice") is True:
        _mark_protocol_fallback_relax_tool_choice(request_kwargs)
    _trace_module._route_trace(
        "protocol_fallback_cache_hit",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        deployment_id=deployment_id,
        client_surface=client_surface,
        from_surface=state.get("from_surface"),
        fallback_surface=fallback_surface,
        expires_at=state.get("expires_at"),
        remaining_seconds=round(max(0.0, float(state.get("expires_at") or 0.0) - time.time()), 3),
        request=_trace_module._trace_request_summary(request_kwargs),
    )
    return fallback_surface


def _record_protocol_fallback_success(request_kwargs: Optional[dict]) -> None:
    if not isinstance(request_kwargs, dict):
        return
    from_surface = _normalized_request_surface(
        request_kwargs.get(_PROTOCOL_FALLBACK_FROM_SURFACE_KEY)
    )
    client_surface = _normalized_request_surface(
        request_kwargs.get(_PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY)
    )
    fallback_surface = _request_current_upstream_surface(request_kwargs)
    deployment_id = _request_surface_deployment_id(request_kwargs) or _deployment_id_from_request(request_kwargs)
    cache_key = _protocol_fallback_cache_key(deployment_id, client_surface)
    relax_tool_choice = _protocol_fallback_relax_tool_choice(request_kwargs)
    fallback_succeeded = bool(
        from_surface
        and client_surface
        and fallback_surface
        and fallback_surface != from_surface
    )
    # The first protocol's rejection is provisional.  A successful request
    # on the configured alternate protocol proves the route is usable, so it
    # must not leave a recovery/cooldown failure behind.
    if fallback_succeeded:
        _record_deployment_success_for_cooldown(request_kwargs)
    ttl = _protocol_fallback_ttl_seconds()
    if not fallback_succeeded or not cache_key or ttl <= 0:
        _clear_protocol_fallback_request_state(request_kwargs)
        return
    now = time.time()
    expires_at = now + ttl

    def record(fallbacks: dict[str, Any], _now: float) -> None:
        fallbacks[cache_key] = {
            "deployment_id": deployment_id,
            "client_surface": client_surface,
            "from_surface": from_surface,
            "fallback_surface": fallback_surface,
            "relax_tool_choice": relax_tool_choice,
            "created_at": now,
            "expires_at": expires_at,
        }

    result = _protocol_fallback_update_shared(record)
    if result is None:
        with _PROTOCOL_FALLBACK_LOCK:
            _PROTOCOL_FALLBACKS[cache_key] = {
                "deployment_id": deployment_id,
                "client_surface": client_surface,
                "from_surface": from_surface,
                "fallback_surface": fallback_surface,
                "relax_tool_choice": relax_tool_choice,
                "created_at": now,
                "expires_at": expires_at,
            }
    _trace_module._route_trace(
        "protocol_fallback_success",
        request_id=_trace_request_id(request_kwargs),
        session=_trace_session_context(request_kwargs),
        deployment_id=deployment_id,
        client_surface=client_surface,
        from_surface=from_surface,
        fallback_surface=fallback_surface,
        ttl_seconds=ttl,
        expires_at=expires_at,
        request=_trace_module._trace_request_summary(request_kwargs),
    )
    _clear_protocol_fallback_request_state(request_kwargs)


def _clear_protocol_fallback_cache_for_request(
    request_kwargs: Optional[dict],
    *,
    preserve_relaxed_tool_choice: bool = False,
) -> None:
    if not isinstance(request_kwargs, dict):
        return
    keep_relaxed_tool_choice = (
        preserve_relaxed_tool_choice
        and _protocol_fallback_relax_tool_choice(request_kwargs)
    )
    client_surface = _normalized_request_surface(
        request_kwargs.get(_PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY)
    )
    deployment_id = _request_surface_deployment_id(request_kwargs) or _deployment_id_from_request(request_kwargs)
    cache_key = _protocol_fallback_cache_key(deployment_id, client_surface)
    if not cache_key:
        _clear_protocol_fallback_request_state(request_kwargs)
        if keep_relaxed_tool_choice:
            _mark_protocol_fallback_relax_tool_choice(request_kwargs)
        return

    def clear(fallbacks: dict[str, Any], _now: float) -> Optional[dict[str, Any]]:
        state = fallbacks.pop(cache_key, None)
        return state if isinstance(state, dict) else None

    result = _protocol_fallback_update_shared(clear)
    state = result[0] if isinstance(result, tuple) else None
    if state is None:
        with _PROTOCOL_FALLBACK_LOCK:
            state = _PROTOCOL_FALLBACKS.pop(cache_key, None)
    if isinstance(state, dict):
        _trace_module._route_trace(
            "protocol_fallback_cache_cleared",
            request_id=_trace_request_id(request_kwargs),
            session=_trace_session_context(request_kwargs),
            deployment_id=deployment_id,
            client_surface=client_surface,
            from_surface=state.get("from_surface"),
            fallback_surface=state.get("fallback_surface"),
            request=_trace_module._trace_request_summary(request_kwargs),
        )
    _clear_protocol_fallback_request_state(request_kwargs)
    if keep_relaxed_tool_choice:
        _mark_protocol_fallback_relax_tool_choice(request_kwargs)


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
    for item_type in _responses_output_module._response_types(response_obj):
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


def _request_public_model(request_kwargs: Optional[dict]) -> Optional[str]:
    request_kwargs = request_kwargs or {}
    litellm_params = _as_dict(request_kwargs.get("litellm_params"))
    for container in (request_kwargs, litellm_params):
        for metadata_key in ("metadata", "litellm_metadata"):
            metadata = container.get(metadata_key)
            if not isinstance(metadata, dict):
                continue
            for model_key in ("deployment_model_name", "model_group"):
                model_name = metadata.get(model_key)
                if isinstance(model_name, str) and model_name.strip():
                    return model_name.strip()

    model_info = _request_context_module._request_model_info(request_kwargs)
    for model_key in ("model_group", "model_name"):
        model_name = model_info.get(model_key)
        if isinstance(model_name, str) and model_name.strip():
            return model_name.strip()
    return _responses_execution_module._request_model_group(request_kwargs)


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
    model_info = _request_context_module._request_model_info(request_kwargs)
    api_base = _responses_request_module._request_api_base(request_kwargs)
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
    public_model = _request_public_model(request_kwargs)
    route_key = _state_module._safe_log_text(
        _deployment_route_key_from_request(request_kwargs),
        limit=260,
    )
    deployment_id = _state_module._safe_log_text(
        _deployment_id_from_request(request_kwargs),
        limit=180,
    )
    request_exception = _request_log_exception(request_kwargs, response_obj)
    if route_key or deployment_id:
        routing_state = "selected"
    elif request_exception is not None and _is_no_deployments_available_error(
        request_exception
    ):
        routing_state = "no_available_deployment"
    elif (
        request_exception is not None
        and type(request_exception).__name__ == "ProxyModelNotFoundError"
    ):
        routing_state = "model_not_configured"
    else:
        routing_state = "unselected"
    tools_summary = _trace_module._trace_tools_summary(request_kwargs)
    request_started_at = _request_started_time(request_kwargs)
    request_timestamp = (
        _event_time(end_time)
        or _event_time(start_time)
        or _event_time(request_started_at)
    )

    record: dict[str, Any] = {
        "ts": request_timestamp
        or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "status": status,
        "duration_ms": _duration_ms(start_time, end_time),
        "time_to_first_token_ms": _time_to_first_token_ms(
            request_kwargs,
            start_time,
            end_time,
        ),
        "time_to_first_token_source": _completion_start_time(
            request_kwargs,
            end_time,
        )[1],
        "first_stream_output_at": _event_time(
            _completion_start_time(request_kwargs, end_time)[0]
        ),
        "call_type": _state_module._safe_log_text(request_kwargs.get("call_type"), limit=80),
        "model_group": _state_module._safe_log_text(_responses_execution_module._request_model_group(request_kwargs), limit=160),
        "public_model": _state_module._safe_log_text(public_model, limit=160),
        "deployment_id": deployment_id,
        "route_key": route_key,
        "routing_state": routing_state,
        "deployment_order": _deployment_order_from_request(request_kwargs),
        "provider": _state_module._safe_log_text(provider, limit=120),
        "api_key_name": api_key_name,
        "upstream_model": _state_module._safe_log_text(upstream_model, limit=180),
        "api_base_host": _state_module._safe_log_text(_responses_request_module._api_base_host(api_base), limit=180),
        "request_id": _state_module._safe_log_text(_trace_request_id(request_kwargs), limit=180),
        "session": _trace_session_context(request_kwargs),
        "tool_types": tools_summary["types"],
        "tool_names": tools_summary["names"],
        "tool_count": tools_summary["count"],
        "top_level_tool_count": tools_summary["top_level_count"],
        "additional_tools_count": tools_summary["additional_tools_count"],
        "tool_origins": tools_summary["origins"],
        "tool_choice": _state_module._safe_log_text(request_kwargs.get("tool_choice"), limit=120),
        "has_web_search_tool": tools_summary["has_web_search_tool"],
        "has_image_generation_tool": tools_summary["has_image_generation_tool"],
        "has_image_input": _image_inputs_module._request_has_image_input(request_kwargs),
        "cache_hit": request_kwargs.get("cache_hit"),
        "response_cost": response_cost,
        "usage": _usage_summary(response_obj, request_kwargs),
        "response_types": _response_type_summary(response_obj),
    }

    if status in {"failure", "stuck"}:
        record["error"] = _request_log_error_summary(request_kwargs, response_obj)
    if status == "pending":
        started_at = _event_time(request_started_at or start_time)
        if started_at:
            record["started_at"] = started_at
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
    host = _responses_request_module._api_base_host(str(api_base or "").strip())
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
        order=_responses_request_module._deployment_order(deployment),
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


def _deployment_cooldown_keys_for_request(
    *,
    deployment_id: Optional[str],
    route_key: Optional[str],
    request_kwargs: Optional[dict],
) -> list[str]:
    return _deployment_cooldown_keys(
        deployment_id=deployment_id,
        route_key=route_key,
    )


def _deployment_cooldown_key_from_request(request_kwargs: Optional[dict]) -> Optional[str]:
    return _deployment_cooldown_key(
        deployment_id=_deployment_id_from_request(request_kwargs),
        route_key=_deployment_route_key_from_request(request_kwargs),
    )


def _deployment_cooldown_key_from_deployment(deployment: Any) -> Optional[str]:
    return _deployment_cooldown_key(
        deployment_id=_responses_request_module._deployment_id(deployment),
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
        deployment_id=_responses_request_module._deployment_id(deployment),
        route_key=_deployment_route_key_from_deployment(deployment),
    )


def _deployment_cooldown_keys_from_deployment_for_request(
    deployment: Any,
    request_kwargs: Optional[dict],
) -> list[str]:
    return _deployment_cooldown_keys_for_request(
        deployment_id=_responses_request_module._deployment_id(deployment),
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
    order = _coerce_order(litellm_params.get("order"))
    if order is None:
        order = _coerce_order(model_info.get("order"))
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
        "upstream_url_surface": model_info.get(_UPSTREAM_URL_SURFACE_KEY),
        "upstream_protocol_mode": model_info.get(_UPSTREAM_PROTOCOL_MODE_KEY, _UPSTREAM_PROTOCOL_MODE_FALLBACK),
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
        metadata = _request_context_module._request_metadata_dict(
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


def _request_proxy_request_values(request_kwargs: Optional[dict]) -> list[str]:
    if not isinstance(request_kwargs, dict):
        return []
    values: list[str] = []
    containers: list[Any] = [request_kwargs]
    for key in ("litellm_params", "litellm_metadata", "metadata"):
        container = request_kwargs.get(key)
        if isinstance(container, dict):
            containers.append(container)
    for container in containers:
        proxy_request = container.get("proxy_server_request") if isinstance(container, dict) else None
        if isinstance(proxy_request, dict):
            candidates = [
                proxy_request.get(key)
                for key in ("url", "path", "route", "endpoint", "method")
            ]
        else:
            candidates = [
                getattr(proxy_request, key, None)
                for key in ("url", "path", "route", "endpoint", "method")
            ]
        for value in candidates:
            if isinstance(value, str) and value.strip() and value.strip() not in values:
                values.append(value.strip())
    return values


def _request_client_surface(request_kwargs: Optional[dict]) -> str:
    """Return the protocol used by the client-facing request, if known."""

    if not isinstance(request_kwargs, dict):
        return ""
    for value in _request_proxy_request_values(request_kwargs):
        lowered = value.lower()
        if "/v1/messages" in lowered:
            return _UPSTREAM_URL_SURFACE_ANTHROPIC
        if "/v1/responses" in lowered:
            return _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
        if "/v1/chat/completions" in lowered or "/v1/completions" in lowered:
            return _UPSTREAM_URL_SURFACE_OPENAI_CHAT
    call_type = str(request_kwargs.get("call_type") or "").strip().lower()
    if call_type in {"messages", "amessages", "anthropic", "anthropic_messages"}:
        return _UPSTREAM_URL_SURFACE_ANTHROPIC
    if call_type in {"responses", "aresponses", "response"}:
        return _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    if call_type in {"completion", "acompletion", "chat_completion", "achat_completion"}:
        return _UPSTREAM_URL_SURFACE_OPENAI_CHAT
    if _responses_request_module._request_is_responses_api(request_kwargs):
        return _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    if request_kwargs.get("messages") is not None:
        return _UPSTREAM_URL_SURFACE_OPENAI_CHAT
    if request_kwargs.get("input") is not None:
        return _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    return ""


def _request_attempted_upstream_surfaces(request_kwargs: Optional[dict]) -> list[str]:
    if not isinstance(request_kwargs, dict):
        return []
    raw_values: Any = request_kwargs.get(_ATTEMPTED_UPSTREAM_URL_SURFACES_KEY)
    if raw_values is None:
        metadata = _request_context_module._request_metadata_dict(
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
        metadata = _request_context_module._request_metadata_dict(
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
    previous_deployment_id = _request_surface_deployment_id(request_kwargs)
    if (
        previous_deployment_id
        and isinstance(deployment_id, str)
        and deployment_id.strip()
        and deployment_id.strip() != previous_deployment_id
    ):
        _clear_protocol_fallback_request_state(request_kwargs)
    request_kwargs[_CURRENT_UPSTREAM_URL_SURFACE_KEY] = surface
    metadata = _request_context_module._request_metadata_dict(
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
        target_deployment_id = target_deployment_id.strip()
        request_kwargs[_SURFACE_TARGET_DEPLOYMENT_ID_KEY] = target_deployment_id
        updated_metadata[_SURFACE_TARGET_DEPLOYMENT_ID_KEY] = target_deployment_id
    else:
        request_kwargs.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
        updated_metadata.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
    request_kwargs["litellm_metadata"] = updated_metadata


def _request_surface_target_deployment_id(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    if not isinstance(request_kwargs, dict):
        return None
    value = request_kwargs.get(_SURFACE_TARGET_DEPLOYMENT_ID_KEY)
    if not isinstance(value, str) or not value.strip():
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs, "litellm_metadata"
        ) or {}
        value = metadata.get(_SURFACE_TARGET_DEPLOYMENT_ID_KEY)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _clear_request_surface_target(request_kwargs: Optional[dict]) -> None:
    if not isinstance(request_kwargs, dict):
        return
    request_kwargs.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs, "litellm_metadata"
    )
    if isinstance(metadata, dict) and _SURFACE_TARGET_DEPLOYMENT_ID_KEY in metadata:
        updated_metadata = metadata.copy()
        updated_metadata.pop(_SURFACE_TARGET_DEPLOYMENT_ID_KEY, None)
        request_kwargs["litellm_metadata"] = updated_metadata


def _deployment_surface(deployment: Any) -> str:
    if not isinstance(deployment, dict):
        return ""
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return ""
    return _normalized_deployment_surface(model_info.get(_UPSTREAM_URL_SURFACE_KEY))


def _deployment_protocol_mode(deployment: Any) -> str:
    if not isinstance(deployment, dict):
        return _UPSTREAM_PROTOCOL_MODE_FALLBACK
    model_info = deployment.get("model_info")
    if not isinstance(model_info, dict):
        return _UPSTREAM_PROTOCOL_MODE_FALLBACK
    value = str(model_info.get(_UPSTREAM_PROTOCOL_MODE_KEY, _UPSTREAM_PROTOCOL_MODE_FALLBACK)).strip().lower()
    return value if value in {_UPSTREAM_PROTOCOL_MODE_FALLBACK, _UPSTREAM_PROTOCOL_MODE_FIXED} else _UPSTREAM_PROTOCOL_MODE_FALLBACK


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


def _request_surface_for_deployment(
    request_kwargs: Optional[dict],
    deployment: Any,
) -> str:
    if _responses_request_module._request_has_structured_codex_compaction(
        request_kwargs
    ):
        # Remote Codex compaction is a Responses protocol operation: the
        # only acceptable terminal payload is an encrypted ``compaction``
        # item. A Chat or Anthropic adapter can produce an ordinary answer,
        # but it cannot satisfy that contract. Ignore fixed/stale fallback
        # surface state and probe the route's Responses endpoint instead.
        return _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    if _deployment_protocol_mode(deployment) == _UPSTREAM_PROTOCOL_MODE_FIXED:
        return _deployment_surface(deployment)
    deployment_id = _responses_request_module._deployment_id(deployment)
    active_deployment_id = _request_surface_deployment_id(request_kwargs)
    target_deployment_id = _request_surface_target_deployment_id(request_kwargs)
    requested_surface = _request_current_upstream_surface(request_kwargs)
    if (
        requested_surface
        and deployment_id
        and deployment_id in {active_deployment_id, target_deployment_id}
    ):
        return requested_surface
    client_surface = _request_client_surface(request_kwargs)
    cached_surface = _protocol_fallback_cached_surface(request_kwargs, deployment)
    return cached_surface or client_surface or _deployment_surface(deployment)


def _surface_adapter_model(model: Any, surface: str) -> Any:
    if not isinstance(model, str) or not model.strip():
        return model
    upstream = model.strip()
    # Subscription-backed adapters carry their authentication transport in
    # the LiteLLM model prefix. Do not collapse ChatGPT OAuth routes into the
    # ordinary OpenAI API adapter merely because both speak Responses.
    if upstream.startswith("chatgpt/") and surface != _UPSTREAM_URL_SURFACE_ANTHROPIC:
        return upstream
    for prefix in (
        "anthropic/",
        "openai/",
        "chatgpt/",
    ):
        if upstream.startswith(prefix):
            upstream = upstream[len(prefix):]
            break
    if surface == _UPSTREAM_URL_SURFACE_ANTHROPIC:
        return f"anthropic/{upstream}"
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
    adapter = (
        "chatgpt"
        if isinstance(adapted_model, str) and adapted_model.startswith("chatgpt/")
        else "anthropic"
        if surface == _UPSTREAM_URL_SURFACE_ANTHROPIC
        else "openai"
    )
    request_kwargs["custom_llm_provider"] = adapter
    if surface == _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES:
        request_kwargs.pop("use_chat_completions_api", None)
    else:
        request_kwargs["use_chat_completions_api"] = True
        if _protocol_fallback_relax_tool_choice(request_kwargs):
            request_kwargs["tool_choice"] = "auto"
            for container_key in ("extra_body", "litellm_params"):
                container = request_kwargs.get(container_key)
                if not isinstance(container, dict):
                    continue
                updated_container = container.copy()
                updated_container.pop("tool_choice", None)
                updated_container.pop("function_call", None)
                request_kwargs[container_key] = updated_container

    litellm_params = request_kwargs.get("litellm_params")
    if isinstance(litellm_params, dict):
        updated_params = litellm_params.copy()
        updated_params["model"] = adapted_model
        updated_params["custom_llm_provider"] = request_kwargs[
            "custom_llm_provider"
        ]
        request_kwargs["litellm_params"] = updated_params
    _api_base_module.apply_surface_api_base(request_kwargs, surface)


def _next_upstream_surface_for_failed_deployment(
    router: Any,
    exception: Exception,
    request_kwargs: Optional[dict],
) -> Optional[tuple[str, str]]:
    """Return the configured fallback for this exact failed deployment.

    The primary protocol is always derived from the incoming request.  A
    fallback is therefore valid only after that primary protocol is known to
    be unsupported, and it must stay on the same deployment.
    """

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
            if _responses_request_module._deployment_id(candidate) == failed_id
        ),
        None,
    )
    if _deployment_protocol_mode(deployment) != _UPSTREAM_PROTOCOL_MODE_FALLBACK:
        return None
    fallback_surface = _deployment_surface(deployment)
    if not fallback_surface:
        return None
    if (
        _responses_request_module._request_has_structured_codex_compaction(
            request_kwargs
        )
        and fallback_surface != _UPSTREAM_URL_SURFACE_OPENAI_RESPONSES
    ):
        # A structured compaction cannot be downgraded to the configured Chat
        # or Anthropic surface. Let normal ordered failover select another
        # deployment instead of accepting an ordinary assistant response as a
        # false compaction success.
        return None
    attempted = _request_attempted_upstream_surfaces(request_kwargs)
    current_surface = _request_current_upstream_surface(request_kwargs)
    if not current_surface:
        current_surface = _normalized_request_surface(
            getattr(exception, "failed_deployment_surface", None)
        )
    if current_surface and current_surface not in attempted:
        attempted.append(current_surface)
    if fallback_surface in attempted:
        return None
    client_surface = _request_client_surface(request_kwargs) or current_surface
    if not current_surface or not client_surface:
        return None
    _set_protocol_fallback_request_state(
        request_kwargs,
        from_surface=current_surface,
        client_surface=client_surface,
        cache_hit=False,
    )
    return fallback_surface, failed_id


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

    litellm_metadata = _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    updated_metadata = litellm_metadata.copy()
    updated_metadata["model_info"] = selected_model_info
    deployment_model_name = selected_model_info.get("model_group")
    if isinstance(deployment_model_name, str) and deployment_model_name.strip():
        updated_metadata["deployment_model_name"] = deployment_model_name.strip()
    api_base = selected_litellm_params.get("api_base")
    if isinstance(api_base, str) and api_base.strip():
        updated_metadata["api_base"] = api_base
    request_kwargs["litellm_metadata"] = updated_metadata
    excluded_ids = _responses_request_module._request_excluded_deployment_ids(
        request_kwargs
    )
    excluded_ids.update(
        _responses_request_module._request_excluded_deployment_ids(marker)
    )
    if excluded_ids:
        request_kwargs["_excluded_deployment_ids"] = sorted(excluded_ids)
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


def _surface_adapted_dispatch_kwargs(request_kwargs: Optional[dict]) -> dict:
    """Copy callback kwargs and reapply the selected protocol's wire values.

    LiteLLM's generic router rebuilds callback kwargs from the raw deployment
    parameters.  That merge can overwrite the surface adapter's top-level
    model/provider/api_base values even though the request-scoped surface
    marker is still present.  Keep routing state untouched and adapt only the
    copy handed to the provider callback.
    """

    if not isinstance(request_kwargs, dict):
        return {}
    adapted = request_kwargs.copy()
    marker = _selected_deployment_marker_from_box()
    surface = _request_current_upstream_surface(adapted)
    if not surface and isinstance(marker, dict):
        surface = _normalized_request_surface(
            marker.get(_CURRENT_UPSTREAM_URL_SURFACE_KEY)
        )
    if not surface:
        return adapted
    upstream_model = None
    if isinstance(marker, dict):
        marker_params = marker.get("litellm_params")
        if isinstance(marker_params, dict):
            upstream_model = marker_params.get("model")
    if upstream_model is None:
        upstream_model = adapted.get("model")
    _apply_surface_adapter_to_request(adapted, surface, upstream_model)
    return adapted


def _remember_selected_deployment_for_request(
    request_kwargs: Optional[dict],
    deployment: Any,
) -> None:
    deployment_marker = _selected_deployment_request_marker(deployment)
    marker = _selected_deployment_marker_from_box()
    deployment_id = _responses_request_module._deployment_id(deployment)
    marker_id = (marker.get("model_info") or {}).get("id") if isinstance(marker, dict) else None
    if (
        not isinstance(marker, dict)
        or (deployment_id and marker_id and marker_id != deployment_id)
    ):
        marker = deployment_marker
    if marker is not None and isinstance(request_kwargs, dict):
        selected_surface = _normalized_request_surface(
            marker.get(_CURRENT_UPSTREAM_URL_SURFACE_KEY)
        )
        if not selected_surface:
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
    if _is_image_generation_all_deployments_unsupported_error(exception):
        reason = "image-generation-tool-all-deployments-unsupported"
    elif _is_no_deployments_available_error(exception):
        reason = "no-available-deployment"
    elif type(exception).__name__ == "ProxyModelNotFoundError":
        reason = "model-not-configured"
    elif _is_terminal_prompt_or_policy_error(exception):
        reason = "terminal-prompt-or-policy"
    elif _is_image_generation_tool_runtime_fallback_error(exception):
        reason = "image-generation-tool-runtime-fallback"
    elif _is_upstream_deployment_failover_error(exception):
        reason = "upstream-auth-or-balance"
    elif _is_upstream_gateway_bad_request_error(exception):
        reason = "upstream-gateway-bad-request"
    elif _is_upstream_request_body_storage_capacity_error(exception):
        reason = "upstream-request-body-capacity"
    elif _is_responses_schema_unsupported_error(exception):
        reason = "responses-schema-unsupported"
    elif _is_image_parameter_or_capability_bad_request_error(exception):
        reason = "image-parameter-or-capability-bad-request"
    elif _is_deployment_compatible_bad_request_error(exception):
        reason = "upstream-compatible-bad-request"
    elif getattr(exception, "stream_incomplete", False) and status_code is None:
        reason = "upstream-stream-incomplete"
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
        "recovery_policy": _recovery_policy_for_exception(exception),
    }


def _recovery_diagnostic(exception: Exception) -> dict[str, Any]:
    """Return a stable, secret-free explanation for local recovery status."""
    status_code = _exception_status_code(exception)
    text = _exception_text(exception)

    if any(marker in text for marker in _UPSTREAM_BALANCE_ERROR_MARKERS):
        result = {
            "kind": "billing",
            "title": "Billing or credit limit",
            "detail": "The upstream reported insufficient balance, quota, or credits.",
        }
    elif status_code in (401, 403) or any(
        marker in text
        for marker in (
            "authentication",
            "unauthorized",
            "api key",
            "invalid key",
            "permission denied",
            "access denied",
        )
    ):
        result = {
            "kind": "authentication",
            "title": "Authentication rejected",
            "detail": "Check the provider API key and account permissions.",
        }
    elif _exception_indicates_network_connectivity_error(exception):
        result = {
            "kind": "network",
            "title": "Network connection failed",
            "detail": "LiteLLM cannot currently reach the upstream service.",
        }
    elif status_code == 429 or any(
        marker in text
        for marker in (
            "rate limit",
            "too many requests",
            "concurrency limit",
            "retry later",
            "try again later",
        )
    ):
        result = {
            "kind": "rate_limit",
            "title": "Rate limited",
            "detail": "The upstream is asking LiteLLM to wait before trying again.",
        }
    elif _is_local_stream_timeout_error(exception) or _exception_indicates_timeout_or_long_wait(exception):
        result = {
            "kind": "timeout",
            "title": "Upstream timed out",
            "detail": "The upstream did not complete within the configured time.",
        }
    elif _is_no_deployments_available_error(exception):
        result = {
            "kind": "unknown",
            "title": "No healthy route",
            "detail": "No configured upstream route is currently available.",
        }
    elif status_code is not None and status_code >= 500:
        result = {
            "kind": "unknown",
            "title": "Upstream service unavailable",
            "detail": "The upstream returned a temporary server error.",
        }
    else:
        result = {
            "kind": "unknown",
            "title": "Recovery is retrying",
            "detail": "LiteLLM is waiting for the upstream to become available.",
        }

    if status_code is not None:
        result["status_code"] = status_code
    return result


def _coerce_order(value: Any) -> Optional[_RouteOrder]:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return value if math.isfinite(value) else None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return 1
        try:
            number = float(text)
        except ValueError:
            return None
        if not math.isfinite(number):
            return None
        return int(number) if number.is_integer() else number
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
            "error sending request",
            "error sending a request",
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


_NETWORK_CONNECTIVITY_MARKER_ATTR = "_litellm_menu_network_connectivity"


def _is_network_recovery_exception(exception: Exception) -> bool:
    """Return whether a route failure should keep the client stream in recovery.

    A sanitized upstream exception intentionally replaces the original message,
    so preserve an explicit marker when that wrapper is created.  The textual
    check also covers wrappers produced by LiteLLM before they reach this
    module.
    """

    pending = [exception]
    seen: set[int] = set()
    while pending:
        candidate = pending.pop()
        candidate_id = id(candidate)
        if candidate_id in seen:
            continue
        seen.add(candidate_id)
        if bool(getattr(candidate, _NETWORK_CONNECTIVITY_MARKER_ATTR, False)):
            return True
        if _exception_indicates_network_connectivity_error(candidate):
            return True
        if "temporary network connectivity error" in _exception_text(candidate):
            return True
        for attr in ("__cause__", "__context__", "original_exception"):
            nested = getattr(candidate, attr, None)
            if isinstance(nested, Exception) and id(nested) not in seen:
                pending.append(nested)
    return False


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
        and not _responses_request_module._request_forces_browser_compatible_headers(request_kwargs)
    )


def _is_image_generation_tool_runtime_fallback_error(exception: Exception) -> bool:
    return getattr(exception, "image_generation_tool_runtime_fallback", False) is True


def _is_image_generation_tool_capability_error(exception: Exception) -> bool:
    if getattr(
        exception,
        _IMAGE_GENERATION_TOOL_CAPABILITY_UNSUPPORTED_ATTR,
        False,
    ) is True:
        return True
    if _exception_status_code(exception) not in (400, 404, 422):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_responses_schema_unsupported_error(exception):
        return False
    text = _exception_text(exception)
    if not text:
        return False
    mentions_image_tool = any(
        marker in text
        for marker in (
            "image_generation",
            "image generation",
            "image_generation_tool",
            "image generation tool",
            "imagegen",
            "gpt-image",
        )
    )
    if not mentions_image_tool:
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
            "invalid tool type",
            "tool type is invalid",
            "not available",
            "isn't available",
            "is not available",
            "not directly exposed",
            "does not support",
            "doesn't support",
            "not support image",
            "requires an image model",
            "no access to",
            "without access to",
        )
    )


def _is_image_generation_all_deployments_unsupported_error(
    exception: Exception,
) -> bool:
    return getattr(
        exception,
        _IMAGE_GENERATION_TOOL_ALL_UNSUPPORTED_ATTR,
        False,
    ) is True


def _mark_image_generation_all_deployments_unsupported(
    exception: Exception,
) -> None:
    message = (
        "All configured deployments for this model rejected the "
        "image_generation tool."
    )
    try:
        setattr(exception, _IMAGE_GENERATION_TOOL_ALL_UNSUPPORTED_ATTR, True)
        setattr(exception, "status_code", 400)
        setattr(exception, "message", message)
        exception.args = (message,)
    except Exception:
        pass


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
            "considered high risk",
            "classified as high risk",
            "high-risk request",
            "high risk request",
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


def _request_contains_hosted_web_search_tool(request_kwargs: Optional[dict]) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    tools = request_kwargs.get("tools")
    if not isinstance(tools, list):
        return False
    return any(
        isinstance(tool, dict)
        and tool.get("type")
        in (_HOSTED_WEB_SEARCH_TOOL_TYPES | _PROVIDER_NATIVE_WEB_SEARCH_TOOL_TYPES)
        for tool in tools
    )


def _is_native_responses_web_search_unsupported_error(
    exception: Exception,
    request_kwargs: Optional[dict] = None,
) -> bool:
    status_code = _exception_status_code(exception)
    if status_code is not None and status_code not in {400, 404, 422}:
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    text = _exception_text(exception)
    if not text:
        return False
    # A provider may wrap quota, auth, transport, or backend-search failures
    # in a 400/422 ``invalid_request_error`` that also repeats the requested
    # web-search type.  Those are not capability evidence and must not poison
    # the short negative probe memory.
    if (
        _is_upstream_deployment_failover_error(exception)
        or _is_network_recovery_exception(exception)
        or any(
            marker in text
            for marker in (
                *_UPSTREAM_BALANCE_ERROR_MARKERS,
                *_UPSTREAM_TEMPORARY_ERROR_MARKERS,
                "quota",
                "credit",
                "billing",
                "authentication",
                "unauthorized",
                "permission",
                "forbidden",
                "api key",
                "timed out",
                "timeout",
                "temporarily",
                "provider search",
                "search provider",
                "provider-search",
                "fetch failed",
                "search failed",
                "search error",
                "exa",
                "tavily",
                "brave search",
                "duckduckgo",
            )
        )
    ):
        return False
    request_has_hosted_web_search = _request_contains_hosted_web_search_tool(
        request_kwargs
    )
    if not any(
        marker in text
        for marker in (
            "web_search",
            "web search",
            "web_search_preview",
            "hosted web search",
        )
    ) and not request_has_hosted_web_search:
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
            # Some OpenAI-compatible gateways reject Hosted web-search
            # declarations at schema validation time instead of returning a
            # conventional "unsupported tool" message. Keep these markers
            # behind the web-search guard above so unrelated schema errors do
            # not enter the web-search bridge.
            "input_schema",
            "input schema",
            "schema type error",
            "类型错误",
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


def _is_upstream_request_body_storage_capacity_error(exception: Exception) -> bool:
    """Recognize the route-local gateway capacity rejection seen on large bodies."""
    if _exception_status_code(exception) not in {400, 413}:
        return False
    return "request body storage capacity exhausted" in _exception_text(exception)


def _is_structured_codex_compaction_body_capacity_error(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> bool:
    """A signed compaction body may be too large for one gateway, not the model."""
    return (
        _responses_request_module._request_has_structured_codex_compaction(
            request_kwargs
        )
        and _is_upstream_request_body_storage_capacity_error(exception)
    )


def _is_request_scoped_priority_deployment_failover_error(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> bool:
    """Allow only the known compaction storage rejection to advance routes."""
    return _is_codex_compaction_capability_unsupported_error(exception) or _is_priority_deployment_failover_error(
        exception
    ) or _is_structured_codex_compaction_body_capacity_error(
        exception,
        request_kwargs,
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
    model_info = _request_context_module._request_model_info(request_kwargs)
    deployment_id = model_info.get("id")
    if isinstance(deployment_id, str) and deployment_id.strip():
        return deployment_id
    for metadata_key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key)
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
    model_info = _request_context_module._request_model_info(request_kwargs)
    route_key = model_info.get("route_key")
    api_base = _responses_request_module._request_api_base(request_kwargs)
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
    model_group = _request_public_model(request_kwargs)
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


def _order_from_route_key(route_key: Any) -> Optional[_RouteOrder]:
    if not isinstance(route_key, str):
        return None
    match = re.search(
        r"(?:^|/)\s*order\s*=\s*([+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?)\s*(?:/|$)",
        route_key,
    )
    if match is None:
        return None
    return _coerce_order(match.group(1))


def _deployment_order_from_request(request_kwargs: Optional[dict]) -> Optional[_RouteOrder]:
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
    model_info = _request_context_module._request_model_info(request_kwargs)
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
        metadata = _request_context_module._request_metadata_dict(request_kwargs, metadata_key)
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
    request_model_info = _request_context_module._request_model_info(request_kwargs)
    if isinstance(request_model_info, dict) and "order" in request_model_info:
        return True
    if isinstance(request_model_info, dict) and _order_from_route_key(request_model_info.get("route_key")) is not None:
        return True
    return False


def _mark_exception_for_deployment_failover(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    try:
        setattr(
            exception,
            _ROUTE_FAILURE_POLICY_ATTR,
            _recovery_policy_for_exception(exception),
        )
    except Exception:
        pass
    _apply_current_selected_deployment_to_request(request_kwargs)
    deployment_id = _deployment_id_from_request(request_kwargs)
    route_key = _deployment_route_key_from_request(request_kwargs)
    _record_image_generation_tool_unsupported(exception, request_kwargs)
    _record_web_search_tool_unsupported(exception, request_kwargs)
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
        and getattr(exception, "failed_deployment_order", None) is None
    ):
        try:
            exception.failed_deployment_order = deployment_order  # type: ignore[attr-defined]
        except Exception:
            pass
    should_sync_exclusions = (
        not _is_local_stream_timeout_error(exception)
        and (
            not _should_retry_same_deployment_before_fallback(exception)
            or _same_deployment_retry_exhausted(exception)
        )
    )
    protocol_surface_failover = _is_upstream_surface_failover_error(exception)
    if should_sync_exclusions and not protocol_surface_failover:
        _sync_failed_deployment_exclusions(
            request_kwargs, exception, deployment_id=deployment_id
        )
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
        route_key=route_key,
        deployment_order=deployment_order,
        request=_trace_module._trace_request_summary(request_kwargs),
        exception=_trace_exception(exception),
    )
    if _protocol_fallback_attempt_active(request_kwargs):
        _mark_protocol_fallback_failure(exception, request_kwargs)
        return
    if protocol_surface_failover:
        return
    if not _protocol_fallback_failure_recorded(request_kwargs):
        _record_deployment_failure_for_cooldown(exception, request_kwargs)


def _mark_exception_for_upstream_surface_failover(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    if _is_forced_tool_choice_unsupported_error(exception, request_kwargs):
        _mark_protocol_fallback_relax_tool_choice(request_kwargs)
    try:
        exception.upstream_surface_unsupported = True  # type: ignore[attr-defined]
    except Exception:
        pass
    _mark_exception_for_deployment_failover(exception, request_kwargs)


def _protocol_fallback_attempt_active(
    request_kwargs: Optional[dict],
) -> bool:
    if not isinstance(request_kwargs, dict):
        return False
    from_surface = _normalized_request_surface(
        request_kwargs.get(_PROTOCOL_FALLBACK_FROM_SURFACE_KEY)
    )
    if not from_surface:
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        ) or {}
        from_surface = _normalized_request_surface(
            metadata.get(_PROTOCOL_FALLBACK_FROM_SURFACE_KEY)
        )
    current_surface = _request_current_upstream_surface(request_kwargs)
    return bool(
        from_surface
        and current_surface
        and current_surface != from_surface
    )


def _mark_protocol_fallback_failure(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    if _protocol_fallback_failure_recorded(request_kwargs):
        return
    if getattr(exception, _PROTOCOL_FALLBACK_FAILURE_RECORDED_ATTR, False):
        return
    try:
        setattr(exception, _PROTOCOL_FALLBACK_FAILURE_RECORDED_ATTR, True)
    except Exception:
        pass
    recovery_policy = _recovery_policy_for_exception(exception)
    if recovery_policy == _RECOVERY_POLICY_COOLDOWN and isinstance(request_kwargs, dict):
        marker = {
            "deployment_id": (
                _responses_execution_module._failed_deployment_id(exception)
                or _deployment_id_from_request(request_kwargs)
            ),
            "route_key": (
                _responses_execution_module._failed_deployment_route_key(exception)
                or _deployment_route_key_from_request(request_kwargs)
            ),
        }
        request_kwargs[_PROTOCOL_FALLBACK_FAILURE_RECORDED_KEY] = marker
        metadata = _request_context_module._request_metadata_dict(
            request_kwargs,
            "litellm_metadata",
        ) or {}
        updated_metadata = metadata.copy()
        updated_metadata[_PROTOCOL_FALLBACK_FAILURE_RECORDED_KEY] = marker
        request_kwargs["litellm_metadata"] = updated_metadata
    # Count the completed protocol pair once only when its configured policy
    # permits cooldown accounting. A deterministic request error records
    # neither a deployment failure nor a marker that could suppress a later,
    # genuinely transient wrapper failure.
    _record_deployment_failure_for_cooldown(
        exception,
        request_kwargs,
        force=True,
    )
    _clear_protocol_fallback_cache_for_request(request_kwargs)


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
        excluded_ids.update(_responses_request_module._request_excluded_deployment_ids(request_kwargs))
    failed_id = deployment_id or _responses_execution_module._failed_deployment_id(exception)
    if (
        failed_id
        and not _is_local_stream_timeout_error(exception)
        and (
            not _should_retry_same_deployment_before_fallback(exception)
            or _same_deployment_retry_exhausted(exception)
        )
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
    if _is_codex_compaction_capability_unsupported_error(exception):
        return True
    if _is_context_size_error(exception):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_image_generation_all_deployments_unsupported_error(exception):
        return False
    if _is_image_generation_tool_runtime_fallback_error(exception):
        return True
    if _is_upstream_surface_failover_error(exception):
        return True
    if getattr(exception, "stream_incomplete", False):
        return True
    if _is_upstream_deployment_failover_error(exception):
        return True
    if _is_upstream_gateway_bad_request_error(exception):
        return True
    if _is_image_parameter_or_capability_bad_request_error(exception):
        return True
    if _is_deployment_compatible_bad_request_error(exception):
        return True
    if _exception_indicates_network_connectivity_error(exception):
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
    if _is_sanitized_upstream_route_failure_error(exception):
        return False
    return _recovery_policy_for_exception(exception) == _RECOVERY_POLICY_COOLDOWN


def _record_deployment_failure_for_cooldown(
    exception: Exception,
    request_kwargs: Optional[dict],
    *,
    force: bool = False,
) -> None:
    if not _deployment_cooldown_enabled():
        return
    if not _deployment_cooldown_recording_enabled_for_request(request_kwargs):
        return
    if _is_sanitized_upstream_route_failure_error(exception):
        return
    # `force` only bypasses the once-per-fallback de-duplication gate. It
    # must never turn a deterministic request/construction error into a
    # cooldown failure. An explicit request-error recovery policy may opt
    # into cooldown accounting, so evaluate the policy before honoring the
    # protocol-fallback force path.
    recovery_policy = _recovery_policy_for_exception(exception)
    if recovery_policy != _RECOVERY_POLICY_COOLDOWN:
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

    # An upstream stream that terminates before its protocol terminal event
    # has already forced the client to reconnect. Unlike an ordinary
    # transient request error, another attempt through that route would
    # surface another user-visible stream failure, so quarantine it after the
    # first occurrence regardless of the client protocol.
    threshold = (
        1
        if getattr(exception, "stream_incomplete", False)
        else _deployment_cooldown_failure_threshold()
    )
    cooldown_seconds = _deployment_cooldown_seconds()
    request_log = _request_log_record("cooldown", request_kwargs)

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
            for key in (
                "model_group",
                "provider",
                "upstream_model",
                "api_base_host",
                "deployment_order",
            ):
                value = request_log.get(key)
                if value not in (None, ""):
                    state[key] = value

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
    entry["cooldown_until"] = round(
        max(0.0, float(state.get("cooldown_until") or 0.0)),
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
            active_cooldown = next(
                (
                    (cooldown_key, state)
                    for cooldown_key in _deployment_cooldown_keys_from_deployment(
                        deployment
                    )
                    if (
                        state := _active_cooldown_state_for_key(
                            cooldowns, cooldown_key, now
                        )
                    )
                    is not None
                ),
                None,
            )
            if active_cooldown is not None:
                cooldown_key, state = active_cooldown
                trace_entry = _deployment_cooldown_trace_entry(
                    deployment,
                    state,
                    now,
                    cooldown_key=cooldown_key,
                )
                cooled.append(trace_entry)
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
        _responses_request_module._request_target_order(request_kwargs) is not None
        and bool(_responses_request_module._request_excluded_deployment_ids(request_kwargs))
    )


def _mark_no_deployments_for_order_exhaustion(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> None:
    if not _is_no_deployments_available_error(exception):
        return
    target_order = _responses_request_module._request_target_order(request_kwargs)
    if target_order is None:
        return
    if _responses_execution_module._failed_deployment_order(exception) is None:
        try:
            exception.failed_deployment_order = target_order  # type: ignore[attr-defined]
        except Exception:
            pass
    # No-healthy means the selected pool has no candidate, rather than a
    # newly identified failed deployment. Keep existing constraints intact;
    # recovery will deliberately rotate/reset orders on its next poll.


def _raise_retryable_stream_disconnect(
    request_data: dict,
    *,
    original_exception: Exception,
    fallback_exception: Optional[Exception],
) -> None:
    """Propagate an exhausted route as an ordinary upstream error.

    ``asyncio.CancelledError`` is reserved for an actual cancelled task.  A
    route pool that has been exhausted is a server-side failure; exposing it
    as cancellation makes streaming clients such as Cherry Studio report an
    unexplained interruption and discard the useful error details.
    """
    trigger_exception = fallback_exception or original_exception
    _trace_module._route_trace(
        "retryable_stream_disconnect",
        request_id=_trace_request_id(request_data),
        session=_trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        original_exception=_trace_exception(original_exception),
        exception=_trace_exception(trigger_exception),
    )
    raise trigger_exception


def _should_sanitize_final_upstream_route_error(exception: Exception) -> bool:
    if _is_sanitized_upstream_route_failure_error(exception):
        return False
    if _is_context_size_error(exception):
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _is_image_generation_tool_runtime_fallback_error(exception):
        return True
    return _recovery_policy_for_exception(exception) != _RECOVERY_POLICY_ERROR


def _should_retry_final_upstream_route_error(
    exception: Exception,
    request_kwargs: Optional[dict] = None,
) -> bool:
    if _is_constrained_no_deployments_error(exception, request_kwargs):
        return False
    return _recovery_policy_for_exception(exception) != _RECOVERY_POLICY_ERROR


def _should_retry_same_deployment_before_fallback(exception: Exception) -> bool:
    return _same_deployment_retry_pending(exception)


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
        preserve_failed_deployment=not no_deployments_available,
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
    if _is_network_recovery_exception(exception):
        try:
            setattr(sanitized, _NETWORK_CONNECTIVITY_MARKER_ATTR, True)
        except Exception:
            pass
    try:
        setattr(
            sanitized,
            _SANITIZED_UPSTREAM_ROUTE_FAILURE_POLICY_ATTR,
            _recovery_policy_for_exception(exception),
        )
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
    model_info = _request_context_module._request_model_info(request_kwargs)
    if not model_info:
        model_info = _request_context_module._request_model_info(
            outer_request_kwargs
        )
    configured_surface = _normalized_request_surface(
        model_info.get(_UPSTREAM_URL_SURFACE_KEY)
    )
    protocol_mode = str(
        model_info.get(
            _UPSTREAM_PROTOCOL_MODE_KEY,
            _UPSTREAM_PROTOCOL_MODE_FALLBACK,
        )
    ).strip().lower()
    client_surface = _request_client_surface(outer_request_kwargs)
    if not client_surface:
        client_surface = _request_client_surface(request_kwargs)
    invalid_parameter_combination = any(
        marker in text
        for marker in (
            "请求参数组合无效",
            "invalid parameter combination",
            "invalid parameters combination",
            "invalid combination of parameters",
            "invalid request parameter combination",
        )
    )
    forced_tool_choice_unsupported = _is_forced_tool_choice_unsupported_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    )
    # A named choice may already have been retried as ``auto`` and then had
    # its namespace/custom tools bridged to functions.  The bridge retains
    # the confirmed incompatibility state, so a repeated explicit
    # tool-choice rejection can advance to the configured protocol fallback
    # even though the active retry no longer carries a named choice.
    relaxed_tool_choice_still_rejected = (
        (_protocol_fallback_relax_tool_choice(request_kwargs)
         or _protocol_fallback_relax_tool_choice(outer_request_kwargs))
        and _is_forced_tool_choice_unsupported_text(exception)
    )
    if (
        (
            invalid_parameter_combination
            or forced_tool_choice_unsupported
            or relaxed_tool_choice_still_rejected
        )
        and protocol_mode == _UPSTREAM_PROTOCOL_MODE_FALLBACK
        and client_surface == current_surface
        and configured_surface
        and configured_surface != current_surface
    ):
        return True
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


def _request_forced_tool_choice(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    """Return an explicitly forced tool choice from the active request."""

    for request in (request_kwargs, outer_request_kwargs):
        if not isinstance(request, dict) or "tool_choice" not in request:
            continue
        value = request.get("tool_choice")
        if value is None:
            continue
        if isinstance(value, str):
            normalized = value.strip().lower()
            if normalized in {"", "auto", "none"}:
                continue
            return value
        if isinstance(value, dict):
            choice_type = str(value.get("type") or "").strip().lower()
            if choice_type in {"", "auto", "none"}:
                continue
            return value
        return value
    return None


def _is_forced_tool_choice_unsupported_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    """Recognize an explicit upstream rejection of a forced tool choice.

    This is intentionally narrower than a generic bad-request classifier. A
    protocol retry is safe only when the request actually forced a tool and
    the upstream response explicitly says that mode is unsupported or asks
    for tool_choice=auto.
    """

    if _exception_status_code(exception) not in {400, 422}:
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    if _request_forced_tool_choice(request_kwargs, outer_request_kwargs) is None:
        return False
    return _is_forced_tool_choice_unsupported_text(exception)


def _is_forced_tool_choice_unsupported_text(exception: Exception) -> bool:
    """Match an upstream error that explicitly rejects forced tool choice."""

    if _exception_status_code(exception) not in {400, 422}:
        return False
    if _is_terminal_prompt_or_policy_error(exception):
        return False
    text = _exception_text(exception)
    if not text or ("tool_choice" not in text and "tool choice" not in text):
        return False

    unsupported_markers = (
        "unsupported",
        "not support",
        "does not support",
        "not allowed",
        "invalid",
        "不支持",
        "不允许",
        "无效",
    )
    has_unsupported_marker = any(marker in text for marker in unsupported_markers)
    requests_auto = any(
        marker in text
        for marker in (
            "tool_choice=auto",
            "tool_choice = auto",
            "tool choice=auto",
            "tool choice = auto",
            "改用tool_choice=auto",
            "改用 tool_choice=auto",
            "请改用tool_choice=auto",
            "请改用 tool_choice=auto",
        )
    )
    forced_mode_marker = any(
        marker in text
        for marker in (
            "forced tool choice",
            "force tool choice",
            "forced choice",
            "强制选择",
            "强制工具",
        )
    )
    return bool(requests_auto or (has_unsupported_marker and forced_mode_marker))


def _request_has_tool_compatibility_candidate(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    """Require an actual tool payload before retrying an ambiguous 400.

    Some gateways use the same generic ``invalid parameter combination``
    text for unrelated request errors.  The permissive retry is only useful
    when the request carries a tool definition (including Responses input
    tool items), so a forced choice alone must not trigger it.
    """

    for request in (request_kwargs, outer_request_kwargs):
        if not isinstance(request, dict):
            continue
        tools = request.get("tools")
        if isinstance(tools, list) and any(
            isinstance(tool, dict) and tool.get("type") for tool in tools
        ):
            return True
        input_items = request.get("input")
        if isinstance(input_items, list) and any(
            isinstance(item, dict)
            and item.get("type")
            in {
                "function_call",
                "function_call_output",
                "namespace",
                "custom",
                "tool_search",
            }
            for item in input_items
        ):
            return True
    return False


def _is_forced_tool_choice_auto_retry_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    """Allow one same-protocol auto-choice retry for an ambiguous 400.

    A few OpenAI-compatible gateways collapse the named-tool rejection into a
    generic invalid-parameter-combination response. The request still
    contains a forced choice, so trying auto once is the narrow tool
    compatibility fallback before changing protocol surfaces.
    """

    if not _request_has_tool_compatibility_candidate(
        request_kwargs,
        outer_request_kwargs,
    ):
        return False
    if _is_forced_tool_choice_unsupported_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        return True
    if _exception_status_code(exception) not in {400, 422}:
        return False
    if _request_forced_tool_choice(request_kwargs, outer_request_kwargs) is None:
        return False
    text = _exception_text(exception)
    return any(
        marker in text
        for marker in (
            "请求参数组合无效",
            "invalid parameter combination",
            "invalid parameters combination",
            "invalid combination of parameters",
            "invalid request parameter combination",
        )
    )


def _is_upstream_model_not_found_error(exception: Exception) -> bool:
    if _exception_status_code(exception) != 404:
        return False
    text = _exception_text(exception)
    if not text:
        return False
    if "model_not_found" in text:
        return True
    return bool(
        re.search(
            r"\bmodel\b[^\n]{0,200}\b(?:not found|does not exist|not supported|unsupported|unknown)\b",
            text,
        )
    )


def _is_responses_endpoint_not_found_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> bool:
    if not (
        _responses_request_module._request_is_responses_api(request_kwargs)
        or _responses_request_module._request_is_responses_api(outer_request_kwargs)
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
    if any(
        marker in text
        for marker in (
            "unknown tool type",
            "unsupported tool type",
            "invalid tool type",
        )
    ):
        return False
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
