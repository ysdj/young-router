from __future__ import annotations

import re

from . import codex_fast_tier as _codex_fast_tier_module
from . import computer_facade as _computer_facade_module
from . import image_generation as _image_generation_module
from . import image_inputs as _image_inputs_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_output as _responses_output_module
from . import responses_tools as _responses_tools_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import state as _state_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    Dict,
    Enum,
    List,
    Optional,
    _CURRENT_EXCLUDED_DEPLOYMENT_IDS,
    _CURRENT_SELECTED_DEPLOYMENT_BOX,
    _IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY,
    _JSONStreamEvent,
    _RESPONSES_STREAM_COMPLETED_TYPES,
    _RESPONSES_STREAM_INCOMPLETE_STATUSES,
    _RESPONSES_STREAM_INCOMPLETE_TYPES,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _ROUTE_RECOVERY_POLL_METADATA_KEY,
    _RouteOrder,
    _STREAM_ERROR_FALLBACK_METADATA_KEY,
    _STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS,
    _STREAM_FALLBACK_METADATA_KEY,
    _STREAM_IDLE_TIMEOUT_METADATA_KEY,
    _STREAM_START_TIMEOUT_METADATA_KEY,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _normalize_response_completed_event_usage,
    asyncio,
    copy,
    datetime,
    inspect,
    json,
    os,
    time,
    timezone,
)


_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY = "_route_recovery_forced_target_order"
_CODEX_USAGE_REQUEST_OVERHEAD_BYTES = 32_768
_CODEX_USAGE_TOKENS_PER_BYTE = 1
_USAGE_BOUND_UNSERIALIZABLE = object()


def _route_recovery_state_key(request_data: Optional[dict]) -> str:
    request_id = _routing_module._trace_request_id(request_data)
    if request_id:
        return f"request:{request_id}"
    session = _routing_module._trace_session_context(request_data)
    session_id = session.get("id") if isinstance(session, dict) else None
    model_group = _responses_execution_module._request_model_group(request_data) or "unknown"
    if session_id:
        return f"session:{session_id}:model:{model_group}"
    return f"process:{os.getpid()}:model:{model_group}"


def _route_recovery_state_record(
    request_data: Optional[dict],
    exception: Optional[Exception],
    *,
    status: str,
    attempt: Optional[int] = None,
    started_at_monotonic: Optional[float] = None,
    max_poll_seconds: Optional[float] = None,
    poll_interval_seconds: Optional[float] = None,
    target_order: Any = None,
    cooldown_until: Optional[float] = None,
    cooldown_deployments: Optional[list[dict[str, Any]]] = None,
) -> dict[str, Any]:
    now = time.monotonic()
    record: dict[str, Any] = {
        "key": _route_recovery_state_key(request_data),
        "status": status,
        "pid": os.getpid(),
        "request_id": _routing_module._trace_request_id(request_data),
        "session": _routing_module._trace_session_context(request_data),
        "model_group": _responses_execution_module._request_model_group(request_data),
        "attempt": attempt,
        "max_poll_seconds": max_poll_seconds,
        "poll_interval_seconds": poll_interval_seconds,
        "attempt_timeout_seconds": _routing_module._stream_start_timeout_seconds_for_request(request_data),
        "target_order": target_order,
        "request": _trace_module._trace_request_summary(request_data),
    }
    if cooldown_until is not None and cooldown_until > 0:
        record["cooldown_until"] = round(cooldown_until, 3)
        record["cooldown_remaining_seconds"] = round(
            max(0.0, cooldown_until - time.time()), 3
        )
    if cooldown_deployments:
        record["cooldown_deployments"] = cooldown_deployments
    if started_at_monotonic is not None:
        record["elapsed_seconds"] = round(max(0.0, now - started_at_monotonic), 3)
        if max_poll_seconds is not None and max_poll_seconds > 0:
            record["remaining_poll_seconds"] = round(
                max(0.0, max_poll_seconds - (now - started_at_monotonic)),
                3,
            )
    if exception is not None:
        record["exception"] = _routing_module._trace_exception(exception)
        record["diagnostic"] = _routing_module._recovery_diagnostic(exception)
    return record


def _route_recovery_state_upsert(
    request_data: Optional[dict],
    exception: Optional[Exception],
    *,
    status: str,
    attempt: Optional[int] = None,
    started_at_monotonic: Optional[float] = None,
    max_poll_seconds: Optional[float] = None,
    poll_interval_seconds: Optional[float] = None,
    target_order: Any = None,
    cooldown_until: Optional[float] = None,
    cooldown_deployments: Optional[list[dict[str, Any]]] = None,
) -> str:
    record = _route_recovery_state_record(
        request_data,
        exception,
        status=status,
        attempt=attempt,
        started_at_monotonic=started_at_monotonic,
        max_poll_seconds=max_poll_seconds,
        poll_interval_seconds=poll_interval_seconds,
        target_order=target_order,
        cooldown_until=cooldown_until,
        cooldown_deployments=cooldown_deployments,
    )
    _state_module._upsert_route_recovery_state(record)
    return str(record.get("key") or "")


def _route_recovery_state_remove(key: str) -> None:
    if key:
        _state_module._remove_route_recovery_state(key)


def _jsonable(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _jsonable(value.value)
    if isinstance(value, list):
        return [_jsonable(item) for item in value]
    if isinstance(value, tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if hasattr(value, "model_dump"):
        try:
            return _jsonable(value.model_dump(mode="json", exclude_none=True))
        except TypeError:
            try:
                return _jsonable(value.model_dump())
            except Exception:
                return None
        except Exception:
            return None
    return None


def _usage_bound_jsonable(value: Any, *, depth: int = 0) -> Any:
    if depth > 32:
        return _USAGE_BOUND_UNSERIALIZABLE
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, Enum):
        return _usage_bound_jsonable(value.value, depth=depth + 1)
    if isinstance(value, (list, tuple)):
        items = []
        for item in value:
            converted = _usage_bound_jsonable(item, depth=depth + 1)
            if converted is _USAGE_BOUND_UNSERIALIZABLE:
                return _USAGE_BOUND_UNSERIALIZABLE
            items.append(converted)
        return items
    if isinstance(value, dict):
        converted_dict: dict[str, Any] = {}
        for key, item in value.items():
            converted = _usage_bound_jsonable(item, depth=depth + 1)
            if converted is _USAGE_BOUND_UNSERIALIZABLE:
                return _USAGE_BOUND_UNSERIALIZABLE
            converted_dict[str(key)] = converted
        return converted_dict
    if hasattr(value, "model_dump"):
        try:
            dumped = value.model_dump(mode="json", exclude_none=True)
        except TypeError:
            try:
                dumped = value.model_dump()
            except Exception:
                return _USAGE_BOUND_UNSERIALIZABLE
        except Exception:
            return _USAGE_BOUND_UNSERIALIZABLE
        return _usage_bound_jsonable(dumped, depth=depth + 1)
    return _USAGE_BOUND_UNSERIALIZABLE


def _explicit_route_model_group(request_data: Optional[dict]) -> Optional[str]:
    route_key = _request_context_module._request_model_info(request_data).get(
        "route_key"
    )
    if not isinstance(route_key, str) or not route_key.strip():
        return None
    for route_part in route_key.split(" / "):
        key, separator, value = route_part.partition("=")
        if separator and key.strip().lower() == "model" and value.strip():
            return value.strip()
    return None


def _codex_request_input_token_upper_bound(
    request_data: Optional[dict],
) -> Optional[int]:
    if not _responses_request_module._request_has_codex_client_evidence(request_data):
        return None
    if not _responses_request_module._request_has_responses_shape(request_data):
        return None
    request_data = request_data or {}
    if "input" not in request_data:
        return None
    if request_data.get("previous_response_id"):
        return None
    if _image_inputs_module._request_has_image_input(request_data):
        return None
    payload: dict[str, Any] = {}
    for key in (
        "model",
        "input",
        "instructions",
        "tools",
        "tool_choice",
        "parallel_tool_calls",
        "reasoning",
        "text",
        "include",
        "temperature",
        "top_p",
        "max_output_tokens",
        "truncation",
        "response_format",
        "user",
        "service_tier",
        "seed",
        "stop",
        "store",
        "client_metadata",
        "prompt_cache_key",
        "metadata",
        "extra_body",
    ):
        value = request_data.get(key)
        if key not in request_data or value is None:
            continue
        json_value = _usage_bound_jsonable(value)
        if json_value is _USAGE_BOUND_UNSERIALIZABLE:
            return None
        payload[key] = json_value
    try:
        payload_bytes = len(
            json.dumps(
                payload,
                ensure_ascii=False,
                separators=(",", ":"),
                default=str,
            ).encode("utf-8")
        )
    except Exception:
        return None
    return (
        payload_bytes + _CODEX_USAGE_REQUEST_OVERHEAD_BYTES
    ) * _CODEX_USAGE_TOKENS_PER_BYTE


def _normalize_sse_response_completed_block(
    block: str,
    delimiter: str,
    *,
    input_token_upper_bound: Optional[int] = None,
) -> str:
    if "data:" not in block:
        return block + delimiter
    line_separator = "\r\n" if "\r\n" in block else "\n"
    data_values: list[str] = []
    for line in block.splitlines():
        if not line.startswith("data:"):
            continue
        value = line[len("data:") :]
        if value.startswith(" "):
            value = value[1:]
        data_values.append(value)
    payload_text = "\n".join(data_values).strip()
    if not payload_text or payload_text == "[DONE]":
        return block + delimiter
    try:
        payload = json.loads(payload_text)
    except Exception:
        return block + delimiter
    if not isinstance(payload, dict):
        return block + delimiter
    original_payload = copy.deepcopy(payload)
    _normalize_response_function_call_arguments(payload)
    if payload.get("type") == "response.completed":
        _normalize_response_completed_event_usage(
            payload,
            input_token_upper_bound=input_token_upper_bound,
        )
    _image_generation_module._normalize_image_generation_result_status(payload)
    if payload == original_payload:
        return block + delimiter
    normalized_data = json.dumps(payload, ensure_ascii=False)
    next_lines: list[str] = []
    replaced_data = False
    for line in block.splitlines():
        if line.startswith("data:"):
            if not replaced_data:
                next_lines.append(f"data: {normalized_data}")
                replaced_data = True
            continue
        next_lines.append(line)
    return line_separator.join(next_lines) + delimiter


def _normalize_sse_response_completed_text(
    text: str,
    *,
    input_token_upper_bound: Optional[int] = None,
) -> str:
    if (
        "data:" not in text
        or (
            "response.completed" not in text
            and "image_generation_call" not in text
            and "response.output_item.added" not in text
        )
    ):
        return text
    output: list[str] = []
    index = 0
    while index < len(text):
        newline_index = text.find("\n\n", index)
        crlf_index = text.find("\r\n\r\n", index)
        candidates = [
            (position, delimiter)
            for position, delimiter in (
                (newline_index, "\n\n"),
                (crlf_index, "\r\n\r\n"),
            )
            if position >= 0
        ]
        if not candidates:
            output.append(
                _normalize_sse_response_completed_block(
                    text[index:],
                    "",
                    input_token_upper_bound=input_token_upper_bound,
                )
            )
            break
        position, delimiter = min(candidates, key=lambda item: item[0])
        output.append(
            _normalize_sse_response_completed_block(
                text[index:position],
                delimiter,
                input_token_upper_bound=input_token_upper_bound,
            )
        )
        index = position + len(delimiter)
    return "".join(output)


def _normalize_sse_response_completed_chunk(
    chunk: str | bytes,
    *,
    input_token_upper_bound: Optional[int] = None,
) -> str | bytes:
    if isinstance(chunk, bytes):
        try:
            text = chunk.decode("utf-8")
        except Exception:
            return chunk
        normalized_text = _normalize_sse_response_completed_text(
            text,
            input_token_upper_bound=input_token_upper_bound,
        )
        if normalized_text == text:
            return chunk
        return normalized_text.encode("utf-8")
    return _normalize_sse_response_completed_text(
        chunk,
        input_token_upper_bound=input_token_upper_bound,
    )


def _function_call_arguments_valid(value: Any) -> bool:
    normalized, valid = _responses_request_module._codex_normalized_function_arguments(
        value,
        empty_is_object=True,
    )
    return bool(valid and isinstance(normalized, str))


def _payload_invalid_function_call_reason(payload: Any) -> Optional[str]:
    if not isinstance(payload, dict):
        return None
    event_type = payload.get("type")
    if event_type == "response.function_call_arguments.done":
        if not _function_call_arguments_valid(payload.get("arguments")):
            return "function call arguments are not a valid JSON object"
        return None
    if event_type == "response.output_item.done":
        item = payload.get("item")
        if isinstance(item, dict) and item.get("type") == "function_call":
            if not _function_call_arguments_valid(item.get("arguments")):
                return "function call arguments are not a valid JSON object"
        return None
    if _responses_stream_chunk_is_completed(payload):
        response = payload.get("response")
        output = response.get("output") if isinstance(response, dict) else None
        if isinstance(output, list):
            for item in output:
                if (
                    isinstance(item, dict)
                    and item.get("type") == "function_call"
                    and not _function_call_arguments_valid(item.get("arguments"))
                ):
                    return "function call arguments are not a valid JSON object"
    return None


def _responses_stream_chunk_for_delivery(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> Any:
    input_token_upper_bound = _codex_request_input_token_upper_bound(request_data)
    if isinstance(chunk, _JSONStreamEvent):
        _normalize_response_function_call_arguments(chunk)
        invalid_reason = _payload_invalid_function_call_reason(chunk)
        if invalid_reason is not None:
            return _synthesized_failed_response_event(
                request_data or {},
                RuntimeError(invalid_reason),
            )
        _image_generation_module._normalize_image_generation_result_status(chunk)
        _normalize_response_completed_event_usage(
            chunk,
            input_token_upper_bound=input_token_upper_bound,
        )
        return chunk
    if isinstance(chunk, (str, bytes)):
        normalized = _normalize_sse_response_completed_chunk(
            chunk,
            input_token_upper_bound=input_token_upper_bound,
        )
        if _request_is_responses_stream(request_data):
            text = (
                normalized.decode("utf-8", errors="replace")
                if isinstance(normalized, bytes)
                else normalized
            )
            stripped = text.strip()
            if stripped.startswith(":") and not stripped.startswith("data:"):
                return _json_stream_event(
                    {
                        "type": "response.metadata",
                        "metadata": {"phase": "comment"},
                    }
                )
        return normalized
    dumped = _stream_chunk_dump(chunk)
    chunk_type = _stream_chunk_type(dumped)
    if (
        not chunk_type.startswith("response.")
        and chunk_type != "error"
        and not _request_is_responses_stream(request_data)
    ):
        return chunk
    json_chunk = _jsonable(chunk)
    if isinstance(json_chunk, dict):
        _normalize_response_function_call_arguments(json_chunk)
        invalid_reason = _payload_invalid_function_call_reason(json_chunk)
        if invalid_reason is not None:
            return _synthesized_failed_response_event(
                request_data or {},
                RuntimeError(invalid_reason),
            )
        _image_generation_module._normalize_image_generation_result_status(json_chunk)
        _normalize_response_completed_event_usage(
            json_chunk,
            input_token_upper_bound=input_token_upper_bound,
        )
        return _json_stream_event(json_chunk)
    return chunk


def _normalize_response_function_call_added_arguments(payload: Any) -> None:
    if not isinstance(payload, dict) or payload.get("type") != "response.output_item.added":
        return
    item = payload.get("item")
    if not isinstance(item, dict) or item.get("type") != "function_call":
        return
    if item.get("arguments") == "":
        return
    normalized_item = item.copy()
    normalized_item["arguments"] = ""
    payload["item"] = normalized_item


def _normalize_response_function_call_arguments(payload: Any) -> None:
    """Normalize function-call arguments before a Responses event is delivered."""

    if not isinstance(payload, dict):
        return
    _normalize_response_function_call_added_arguments(payload)

    def normalize_item(item: Any) -> Any:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return item
        normalized, valid = _responses_request_module._codex_normalized_function_arguments(
            item.get("arguments"),
            empty_is_object=True,
        )
        if not valid or normalized is None:
            return item
        if item.get("arguments") == normalized:
            return item
        normalized_item = item.copy()
        normalized_item["arguments"] = normalized
        return normalized_item

    event_type = payload.get("type")
    if event_type == "response.output_item.done":
        item = normalize_item(payload.get("item"))
        if item is not payload.get("item"):
            payload["item"] = item
        return

    if event_type == "response.function_call_arguments.done":
        normalized, valid = _responses_request_module._codex_normalized_function_arguments(
            payload.get("arguments"),
            empty_is_object=True,
        )
        if valid and normalized is not None:
            payload["arguments"] = normalized
        return

    if event_type != "response.completed":
        return
    response = payload.get("response")
    if not isinstance(response, dict):
        return
    output = response.get("output")
    if not isinstance(output, list):
        return
    normalized_output = [normalize_item(item) for item in output]
    if normalized_output != output:
        normalized_response = response.copy()
        normalized_response["output"] = normalized_output
        payload["response"] = normalized_response


def _stream_chunk_error_payload(chunk: Any) -> Any:
    sse_payload = _sse_stream_chunk_payload(chunk)
    if sse_payload is not None:
        return _stream_chunk_error_payload(sse_payload)
    if isinstance(chunk, dict):
        error = chunk.get("error")
        if error is not None:
            return error
        response = chunk.get("response")
        if isinstance(response, dict):
            response_error = response.get("error")
            if response_error is not None:
                return response_error
            incomplete_details = response.get("incomplete_details")
            if incomplete_details is not None and (
                response.get("status") in _RESPONSES_STREAM_INCOMPLETE_STATUSES
                or chunk.get("type") in _RESPONSES_STREAM_INCOMPLETE_TYPES
            ):
                return incomplete_details
        chunk_type = chunk.get("type") or chunk.get("event") or chunk.get("object")
        if isinstance(chunk_type, str):
            normalized = chunk_type.strip().lower()
            if normalized == "error" or normalized.endswith(".error"):
                return chunk
        return None
    error = getattr(chunk, "error", None)
    if error is not None:
        return error
    if hasattr(chunk, "model_dump"):
        try:
            dumped = chunk.model_dump()
        except Exception:
            return None
        if isinstance(dumped, dict):
            return _stream_chunk_error_payload(dumped)
    return None


def _stream_chunk_error_exception(chunk: Any) -> Optional[Exception]:
    error = _stream_chunk_error_payload(chunk)
    if error is None:
        return None
    if isinstance(error, Exception):
        return error

    status_code: Optional[int] = None
    if isinstance(error, dict):
        message = _routing_module._first_not_none(
            error.get("message"),
            error.get("detail"),
            error.get("error"),
            error,
        )
        error_type = error.get("type")
        error_code = error.get("code")
        raw_status = _routing_module._first_not_none(error.get("status_code"), error.get("status"))
        if isinstance(raw_status, int):
            status_code = raw_status
        elif isinstance(raw_status, str) and raw_status.strip().isdigit():
            status_code = int(raw_status.strip())
    else:
        message = error
        error_type = None
        error_code = None

    parts = [str(message or "streaming upstream error")]
    if error_type:
        parts.append(f"type={error_type}")
    if error_code:
        parts.append(f"code={error_code}")
    text = " ".join(parts)
    lowered = text.lower()
    if status_code is None and (
        "rate_limit" in lowered
        or "rate limit" in lowered
        or "concurrency" in lowered
    ):
        status_code = 429
    elif status_code is None and (
        error_type == "invalid_request_error"
        or error_code == "invalid_request_error"
        or error_code == "thinking_signature_invalid"
        or "encrypted function output content could not be decrypted or decoded"
        in lowered
    ):
        status_code = 400
    elif status_code is None and error_type == "rate_limit_error":
        status_code = 429
    elif status_code is None and error_type == "overloaded_error":
        status_code = 529
    elif status_code is None and error_type == "api_error":
        status_code = 500
    elif status_code is None and error_code in {
        "upstream_compaction_failure",
        "upstream_route_failure",
    }:
        match = re.search(r"\bhttp\s+([45]\d\d)\b", lowered)
        if match is not None:
            status_code = int(match.group(1))

    exception = RuntimeError(text)
    if status_code is not None:
        try:
            exception.status_code = status_code  # type: ignore[attr-defined]
        except Exception:
            pass
    try:
        exception.body = error  # type: ignore[attr-defined]
    except Exception:
        pass
    return exception


def _stream_chunk_priority_error_exception(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> Optional[Exception]:
    exception = _stream_chunk_error_exception(chunk)
    if exception is None:
        return None
    return (
        exception
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            exception,
            request_data,
        )
        else None
    )


def _is_structured_codex_compaction_failure_chunk(chunk: Any) -> bool:
    """Recognize the terminal failure event produced for a prior compaction attempt."""
    dumped = _stream_chunk_dump(chunk)
    if dumped.get("type") != "response.failed":
        return False
    response = dumped.get("response")
    response = response if isinstance(response, dict) else {}
    error = response.get("error")
    return isinstance(error, dict) and error.get("code") in {
        "upstream_compaction_failure",
        "upstream_compaction_unsupported",
    }


def _stream_chunk_dump(chunk: Any) -> dict[str, Any]:
    if isinstance(chunk, dict):
        return chunk
    sse_payload = _sse_stream_chunk_payload(chunk)
    if sse_payload is not None:
        return sse_payload
    if hasattr(chunk, "model_dump"):
        try:
            dumped = chunk.model_dump()
        except Exception:
            return {}
        return dumped if isinstance(dumped, dict) else {}
    return {}


def _sse_stream_chunk_payload(chunk: Any) -> Optional[dict[str, Any]]:
    """Decode a structured wire chunk for local classification, never delivery."""

    if isinstance(chunk, bytes):
        try:
            text = chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
    elif isinstance(chunk, str):
        text = chunk
    else:
        return None
    event_type: Optional[str] = None
    data_lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("event:"):
            value = line.split(":", 1)[1].strip()
            event_type = value or None
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    payload_text = "\n".join(data_lines) if data_lines else text.strip()
    if not data_lines and not payload_text.startswith("{"):
        return None
    try:
        payload = json.loads(payload_text)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict):
        return None
    if not data_lines:
        chunk_type = payload.get("type")
        if not isinstance(chunk_type, str) or not (
            chunk_type == "error" or chunk_type.startswith("response.")
        ):
            return None
    if event_type and not isinstance(payload.get("type"), str):
        payload = payload.copy()
        payload["type"] = event_type
    return payload


def _stream_chunk_type(chunk: Any) -> str:
    dumped = _stream_chunk_dump(chunk)
    chunk_type = dumped.get("type")
    return chunk_type if isinstance(chunk_type, str) else ""


def _stream_chunk_response_status(chunk: Any) -> str:
    dumped = _stream_chunk_dump(chunk)
    response = dumped.get("response")
    if isinstance(response, dict):
        status = response.get("status")
        if isinstance(status, str):
            return status
    status = dumped.get("status")
    return status if isinstance(status, str) else ""


def _responses_stream_chunk_is_completed(chunk: Any) -> bool:
    return _stream_chunk_type(chunk) in _RESPONSES_STREAM_COMPLETED_TYPES or (
        _stream_chunk_type(chunk) == "response"
        and _stream_chunk_response_status(chunk).lower() == "completed"
    )


def _responses_completed_chunk_has_usable_output(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    if not _responses_stream_chunk_is_completed(chunk):
        return False
    dumped = _stream_chunk_dump(chunk)
    response = dumped.get("response")
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        return isinstance(response, dict) and _codex_compaction_output_is_valid(
            response.get("output")
        )
    if isinstance(response, dict):
        return bool(
            _stream_chunk_has_visible_output(response)
            or _responses_output_module._payload_has_tool_activity(response)
        )
    return _stream_chunk_has_visible_output(dumped)


def _responses_completed_chunk_is_empty(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    return _responses_stream_chunk_is_completed(
        chunk
    ) and not _responses_completed_chunk_has_usable_output(chunk, request_data)


def _responses_stream_chunk_is_incomplete_terminal(chunk: Any) -> bool:
    return _stream_chunk_type(chunk) in _RESPONSES_STREAM_INCOMPLETE_TYPES or (
        _stream_chunk_response_status(chunk).lower() in _RESPONSES_STREAM_INCOMPLETE_STATUSES
    )


_OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS = {
    "length",
    "max_completion_tokens",
    "max_output_tokens",
    "max_tokens",
}


def _output_limit_incomplete_reason_from_text(value: str) -> Optional[str]:
    lowered = value.strip().lower()
    if not lowered:
        return None
    for reason in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS:
        if reason in lowered:
            return reason
    return None


def _responses_incomplete_terminal_reason(chunk: Any) -> str:
    event = _jsonable(chunk)
    if not isinstance(event, dict):
        event = _stream_chunk_dump(chunk)
    candidates: list[Any] = []
    response = event.get("response") if isinstance(event, dict) else None
    if isinstance(response, dict):
        candidates.extend(
            [
                response.get("incomplete_details"),
                response.get("error"),
            ]
        )
    if isinstance(event, dict):
        candidates.extend(
            [
                event.get("incomplete_details"),
                event.get("error"),
            ]
        )
    for candidate in candidates:
        if isinstance(candidate, dict):
            for key in ("reason", "code", "type"):
                value = candidate.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip().lower()
            for key in ("message", "detail"):
                value = candidate.get(key)
                if isinstance(value, str):
                    reason = _output_limit_incomplete_reason_from_text(value)
                    if reason is not None:
                        return reason
        elif isinstance(candidate, str) and candidate.strip():
            reason = _output_limit_incomplete_reason_from_text(candidate)
            return reason or candidate.strip().lower()
    return ""


def _complete_incomplete_response_statuses(value: Any) -> None:
    if isinstance(value, dict):
        if value.get("status") == "incomplete":
            value["status"] = "completed"
        for nested in value.values():
            _complete_incomplete_response_statuses(nested)
    elif isinstance(value, list):
        for nested in value:
            _complete_incomplete_response_statuses(nested)


def _responses_output_limit_incomplete_as_completed_chunk(
    chunk: Any,
    request_data: Optional[dict],
) -> Optional[_JSONStreamEvent]:
    if not _responses_stream_chunk_is_incomplete_terminal(chunk):
        return None
    reason = _responses_incomplete_terminal_reason(chunk)
    if reason not in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS:
        return None
    event = _jsonable(chunk)
    if not isinstance(event, dict):
        return None
    response = event.get("response")
    if not isinstance(response, dict):
        return None
    if _responses_output_module._response_is_effectively_empty(response):
        return None

    completed_event = copy.deepcopy(event)
    completed_response = completed_event.get("response")
    if not isinstance(completed_response, dict):
        return None
    completed_event["type"] = "response.completed"
    completed_response["status"] = "completed"
    completed_response.pop("incomplete_details", None)
    _complete_incomplete_response_statuses(completed_response.get("output"))
    _normalize_response_completed_event_usage(completed_event)
    _trace_module._route_trace(
        "responses_stream_output_limit_incomplete_completed_compat",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        reason=reason,
        response=_trace_module._trace_response_summary(completed_response, request_data),
    )
    return _json_stream_event(completed_event)


def _responses_output_limit_terminal_state_as_completed_chunk(
    chunk: Any,
    completion_state: Any,
    request_data: Optional[dict],
) -> Optional[_JSONStreamEvent]:
    if not _responses_stream_chunk_is_incomplete_terminal(chunk):
        return None
    reason = _responses_incomplete_terminal_reason(chunk)
    if reason not in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS:
        return None
    completed_response = completion_state.completed_payload(request_data)
    if _responses_output_module._response_is_effectively_empty(completed_response):
        return None
    completed_event = {"type": "response.completed", "response": completed_response}
    _normalize_response_completed_event_usage(completed_event)
    _trace_module._route_trace(
        "responses_stream_output_limit_state_completed_compat",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        reason=reason,
        response=_trace_module._trace_response_summary(completed_response, request_data),
    )
    return _json_stream_event(completed_event)


_STREAM_OUTPUT_TOOL_ITEM_TYPES = {"function_call", "tool_search_call", "web_search_call"}


def _stream_output_item_is_tool_call(item_type: Any) -> bool:
    if not isinstance(item_type, str) or not item_type:
        return False
    if item_type in _STREAM_OUTPUT_TOOL_ITEM_TYPES:
        return True
    if item_type in {"custom_tool_call", "tool_call", "computer_call", "image_generation_call"}:
        return True
    return item_type.endswith("_call") or "tool_call" in item_type


def _stream_output_item_key(item: Any) -> Optional[str]:
    item_id = _responses_web_search_bridge_module._response_item_get(item, "id")
    if not isinstance(item_id, str) or not item_id:
        item_id = _responses_web_search_bridge_module._response_item_get(item, "call_id")
    return item_id if isinstance(item_id, str) and item_id else None


def _stream_output_item_identity_keys(item: Any) -> list[str]:
    keys: list[str] = []
    item_type = _responses_web_search_bridge_module._response_item_get(item, "type")
    for key_name in ("id", "call_id"):
        if key_name == "call_id" and not _stream_output_item_is_tool_call(item_type):
            continue
        value = _responses_web_search_bridge_module._response_item_get(item, key_name)
        if isinstance(value, str) and value and value not in keys:
            keys.append(value)
    return keys


def _mark_stream_output_item_identity_seen(item: Any, seen_item_ids: set[str]) -> None:
    for item_id in _stream_output_item_identity_keys(item):
        seen_item_ids.add(item_id)


def _pending_tool_item_key_for_item(
    pending_tool_items: dict[str, tuple[int, dict[str, Any]]],
    item: Any,
) -> Optional[str]:
    for item_id in _stream_output_item_identity_keys(item):
        if item_id in pending_tool_items:
            return item_id
    item_call_id = _responses_web_search_bridge_module._response_item_get(item, "call_id")
    if isinstance(item_call_id, str) and item_call_id:
        for pending_key, (_index, pending_item) in pending_tool_items.items():
            pending_call_id = _responses_web_search_bridge_module._response_item_get(
                pending_item,
                "call_id",
            )
            if pending_call_id == item_call_id:
                return pending_key
    return None


def _pop_pending_tool_item_for_item(
    pending_tool_items: dict[str, tuple[int, dict[str, Any]]],
    item: Any,
) -> Optional[tuple[int, dict[str, Any]]]:
    pending_key = _pending_tool_item_key_for_item(pending_tool_items, item)
    if pending_key is None:
        return None
    return pending_tool_items.pop(pending_key, None)


def _remember_stream_output_item_ids(
    chunk: Any,
    seen_item_ids: set[str],
    pending_tool_items: Optional[dict[str, tuple[int, dict[str, Any]]]] = None,
    completed_output_items: Optional[dict[int, dict[str, Any]]] = None,
) -> None:
    dumped = _stream_chunk_dump(chunk)
    chunk_type = _stream_chunk_type(dumped)
    if chunk_type not in {"response.output_item.added", "response.output_item.done"}:
        return
    item = dumped.get("item")
    item_id = _stream_output_item_key(item)
    item_type = _responses_web_search_bridge_module._response_item_get(item, "type")
    if item_type == "web_search_call":
        json_item = _jsonable(item)
        if not isinstance(json_item, dict):
            return
        sanitized_item = _responses_web_search_bridge_module._sanitize_web_search_call_item(json_item)
        if sanitized_item is None:
            return
        item = sanitized_item
        item_id = _stream_output_item_key(item)
    output_index = dumped.get("output_index")
    if not isinstance(output_index, int):
        output_index = len(completed_output_items or {})

    if (
        chunk_type == "response.output_item.added"
        and pending_tool_items is not None
        and _stream_output_item_is_tool_call(item_type)
        and item_id
    ):
        json_item = _jsonable(item)
        if isinstance(json_item, dict):
            pending_tool_items[item_id] = (output_index, json_item)
        return

    if chunk_type != "response.output_item.done":
        return

    if item_id:
        _mark_stream_output_item_identity_seen(item, seen_item_ids)
        if pending_tool_items is not None:
            _pop_pending_tool_item_for_item(pending_tool_items, item)
    if completed_output_items is not None:
        json_item = _jsonable(item)
        if isinstance(json_item, dict):
            completed_output_items[output_index] = json_item


def _remember_completed_response_output_items(
    chunk: Any,
    completed_output_items: dict[int, dict[str, Any]],
    pending_tool_items: Optional[dict[str, tuple[int, dict[str, Any]]]] = None,
    seen_item_ids: Optional[set[str]] = None,
) -> None:
    for output_index, item in _completed_response_output_items(chunk):
        json_item = _jsonable(item)
        if not isinstance(json_item, dict):
            continue
        item_type = _responses_web_search_bridge_module._response_item_get(
            json_item,
            "type",
        )
        if item_type == "web_search_call":
            sanitized_item = _responses_web_search_bridge_module._sanitize_web_search_call_item(
                json_item,
            )
            if sanitized_item is None:
                continue
            json_item = sanitized_item
        completed_output_items[output_index] = json_item
        item_id = _stream_output_item_key(json_item)
        if item_id:
            if seen_item_ids is not None:
                _mark_stream_output_item_identity_seen(json_item, seen_item_ids)
            if pending_tool_items is not None:
                _pop_pending_tool_item_for_item(pending_tool_items, json_item)


def _completed_response_output_items(chunk: Any) -> list[tuple[int, Any]]:
    if not _responses_stream_chunk_is_completed(chunk):
        return []
    dumped = _stream_chunk_dump(chunk)
    response = dumped.get("response")
    if not isinstance(response, dict):
        return []
    output = response.get("output")
    if not isinstance(output, list):
        return []
    return list(enumerate(output))


def _json_stream_event(event: dict[str, Any]) -> _JSONStreamEvent:
    return _JSONStreamEvent(event)


def _sse_comment_event(text: str) -> str:
    safe = str(text).replace("\r", " ").replace("\n", " ")
    return f": {safe}\n\n"


def _synthesized_missing_completed_tool_events(
    chunk: Any,
    seen_item_ids: set[str],
    pending_tool_items: Optional[dict[str, tuple[int, dict[str, Any]]]] = None,
) -> list[_JSONStreamEvent]:
    dumped = _stream_chunk_dump(chunk)
    model = dumped.get("model")
    events: list[_JSONStreamEvent] = []
    for output_index, item in _completed_response_output_items(chunk):
        item_type = _responses_web_search_bridge_module._response_item_get(item, "type")
        if not _stream_output_item_is_tool_call(item_type):
            continue
        if any(identity_key in seen_item_ids for identity_key in _stream_output_item_identity_keys(item)):
            continue

        done_item = _jsonable(item)
        if not isinstance(done_item, dict):
            continue
        if done_item.get("type") == "web_search_call":
            sanitized_item = _responses_web_search_bridge_module._sanitize_web_search_call_item(done_item)
            if sanitized_item is None:
                continue
            done_item = sanitized_item
        added_item = done_item.copy()
        if added_item.get("status") == "completed":
            added_item["status"] = "in_progress"

        added_event: dict[str, Any] = {
            "type": "response.output_item.added",
            "output_index": output_index,
            "item": added_item,
        }
        done_event: dict[str, Any] = {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": done_item,
        }
        if model is not None:
            done_event["model"] = model
        pending_item = (
            _pop_pending_tool_item_for_item(pending_tool_items, item)
            if pending_tool_items is not None
            else None
        )
        if pending_item is not None:
            _pending_output_index, pending_done_item = pending_item
            if (
                not isinstance(done_item.get("id"), str)
                and isinstance(pending_done_item.get("id"), str)
            ):
                done_item["id"] = pending_done_item["id"]
            events.append(_json_stream_event(done_event))
        else:
            if model is not None:
                added_event["model"] = model
            events.extend([_json_stream_event(added_event), _json_stream_event(done_event)])
        _mark_stream_output_item_identity_seen(done_item, seen_item_ids)
    return events


def _synthesized_pending_tool_completion_events(
    pending_tool_items: dict[str, tuple[int, dict[str, Any]]],
    completed_output_items: dict[int, dict[str, Any]],
    created_response: Optional[dict[str, Any]],
    model: Optional[str],
) -> list[_JSONStreamEvent]:
    if not pending_tool_items:
        return []

    events: list[_JSONStreamEvent] = []
    for item_id, (output_index, item) in sorted(
        pending_tool_items.items(),
        key=lambda entry: entry[1][0],
    ):
        done_item = copy.deepcopy(item)
        done_item["status"] = "completed"
        if done_item.get("type") == "web_search_call":
            sanitized_item = _responses_web_search_bridge_module._sanitize_web_search_call_item(done_item)
            if sanitized_item is None:
                continue
            done_item = sanitized_item
        done_event: dict[str, Any] = {
            "type": "response.output_item.done",
            "output_index": output_index,
            "item": done_item,
        }
        if model:
            done_event["model"] = model
        events.append(_json_stream_event(done_event))
        completed_output_items[output_index] = done_item

    response = copy.deepcopy(created_response) if isinstance(created_response, dict) else {}
    response.setdefault("id", "resp_synthesized")
    response.setdefault("object", "response")
    response["status"] = "completed"
    response["output"] = [
        item for _, item in sorted(completed_output_items.items(), key=lambda entry: entry[0])
    ]
    response = _responses_web_search_bridge_module._sanitize_response_web_search_call_items(response)
    completed_event: dict[str, Any] = {
        "type": "response.completed",
        "response": response,
    }
    if model:
        completed_event["model"] = model
    events.append(_json_stream_event(completed_event))
    pending_tool_items.clear()
    return events


def _synthesized_completed_response_event(
    completed_output_items: dict[int, dict[str, Any]],
    created_response: Optional[dict[str, Any]],
    model: Optional[str],
    *,
    fallback_text: Optional[str] = None,
) -> Optional[_JSONStreamEvent]:
    text = fallback_text if isinstance(fallback_text, str) else ""
    if not completed_output_items and not text.strip():
        return None
    response = copy.deepcopy(created_response) if isinstance(created_response, dict) else {}
    response.setdefault("id", "resp_synthesized")
    response.setdefault("object", "response")
    if completed_output_items:
        response["output"] = [
            item for _, item in sorted(completed_output_items.items(), key=lambda entry: entry[0])
        ]
    else:
        response["output"] = [
            {
                "id": f"msg_synthesized_{os.getpid()}_{time.time_ns()}",
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {
                        "type": "output_text",
                        "text": text,
                        "annotations": [],
                    }
                ],
            }
        ]
        response["output_text"] = text
    response["status"] = "completed"
    _image_generation_module._normalize_image_generation_result_status(response)
    response = _responses_web_search_bridge_module._sanitize_response_web_search_call_items(response)
    completed_event: dict[str, Any] = {
        "type": "response.completed",
        "response": response,
    }
    if model:
        completed_event["model"] = model
    return _json_stream_event(completed_event)


def _stream_chunk_text_fragment(chunk: Any) -> tuple[str, bool]:
    dumped = _stream_chunk_dump(chunk)
    chunk_type = _stream_chunk_type(dumped)
    if chunk_type == "response.output_text.delta":
        delta = dumped.get("delta")
        return (delta, False) if isinstance(delta, str) else ("", False)
    if chunk_type == "response.output_text.done":
        text = dumped.get("text")
        return (text, True) if isinstance(text, str) else ("", True)
    return "", False


def _synthesized_failed_response_event(
    request_data: dict,
    exception: Exception,
) -> _JSONStreamEvent:
    model = _responses_execution_module._request_model_group(request_data) or request_data.get("model") or "unknown"
    is_structured_compaction = (
        _responses_request_module._request_has_structured_codex_compaction(
            request_data
        )
    )
    if is_structured_compaction:
        status_code = _routing_module._exception_status_code(exception)
        status_suffix = f" (HTTP {status_code})" if status_code is not None else ""
        if _routing_module._is_codex_compaction_capability_unsupported_error(exception):
            message = str(exception) or (
                "The selected upstream route does not support encrypted Responses "
                "compaction. Codex should use its local context-checkpoint summary fallback."
            )
            error_type = "invalid_request_error"
            error_code = "upstream_compaction_unsupported"
        else:
            message = (
                "The upstream model route failed the structured context compaction "
                f"request{status_suffix}."
            )
            error_type = (
                "invalid_request_error"
                if _routing_module._is_terminal_responses_stream_failure_error(exception)
                else "server_error"
            )
            error_code = "upstream_compaction_failure"
    elif _routing_module._is_image_generation_all_deployments_unsupported_error(
        exception
    ):
        message = (
            "All configured deployments for this model rejected the "
            "image_generation tool."
        )
        error_type = "invalid_request_error"
        error_code = "image_generation_tool_unavailable"
    else:
        message = "The upstream model route failed before a final assistant response was available."
        error_type = "server_error"
        error_code = "upstream_route_failure"
    response = {
        "id": f"resp_failed_{os.getpid()}_{time.time_ns()}",
        "object": "response",
        "created_at": int(time.time()),
        "model": model,
        "status": "failed",
        "output": [],
        "error": {
            "type": error_type,
            "code": error_code,
            "message": message,
        },
    }
    event: dict[str, Any] = {
        "type": "response.failed",
        "response": response,
    }
    if isinstance(model, str):
        event["model"] = model
    return _json_stream_event(event)


def _synthesized_native_stream_error_chunk(
    request_data: dict,
    exception: Exception,
) -> Any:
    """Return a terminal error in the client's native streaming format."""

    message = "The upstream model route failed before a final assistant response was available."
    if _request_is_anthropic_messages_stream(request_data):
        payload = {
            "type": "error",
            "error": {
                "type": "api_error",
                "message": message,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: error\ndata: {encoded}\n\n".encode("utf-8")
    return {
        "error": {
            "type": "server_error",
            "code": "upstream_route_failure",
            "message": message,
        }
    }


def _external_web_search_missing_answer_failed_event(
    request_data: dict,
    exception: Exception,
) -> _JSONStreamEvent:
    return _synthesized_failed_response_event(request_data, exception)


def _responses_incomplete_stream_exception(
    reason: str,
    *,
    buffer: Optional[List[Any]] = None,
    request_data: Optional[dict] = None,
) -> Exception:
    terminal_chunk = buffer[-1] if buffer else None
    terminal_summary = _responses_incomplete_terminal_summary(terminal_chunk)
    terminal_exception = _stream_chunk_error_exception(terminal_chunk)
    terminal_error_text = " ".join(
        str(terminal_summary.get(key) or "").strip()
        for key in ("error_code", "error_type", "error_message", "error_detail")
        if terminal_summary.get(key)
    ).strip()
    message = f"Responses stream incomplete: {reason}"
    if terminal_error_text:
        message = f"{message}; upstream terminal error: {terminal_error_text}"
    candidate = RuntimeError(message)
    exception = candidate
    try:
        exception.responses_stream_incomplete = True  # type: ignore[attr-defined]
        # Routing and cooldown must treat an incomplete stream the same way
        # regardless of the client-facing protocol.  Preserve the Responses
        # marker above for its event-specific compatibility paths.
        exception.stream_incomplete = True  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        terminal_status = (
            _routing_module._exception_status_code(terminal_exception)
            if terminal_exception is not None
            else None
        )
        if terminal_status is not None:
            exception.status_code = terminal_status  # type: ignore[attr-defined]
        elif _routing_module._is_context_size_error(exception):
            exception.status_code = 400  # type: ignore[attr-defined]
    except Exception:
        pass
    if buffer is not None:
        try:
            exception.body = {  # type: ignore[attr-defined]
                "reason": reason,
                "buffered_event_types": [_stream_chunk_type(chunk) for chunk in buffer],
                "buffered_has_visible_output": any(
                    _stream_chunk_has_visible_output(chunk) for chunk in buffer
                ),
                "terminal_event": terminal_summary,
            }
        except Exception:
            pass
    return exception


def _responses_incomplete_terminal_summary(chunk: Any) -> dict[str, Any]:
    dumped = _stream_chunk_dump(chunk)
    response = dumped.get("response") if isinstance(dumped, dict) else None
    response = response if isinstance(response, dict) else {}
    error = response.get("error")
    if not isinstance(error, dict) and isinstance(dumped, dict):
        top_level_error = dumped.get("error")
        if isinstance(top_level_error, dict):
            error = top_level_error
        elif dumped.get("type") == "error":
            error = dumped
    error = error if isinstance(error, dict) else {}
    incomplete_details = response.get("incomplete_details")
    incomplete_details = (
        incomplete_details if isinstance(incomplete_details, dict) else {}
    )
    output = response.get("output")
    text_fragment, _is_done = _stream_chunk_text_fragment(dumped)
    return {
        "type": _stream_chunk_type(dumped),
        "status": _stream_chunk_response_status(dumped),
        "reason": _responses_incomplete_terminal_reason(dumped),
        "has_response_output": isinstance(output, list) and bool(output),
        "has_text_fragment": bool(text_fragment.strip()),
        "error_type": error.get("type") if isinstance(error.get("type"), str) else None,
        "error_code": error.get("code") if isinstance(error.get("code"), str) else None,
        "error_message": (
            _trace_module._sanitize_trace_text(error.get("message"), limit=800)
            if isinstance(error.get("message"), str)
            else None
        ),
        "error_detail": (
            _trace_module._sanitize_trace_text(
                error.get("detail") or incomplete_details.get("detail"),
                limit=800,
            )
            if isinstance(
                error.get("detail") or incomplete_details.get("detail"),
                str,
            )
            else None
        ),
    }


def _is_responses_incomplete_stream_error(exception: Exception) -> bool:
    if getattr(exception, "stream_incomplete", False):
        return True
    return "responses stream incomplete" in _routing_module._exception_text(exception)


def _response_has_tool_call_activity(response: Any) -> bool:
    def walk(value: Any) -> bool:
        if value is None:
            return False
        if isinstance(value, list):
            return any(walk(item) for item in value)
        if isinstance(value, dict):
            item_type = value.get("type")
            if _stream_output_item_is_tool_call(item_type):
                return True
            for key in ("tool_calls", "function_call"):
                item = value.get(key)
                if item:
                    return True
            return any(walk(item) for item in value.values())
        if hasattr(value, "model_dump"):
            try:
                return walk(value.model_dump())
            except Exception:
                return False
        return False

    return walk(response)


def _stream_chunk_has_visible_text_output(chunk: Any) -> bool:
    if not _responses_output_module._response_text(chunk):
        return False
    types = [item_type.lower() for item_type in _responses_output_module._response_types(chunk)]
    if not any("reasoning" in item_type for item_type in types):
        return True
    return any("output_text" in item_type for item_type in types)


def _stream_chunk_has_visible_output(chunk: Any) -> bool:
    dumped = _stream_chunk_dump(chunk)
    visible_chunk = dumped if dumped else chunk
    return bool(
        _stream_chunk_has_visible_text_output(visible_chunk)
        or _image_generation_module._response_has_image_generation_activity(visible_chunk)
        or _response_has_tool_call_activity(visible_chunk)
    )


def _response_item_is_web_search_call(item: Any) -> bool:
    json_item = _jsonable(item)
    if not isinstance(json_item, dict):
        return False
    if json_item.get("type") == "web_search_call":
        return True
    return _responses_web_search_bridge_module._is_web_search_function_call_item(json_item)


def _response_item_is_codex_compaction(item: Any) -> bool:
    """Recognize both canonical and server-reference compaction item names."""
    json_item = _jsonable(item)
    if not isinstance(json_item, dict):
        return False
    item_type = _responses_web_search_bridge_module._response_item_get(
        json_item,
        "type",
    )
    return item_type in {"compaction", "compaction_summary"}


def _response_item_has_encrypted_codex_compaction(item: Any) -> bool:
    if not _response_item_is_codex_compaction(item):
        return False
    json_item = _jsonable(item)
    if not isinstance(json_item, dict):
        return False
    encrypted_content = _responses_web_search_bridge_module._response_item_get(
        json_item,
        "encrypted_content",
    )
    return isinstance(encrypted_content, str) and bool(encrypted_content)


def _codex_compaction_output_is_valid(output: Any) -> bool:
    """Return whether output has exactly one encrypted item Codex can resume from."""
    if not isinstance(output, list):
        return False
    compaction_items = [
        item for item in output if _response_item_is_codex_compaction(item)
    ]
    return len(compaction_items) == 1 and _response_item_has_encrypted_codex_compaction(
        compaction_items[0]
    )


def _response_item_has_assistant_text(item: Any) -> bool:
    json_item = _jsonable(item)
    if not isinstance(json_item, dict):
        return False
    item_type = _responses_web_search_bridge_module._response_item_get(json_item, "type")
    if item_type == "reasoning" or _stream_output_item_is_tool_call(item_type):
        return False
    return bool(_responses_output_module._response_text(json_item).strip())


def _responses_completed_chunk_is_web_search_only(chunk: Any) -> bool:
    if not _responses_stream_chunk_is_completed(chunk):
        return False
    dumped = _stream_chunk_dump(chunk)
    response = dumped.get("response")
    if not isinstance(response, dict):
        return False
    output = response.get("output")
    if not isinstance(output, list) or not output:
        return False
    return all(_response_item_is_web_search_call(item) for item in output)


def _responses_completed_chunk_has_route_recovery_output(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    if not _responses_stream_chunk_is_completed(chunk):
        return False
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        dumped = _stream_chunk_dump(chunk)
        response = dumped.get("response")
        return isinstance(response, dict) and _codex_compaction_output_is_valid(
            response.get("output")
        )
    dumped = _stream_chunk_dump(chunk)
    response = dumped.get("response")
    if not isinstance(response, dict):
        return _stream_chunk_has_visible_output(dumped)
    output = response.get("output")
    if isinstance(output, list) and output:
        if any(_response_item_has_encrypted_codex_compaction(item) for item in output):
            return True
        if any(_response_item_has_assistant_text(item) for item in output):
            return True
        if all(_response_item_is_web_search_call(item) for item in output):
            return False
    return _responses_completed_chunk_has_usable_output(chunk, request_data)


def _stream_chunk_has_deliverable_route_recovery_output(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    dumped = _stream_chunk_dump(chunk)
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        if _responses_stream_chunk_is_completed(dumped):
            return _responses_completed_chunk_has_route_recovery_output(
                dumped,
                request_data,
            )
        return False
    if _stream_chunk_type(dumped) in {"response.output_item.added", "response.output_item.done"}:
        item = dumped.get("item")
        if _response_item_is_web_search_call(item):
            return False
        return _stream_chunk_has_visible_output(chunk)
    if _stream_chunk_type(dumped) == "response.web_search_call.completed":
        return False
    if _responses_completed_chunk_is_web_search_only(dumped):
        return False
    if _stream_chunk_has_visible_output(chunk):
        return True
    if _responses_completed_chunk_has_route_recovery_output(dumped, request_data):
        return True
    if _responses_stream_chunk_is_incomplete_terminal(dumped):
        return True
    return False


def _stream_chunk_is_assistant_text_without_tool_activity(chunk: Any) -> bool:
    dumped = _stream_chunk_dump(chunk)
    chunk_type = _stream_chunk_type(dumped)
    if chunk_type in {
        "response.output_text.delta",
        "response.output_text.done",
        "response.content_part.added",
        "response.content_part.done",
    }:
        return bool(_stream_chunk_has_visible_text_output(dumped))
    if chunk_type in {"response.output_item.added", "response.output_item.done"}:
        item = dumped.get("item")
        item_type = _responses_web_search_bridge_module._response_item_get(item, "type")
        if _stream_output_item_is_tool_call(item_type):
            return False
        return bool(_responses_output_module._response_text(item).strip())
    return False


def _stream_start_timeout_exception(
    request_data: dict,
    *,
    start_seconds: float,
    saw_chunk: bool,
    buffered_chunks: int,
) -> Exception:
    exception = TimeoutError(
        f"LiteLLM Menu stream start timeout after {start_seconds:g}s without the first stream event"
    )
    try:
        exception.status_code = 504  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        exception.body = {  # type: ignore[attr-defined]
            "reason": "stream_start_timeout",
            "start_seconds": start_seconds,
            "saw_chunk": saw_chunk,
            "buffered_chunks": buffered_chunks,
        }
    except Exception:
        pass
    _routing_module._mark_exception_for_deployment_failover(exception, request_data)
    _state_module._append_recent_request(
        _routing_module._request_log_record(
            "stuck",
            {
                **(request_data or {}),
                "exception": exception,
                "stuck_reason": "stream_start_timeout",
                "stream_start_timeout_seconds": start_seconds,
                "stream_saw_chunk": saw_chunk,
                "stream_buffered_chunks": buffered_chunks,
            },
            exception,
            None,
            datetime.now(timezone.utc),
        )
    )
    _trace_module._route_trace(
        "stream_start_timeout",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        start_seconds=start_seconds,
        saw_chunk=saw_chunk,
        buffered_chunks=buffered_chunks,
        request=_trace_module._trace_request_summary(request_data),
        exception=_routing_module._trace_exception(exception),
    )
    return exception

def _stream_idle_timeout_exception(
    request_data: dict,
    *,
    idle_seconds: float,
    saw_chunk: bool,
) -> Exception:
    exception = TimeoutError(
        f"LiteLLM Menu stream idle timeout after {idle_seconds:g}s without a new chunk"
    )
    try:
        exception.status_code = 504  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        exception.body = {  # type: ignore[attr-defined]
            "reason": "stream_idle_timeout",
            "idle_seconds": idle_seconds,
            "saw_chunk": saw_chunk,
        }
    except Exception:
        pass
    _routing_module._mark_exception_for_deployment_failover(exception, request_data)
    _state_module._append_recent_request(
        _routing_module._request_log_record(
            "stuck",
            {
                **(request_data or {}),
                "exception": exception,
                "stuck_reason": "stream_idle_timeout",
                "stream_idle_timeout_seconds": idle_seconds,
                "stream_saw_chunk": saw_chunk,
            },
            exception,
            None,
            datetime.now(timezone.utc),
        )
    )
    _trace_module._route_trace(
        "stream_idle_timeout",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        idle_seconds=idle_seconds,
        saw_chunk=saw_chunk,
        request=_trace_module._trace_request_summary(request_data),
        exception=_routing_module._trace_exception(exception),
    )
    return exception


def _stream_route_recovery_wait_timeout_exception(
    request_data: dict,
    *,
    timeout_seconds: float,
    buffered_chunks: int,
    selected_deployment_box: Optional[dict[str, Any]] = None,
) -> Exception:
    _routing_module._apply_current_selected_deployment_to_request(
        request_data,
        selected_box=selected_deployment_box,
    )
    if buffered_chunks > 0:
        return _stream_idle_timeout_exception(
            request_data,
            idle_seconds=timeout_seconds,
            saw_chunk=True,
        )
    return _stream_start_timeout_exception(
        request_data,
        start_seconds=timeout_seconds,
        saw_chunk=False,
        buffered_chunks=0,
    )


def _route_recovery_terminal_chunk_exception(
    chunk: Any,
    request_data: dict,
    selected_deployment_box: Optional[dict[str, Any]],
    *,
    buffer: Optional[List[Any]] = None,
) -> Optional[Exception]:
    dumped = _stream_chunk_dump(chunk)
    chunk_type = _stream_chunk_type(dumped)
    if chunk_type == "response.failed":
        exception = _responses_incomplete_stream_exception(
            "response.failed before response.completed",
            buffer=buffer or [chunk],
            request_data=request_data,
        )
    elif _responses_stream_chunk_is_incomplete_terminal(dumped):
        exception = _responses_incomplete_stream_exception(
            "terminal response event before response.completed",
            buffer=buffer,
            request_data=request_data,
        )
    else:
        return None
    # The active request still names the route that started the recovery poll.
    # Attribute a terminal event to the deployment that actually produced the
    # stream instead.  Otherwise a same-order peer which ends early is never
    # excluded: every poll reselects it and a Codex compaction can remain in
    # progress forever.
    failure_request = request_data.copy()
    _routing_module._apply_current_selected_deployment_to_request(
        failure_request,
        selected_box=selected_deployment_box,
    )
    _routing_module._mark_exception_for_deployment_failover(
        exception,
        failure_request,
    )
    return exception


async def _stream_with_idle_timeout(
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float] = None,
    saw_visible_output: bool = False,
    initial_chunk_count: int = 0,
) -> AsyncIterator[Any]:
    idle_timeout_seconds = _routing_module._stream_idle_timeout_seconds_for_request(
        request_data
    )
    start_timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(
        request_data
    )
    iterator = response.__aiter__()
    chunk_count = max(0, initial_chunk_count)
    saw_chunk = chunk_count > 0
    visible_output_seen = saw_visible_output
    idle_deadline = (
        time.monotonic() + idle_timeout_seconds
        if saw_chunk and idle_timeout_seconds > 0
        else None
    )
    while True:
        if saw_chunk:
            timeout_seconds = idle_timeout_seconds
            effective_timeout: Optional[float] = (
                max(0.0, idle_deadline - time.monotonic())
                if idle_deadline is not None
                else None
            )
        else:
            timeout_seconds = start_timeout_seconds
            effective_timeout = timeout_seconds if timeout_seconds > 0 else None
        try:
            if effective_timeout is not None:
                chunk = await asyncio.wait_for(iterator.__anext__(), timeout=effective_timeout)
            else:
                chunk = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except asyncio.TimeoutError:
            if not saw_chunk:
                raise _stream_start_timeout_exception(
                    request_data,
                    start_seconds=timeout_seconds,
                    saw_chunk=False,
                    buffered_chunks=chunk_count,
                ) from None
            raise _stream_idle_timeout_exception(
                request_data,
                idle_seconds=timeout_seconds,
                saw_chunk=saw_chunk,
            ) from None
        was_saw_chunk = saw_chunk
        saw_chunk = True
        chunk_count += 1
        if not was_saw_chunk:
            idle_deadline = (
                time.monotonic() + idle_timeout_seconds
                if idle_timeout_seconds > 0
                else None
            )
        elif _stream_chunk_has_meaningful_delta(chunk):
            idle_deadline = (
                time.monotonic() + idle_timeout_seconds
                if idle_timeout_seconds > 0
                else None
            )
        if (
            _routing_module._FIRST_STREAM_OUTPUT_TIME_KEY not in request_data
            and _stream_chunk_has_meaningful_delta(chunk)
        ):
            observed_at = datetime.now(timezone.utc)
            request_data[_routing_module._FIRST_STREAM_OUTPUT_TIME_KEY] = observed_at
            _routing_module._record_first_stream_output_time(
                request_data,
                observed_at,
            )
        if not visible_output_seen:
            visible_output_seen = _stream_chunk_has_visible_output(chunk) or (
                _request_is_responses_stream(request_data)
                and _responses_completed_chunk_has_usable_output(chunk, request_data)
            )
        yield chunk


def _should_emit_codex_responses_initial_sse_keepalive(
    request_data: Optional[dict],
) -> bool:
    return bool(
        isinstance(request_data, dict)
        and request_data.get("stream") is True
        and _responses_request_module._request_is_responses_api(request_data)
        and _responses_request_module._request_has_codex_client_evidence(request_data)
        and not _responses_request_module._request_has_structured_codex_compaction(
            request_data
        )
    )


def _codex_responses_initial_sse_keepalive() -> _JSONStreamEvent:
    # Responses clients parse every ``data:`` payload as JSON.  A bare SSE
    # comment yielded by the hook would be re-framed by the proxy as a data
    # payload (``data: : litellm_menu ...``), aborting the client stream
    # before ``response.completed``.  Use a parseable Responses event instead.
    return _JSONStreamEvent(
        {
            "type": "response.metadata",
            "metadata": {"phase": "initial_wait"},
        }
    )


def _is_codex_responses_initial_sse_keepalive(chunk: Any) -> bool:
    if isinstance(chunk, dict) and chunk.get("type") == "response.metadata":
        metadata = chunk.get("metadata")
        return isinstance(metadata, dict) and metadata.get("phase") == "initial_wait"
    if isinstance(chunk, bytes):
        chunk = chunk.decode("utf-8", errors="replace")
    if isinstance(chunk, str):
        return chunk.startswith(": litellm_menu initial_wait ")
    return False


async def _yield_codex_responses_initial_keepalive_stream(
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float] = None,
) -> AsyncIterator[Any]:
    """Open a Codex Responses SSE stream before a slow upstream first event.

    The pending upstream read is deliberately shielded while the local metadata
    event opens the downstream SSE response.  The original first-event deadline
    still applies to that same read, so a keepalive never turns an upstream
    first-event timeout into an indefinitely live stream.
    """

    iterator = response.__aiter__()
    start_timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(
        request_data
    )
    deadline = (
        time.monotonic() + start_timeout_seconds
        if start_timeout_seconds > 0
        else None
    )
    next_chunk_task: Optional[asyncio.Task[Any]] = asyncio.create_task(
        iterator.__anext__()
    )
    emitted_keepalive = False

    async def cancel_pending_read() -> None:
        nonlocal next_chunk_task
        task = next_chunk_task
        next_chunk_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception:
            pass

    try:
        while True:
            task = next_chunk_task
            if task is None:
                return
            if task.done():
                try:
                    first_chunk = task.result()
                except StopAsyncIteration:
                    next_chunk_task = None
                    return
                next_chunk_task = None
                break

            remaining_seconds: Optional[float] = None
            if deadline is not None:
                remaining_seconds = deadline - time.monotonic()
                if remaining_seconds <= 0:
                    await cancel_pending_read()
                    raise _stream_start_timeout_exception(
                        request_data,
                        start_seconds=start_timeout_seconds,
                        saw_chunk=False,
                        buffered_chunks=0,
                    ) from None

            wait_seconds: Optional[float]
            if emitted_keepalive:
                wait_seconds = remaining_seconds
            else:
                wait_seconds = _CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS
                if remaining_seconds is not None:
                    wait_seconds = min(wait_seconds, remaining_seconds)

            try:
                if wait_seconds is None:
                    first_chunk = await asyncio.shield(task)
                else:
                    first_chunk = await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=wait_seconds,
                    )
            except asyncio.TimeoutError:
                if task.done():
                    continue
                if deadline is not None and time.monotonic() >= deadline:
                    await cancel_pending_read()
                    raise _stream_start_timeout_exception(
                        request_data,
                        start_seconds=start_timeout_seconds,
                        saw_chunk=False,
                        buffered_chunks=0,
                    ) from None
                if not emitted_keepalive:
                    emitted_keepalive = True
                    yield _codex_responses_initial_sse_keepalive()
                    continue
                await cancel_pending_read()
                raise _stream_start_timeout_exception(
                    request_data,
                    start_seconds=start_timeout_seconds,
                    saw_chunk=False,
                    buffered_chunks=0,
                ) from None
            except StopAsyncIteration:
                next_chunk_task = None
                return
            else:
                next_chunk_task = None
                break

        if (
            _routing_module._FIRST_STREAM_OUTPUT_TIME_KEY not in request_data
            and _stream_chunk_has_meaningful_delta(first_chunk)
        ):
            observed_at = datetime.now(timezone.utc)
            request_data[_routing_module._FIRST_STREAM_OUTPUT_TIME_KEY] = observed_at
            _routing_module._record_first_stream_output_time(
                request_data,
                observed_at,
            )
        yield first_chunk
        async for chunk in _stream_with_idle_timeout(
            iterator,
            request_data,
            stream_started_at=stream_started_at,
            initial_chunk_count=1,
        ):
            yield chunk
    finally:
        await cancel_pending_read()


def _stream_chunk_has_meaningful_delta(chunk: Any) -> bool:
    dumped = _stream_chunk_dump(chunk)
    chunk_type = _stream_chunk_type(dumped)
    if isinstance(chunk_type, str) and chunk_type.endswith(".delta"):
        delta = dumped.get("delta") if isinstance(dumped, dict) else None
        if isinstance(delta, str):
            return bool(delta.strip())
        if delta not in (None, "", [], {}):
            return True
    return _stream_chunk_has_visible_output(chunk)


async def _stream_with_selected_deployment_box(
    response: Any,
    selected_deployment_box: dict[str, Any],
) -> AsyncIterator[Any]:
    iterator = response.__aiter__()
    while True:
        selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
            selected_deployment_box
        )
        try:
            chunk = await iterator.__anext__()
        except StopAsyncIteration:
            return
        finally:
            _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(selected_deployment_box_token)
        yield chunk


def _request_is_responses_stream(request_data: Optional[dict]) -> bool:
    return _responses_request_module._request_is_responses_api(request_data) or "input" in (request_data or {})


def _request_supports_streaming_error_fallback(request_data: Optional[dict]) -> bool:
    """Whether this is a streaming request with a route-replay payload.

    This deliberately does not inspect the client or upstream protocol.  The
    selected adapter decides which LiteLLM method emits the replay; routing,
    retry, recovery and cooldown all share this eligibility rule.
    """

    if not isinstance(request_data, dict) or request_data.get("stream") is not True:
        return False
    return request_data.get("input") is not None or request_data.get("messages") is not None


def _request_is_anthropic_messages_stream(request_data: Optional[dict]) -> bool:
    if not _request_supports_streaming_error_fallback(request_data):
        return False
    if _request_is_responses_stream(request_data):
        return False
    return (
        _routing_module._request_client_surface(request_data)
        == _UPSTREAM_URL_SURFACE_ANTHROPIC
    )


def _request_uses_native_event_stream(request_data: Optional[dict]) -> bool:
    """Return true when recovery must preserve a non-Responses SSE wire form."""

    return _request_supports_streaming_error_fallback(
        request_data
    ) and not _request_is_responses_stream(request_data)


def _streaming_error_fallback_method_name(request_data: Optional[dict]) -> Optional[str]:
    request_data = request_data or {}
    if _request_is_responses_stream(request_data):
        return "aresponses"
    if "messages" in request_data:
        if _request_is_anthropic_messages_stream(request_data):
            return "anthropic_messages"
        return "acompletion"
    return None


def _model_route_slug(model: Any) -> Optional[str]:
    if not isinstance(model, str):
        return None
    text = model.strip()
    if not text:
        return None
    if "/" in text:
        text = text.rsplit("/", 1)[-1].strip()
    return text or None


def _should_prefer_selected_route_model(
    payload_model: Any,
    selected_route_model: Any,
) -> bool:
    if not isinstance(payload_model, str) or not payload_model.strip():
        return False
    if not isinstance(selected_route_model, str) or not selected_route_model.strip():
        return False
    if selected_route_model == payload_model:
        return False
    payload_slug = _model_route_slug(payload_model)
    selected_slug = _model_route_slug(selected_route_model)
    return bool(payload_slug and selected_slug and selected_slug != payload_slug)


def _build_streaming_error_fallback_payload(
    request_data: dict,
    *,
    method_name: str,
    allow_repeated_attempt: bool = False,
) -> Optional[dict]:
    if (
        not allow_repeated_attempt
        and _responses_request_module._request_already_attempted_streaming_error_fallback(request_data)
    ):
        return None

    common_keys = (
        "model",
        "tools",
        "tool_choice",
        "temperature",
        "top_p",
        "stream_timeout",
        "api_base",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "litellm_params",
        "model_info",
        "extra_body",
        "extra_headers",
    )
    responses_keys = (
        "input",
        "instructions",
        "max_output_tokens",
        "truncation",
        "text",
        "include",
        "store",
        "previous_response_id",
        "client_metadata",
        "prompt_cache_key",
        "parallel_tool_calls",
        "reasoning",
        "user",
        "service_tier",
        "seed",
        "stop",
        "response_format",
        "stream_options",
    )
    chat_completion_keys = (
        "messages",
        "max_tokens",
        "max_completion_tokens",
        "parallel_tool_calls",
        "reasoning",
        "user",
        "service_tier",
        "seed",
        "stop",
        "response_format",
        "stream_options",
        "functions",
        "function_call",
        "modalities",
        "audio",
    )
    anthropic_messages_keys = (
        "messages",
        "max_tokens",
        "stop_sequences",
        "system",
        "thinking",
        "container",
        "top_k",
        "metadata",
    )

    if method_name == "aresponses":
        method_keys = responses_keys
    elif method_name == "anthropic_messages":
        method_keys = anthropic_messages_keys
    else:
        method_keys = chat_completion_keys
    allowed_keys = common_keys + method_keys
    payload: Dict[str, Any] = {}
    for key in allowed_keys:
        if key not in request_data:
            continue
        value = _jsonable(request_data.get(key))
        if value is not None:
            payload[key] = value
    if method_name != "anthropic_messages" and "service_tier" not in payload:
        default_service_tier = (
            _codex_fast_tier_module._codex_fast_default_service_tier(request_data)
        )
        if default_service_tier is not None:
            payload["service_tier"] = default_service_tier

    litellm_metadata = _request_context_module._request_metadata_dict(request_data, "litellm_metadata") or {}
    external_web_search_payload = bool(
        litellm_metadata.get("external_web_search_continuation") is True
        or litellm_metadata.get("external_web_search_synthesis") is True
    )
    selected_route_upstream_model = (
        _responses_execution_module._request_selected_route_upstream_model(request_data)
    )
    request_model_group = _responses_execution_module._request_model_group(request_data)
    selected_model_group = (
        _responses_execution_module._request_selected_deployment_model_group(
            request_data
        )
    )
    original_model_group = _responses_execution_module._request_metadata_model_group(
        request_data
    )
    payload_model = payload.get("model")
    if external_web_search_payload:
        model_group = (
            selected_model_group
            or original_model_group
            or request_model_group
            or payload_model
        )
        if isinstance(model_group, str) and model_group.strip():
            payload["model"] = model_group
    elif not payload.get("model"):
        if isinstance(request_model_group, str) and request_model_group.strip():
            payload["model"] = request_model_group
    elif (
        isinstance(payload_model, str)
        and payload_model.strip()
        and isinstance(selected_route_upstream_model, str)
        and selected_route_upstream_model.strip()
        and selected_route_upstream_model != payload_model
        and _should_prefer_selected_route_model(
            payload_model,
            selected_route_upstream_model,
        )
        and (
            litellm_metadata.get(_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY)
            == payload_model
            or _routing_module._deployment_route_key_from_request(request_data)
        )
    ):
        explicit_route_model_group = _explicit_route_model_group(request_data)
        if explicit_route_model_group != payload_model:
            payload["model"] = selected_route_upstream_model

    if not payload.get("model"):
        return None
    if method_name == "aresponses" and "input" not in payload:
        return None
    if method_name in {"acompletion", "anthropic_messages"} and "messages" not in payload:
        return None
    if method_name == "anthropic_messages" and "max_tokens" not in payload:
        return None

    metadata = _request_context_module._request_metadata_dict(request_data, "metadata")
    merged_litellm_metadata = litellm_metadata.copy()
    if metadata is not None:
        merged_litellm_metadata.update(metadata)
        if (
            method_name == "anthropic_messages"
            or _responses_request_module._request_allows_upstream_metadata(request_data)
        ):
            payload["metadata"] = metadata.copy()
    for trace_key in ("request_id", "litellm_call_id", "call_id"):
        trace_value = request_data.get(trace_key)
        if isinstance(trace_value, str) and trace_value.strip():
            merged_litellm_metadata.setdefault(trace_key, trace_value)
    merged_litellm_metadata[_STREAM_ERROR_FALLBACK_METADATA_KEY] = True
    payload["litellm_metadata"] = merged_litellm_metadata
    payload["stream"] = True
    if method_name == "aresponses":
        payload.setdefault("call_type", "aresponses")
    elif method_name == "anthropic_messages":
        payload.setdefault("call_type", "anthropic_messages")
    descendant_cleanup_payload = (
        _responses_request_module._with_codex_descendant_cleanup_instruction(payload)
    )
    if descendant_cleanup_payload is not None:
        payload = descendant_cleanup_payload
    compaction_payload = _responses_request_module._with_codex_compaction_controls(payload)
    if compaction_payload is not None:
        payload = compaction_payload
    if (
        method_name == "aresponses"
        and _responses_request_module._request_is_codex_compaction(payload)
    ):
        # Streaming fallback payloads do not pass through the ordinary
        # deployment pre-call hook. Reuse the same Responses surface adapter
        # so unknown gateways receive a schema-valid compaction request while
        # official/native-capable routes keep additional_tools intact.
        from . import responses_surfaces as _responses_surfaces_module

        native_client_tool_payload = (
            _responses_surfaces_module._with_responses_native_client_tool_passthrough(
                payload,
                request_data,
            )
        )
        if native_client_tool_payload is not None:
            payload = native_client_tool_payload
    extra_body_payload = _responses_request_module._with_responses_native_extra_body(payload)
    if extra_body_payload is not None:
        payload = extra_body_payload
    header_payload = _responses_request_module._with_codex_compaction_headers_from_source(
        payload,
        request_data,
    )
    if header_payload is not None:
        payload = header_payload
    return payload


def _apply_streaming_error_fallback_constraints(
    payload: dict,
    router: Any,
    exception: Exception,
    request_data: dict,
    *,
    route_recovery_poll: bool = False,
) -> None:
    _routing_module._sync_failed_deployment_exclusions(request_data, exception)

    def cumulative_excluded_ids() -> set[str]:
        excluded_ids = set(_CURRENT_EXCLUDED_DEPLOYMENT_IDS.get() or ())
        excluded_ids.update(
            _routing_module._exception_excluded_deployment_ids(exception)
        )
        excluded_ids.update(
            _responses_request_module._request_excluded_deployment_ids(request_data)
        )
        return excluded_ids

    def preserve_cumulative_exclusions(*additional_sources: dict) -> set[str]:
        excluded_ids = cumulative_excluded_ids()
        for source in additional_sources:
            excluded_ids.update(
                _responses_request_module._request_excluded_deployment_ids(source)
            )
        if not excluded_ids:
            return excluded_ids
        serialized_excluded_ids = sorted(excluded_ids)
        request_data["_excluded_deployment_ids"] = serialized_excluded_ids
        payload["_excluded_deployment_ids"] = serialized_excluded_ids
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids)
        return excluded_ids

    forced_target_order = _routing_module._coerce_order(
        request_data.get(_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY)
    )
    if route_recovery_poll and forced_target_order is not None:
        payload["_target_order"] = forced_target_order
        excluded_ids = preserve_cumulative_exclusions()
        if excluded_ids:
            payload["_excluded_deployment_ids"] = sorted(excluded_ids)
        else:
            payload.pop("_excluded_deployment_ids", None)
        return

    peer_entry = _responses_execution_module._ordered_deployment_fallback_entry(router, exception, request_data)
    if peer_entry is not None:
        for key, value in peer_entry.items():
            if key != "model":
                payload[key] = value
        preserve_cumulative_exclusions(peer_entry)
        return

    def explicit_request_order() -> Optional[_RouteOrder]:
        for key in ("order", "_target_order"):
            order = _routing_module._coerce_order(request_data.get(key))
            if order is not None:
                return order
        for section_name in ("litellm_params", "model_info"):
            section = request_data.get(section_name)
            if not isinstance(section, dict):
                continue
            if "order" in section:
                order = _routing_module._coerce_order(section.get("order"))
                if order is not None:
                    return order
            order = _routing_module._order_from_route_key(section.get("route_key"))
            if order is not None:
                return order
        model_info = _request_context_module._request_model_info(request_data)
        if "order" in model_info:
            order = _routing_module._coerce_order(model_info.get("order"))
            if order is not None:
                return order
        return _routing_module._order_from_route_key(model_info.get("route_key"))

    no_deployments_available = _routing_module._is_no_deployments_available_error(exception)
    target_order = _responses_request_module._request_target_order(request_data)
    failed_order = _responses_execution_module._failed_deployment_order(exception)
    if target_order is None and failed_order is not None and not no_deployments_available:
        target_order = failed_order
    if target_order is None and not no_deployments_available:
        target_order = explicit_request_order()
    if target_order is not None:
        payload["_target_order"] = target_order

    excluded_ids = cumulative_excluded_ids()
    retry_same_deployment = _routing_module._should_retry_same_deployment_before_fallback(exception)
    failed_id = _responses_execution_module._failed_deployment_id(exception)
    if failed_id is None and not no_deployments_available and not retry_same_deployment:
        failed_id = _routing_module._deployment_id_from_request(request_data)
    if failed_id and not retry_same_deployment and not _routing_module._is_local_stream_timeout_error(exception):
        excluded_ids.add(failed_id)
    if excluded_ids:
        preserve_cumulative_exclusions(
            {"_excluded_deployment_ids": sorted(excluded_ids)}
        )


async def _streaming_error_fallback_response(
    request_data: dict,
    exception: Exception,
    *,
    allow_repeated_attempt: bool = False,
    route_recovery_poll: bool = False,
    selected_deployment_box: Optional[dict[str, Any]] = None,
) -> Optional[tuple[Any, dict]]:
    if not isinstance(request_data, dict):
        return None
    if (
        not allow_repeated_attempt
        and _responses_request_module._request_already_attempted_streaming_error_fallback(request_data)
    ):
        return None
    if not (
        _routing_module._is_request_scoped_priority_deployment_failover_error(
            exception,
            request_data,
        )
        or _routing_module._is_no_deployments_available_error(exception)
    ):
        return None

    method_name = _streaming_error_fallback_method_name(request_data)
    if method_name is None:
        return None

    if getattr(exception, "stream_incomplete", False):
        # The stream itself consumed this deployment attempt.  It is not an
        # additional same-route retry, so proceed to the next ordered route.
        _routing_module._mark_same_deployment_retry_exhausted(exception)
    if (
        _routing_module._is_request_scoped_priority_deployment_failover_error(
            exception,
            request_data,
        )
        and not _routing_module._should_retry_same_deployment_before_fallback(exception)
    ):
        _routing_module._mark_exception_for_deployment_failover(exception, request_data)
    payload = _build_streaming_error_fallback_payload(
        request_data,
        method_name=method_name,
        allow_repeated_attempt=allow_repeated_attempt,
    )
    if payload is None:
        return None
    exception_body = getattr(exception, "body", None)
    if route_recovery_poll:
        litellm_metadata = _request_context_module._request_metadata_dict(payload, "litellm_metadata") or {}
        updated_metadata = litellm_metadata.copy()
        updated_metadata[_ROUTE_RECOVERY_POLL_METADATA_KEY] = True
        payload["litellm_metadata"] = updated_metadata
    if isinstance(exception_body, dict) and exception_body.get("reason") == "stream_idle_timeout":
        litellm_metadata = _request_context_module._request_metadata_dict(payload, "litellm_metadata") or {}
        updated_metadata = litellm_metadata.copy()
        updated_metadata[_STREAM_IDLE_TIMEOUT_METADATA_KEY] = True
        payload["litellm_metadata"] = updated_metadata
    if isinstance(exception_body, dict) and exception_body.get("reason") == "stream_start_timeout":
        litellm_metadata = _request_context_module._request_metadata_dict(payload, "litellm_metadata") or {}
        updated_metadata = litellm_metadata.copy()
        updated_metadata[_STREAM_START_TIMEOUT_METADATA_KEY] = True
        payload["litellm_metadata"] = updated_metadata

    from litellm.proxy.proxy_server import llm_router

    if llm_router is None:
        raise RuntimeError("LiteLLM router is unavailable for streaming fallback")

    _apply_streaming_error_fallback_constraints(
        payload,
        llm_router,
        exception,
        request_data,
        route_recovery_poll=route_recovery_poll,
    )
    retry_surface = _routing_module._request_current_upstream_surface(payload)
    if (
        method_name == "aresponses"
        and retry_surface in {"openai/chat", "anthropic"}
        and not _responses_request_module._request_has_structured_codex_compaction(
            payload
        )
    ):
        from . import responses_surfaces as _responses_surfaces_module

        retry_metadata = (
            _request_context_module._request_metadata_dict(
                payload, "litellm_metadata"
            )
            or {}
        )
        _responses_surfaces_module._with_responses_chat_bridge_compatible_tools(
            payload,
            retry_metadata,
        )
        bridge_input, input_stats = (
            _responses_tools_module._responses_chat_bridge_input(
                payload.get("input")
            )
        )
        if input_stats.get("changed"):
            payload["input"] = bridge_input
        payload["litellm_metadata"] = retry_metadata
    router_method = getattr(llm_router, method_name, None)
    if router_method is None:
        raise RuntimeError(f"LiteLLM router does not support {method_name} streaming fallback")

    _trace_module._route_trace(
        "streaming_error_fallback_start",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        method=method_name,
        target_order=payload.get("_target_order"),
        excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
        request=_trace_module._trace_request_summary(request_data),
        retry_request=_trace_module._trace_request_summary(payload, method_name=method_name),
        exception=_routing_module._trace_exception(exception),
        route_recovery_poll=route_recovery_poll,
    )
    if selected_deployment_box is None:
        selected_deployment_box = {}
    selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
        selected_deployment_box
    )
    try:
        stream_start_timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(
            payload,
        )
        if stream_start_timeout_seconds > 0:
            response = await asyncio.wait_for(
                router_method(**payload),
                timeout=stream_start_timeout_seconds,
            )
        else:
            response = await router_method(**payload)
        _routing_module._apply_current_selected_deployment_to_request(
            payload,
            selected_box=selected_deployment_box,
        )
        return response, payload
    except Exception as fallback_exception:
        _routing_module._apply_current_selected_deployment_to_request(
            payload,
            selected_box=selected_deployment_box,
        )
        if isinstance(fallback_exception, asyncio.TimeoutError):
            fallback_exception = _stream_start_timeout_exception(
                payload,
                start_seconds=stream_start_timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            )
        _routing_module._mark_no_deployments_for_order_exhaustion(fallback_exception, payload)
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            fallback_exception,
            payload,
        ):
            _routing_module._mark_exception_for_deployment_failover(fallback_exception, payload)
        _trace_module._route_trace(
            "streaming_error_fallback_error",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            method=method_name,
            target_order=payload.get("_target_order"),
            excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
            request=_trace_module._trace_request_summary(request_data),
            retry_request=_trace_module._trace_request_summary(payload, method_name=method_name),
            original_exception=_routing_module._trace_exception(exception),
            exception=_routing_module._trace_exception(fallback_exception),
            route_recovery_poll=route_recovery_poll,
        )
        raise fallback_exception
    finally:
        _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(selected_deployment_box_token)


async def _stream_streaming_error_fallback_round(
    request_data: dict,
    exception: Exception,
    *,
    allow_repeated_attempt: bool = False,
    route_recovery_poll: bool = False,
) -> AsyncIterator[Any]:
    selected_deployment_box = _CURRENT_SELECTED_DEPLOYMENT_BOX.get()
    if not isinstance(selected_deployment_box, dict):
        selected_deployment_box = {}
    try:
        fallback_attempt = await _streaming_error_fallback_response(
            request_data,
            exception,
            allow_repeated_attempt=allow_repeated_attempt,
            route_recovery_poll=route_recovery_poll,
            selected_deployment_box=selected_deployment_box,
        )
        if fallback_attempt is None:
            return
        fallback_response, fallback_payload = fallback_attempt
        if not _responses_output_module._response_is_async_iterable(
            fallback_response
        ):
            fallback_response = _non_streaming_response_as_stream(
                fallback_response,
                fallback_payload,
            )
        guarded_fallback_response = _stream_with_selected_deployment_box(
            fallback_response,
            selected_deployment_box,
        )
        if _request_uses_native_event_stream(fallback_payload):
            guarded_chunks = _yield_native_stream_after_terminal(
                guarded_fallback_response,
                fallback_payload,
                selected_deployment_box=selected_deployment_box,
            )
        else:
            guarded_chunks = _yield_guarded_original_stream(
                [],
                guarded_fallback_response,
                fallback_payload,
                synthesize_completed_on_clean_eof_after_visible_output=True,
            )
        async for chunk in guarded_chunks:
            if _routing_module._apply_current_selected_deployment_to_request(
                fallback_payload,
                selected_box=selected_deployment_box,
            ):
                _routing_module._apply_current_selected_deployment_to_request(
                    request_data,
                    selected_box=selected_deployment_box,
                    update_top_level=False,
                )
            yield chunk
    except Exception as fallback_exception:
        _routing_module._apply_current_selected_deployment_to_request(
            fallback_payload if "fallback_payload" in locals() else request_data,
            selected_box=selected_deployment_box,
        )
        _routing_module._apply_current_selected_deployment_to_request(
            request_data,
            selected_box=selected_deployment_box,
            update_top_level=False,
        )
        if (
            _routing_module._is_request_scoped_priority_deployment_failover_error(
                fallback_exception,
                fallback_payload if "fallback_payload" in locals() else request_data,
            )
            and (
                route_recovery_poll
                or getattr(fallback_exception, "stream_incomplete", False)
                or _routing_module._same_deployment_retries() <= 0
            )
        ):
            # An incomplete stream consumed this deployment attempt, even if
            # it emitted only buffered protocol events.  Route-recovery rounds
            # likewise own a complete attempt, and a zero retry budget must
            # advance immediately.
            _routing_module._mark_same_deployment_retry_exhausted(
                fallback_exception
            )
        if (
            _routing_module._is_request_scoped_priority_deployment_failover_error(
                fallback_exception,
                fallback_payload if "fallback_payload" in locals() else request_data,
            )
            and not _routing_module._should_retry_same_deployment_before_fallback(fallback_exception)
        ):
            failed_deployment_id = (
                _responses_execution_module._failed_deployment_id(fallback_exception)
                or _routing_module._deployment_id_from_request(
                    fallback_payload if "fallback_payload" in locals() else request_data
                )
                or _routing_module._deployment_id_from_request(request_data)
            )
            _routing_module._mark_exception_for_deployment_failover(
                fallback_exception,
                fallback_payload if "fallback_payload" in locals() else request_data,
            )
            _routing_module._sync_failed_deployment_exclusions(
                request_data,
                fallback_exception,
                deployment_id=failed_deployment_id,
            )
        raise fallback_exception


def _reset_route_exhaustion_retry_state(
    request_data: dict,
    exception: Exception,
    *,
    preserve_failed_deployment: bool = False,
    preserve_existing_exclusions: bool = False,
    preserve_target_order: bool = False,
) -> None:
    target_order = _responses_request_module._request_target_order(request_data)
    if target_order is None:
        target_order = _responses_execution_module._failed_deployment_order(exception)
    failed_id = (
        _responses_execution_module._failed_deployment_id(exception)
        or _routing_module._deployment_id_from_request(request_data)
    )
    existing_excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_data)
    if preserve_failed_deployment and failed_id:
        excluded_ids = existing_excluded_ids
        excluded_ids.add(failed_id)
        request_data["_excluded_deployment_ids"] = sorted(excluded_ids)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(excluded_ids)
        try:
            exception.excluded_deployment_ids = sorted(excluded_ids)  # type: ignore[attr-defined]
        except Exception:
            pass
    elif preserve_existing_exclusions and existing_excluded_ids:
        request_data["_excluded_deployment_ids"] = sorted(existing_excluded_ids)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(existing_excluded_ids)
    else:
        request_data.pop("_excluded_deployment_ids", None)
        _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(set())
        try:
            delattr(exception, "excluded_deployment_ids")
        except AttributeError:
            pass
        except Exception:
            pass
        if _routing_module._is_no_deployments_available_error(exception):
            try:
                delattr(exception, "failed_deployment_order")
            except AttributeError:
                pass
            except Exception:
                pass
    if preserve_target_order and target_order is not None:
        request_data["_target_order"] = target_order
    else:
        request_data.pop("_target_order", None)


def _configured_deployment_orders(router: Any, request_data: dict) -> list[_RouteOrder]:
    model_group = _responses_execution_module._request_model_group(request_data)
    if not isinstance(model_group, str) or not model_group.strip():
        return []
    try:
        metadata = _request_context_module._request_metadata_dict(request_data, "metadata") or {}
        team_id = metadata.get("user_api_key_team_id")
        deployments = _routing_module._router_configured_deployments(
            router,
            model_group,
            team_id=team_id,
        )
    except Exception:
        return []
    orders = {
        order
        for order in (
            _responses_request_module._deployment_order(deployment)
            for deployment in list(deployments or [])
        )
        if order is not None
    }
    return sorted(orders)


def _configured_deployment_ids(router: Any, request_data: dict) -> set[str]:
    model_group = _responses_execution_module._request_model_group(request_data)
    if not isinstance(model_group, str) or not model_group.strip():
        return set()
    try:
        metadata = _request_context_module._request_metadata_dict(
            request_data,
            "metadata",
        ) or {}
        deployments = _routing_module._router_configured_deployments(
            router,
            model_group,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return set()
    return {
        deployment_id
        for deployment_id in (
            _responses_request_module._deployment_id(deployment)
            for deployment in list(deployments or [])
        )
        if deployment_id
    }


def _next_configured_order(
    orders: list[_RouteOrder],
    previous_order: Optional[_RouteOrder],
) -> Optional[_RouteOrder]:
    if not orders:
        return None
    if previous_order is None:
        return orders[0]
    for order in orders:
        if order > previous_order:
            return order
    return orders[0]


def _route_recovery_exhausted_order(
    request_data: dict,
    exception: Exception,
) -> Optional[_RouteOrder]:
    order = _responses_execution_module._failed_deployment_order(exception)
    if order is None:
        order = _responses_request_module._request_target_order(request_data)
    if order is None:
        order = _routing_module._deployment_order_from_request(request_data)
    return order


def _route_recovery_next_poll_order(
    router: Any,
    request_data: dict,
    exception: Exception,
) -> Optional[_RouteOrder]:
    orders = _configured_deployment_orders(router, request_data)
    return _next_configured_order(orders, _route_recovery_exhausted_order(request_data, exception))


async def _stream_streaming_error_fallback(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    # Streaming follows the same explicit same-route budget regardless of
    # client protocol. A failed fallback stream that has not delivered
    # visible output or a terminal event is a completed route attempt, not a
    # same-route retry; keep advancing while each failure adds a new excluded
    # deployment.  The configured deployment pool therefore bounds the loop.
    max_retries = _routing_module._same_deployment_retries()
    delay_seconds = _routing_module._stream_route_exhaustion_retry_delay_seconds()
    attempt = 0
    allow_repeated_attempt = False
    is_responses_stream = _request_is_responses_stream(request_data)
    is_structured_compaction = (
        _responses_request_module._request_has_structured_codex_compaction(
            request_data
        )
    )
    while True:
        buffered_chunks: List[Any] = []
        started_delivery = False
        excluded_before = (
            _responses_request_module._request_excluded_deployment_ids(
                request_data
            )
        )
        try:
            async for chunk in _stream_streaming_error_fallback_round(
                request_data,
                exception,
                allow_repeated_attempt=allow_repeated_attempt,
            ):
                if is_responses_stream and not started_delivery:
                    deliverable = bool(
                        _stream_chunk_has_visible_output(chunk)
                        or _responses_completed_chunk_has_usable_output(
                            chunk,
                            request_data,
                        )
                        or _responses_stream_chunk_is_incomplete_terminal(chunk)
                    )
                    if not deliverable:
                        buffered_chunks.append(chunk)
                        continue
                    started_delivery = True
                    for buffered_chunk in buffered_chunks:
                        yield buffered_chunk
                    buffered_chunks.clear()
                else:
                    started_delivery = True
                yield chunk
            return
        except Exception as exc:
            buffered_chunks.clear()
            if started_delivery:
                _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._sync_failed_deployment_exclusions(request_data, exc)
                raise
            if (
                not _routing_module._is_no_deployments_available_error(exc)
                and not _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    request_data,
                )
            ):
                raise
            structured_compaction_wait_failed = (
                is_structured_compaction
                and _routing_module._is_local_stream_timeout_error(exc)
            )
            if structured_compaction_wait_failed:
                # A structured compaction replays the complete signed history.
                # Once its replacement route has consumed a timed-out stream
                # attempt, another ordered replay only adds another full
                # stream wait and cannot make that failed attempt valid.
                # Keep the existing timeout values; bound only the route hop.
                _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._sync_failed_deployment_exclusions(
                    request_data,
                    exc,
                )
                _trace_module._route_trace(
                    "codex_compaction_streaming_fallback_stopped",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(
                        request_data
                    ),
                    reason="stream_wait_timeout",
                    exception=_routing_module._trace_exception(exc),
                )
                raise
            excluded_after = (
                _responses_request_module._request_excluded_deployment_ids(
                    request_data
                )
            )
            newly_excluded = excluded_after - excluded_before
            from litellm.proxy.proxy_server import llm_router

            configured_deployment_ids = (
                _configured_deployment_ids(llm_router, request_data)
                if llm_router is not None
                else set()
            )
            remaining_deployment_ids = (
                configured_deployment_ids - excluded_after
            )
            route_attempt_consumed = bool(
                newly_excluded & configured_deployment_ids
            )
            if route_attempt_consumed:
                if not remaining_deployment_ids:
                    raise
                _trace_module._route_trace(
                    "streaming_error_fallback_route_hop",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(
                        request_data
                    ),
                    excluded_deployment_ids=sorted(excluded_after),
                    newly_excluded_deployment_ids=sorted(newly_excluded),
                    remaining_deployment_ids=sorted(remaining_deployment_ids),
                    exception=_routing_module._trace_exception(exc),
                )
                exception = exc
                attempt = 0
                allow_repeated_attempt = True
                continue

            if attempt < max_retries:
                attempt += 1
                retry_delay_seconds = _routing_module._route_exhaustion_retry_delay_for_exception(
                    exc,
                    delay_seconds,
                )
                _trace_module._route_trace(
                    "route_exhaustion_retry",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    retry_attempt=attempt,
                    max_retries=max_retries,
                    retry_delay_seconds=retry_delay_seconds,
                    configured_retry_delay_seconds=delay_seconds,
                    exception=_routing_module._trace_exception(exc),
                )
                _reset_route_exhaustion_retry_state(
                    request_data,
                    exc,
                    preserve_failed_deployment=False,
                    preserve_existing_exclusions=False,
                )
                exception = exc
                allow_repeated_attempt = True
                if retry_delay_seconds > 0:
                    await asyncio.sleep(retry_delay_seconds)
                continue

            _routing_module._mark_same_deployment_retry_exhausted(exc)
            _routing_module._sync_failed_deployment_exclusions(request_data, exc)
            excluded_after = (
                _responses_request_module._request_excluded_deployment_ids(
                    request_data
                )
            )
            newly_excluded = excluded_after - excluded_before
            remaining_deployment_ids = configured_deployment_ids - excluded_after
            if not (
                newly_excluded & configured_deployment_ids
                and remaining_deployment_ids
            ):
                raise
            _trace_module._route_trace(
                "streaming_error_fallback_route_hop",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(
                    request_data
                ),
                excluded_deployment_ids=sorted(excluded_after),
                newly_excluded_deployment_ids=sorted(newly_excluded),
                remaining_deployment_ids=sorted(remaining_deployment_ids),
                exception=_routing_module._trace_exception(exc),
            )
            exception = exc
            attempt = 0
            allow_repeated_attempt = True


async def _stream_route_recovery_poll_attempt(
    request_data: dict,
    exception: Exception,
    *,
    attempt: int,
    deadline: Optional[float] = None,
) -> AsyncIterator[Any]:
    selected_deployment_box: dict[str, Any] = {}
    fallback_iterator = _stream_streaming_error_fallback_round(
        request_data,
        exception,
        allow_repeated_attempt=True,
        route_recovery_poll=True,
    ).__aiter__()
    buffered_chunks: List[Any] = []
    completion_state = _ResponsesStreamCompletionState(request_data)
    started_delivery = False
    next_chunk_task = None
    configured_timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(request_data)
    request_timeout_seconds = configured_timeout_seconds
    if deadline is not None and attempt > 1:
        remaining_poll_seconds = max(0.0, deadline - time.monotonic())
        if remaining_poll_seconds <= 0:
            raise _stream_start_timeout_exception(
                request_data,
                start_seconds=configured_timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            )
        if request_timeout_seconds > 0:
            request_timeout_seconds = min(request_timeout_seconds, remaining_poll_seconds)
        else:
            request_timeout_seconds = remaining_poll_seconds
    attempt_deadline = (
        time.monotonic() + request_timeout_seconds
        if request_timeout_seconds > 0
        else None
    )

    async def _cancel_next_chunk_task() -> None:
        nonlocal next_chunk_task
        task = next_chunk_task
        next_chunk_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception:
            pass

    try:
        while True:
            if next_chunk_task is None:
                selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
                    selected_deployment_box
                )
                try:
                    next_chunk_task = asyncio.create_task(fallback_iterator.__anext__())
                finally:
                    _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(selected_deployment_box_token)
            try:
                wait_seconds = _ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
                if not started_delivery and attempt_deadline is not None:
                    remaining_seconds = attempt_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        await _cancel_next_chunk_task()
                        completed_compat = (
                            _codex_compaction_done_item_completed_compat(
                                completion_state,
                                request_data,
                                reason="route_recovery_attempt_deadline",
                            )
                        )
                        if completed_compat is not None:
                            for buffered_chunk in buffered_chunks:
                                yield _responses_stream_chunk_for_delivery(
                                    buffered_chunk
                                )
                            yield completed_compat
                            return
                        raise _stream_route_recovery_wait_timeout_exception(
                            request_data,
                            buffered_chunks=len(buffered_chunks),
                            timeout_seconds=configured_timeout_seconds,
                            selected_deployment_box=selected_deployment_box,
                        ) from None
                    wait_seconds = min(wait_seconds, remaining_seconds)
                chunk = await asyncio.wait_for(
                    asyncio.shield(next_chunk_task),
                    timeout=wait_seconds,
                )
                next_chunk_task = None
            except StopAsyncIteration:
                next_chunk_task = None
                completed_compat = _codex_compaction_done_item_completed_compat(
                    completion_state,
                    request_data,
                    reason="route_recovery_clean_eof",
                )
                if completed_compat is not None:
                    for buffered_chunk in buffered_chunks:
                        yield _responses_stream_chunk_for_delivery(buffered_chunk)
                    yield completed_compat
                return
            except asyncio.TimeoutError:
                if next_chunk_task is not None and next_chunk_task.done():
                    completed_task = next_chunk_task
                    next_chunk_task = None
                    # The task may complete at the exact keepalive boundary.
                    # Consume its chunk below rather than dropping it and
                    # starting a second recovery attempt.
                    try:
                        chunk = completed_task.result()
                    except StopAsyncIteration:
                        completed_compat = _codex_compaction_done_item_completed_compat(
                            completion_state,
                            request_data,
                            reason="route_recovery_clean_eof",
                        )
                        if completed_compat is not None:
                            for buffered_chunk in buffered_chunks:
                                yield _responses_stream_chunk_for_delivery(
                                    buffered_chunk
                                )
                            yield completed_compat
                        return
                elif not started_delivery:
                    if (
                        attempt_deadline is not None
                        and (attempt_deadline - time.monotonic()) <= 0
                    ):
                        await _cancel_next_chunk_task()
                        completed_compat = (
                            _codex_compaction_done_item_completed_compat(
                                completion_state,
                                request_data,
                                reason="route_recovery_attempt_timeout",
                            )
                        )
                        if completed_compat is not None:
                            for buffered_chunk in buffered_chunks:
                                yield _responses_stream_chunk_for_delivery(
                                    buffered_chunk
                                )
                            yield completed_compat
                            return
                        raise _stream_route_recovery_wait_timeout_exception(
                            request_data,
                            buffered_chunks=len(buffered_chunks),
                            timeout_seconds=configured_timeout_seconds,
                            selected_deployment_box=selected_deployment_box,
                        ) from None
                    yield _route_recovery_sse_keepalive(
                        attempt,
                        request_data=request_data,
                        phase="attempt",
                    )
                    continue
                else:
                    raise

            if started_delivery:
                yield _responses_stream_chunk_for_delivery(chunk)
                if _responses_stream_chunk_is_completed(chunk):
                    return
                continue

            buffered_chunks.append(chunk)
            completion_state.remember(chunk)
            terminal_exception = _route_recovery_terminal_chunk_exception(
                chunk,
                request_data,
                selected_deployment_box,
                buffer=buffered_chunks,
            )
            if terminal_exception is not None:
                raise terminal_exception
            if not _stream_chunk_has_deliverable_route_recovery_output(
                chunk,
                request_data,
            ):
                continue

            completed_compat_chunk = _responses_output_limit_incomplete_as_completed_chunk(
                chunk,
                request_data,
            )
            if completed_compat_chunk is not None:
                chunk = completed_compat_chunk
                buffered_chunks[-1] = chunk

            if _responses_stream_chunk_is_incomplete_terminal(chunk):
                raise _responses_incomplete_stream_exception(
                    "route recovery attempt ended before response.completed",
                    buffer=buffered_chunks,
                    request_data=request_data,
                )

            started_delivery = True
            for buffered_chunk in buffered_chunks:
                yield _responses_stream_chunk_for_delivery(buffered_chunk)
            buffered_chunks.clear()
            if _responses_stream_chunk_is_completed(chunk):
                return
    except Exception as exc:
        completed_compat = _codex_compaction_done_item_completed_compat(
            completion_state,
            request_data,
            reason="route_recovery_stream_error_after_completed_compaction_item",
            exception=exc,
        )
        if completed_compat is not None:
            for buffered_chunk in buffered_chunks:
                yield _responses_stream_chunk_for_delivery(buffered_chunk)
            yield completed_compat
            return
        raise
    finally:
        await _cancel_next_chunk_task()
        try:
            await fallback_iterator.aclose()
        except Exception:
            pass


def _chat_completions_stream_chunk_is_terminal(
    chunk: Any,
    finished_choice_indices: set[int],
    *,
    expected_choices: int,
) -> bool:
    dumped = _stream_chunk_dump(chunk)
    choices = dumped.get("choices")
    if not isinstance(choices, list):
        return False
    for offset, choice in enumerate(choices):
        if not isinstance(choice, dict) or choice.get("finish_reason") is None:
            continue
        index = choice.get("index")
        finished_choice_indices.add(index if isinstance(index, int) else offset)
    return len(finished_choice_indices) >= expected_choices


def _anthropic_messages_stream_chunk_is_terminal(chunk: Any) -> bool:
    return _stream_chunk_type(chunk) == "message_stop"


def _native_stream_chunk_is_terminal(
    chunk: Any,
    request_data: dict,
    finished_choice_indices: set[int],
) -> bool:
    if _request_is_anthropic_messages_stream(request_data):
        return _anthropic_messages_stream_chunk_is_terminal(chunk)
    requested_choices = request_data.get("n")
    expected_choices = (
        requested_choices
        if isinstance(requested_choices, int) and requested_choices > 0
        else 1
    )
    return _chat_completions_stream_chunk_is_terminal(
        chunk,
        finished_choice_indices,
        expected_choices=expected_choices,
    )


def _native_stream_incomplete_exception(
    request_data: dict,
    *,
    buffered_chunks: int,
    selected_deployment_box: Optional[dict[str, Any]] = None,
) -> Exception:
    is_anthropic = _request_is_anthropic_messages_stream(request_data)
    protocol_name = "Anthropic Messages" if is_anthropic else "Chat Completions"
    terminal_name = "message_stop" if is_anthropic else "finish_reason"
    reason = (
        "anthropic_messages_stream_incomplete"
        if is_anthropic
        else "chat_completions_stream_incomplete"
    )
    exception = RuntimeError(
        f"{protocol_name} stream ended before a terminal {terminal_name}"
    )
    exception.status_code = 503  # type: ignore[attr-defined]
    exception.stream_incomplete = True  # type: ignore[attr-defined]
    exception.body = {  # type: ignore[attr-defined]
        "reason": reason,
        "buffered_chunks": buffered_chunks,
    }
    failure_request = request_data.copy()
    _routing_module._apply_current_selected_deployment_to_request(
        failure_request,
        selected_box=selected_deployment_box,
    )
    _routing_module._mark_exception_for_deployment_failover(
        exception,
        failure_request,
    )
    return exception


async def _yield_native_stream_after_terminal(
    response: Any,
    request_data: dict,
    *,
    selected_deployment_box: Optional[dict[str, Any]] = None,
) -> AsyncIterator[Any]:
    """Validate a native SSE fallback before committing it to the client.

    Chat Completions and Anthropic Messages use different terminal frames, but
    both can be buffered until their own terminal frame. That preserves the
    same routing opportunity as Responses without exposing a partial replay.
    """

    buffered_chunks: List[Any] = []
    finished_choice_indices: set[int] = set()
    saw_terminal = False
    try:
        async for chunk in _stream_with_idle_timeout(response, request_data):
            chunk_exception = _stream_chunk_error_exception(chunk)
            if chunk_exception is not None:
                failure_request = request_data.copy()
                _routing_module._apply_current_selected_deployment_to_request(
                    failure_request,
                    selected_box=selected_deployment_box,
                )
                _routing_module._mark_exception_for_deployment_failover(
                    chunk_exception,
                    failure_request,
                )
                raise chunk_exception
            buffered_chunks.append(chunk)
            saw_terminal = saw_terminal or _native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            )
            if not saw_terminal:
                continue
            success_request = request_data.copy()
            _routing_module._apply_current_selected_deployment_to_request(
                success_request,
                selected_box=selected_deployment_box,
            )
            _routing_module._record_deployment_success_for_cooldown(success_request)
            for buffered_chunk in buffered_chunks:
                yield buffered_chunk
            return
    except Exception:
        raise

    raise _native_stream_incomplete_exception(
        request_data,
        buffered_chunks=len(buffered_chunks),
        selected_deployment_box=selected_deployment_box,
    )


async def _stream_native_route_recovery_poll_attempt(
    request_data: dict,
    exception: Exception,
    *,
    attempt: int,
    deadline: Optional[float] = None,
) -> AsyncIterator[Any]:
    selected_deployment_box: dict[str, Any] = {}
    fallback_iterator = _stream_streaming_error_fallback_round(
        request_data,
        exception,
        allow_repeated_attempt=True,
        route_recovery_poll=True,
    ).__aiter__()
    buffered_chunks: List[Any] = []
    finished_choice_indices: set[int] = set()
    saw_terminal = False
    next_chunk_task = None
    configured_timeout_seconds = (
        _routing_module._stream_start_timeout_seconds_for_request(request_data)
    )
    request_timeout_seconds = configured_timeout_seconds
    if deadline is not None and attempt > 1:
        remaining_poll_seconds = max(0.0, deadline - time.monotonic())
        if remaining_poll_seconds <= 0:
            raise _stream_start_timeout_exception(
                request_data,
                start_seconds=configured_timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            )
        if request_timeout_seconds > 0:
            request_timeout_seconds = min(
                request_timeout_seconds,
                remaining_poll_seconds,
            )
        else:
            request_timeout_seconds = remaining_poll_seconds
    attempt_deadline = (
        time.monotonic() + request_timeout_seconds
        if request_timeout_seconds > 0
        else None
    )

    async def cancel_pending_read() -> None:
        nonlocal next_chunk_task
        task = next_chunk_task
        next_chunk_task = None
        if task is None:
            return
        if not task.done():
            task.cancel()
        try:
            await task
        except (asyncio.CancelledError, StopAsyncIteration):
            pass
        except Exception:
            pass

    def record_success() -> None:
        success_request = request_data.copy()
        _routing_module._apply_current_selected_deployment_to_request(
            success_request,
            selected_box=selected_deployment_box,
        )
        _routing_module._record_deployment_success_for_cooldown(success_request)

    async def yield_completed_attempt() -> AsyncIterator[Any]:
        record_success()
        for buffered_chunk in buffered_chunks:
            yield buffered_chunk

    try:
        while True:
            if next_chunk_task is None:
                selected_deployment_box_token = _CURRENT_SELECTED_DEPLOYMENT_BOX.set(
                    selected_deployment_box
                )
                try:
                    next_chunk_task = asyncio.create_task(
                        fallback_iterator.__anext__()
                    )
                finally:
                    _CURRENT_SELECTED_DEPLOYMENT_BOX.reset(
                        selected_deployment_box_token
                    )
            try:
                wait_seconds = _ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS
                if attempt_deadline is not None:
                    remaining_seconds = attempt_deadline - time.monotonic()
                    if remaining_seconds <= 0:
                        await cancel_pending_read()
                        if saw_terminal:
                            async for chunk in yield_completed_attempt():
                                yield chunk
                            return
                        raise _stream_route_recovery_wait_timeout_exception(
                            request_data,
                            buffered_chunks=len(buffered_chunks),
                            timeout_seconds=configured_timeout_seconds,
                            selected_deployment_box=selected_deployment_box,
                        ) from None
                    wait_seconds = min(wait_seconds, remaining_seconds)
                chunk = await asyncio.wait_for(
                    asyncio.shield(next_chunk_task),
                    timeout=wait_seconds,
                )
                next_chunk_task = None
            except StopAsyncIteration:
                next_chunk_task = None
                if not saw_terminal:
                    raise _native_stream_incomplete_exception(
                        request_data,
                        buffered_chunks=len(buffered_chunks),
                        selected_deployment_box=selected_deployment_box,
                    )
                async for chunk in yield_completed_attempt():
                    yield chunk
                return
            except asyncio.TimeoutError:
                if next_chunk_task is not None and next_chunk_task.done():
                    completed_task = next_chunk_task
                    next_chunk_task = None
                    try:
                        chunk = completed_task.result()
                    except StopAsyncIteration:
                        if not saw_terminal:
                            raise _native_stream_incomplete_exception(
                                request_data,
                                buffered_chunks=len(buffered_chunks),
                                selected_deployment_box=selected_deployment_box,
                            )
                        async for completed_chunk in yield_completed_attempt():
                            yield completed_chunk
                        return
                    except Exception:
                        if saw_terminal:
                            async for completed_chunk in yield_completed_attempt():
                                yield completed_chunk
                            return
                        raise
                elif attempt_deadline is not None and time.monotonic() >= attempt_deadline:
                    await cancel_pending_read()
                    if saw_terminal:
                        async for completed_chunk in yield_completed_attempt():
                            yield completed_chunk
                        return
                    raise _stream_route_recovery_wait_timeout_exception(
                        request_data,
                        buffered_chunks=len(buffered_chunks),
                        timeout_seconds=configured_timeout_seconds,
                        selected_deployment_box=selected_deployment_box,
                    ) from None
                else:
                    yield _route_recovery_sse_keepalive(
                        attempt,
                        request_data=request_data,
                        phase="attempt",
                    )
                    continue
            except Exception:
                if saw_terminal:
                    async for completed_chunk in yield_completed_attempt():
                        yield completed_chunk
                    return
                raise

            chunk_exception = _stream_chunk_error_exception(chunk)
            if chunk_exception is not None:
                if saw_terminal:
                    async for completed_chunk in yield_completed_attempt():
                        yield completed_chunk
                    return
                failure_request = request_data.copy()
                _routing_module._apply_current_selected_deployment_to_request(
                    failure_request,
                    selected_box=selected_deployment_box,
                )
                _routing_module._mark_exception_for_deployment_failover(
                    chunk_exception,
                    failure_request,
                )
                raise chunk_exception

            buffered_chunks.append(chunk)
            saw_terminal = saw_terminal or _native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            )
    finally:
        await cancel_pending_read()
        try:
            await fallback_iterator.aclose()
        except Exception:
            pass


def _route_recovery_poll_keep_going(exception: Exception) -> bool:
    if _routing_module._is_upstream_model_not_found_error(exception):
        return False
    if _external_web_search_exception_has_recovery_request(exception):
        return True
    return _routing_module._is_route_recovery_poll_error(exception)


def _external_web_search_exception_has_recovery_request(
    exception: Exception,
) -> bool:
    return (
        _responses_web_search_bridge_module._external_web_search_recovery_request_from_exception(
            exception,
        )
        is not None
    )


def _external_web_search_recovery_poll_error(exception: Exception) -> bool:
    return bool(
        _routing_module._is_route_recovery_poll_error(exception)
        or _external_web_search_exception_has_recovery_request(exception)
    )


def _external_web_search_recovery_payload_for_blocked_original(
    request_data: dict,
    exception: Exception,
) -> Optional[dict[str, Any]]:
    if not _routing_module._should_block_external_web_search_original_recovery(request_data):
        return None
    recovery_request = (
        _responses_web_search_bridge_module._external_web_search_recovery_request_from_exception(
            exception,
        )
    )
    if recovery_request is None:
        return None
    recovery_request["stream"] = True
    if not _responses_web_search_bridge_module._external_web_search_is_recovery_payload(
        recovery_request,
    ):
        return None
    return recovery_request


def _is_external_web_search_synthesis_recovery_payload(
    request_data: Optional[dict],
) -> bool:
    metadata = _request_context_module._request_metadata_dict(
        request_data,
        "litellm_metadata",
    ) or {}
    return metadata.get("external_web_search_synthesis") is True


def _external_web_search_non_stream_synthesis_payload(
    request_data: dict,
) -> Optional[dict[str, Any]]:
    if not _is_external_web_search_synthesis_recovery_payload(request_data):
        return None
    if not _request_is_responses_stream(request_data):
        return None
    method_name = _streaming_error_fallback_method_name(request_data)
    if method_name != "aresponses":
        return None
    payload = _build_streaming_error_fallback_payload(
        request_data,
        method_name=method_name,
        allow_repeated_attempt=True,
    )
    if payload is None:
        return None
    payload["stream"] = False
    payload.pop("stream_options", None)
    payload.pop("stream_timeout", None)
    litellm_metadata = _request_context_module._request_metadata_dict(
        payload,
        "litellm_metadata",
    ) or {}
    updated_metadata = litellm_metadata.copy()
    updated_metadata[_ROUTE_RECOVERY_POLL_METADATA_KEY] = True
    payload["litellm_metadata"] = updated_metadata
    target_order = _routing_module._coerce_order(
        request_data.get(_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY)
    )
    if target_order is None:
        target_order = _responses_request_module._request_target_order(request_data)
    if target_order is None:
        for section_name in ("litellm_params", "model_info"):
            section = request_data.get(section_name)
            if not isinstance(section, dict):
                continue
            target_order = _routing_module._coerce_order(section.get("order"))
            if target_order is not None:
                break
            target_order = _routing_module._order_from_route_key(section.get("route_key"))
            if target_order is not None:
                break
    if target_order is not None:
        payload["_target_order"] = target_order
    payload.pop("_excluded_deployment_ids", None)
    return payload


async def _external_web_search_non_stream_synthesis_recovery(
    request_data: dict,
    exception: Exception,
) -> Optional[AsyncIterator[Any]]:
    if not _is_external_web_search_synthesis_recovery_payload(request_data):
        return None
    if not _routing_module._is_route_recovery_poll_error(exception):
        return None

    from litellm.proxy.proxy_server import llm_router

    if llm_router is None:
        raise RuntimeError("LiteLLM router is unavailable for external web_search synthesis recovery")
    router_method = getattr(llm_router, "aresponses", None)
    if router_method is None:
        raise RuntimeError("LiteLLM router does not support aresponses external web_search synthesis recovery")
    payload = _external_web_search_non_stream_synthesis_payload(
        request_data,
    )
    if payload is None:
        return None

    _trace_module._route_trace(
        "external_web_search_synthesis_non_stream_recovery_start",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        target_order=payload.get("_target_order"),
        excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
        request=_trace_module._trace_request_summary(request_data),
        retry_request=_trace_module._trace_request_summary(payload, method_name="aresponses"),
        exception=_routing_module._trace_exception(exception),
    )
    try:
        response = await router_method(**payload)
        _responses_web_search_bridge_module._external_web_search_raise_if_invalid_model_response(
            response,
            payload,
            phase="synthesis",
        )
        _trace_module._route_trace(
            "external_web_search_synthesis_non_stream_recovery_done",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            target_order=payload.get("_target_order"),
            excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
            response=_trace_module._trace_response_summary(response, payload),
        )
        return _non_streaming_response_as_stream(response, payload)
    except Exception as recovery_exception:
        _routing_module._mark_no_deployments_for_order_exhaustion(
            recovery_exception,
            payload,
        )
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            recovery_exception,
            payload,
        ):
            _routing_module._mark_exception_for_deployment_failover(
                recovery_exception,
                payload,
            )
        _trace_module._route_trace(
            "external_web_search_synthesis_non_stream_recovery_error",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            target_order=payload.get("_target_order"),
            excluded_deployment_ids=payload.get("_excluded_deployment_ids"),
            request=_trace_module._trace_request_summary(request_data),
            retry_request=_trace_module._trace_request_summary(payload, method_name="aresponses"),
            original_exception=_routing_module._trace_exception(exception),
            exception=_routing_module._trace_exception(recovery_exception),
        )
        raise recovery_exception


_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS = 15.0
_ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS = 1.0
_CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS = 5.0


def _route_recovery_sse_keepalive(
    attempt: int,
    *,
    request_data: Optional[dict] = None,
    phase: str = "poll",
) -> Any:
    if request_data is not None:
        _state_module._touch_route_recovery_state(
            _route_recovery_state_key(request_data)
        )
    if _request_is_responses_stream(request_data):
        return _JSONStreamEvent(
            {
                "type": "response.metadata",
                "metadata": {
                    "phase": phase,
                    "attempt": attempt,
                },
            }
        )
    if _request_uses_native_event_stream(request_data):
        return (
            f": litellm_menu route_recovery phase={phase} attempt={attempt}\n\n"
        ).encode("utf-8")
    return _sse_comment_event(
        f"litellm_menu route_recovery phase={phase} attempt={attempt}"
    )


def _is_route_recovery_sse_keepalive(chunk: Any) -> bool:
    if isinstance(chunk, bytes):
        return chunk.startswith(b": litellm_menu route_recovery ")
    if isinstance(chunk, str):
        return chunk.startswith(": litellm_menu route_recovery ")
    if isinstance(chunk, dict) and chunk.get("type") == "response.metadata":
        metadata = chunk.get("metadata")
        return isinstance(metadata, dict) and isinstance(metadata.get("phase"), str)
    return False


async def _sleep_route_recovery_poll_interval(
    delay_seconds: float,
    *,
    attempt: int,
    request_data: Optional[dict] = None,
    phase: str = "interval",
    emit_for_short_delay: bool = False,
) -> AsyncIterator[Any]:
    if delay_seconds <= 0:
        return
    if delay_seconds < _ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS:
        if emit_for_short_delay:
            yield _route_recovery_sse_keepalive(
                attempt,
                request_data=request_data,
                phase=phase,
            )
        await asyncio.sleep(delay_seconds)
        return

    remaining = delay_seconds
    while remaining > 0:
        yield _route_recovery_sse_keepalive(
            attempt,
            request_data=request_data,
            phase=phase,
        )
        sleep_seconds = min(_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS, remaining)
        await asyncio.sleep(sleep_seconds)
        remaining -= sleep_seconds


async def _stream_route_recovery_poll(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    uses_native_event_stream = _request_uses_native_event_stream(request_data)
    if _responses_request_module._request_is_codex_compaction(request_data):
        _trace_module._route_trace(
            "codex_compaction_route_recovery_blocked",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            request=_trace_module._trace_request_summary(request_data),
            exception=_routing_module._trace_exception(exception),
        )
        yield _synthesized_failed_response_event(request_data, exception)
        return
    recovery_request = _external_web_search_recovery_payload_for_blocked_original(
        request_data,
        exception,
    )
    if recovery_request is not None:
        _trace_module._route_trace(
            "external_web_search_original_route_recovery_resumed_with_payload",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            request=_trace_module._trace_request_summary(request_data),
            retry_request=_trace_module._trace_request_summary(recovery_request),
            exception=_routing_module._trace_exception(exception),
        )
        request_data = recovery_request
    elif _routing_module._should_block_external_web_search_original_recovery(request_data):
        _trace_module._route_trace(
            "external_web_search_original_route_recovery_blocked",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            request=_trace_module._trace_request_summary(request_data),
            exception=_routing_module._trace_exception(exception),
        )
        yield _external_web_search_missing_answer_failed_event(request_data, exception)
        return
    max_poll_seconds = _routing_module._recovery_max_seconds_for_request(request_data)
    if max_poll_seconds <= 0:
        return
    if not _external_web_search_recovery_poll_error(exception):
        return
    started_at_monotonic = time.monotonic()
    poll_interval_seconds = _routing_module._recovery_interval_seconds()
    # A network outage is an external connectivity condition, not a request
    # failure.  Keep the existing stream alive until the client cancels it or
    # an upstream route recovers; otherwise the timeout below becomes the
    # exact point at which Codex receives a connection failure and starts its
    # own reconnect loop.
    network_recovery = (
        _routing_module._is_network_recovery_exception(exception)
        and _routing_module._recovery_policy_for_exception(exception)
        != _routing_module._RECOVERY_POLICY_ERROR
    )
    if network_recovery:
        # Establish every supported stream immediately while the first
        # upstream probe is pending.  Without this event a quiet offline
        # interval is indistinguishable from a failed request to the client.
        yield _route_recovery_sse_keepalive(
            0,
            request_data=request_data,
            phase="network",
        )
    deadline: Optional[float] = (
        None if network_recovery else started_at_monotonic + max_poll_seconds
    )
    last_exception = exception
    attempt = 0
    waited_for_all_cooldown = False
    ignore_local_constraints = bool(
        request_data.pop("_route_recovery_ignore_local_constraints", False)
    )
    recovery_state_key = _route_recovery_state_upsert(
        request_data,
        exception,
        status="polling",
        attempt=attempt,
        started_at_monotonic=started_at_monotonic,
        max_poll_seconds=max_poll_seconds,
        poll_interval_seconds=poll_interval_seconds,
    )

    _trace_module._route_trace(
        "route_recovery_poll_start",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        max_poll_seconds=max_poll_seconds,
        poll_interval_seconds=poll_interval_seconds,
        exception=_routing_module._trace_exception(exception),
    )

    async def yield_non_stream_synthesis_recovery(
        recovery_exception: Exception,
        *,
        reason: str,
    ) -> AsyncIterator[Any]:
        try:
            recovered_stream = await _external_web_search_non_stream_synthesis_recovery(
                request_data,
                recovery_exception,
            )
            if recovered_stream is None:
                return
            async for recovered_chunk in recovered_stream:
                yield recovered_chunk
        except Exception as non_stream_exception:
            nonlocal_last_exception[0] = non_stream_exception
            _trace_module._route_trace(
                "external_web_search_synthesis_non_stream_recovery_failed",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(request_data),
                poll_attempt=attempt,
                reason=reason,
                original_exception=_routing_module._trace_exception(recovery_exception),
                exception=_routing_module._trace_exception(non_stream_exception),
            )
            return

    nonlocal_last_exception: list[Exception] = [last_exception]

    try:
        while True:
            last_exception = nonlocal_last_exception[0]
            cooldown_wait = _route_recovery_poll_cooldown_wait(
                request_data,
                ignore_constraints=ignore_local_constraints,
            )
            if cooldown_wait is not None:
                waited_for_all_cooldown = True
                now = time.monotonic()
                if max_poll_seconds <= 0 or (
                    deadline is not None and now >= deadline
                ):
                    _trace_module._route_trace(
                        "route_recovery_poll_max_duration_reached",
                        request_id=_routing_module._trace_request_id(request_data),
                        session=_routing_module._trace_session_context(request_data),
                        model_group=_responses_execution_module._request_model_group(request_data),
                        poll_attempts=attempt,
                        elapsed_seconds=round(now - started_at_monotonic, 3),
                        max_poll_seconds=max_poll_seconds,
                        exception=_routing_module._trace_exception(last_exception),
                    )
                    break
                cooldown_until = cooldown_wait["cooldown_until"]
                delay_seconds = _route_recovery_cooldown_wait_delay(
                    cooldown_until,
                    poll_interval_seconds=poll_interval_seconds,
                    deadline=deadline,
                )
                _trace_module._route_trace(
                    "route_recovery_poll_waiting_for_cooldown",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    poll_attempt=attempt,
                    cooldown_until=cooldown_until,
                    cooldown_remaining_seconds=round(
                        max(0.0, cooldown_until - time.time()), 3
                    ),
                    cooldown_deployments=cooldown_wait["cooldown_deployments"],
                    exception=_routing_module._trace_exception(last_exception),
                )
                new_recovery_state_key = _route_recovery_state_upsert(
                    request_data,
                    last_exception,
                    status="waiting",
                    attempt=attempt,
                    started_at_monotonic=started_at_monotonic,
                    max_poll_seconds=max_poll_seconds,
                    poll_interval_seconds=delay_seconds,
                    target_order=_responses_request_module._request_target_order(request_data),
                    cooldown_until=cooldown_until,
                    cooldown_deployments=cooldown_wait["cooldown_deployments"],
                )
                if new_recovery_state_key and new_recovery_state_key != recovery_state_key:
                    _route_recovery_state_remove(recovery_state_key)
                    recovery_state_key = new_recovery_state_key
                async for keepalive in _sleep_route_recovery_poll_interval(
                    delay_seconds,
                    attempt=attempt,
                    request_data=request_data,
                    phase="cooldown",
                    emit_for_short_delay=True,
                ):
                    yield keepalive
                continue
            recovery_request = _external_web_search_recovery_payload_for_blocked_original(
                request_data,
                last_exception,
            )
            if recovery_request is not None:
                _trace_module._route_trace(
                    "external_web_search_original_route_recovery_resumed_with_payload",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    request=_trace_module._trace_request_summary(request_data),
                    retry_request=_trace_module._trace_request_summary(recovery_request),
                    exception=_routing_module._trace_exception(last_exception),
                )
                request_data = recovery_request
            elif _routing_module._should_block_external_web_search_original_recovery(request_data):
                _trace_module._route_trace(
                    "external_web_search_original_route_recovery_blocked",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    request=_trace_module._trace_request_summary(request_data),
                    exception=_routing_module._trace_exception(last_exception),
                )
                break
            if waited_for_all_cooldown:
                # Only this path deliberately reopens the full route pool.
                # Normal recovery must retain its ordered fallback constraints.
                request_data.pop("_excluded_deployment_ids", None)
                request_data.pop("_target_order", None)
                request_data.pop(_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY, None)
                _CURRENT_EXCLUDED_DEPLOYMENT_IDS.set(set())
                for attribute in (
                    "excluded_deployment_ids",
                    "failed_deployment_id",
                    "failed_deployment_route_key",
                    "failed_deployment_order",
                    "failed_deployment_surface",
                ):
                    try:
                        delattr(last_exception, attribute)
                    except AttributeError:
                        pass
                    except Exception:
                        pass
                waited_for_all_cooldown = False
                ignore_local_constraints = False
            now = time.monotonic()
            if (
                attempt > 0
                and max_poll_seconds > 0
                and deadline is not None
                and now >= deadline
            ):
                _trace_module._route_trace(
                    "route_recovery_poll_max_duration_reached",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    poll_attempts=attempt,
                    elapsed_seconds=round(now - started_at_monotonic, 3),
                    max_poll_seconds=max_poll_seconds,
                    exception=_routing_module._trace_exception(last_exception),
                )
                break
            attempt += 1
            attempt_started_at = now
            if (
                _routing_module._is_request_scoped_priority_deployment_failover_error(
                    last_exception,
                    request_data,
                )
                and (
                    not _routing_module._should_retry_same_deployment_before_fallback(last_exception)
                    or _routing_module._is_local_stream_start_timeout_error(last_exception)
                )
            ):
                _routing_module._mark_exception_for_deployment_failover(last_exception, request_data)
                _routing_module._sync_failed_deployment_exclusions(request_data, last_exception)
            request_data.pop(_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY, None)
            no_deployments_available = _routing_module._is_no_deployments_available_error(
                last_exception,
            )
            refresh_after_exhausted_poll = no_deployments_available and attempt > 1
            forced_target_order = None
            if refresh_after_exhausted_poll:
                previous_excluded_ids = sorted(
                    _responses_request_module._request_excluded_deployment_ids(request_data),
                )
                try:
                    from litellm.proxy.proxy_server import llm_router
                except Exception:
                    llm_router = None
                if llm_router is not None:
                    forced_target_order = _route_recovery_next_poll_order(
                        llm_router,
                        request_data,
                        last_exception,
                    )
                _trace_module._route_trace(
                    "route_recovery_poll_route_pool_reset",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    poll_attempt=attempt,
                    next_target_order=forced_target_order,
                    excluded_deployment_ids=previous_excluded_ids,
                    exception=_routing_module._trace_exception(last_exception),
                )
            _reset_route_exhaustion_retry_state(
                request_data,
                last_exception,
                preserve_failed_deployment=(
                    _responses_execution_module._failed_deployment_id(last_exception) is not None
                    and not no_deployments_available
                ),
                preserve_existing_exclusions=(
                    no_deployments_available
                    and not refresh_after_exhausted_poll
                ),
                preserve_target_order=(
                    no_deployments_available
                    and not refresh_after_exhausted_poll
                ),
            )
            if forced_target_order is not None:
                request_data[_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY] = forced_target_order

            target_order = forced_target_order
            if target_order is None:
                target_order = _responses_request_module._request_target_order(request_data)
            new_recovery_state_key = _route_recovery_state_upsert(
                request_data,
                last_exception,
                status="polling",
                attempt=attempt,
                started_at_monotonic=started_at_monotonic,
                max_poll_seconds=max_poll_seconds,
                poll_interval_seconds=poll_interval_seconds,
                target_order=target_order,
            )
            if new_recovery_state_key and new_recovery_state_key != recovery_state_key:
                _route_recovery_state_remove(recovery_state_key)
                recovery_state_key = new_recovery_state_key

            _trace_module._route_trace(
                "route_recovery_poll_attempt_start",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(request_data),
                poll_attempt=attempt,
                target_order=target_order,
                elapsed_seconds=round(attempt_started_at - started_at_monotonic, 3),
                remaining_poll_seconds=(
                    None
                    if deadline is None
                    else max(0.0, round(deadline - attempt_started_at, 3))
                ),
                exception=_routing_module._trace_exception(last_exception),
            )

            try:
                yielded = False
                poll_attempt = (
                    _stream_native_route_recovery_poll_attempt
                    if uses_native_event_stream
                    else _stream_route_recovery_poll_attempt
                )
                async for chunk in poll_attempt(
                    request_data,
                    last_exception,
                    attempt=attempt,
                    deadline=deadline,
                ):
                    if _is_route_recovery_sse_keepalive(chunk):
                        yield chunk
                        continue
                    yielded = True
                    if uses_native_event_stream:
                        yield chunk
                    else:
                        yield _responses_stream_chunk_for_delivery(chunk)
                if yielded:
                    _trace_module._route_trace(
                        "route_recovery_poll_success",
                        request_id=_routing_module._trace_request_id(request_data),
                        session=_routing_module._trace_session_context(request_data),
                        model_group=_responses_execution_module._request_model_group(request_data),
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                    )
                    return
                _trace_module._route_trace(
                    "route_recovery_poll_attempt_empty",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    poll_attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                    exception=_routing_module._trace_exception(last_exception),
                )
                async for recovered_chunk in yield_non_stream_synthesis_recovery(
                    last_exception,
                    reason="empty_stream_attempt",
                ):
                    yield _responses_stream_chunk_for_delivery(recovered_chunk)
                    yielded = True
                if yielded:
                    _trace_module._route_trace(
                        "route_recovery_poll_success",
                        request_id=_routing_module._trace_request_id(request_data),
                        session=_routing_module._trace_session_context(request_data),
                        model_group=_responses_execution_module._request_model_group(request_data),
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        recovery_mode="external_web_search_synthesis_non_stream",
                    )
                    return
                last_exception = nonlocal_last_exception[0]
            except Exception as poll_exception:
                if _routing_module._is_context_size_error(poll_exception):
                    _trace_module._route_trace(
                        "route_recovery_poll_context_size_error",
                        request_id=_routing_module._trace_request_id(request_data),
                        session=_routing_module._trace_session_context(request_data),
                        model_group=_responses_execution_module._request_model_group(request_data),
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        exception=_routing_module._trace_exception(poll_exception),
                    )
                    raise
                if not _route_recovery_poll_keep_going(poll_exception):
                    last_exception = poll_exception
                    nonlocal_last_exception[0] = last_exception
                    _trace_module._route_trace(
                        "route_recovery_poll_terminal_error",
                        request_id=_routing_module._trace_request_id(request_data),
                        session=_routing_module._trace_session_context(request_data),
                        model_group=_responses_execution_module._request_model_group(request_data),
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        exception=_routing_module._trace_exception(last_exception),
                    )
                    break
                last_exception = poll_exception
                nonlocal_last_exception[0] = last_exception
                _trace_module._route_trace(
                    "route_recovery_poll_attempt_failed",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    poll_attempt=attempt,
                    elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                    exception=_routing_module._trace_exception(last_exception),
                )
                yielded_non_stream = False
                async for recovered_chunk in yield_non_stream_synthesis_recovery(
                    last_exception,
                    reason="stream_attempt_failed",
                ):
                    yielded_non_stream = True
                    yield _responses_stream_chunk_for_delivery(recovered_chunk)
                if yielded_non_stream:
                    _trace_module._route_trace(
                        "route_recovery_poll_success",
                        request_id=_routing_module._trace_request_id(request_data),
                        session=_routing_module._trace_session_context(request_data),
                        model_group=_responses_execution_module._request_model_group(request_data),
                        poll_attempt=attempt,
                        elapsed_seconds=round(time.monotonic() - started_at_monotonic, 3),
                        recovery_mode="external_web_search_synthesis_non_stream",
                    )
                    return
                last_exception = nonlocal_last_exception[0]

            now = time.monotonic()
            if max_poll_seconds <= 0 or (
                deadline is not None and now >= deadline
            ):
                _trace_module._route_trace(
                    "route_recovery_poll_max_duration_reached",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    poll_attempts=attempt,
                    elapsed_seconds=round(now - started_at_monotonic, 3),
                    max_poll_seconds=max_poll_seconds,
                    exception=_routing_module._trace_exception(last_exception),
                )
                break

            delay_seconds = (
                poll_interval_seconds
                if deadline is None
                else min(poll_interval_seconds, max(0.0, deadline - now))
            )
            _trace_module._route_trace(
                "route_recovery_poll_next_attempt_scheduled",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(request_data),
                poll_attempt=attempt,
                poll_interval_seconds=delay_seconds,
                elapsed_seconds=round(now - started_at_monotonic, 3),
                exception=_routing_module._trace_exception(last_exception),
            )
            new_recovery_state_key = _route_recovery_state_upsert(
                request_data,
                last_exception,
                status="waiting",
                attempt=attempt,
                started_at_monotonic=started_at_monotonic,
                max_poll_seconds=max_poll_seconds,
                poll_interval_seconds=delay_seconds,
                target_order=_responses_request_module._request_target_order(request_data),
            )
            if new_recovery_state_key and new_recovery_state_key != recovery_state_key:
                _route_recovery_state_remove(recovery_state_key)
                recovery_state_key = new_recovery_state_key
            async for keepalive in _sleep_route_recovery_poll_interval(
                delay_seconds,
                attempt=attempt,
                request_data=request_data,
            ):
                yield keepalive

        if uses_native_event_stream:
            if (
                _routing_module._is_context_size_error(last_exception)
                or _routing_module._is_terminal_prompt_or_policy_error(last_exception)
            ):
                raise last_exception
            yield _synthesized_native_stream_error_chunk(request_data, last_exception)
            return
        yield _synthesized_failed_response_event(request_data, last_exception)
    finally:
        _route_recovery_state_remove(recovery_state_key)


def _route_recovery_poll_cooldown_wait(
    request_data: dict,
    *,
    ignore_constraints: bool = False,
) -> Optional[dict[str, Any]]:
    """Describe an all-cooled route pool without opening another upstream call."""

    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return None
    model_group = _responses_execution_module._request_model_group(request_data)
    if not isinstance(model_group, str) or not model_group.strip():
        return None
    try:
        metadata = _request_context_module._request_metadata_dict(
            request_data, "metadata"
        ) or {}
        deployments = _routing_module._router_configured_deployments(
            llm_router,
            model_group,
            team_id=metadata.get("user_api_key_team_id"),
        )
    except Exception:
        return None
    if not deployments:
        return None
    if ignore_constraints:
        # After a local fallback chain has exhausted its own route hints, the
        # next recovery stream must distinguish that local exhaustion from an
        # actual shared cooldown across the complete model group.
        constraints = request_data.copy()
        constraints.pop("_excluded_deployment_ids", None)
        constraints.pop("_target_order", None)
        constraints.pop(_ROUTE_RECOVERY_FORCED_TARGET_ORDER_KEY, None)
    else:
        # An active explicit target/order is a real request constraint. Do not
        # hold an unrelated group route open merely because it is cooling.
        constraints = request_data
    constrained = _responses_request_module._with_retry_target_constraints(
        deployments,
        constraints,
    )
    if not constrained:
        return None
    available, cooled, cooldown_filtered = _routing_module._with_active_deployment_cooldowns(
        constrained,
        request_kwargs=request_data,
    )
    if not (cooldown_filtered and cooled and not available):
        return None
    cooldown_until_values: list[float] = []
    for entry in cooled:
        try:
            cooldown_until = float(entry.get("cooldown_until") or 0.0)
        except (TypeError, ValueError):
            continue
        if cooldown_until > time.time():
            cooldown_until_values.append(cooldown_until)
    if not cooldown_until_values:
        return None
    return {
        "cooldown_until": min(cooldown_until_values),
        "cooldown_deployments": cooled,
    }


def _route_recovery_poll_has_no_available_deployments(request_data: dict) -> bool:
    """Compatibility predicate for callers that only need the cooling state."""

    return _route_recovery_poll_cooldown_wait(request_data) is not None


def _route_recovery_cooldown_wait_delay(
    cooldown_until: float,
    *,
    poll_interval_seconds: float,
    deadline: Optional[float],
) -> float:
    remaining_cooldown = max(0.0, cooldown_until - time.time())
    remaining_poll = (
        float("inf")
        if deadline is None
        else max(0.0, deadline - time.monotonic())
    )
    return max(
        0.001,
        min(poll_interval_seconds, remaining_cooldown or 0.001, remaining_poll or 0.001),
    )

def _build_forced_image_generation_payload(request_data: dict, *, stream: bool) -> Optional[dict]:
    allowed_keys = (
        "model",
        "input",
        "instructions",
        "tools",
        "temperature",
        "top_p",
        "max_output_tokens",
        "parallel_tool_calls",
        "truncation",
        "reasoning",
        "text",
        "include",
        "store",
        "previous_response_id",
        "litellm_metadata",
        "user",
        "service_tier",
        "stream_timeout",
        "_target_order",
        "_excluded_deployment_ids",
        _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    )
    payload: Dict[str, Any] = {}
    for key in allowed_keys:
        if key not in request_data:
            continue
        value = _jsonable(request_data.get(key))
        if value is not None:
            payload[key] = value

    if not payload.get("model") or not _tools_module._request_has_image_generation_tool(payload):
        return None

    attempts = _image_generation_module._with_incremented_image_generation_tool_fallback_attempts(request_data)
    litellm_metadata = _request_context_module._request_metadata_dict(request_data, "litellm_metadata") or {}
    metadata = _request_context_module._request_metadata_dict(request_data, "metadata")
    merged_litellm_metadata = litellm_metadata.copy()
    if metadata is not None:
        merged_litellm_metadata.update(metadata)
        if _responses_request_module._request_allows_upstream_metadata(request_data):
            payload["metadata"] = metadata.copy()
    merged_litellm_metadata[_STREAM_FALLBACK_METADATA_KEY] = True
    merged_litellm_metadata[_IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY] = attempts
    payload["litellm_metadata"] = merged_litellm_metadata
    payload["stream"] = stream
    payload["tool_choice"] = {"type": "image_generation"}
    selected_deployment_id = _routing_module._deployment_id_from_request(request_data)
    excluded_deployment_ids = _responses_request_module._request_excluded_deployment_ids(
        request_data
    )
    if selected_deployment_id and selected_deployment_id not in excluded_deployment_ids:
        payload[_VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY] = [selected_deployment_id]
        selected_order = _routing_module._deployment_order_from_request(request_data)
        if selected_order is not None:
            payload["_target_order"] = selected_order
    return payload


async def _call_forced_image_generation_payload(payload: dict) -> Any:
    from litellm.proxy.proxy_server import llm_router

    if llm_router is None or not hasattr(llm_router, "aresponses"):
        raise RuntimeError("LiteLLM router is unavailable for image_generation fallback")

    return await llm_router.aresponses(**payload)


async def _stream_forced_image_generation_payload(payload: dict) -> AsyncIterator[Any]:
    fallback_response = await _call_forced_image_generation_payload(payload)
    buffer: List[Any] = []
    text = ""
    released = False
    policy_refusal = False
    async for chunk in _stream_with_idle_timeout(fallback_response, payload):
        if released:
            yield chunk
            continue
        buffer.append(chunk)
        if _image_generation_module._response_has_image_generation_activity(chunk):
            released = True
            for buffered_chunk in buffer:
                yield buffered_chunk
            buffer = []
            continue
        chunk_text = _responses_output_module._response_text(chunk)
        if chunk_text:
            text = f"{text}\n{chunk_text}" if text else chunk_text
            if _image_generation_module._response_is_image_generation_policy_refusal(
                {"output_text": text}
            ):
                policy_refusal = True
                break
            if _image_generation_module._response_is_image_generation_unavailable_refusal({"output_text": text}):
                raise _image_generation_module._image_generation_tool_runtime_fallback_exception(
                    capability_unsupported=True,
                )
    if not released:
        if policy_refusal:
            for chunk in buffer:
                yield chunk
            return
        if _image_generation_module._response_should_trigger_image_generation_fallback({"output_text": text}):
            raise _image_generation_module._image_generation_tool_runtime_fallback_exception(
                capability_unsupported=True,
            )
        for chunk in buffer:
            yield chunk


async def _yield_original_stream(
    buffer: List[Any],
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float] = None,
    saw_visible_output: bool = False,
) -> AsyncIterator[Any]:
    visible_output_seen = saw_visible_output
    is_responses_stream = _request_is_responses_stream(request_data)
    for chunk in buffer:
        visible_output_seen = visible_output_seen or _stream_chunk_has_visible_output(chunk) or (
            is_responses_stream
            and _responses_completed_chunk_has_usable_output(chunk, request_data)
        )
        yield chunk
    async for chunk in _stream_with_idle_timeout(
        response,
        request_data,
        stream_started_at=stream_started_at,
        saw_visible_output=visible_output_seen,
        initial_chunk_count=len(buffer),
    ):
        yield chunk


async def _empty_async_iterator() -> AsyncIterator[Any]:
    if False:
        yield None


def _responses_non_streaming_response_has_usable_output(
    response: Any,
    request_data: Optional[dict],
) -> bool:
    payload = _jsonable(response)
    if not isinstance(payload, dict):
        return not _responses_output_module._response_is_effectively_empty(response)
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        return _codex_compaction_output_is_valid(payload.get("output"))
    if (
        _stream_chunk_has_visible_output(payload)
        or _responses_output_module._payload_has_tool_activity(payload)
    ):
        return True
    return False


def _completed_response_payload(response: Any, request_data: Optional[dict]) -> dict[str, Any]:
    payload = _jsonable(response)
    if not isinstance(payload, dict):
        payload = {}
    output = payload.get("output")
    text = _responses_output_module._response_text(payload) or _responses_output_module._response_text(response)
    if not isinstance(output, list) or not output:
        if text.strip():
            message_id = f"msg_completed_{time.time_ns()}"
            payload["output"] = [
                {
                    "id": message_id,
                    "type": "message",
                    "status": "completed",
                    "role": "assistant",
                    "content": [
                        {
                            "type": "output_text",
                            "text": text,
                            "annotations": [],
                        }
                    ],
                }
            ]
            payload["output_text"] = text
        else:
            payload["output"] = []
    payload.setdefault("id", f"resp_completed_{os.getpid()}_{time.time_ns()}")
    payload.setdefault("object", "response")
    payload.setdefault("created_at", int(time.time()))
    payload.setdefault(
        "model",
        _routing_module._first_not_none(
            (request_data or {}).get("model"),
            _responses_execution_module._request_model_group(request_data),
            "unknown",
        ),
    )
    payload["status"] = "completed"
    payload = _responses_web_search_bridge_module._sanitize_response_stream_payload(payload)
    return payload


def _stream_function_arguments_key(dumped: dict[str, Any]) -> Optional[str]:
    for key in ("item_id", "call_id", "id"):
        value = dumped.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _apply_stream_function_arguments(
    item: dict[str, Any],
    arguments_by_item_id: dict[str, str],
) -> dict[str, Any]:
    if item.get("type") != "function_call":
        return item
    item_id = _stream_output_item_key(item)
    arguments = arguments_by_item_id.get(item_id) if item_id else None
    if not isinstance(arguments, str):
        arguments = item.get("arguments")
    normalized, valid = _responses_request_module._codex_normalized_function_arguments(
        arguments,
        empty_is_object=True,
    )
    if not valid or normalized is None:
        return item
    if item.get("arguments") == normalized:
        return item
    next_item = copy.deepcopy(item)
    next_item["arguments"] = normalized
    return next_item


_STREAM_SYNTHETIC_TEXT_MAX_CHARS = 8192


class _ResponsesStreamCompletionState:
    def __init__(self, request_data: Optional[dict] = None) -> None:
        self.created_response: Optional[dict[str, Any]] = None
        self.completed_response: Optional[dict[str, Any]] = None
        self.output_by_index: dict[int, dict[str, Any]] = {}
        self.pending_by_index: dict[int, dict[str, Any]] = {}
        self.arguments_by_item_id: dict[str, str] = {}
        self.argument_parts_by_item_id: dict[str, list[str]] = {}
        self.finished_argument_item_ids: set[str] = set()
        self.synthetic_text = ""
        self.synthetic_done_text: Optional[str] = None
        self.model = (
            request_data.get("model")
            if isinstance((request_data or {}).get("model"), str)
            else None
        )
        self.event_count = 0

    def output_index_for(self, dumped: dict[str, Any]) -> int:
        output_index = dumped.get("output_index")
        if isinstance(output_index, int):
            return output_index
        return len(self.output_by_index) + len(self.pending_by_index)

    def remember(self, chunk: Any) -> None:
        self.event_count += 1
        dumped = _stream_chunk_dump(chunk)
        if not dumped:
            return
        chunk_type = _stream_chunk_type(dumped)

        model = dumped.get("model")
        if isinstance(model, str) and model:
            self.model = model

        if chunk_type == "response.created":
            response = dumped.get("response")
            if isinstance(response, dict):
                self.created_response = copy.deepcopy(response)
        elif chunk_type in {"response.output_item.added", "response.output_item.done"}:
            self._remember_output_item(dumped, chunk_type)
        elif chunk_type in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            self._remember_function_arguments(dumped, chunk_type)
        elif _responses_stream_chunk_is_completed(dumped):
            response = dumped.get("response")
            if isinstance(response, dict):
                self.completed_response = copy.deepcopy(response)

        text_fragment, is_done_text = _stream_chunk_text_fragment(dumped)
        if text_fragment:
            if is_done_text:
                self.synthetic_done_text = text_fragment
            else:
                self._append_synthetic_text(text_fragment)

    def _remember_output_item(self, dumped: dict[str, Any], chunk_type: str) -> None:
        item = dumped.get("item")
        json_item = _jsonable(item)
        if not isinstance(json_item, dict):
            return

        index = self.output_index_for(dumped)
        if chunk_type == "response.output_item.done":
            pending_item = self.pending_by_index.get(index)
            if isinstance(pending_item, dict):
                merged_item = copy.deepcopy(pending_item)
                merged_item.update(json_item)
                json_item = merged_item

        item_id = _stream_output_item_key(json_item)
        if item_id and (
            json_item.get("type") == "function_call"
            or _responses_web_search_bridge_module._is_web_search_function_call_item(json_item)
        ):
            argument_parts = self.argument_parts_by_item_id.get(item_id)
            existing_args = json_item.get("arguments")
            if existing_args is None:
                existing_args = json_item.get("input")
            if argument_parts:
                self.arguments_by_item_id[item_id] = "".join(argument_parts)
            elif (
                item_id not in self.finished_argument_item_ids
                and existing_args is not None
                and existing_args != ""
            ):
                normalized_args, valid = (
                    _responses_request_module._codex_normalized_function_arguments(
                        existing_args,
                        empty_is_object=True,
                    )
                )
                self.arguments_by_item_id[item_id] = (
                    normalized_args if valid and normalized_args is not None else None
                )

        if chunk_type == "response.output_item.done":
            self.output_by_index[index] = _apply_stream_function_arguments(
                json_item,
                self.arguments_by_item_id,
            )
            self.pending_by_index.pop(index, None)
        elif json_item.get("type") == "function_call" or _responses_web_search_bridge_module._is_web_search_function_call_item(json_item):
            if json_item.get("type") == "function_call":
                json_item.setdefault("arguments", "")
            self.pending_by_index[index] = json_item

    def _remember_function_arguments(self, dumped: dict[str, Any], chunk_type: str) -> None:
        item_id = _stream_function_arguments_key(dumped)
        if not item_id:
            return
        if chunk_type.endswith(".delta"):
            delta = dumped.get("delta")
            if isinstance(delta, str):
                self.argument_parts_by_item_id.setdefault(item_id, []).append(delta)
            return
        arguments = dumped.get("arguments")
        if not isinstance(arguments, str):
            parts = self.argument_parts_by_item_id.get(item_id)
            if isinstance(parts, list):
                arguments = "".join(parts)
        if isinstance(arguments, str):
            normalized_args, valid = (
                _responses_request_module._codex_normalized_function_arguments(
                    arguments,
                    empty_is_object=True,
                )
            )
            self.arguments_by_item_id[item_id] = (
                normalized_args if valid and normalized_args is not None else None
            )
            self.finished_argument_item_ids.add(item_id)

    def _append_synthetic_text(self, text: str) -> None:
        self.synthetic_text = (self.synthetic_text + text)[-_STREAM_SYNTHETIC_TEXT_MAX_CHARS:]

    def synthesized_text(self) -> str:
        if isinstance(self.synthetic_done_text, str) and self.synthetic_done_text.strip():
            return self.synthetic_done_text
        return self.synthetic_text

    def completed_payload(self, request_data: Optional[dict]) -> dict[str, Any]:
        arguments_by_item_id = self.arguments_by_item_id.copy()
        for item_id, parts in self.argument_parts_by_item_id.items():
            arguments_by_item_id.setdefault(item_id, "".join(parts))

        if isinstance(self.completed_response, dict):
            completed_response = copy.deepcopy(self.completed_response)
            output = completed_response.get("output")
            if isinstance(output, list):
                completed_response["output"] = [
                    _apply_stream_function_arguments(item, arguments_by_item_id)
                    if isinstance(item, dict)
                    else item
                    for item in output
                ]
            if not completed_response.get("output") and self.output_by_index:
                completed_response["output"] = [
                    _apply_stream_function_arguments(item, arguments_by_item_id)
                    for _index, item in sorted(
                        self.output_by_index.items(),
                        key=lambda entry: entry[0],
                    )
                ]
            return _completed_response_payload(completed_response, request_data)

        response = (
            copy.deepcopy(self.created_response)
            if isinstance(self.created_response, dict)
            else {}
        )
        output_by_index = self.output_by_index.copy()
        for index, item in self.pending_by_index.items():
            output_by_index.setdefault(index, item)
        if output_by_index:
            response["output"] = [
                _apply_stream_function_arguments(item, arguments_by_item_id)
                for _index, item in sorted(output_by_index.items(), key=lambda entry: entry[0])
            ]
        else:
            text = self.synthesized_text()
            if text.strip():
                response["output_text"] = text
        return _completed_response_payload(response, request_data)

    def chunk_for_delivery(
        self,
        chunk: Any,
        request_data: Optional[dict],
    ) -> Any:
        sse_payload = _sse_stream_chunk_payload(chunk)
        dumped = (
            copy.deepcopy(sse_payload)
            if isinstance(sse_payload, dict)
            else _jsonable(chunk)
        )
        if not isinstance(dumped, dict):
            return chunk
        chunk_type = _stream_chunk_type(dumped)

        if chunk_type in {"response.output_item.added", "response.output_item.done"}:
            item = dumped.get("item")
            if not isinstance(item, dict) or item.get("type") != "function_call":
                return chunk
            output_index = self.output_index_for(dumped)
            if chunk_type == "response.output_item.added":
                normalized_item = self.pending_by_index.get(output_index, item)
                normalized_item = copy.deepcopy(normalized_item)
                # Responses clients append argument deltas to this field. Some
                # chat bridges put an empty-object placeholder here, which
                # would otherwise produce an invalid ``{}{...}`` payload.
                normalized_item["arguments"] = ""
            else:
                normalized_item = self.output_by_index.get(output_index, item)
                normalized_item = _apply_stream_function_arguments(
                    copy.deepcopy(normalized_item),
                    self.arguments_by_item_id,
                )
            dumped["item"] = normalized_item
            return _JSONStreamEvent(dumped)

        if chunk_type == "response.function_call_arguments.done":
            item_id = _stream_function_arguments_key(dumped)
            arguments = self.arguments_by_item_id.get(item_id or "")
            if isinstance(arguments, str):
                dumped["arguments"] = arguments
                return _JSONStreamEvent(dumped)
            return chunk

        if _responses_stream_chunk_is_completed(dumped):
            response = dumped.get("response")
            response_output = response.get("output") if isinstance(response, dict) else None
            known_output = list(self.output_by_index.values())
            if isinstance(response_output, list):
                known_output.extend(
                    item for item in response_output if isinstance(item, dict)
                )
            if not any(item.get("type") == "function_call" for item in known_output):
                return chunk
            dumped["response"] = self.completed_payload(request_data)
            return _JSONStreamEvent(dumped)

        return chunk


def _codex_compaction_done_item_completed_compat(
    completion_state: _ResponsesStreamCompletionState,
    request_data: dict,
    *,
    reason: str,
    exception: Optional[Exception] = None,
) -> Optional[_JSONStreamEvent]:
    """Complete a compaction stream that already delivered its encrypted item.

    Some OpenAI-compatible Responses endpoints finish remote compaction with a
    ``response.output_item.done`` compaction item and then close or stall
    without the required ``response.completed`` event.  The encrypted
    compaction item is the result Codex needs, so retrying the whole expensive
    compaction loses a valid result and can loop for minutes.  This compatibility
    path is deliberately limited to structured Codex compaction and to items
    observed in ``output_item.done`` (never merely ``output_item.added``).
    """
    if not _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        return None
    exception_body = getattr(exception, "body", None)
    terminal_event = (
        exception_body.get("terminal_event")
        if isinstance(exception_body, dict)
        else None
    )
    if isinstance(terminal_event, dict) and terminal_event.get("type") in {
        "error",
        "response.failed",
        "response.incomplete",
    }:
        return None

    completed_output_items = completion_state.output_by_index
    completed_items = [
        item for _index, item in sorted(completed_output_items.items())
    ]
    if not _codex_compaction_output_is_valid(completed_items):
        return None

    completed_event = _synthesized_completed_response_event(
        completed_output_items,
        completion_state.created_response,
        completion_state.model,
    )
    if completed_event is None:
        return None

    _trace_module._route_trace(
        "codex_compaction_missing_terminal_completed_compat",
        request_id=_routing_module._trace_request_id(request_data),
        session=_routing_module._trace_session_context(request_data),
        model_group=_responses_execution_module._request_model_group(request_data),
        reason=reason,
        buffered_events=completion_state.event_count,
        completed_output_types=[
            _responses_web_search_bridge_module._response_item_get(item, "type")
            for _index, item in sorted(completed_output_items.items())
        ],
        exception=(
            _routing_module._trace_exception(exception)
            if exception is not None
            else None
        ),
    )
    return completed_event


def _responses_stream_events_to_completed_payload(
    events: list[Any],
    request_data: Optional[dict],
) -> dict[str, Any]:
    for chunk in events:
        if _stream_chunk_type(chunk) == "response.failed":
            raise RuntimeError("responses stream ended with response.failed")
    state = _ResponsesStreamCompletionState(request_data)
    for chunk in events:
        state.remember(chunk)
    return state.completed_payload(request_data)


async def _collect_responses_stream_completed_payload(
    buffer: list[Any],
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float],
    saw_visible_output: bool,
    state: Optional[_ResponsesStreamCompletionState] = None,
) -> dict[str, Any]:
    completion_state = state or _ResponsesStreamCompletionState(request_data)
    if state is None:
        for chunk in buffer:
            completion_state.remember(chunk)
    async for chunk in _stream_with_idle_timeout(
        response,
        request_data,
        stream_started_at=stream_started_at,
        saw_visible_output=saw_visible_output,
        initial_chunk_count=completion_state.event_count or len(buffer),
    ):
        sanitized_chunk = _responses_web_search_bridge_module._sanitize_web_search_stream_chunk(chunk)
        if sanitized_chunk is None:
            continue
        completion_state.remember(sanitized_chunk)
        if _responses_stream_chunk_is_completed(sanitized_chunk):
            break
    return completion_state.completed_payload(request_data)


async def _close_async_iterator_safely(iterator: Any) -> None:
    aclose = getattr(iterator, "aclose", None)
    if not callable(aclose):
        return
    try:
        result = aclose()
        if inspect.isawaitable(result):
            await result
    except Exception:
        return


async def _non_streaming_response_as_stream(
    response: Any,
    request_data: dict,
) -> AsyncIterator[Any]:
    if _request_is_responses_stream(request_data):
        if not _responses_non_streaming_response_has_usable_output(
            response,
            request_data,
        ):
            raise _responses_incomplete_stream_exception(
                "non-streaming response without usable output",
                buffer=[response],
                request_data=request_data,
            )
        payload = _completed_response_payload(response, request_data)
        async for chunk in _computer_facade_module._external_web_search_bridge_stream(payload):
            yield chunk
        return
    yield response


async def _yield_validated_structured_codex_compaction_stream(
    buffer: List[Any],
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float] = None,
) -> AsyncIterator[Any]:
    """Buffer a remote compaction until its required encrypted result is final."""
    events: List[Any] = []
    completion_state = _ResponsesStreamCompletionState(request_data)
    raw_tool_call_text_filter = _responses_web_search_bridge_module._RawToolCallTextFilter()

    def remember(chunk: Any) -> Optional[str]:
        sanitized_chunk = _responses_web_search_bridge_module._sanitize_web_search_stream_chunk(
            chunk
        )
        if sanitized_chunk is None:
            return None
        sanitized_chunk = _responses_web_search_bridge_module._sanitize_raw_tool_call_text_stream_chunk(
            sanitized_chunk,
            raw_tool_call_text_filter,
        )
        if sanitized_chunk is None:
            return None
        if _is_structured_codex_compaction_failure_chunk(sanitized_chunk):
            events.append(sanitized_chunk)
            completion_state.remember(sanitized_chunk)
            return "failed"
        chunk_exception = _stream_chunk_priority_error_exception(
            sanitized_chunk,
            request_data,
        )
        if chunk_exception is not None:
            raise chunk_exception
        events.append(sanitized_chunk)
        completion_state.remember(sanitized_chunk)
        if _responses_stream_chunk_is_incomplete_terminal(sanitized_chunk):
            raise _responses_incomplete_stream_exception(
                "terminal response event before response.completed",
                buffer=events,
                request_data=request_data,
            )
        if not _responses_stream_chunk_is_completed(sanitized_chunk):
            return None
        if not _responses_completed_chunk_has_usable_output(
            sanitized_chunk,
            request_data,
        ):
            exception = _responses_incomplete_stream_exception(
                "structured Codex compaction completed without exactly one encrypted compaction item",
                buffer=events,
                request_data=request_data,
            )
            # The upstream completed a normal Responses exchange, but not the
            # required remote-compaction protocol. Classify this narrowly as
            # a surface incompatibility so routing moves to another deployment
            # rather than retrying the same route or downgrading to Chat.
            _routing_module._mark_exception_for_upstream_surface_failover(
                exception,
                request_data,
            )
            raise exception
        return "completed"

    for chunk in buffer:
        if remember(chunk) is not None:
            for event in events:
                yield _responses_stream_chunk_for_delivery(event)
            await _close_async_iterator_safely(response)
            return

    try:
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
            stream_started_at=stream_started_at,
            initial_chunk_count=len(buffer),
        ):
            if remember(chunk) is not None:
                for event in events:
                    yield _responses_stream_chunk_for_delivery(event)
                await _close_async_iterator_safely(response)
                return
    except Exception as exception:
        completed_compat = _codex_compaction_done_item_completed_compat(
            completion_state,
            request_data,
            reason="stream_error_after_completed_compaction_item",
            exception=exception,
        )
        if completed_compat is None:
            raise
        for event in events:
            yield _responses_stream_chunk_for_delivery(event)
        yield completed_compat
        await _close_async_iterator_safely(response)
        return

    completed_compat = _codex_compaction_done_item_completed_compat(
        completion_state,
        request_data,
        reason="clean_eof_after_completed_compaction_item",
    )
    if completed_compat is not None:
        for event in events:
            yield _responses_stream_chunk_for_delivery(event)
        yield completed_compat
        return
    raise _responses_incomplete_stream_exception(
        "stream ended before response.completed",
        buffer=events,
        request_data=request_data,
    )


async def _yield_guarded_original_stream(
    buffer: List[Any],
    response: Any,
    request_data: dict,
    *,
    saw_responses_completed: bool = False,
    stream_started_at: Optional[float] = None,
    saw_visible_output: bool = False,
    synthesize_completed_on_clean_eof_after_visible_output: bool = False,
) -> AsyncIterator[Any]:
    if not _request_is_responses_stream(request_data):
        finished_choice_indices: set[int] = set()
        saw_terminal = False
        success_recorded = False
        native_chunk_count = 0

        def inspect_native_chunk(chunk: Any) -> bool:
            nonlocal saw_terminal, success_recorded, native_chunk_count
            chunk_exception = _stream_chunk_error_exception(chunk)
            if chunk_exception is not None:
                if saw_terminal:
                    return False
                raise chunk_exception
            native_chunk_count += 1
            saw_terminal = saw_terminal or _native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            )
            if saw_terminal and not success_recorded:
                _routing_module._record_deployment_success_for_cooldown(
                    request_data
                )
                success_recorded = True
            return True

        try:
            for chunk in buffer:
                if not inspect_native_chunk(chunk):
                    return
                yield chunk
            async for chunk in _stream_with_idle_timeout(
                response,
                request_data,
                stream_started_at=stream_started_at,
                saw_visible_output=saw_visible_output,
                initial_chunk_count=len(buffer),
            ):
                if not inspect_native_chunk(chunk):
                    return
                yield chunk
        except Exception as exception:
            if saw_terminal:
                return
            if _routing_module._is_request_scoped_priority_deployment_failover_error(
                exception,
                request_data,
            ):
                _routing_module._mark_exception_for_deployment_failover(
                    exception,
                    request_data,
                )
            raise
        if not saw_terminal:
            raise _native_stream_incomplete_exception(
                request_data,
                buffered_chunks=native_chunk_count,
            )
        return

    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        async for chunk in _yield_validated_structured_codex_compaction_stream(
            buffer,
            response,
            request_data,
            stream_started_at=stream_started_at,
        ):
            yield chunk
        return

    event_tail: List[Any] = []
    visible_output_seen = saw_visible_output
    seen_output_item_ids: set[str] = set()
    pending_tool_items: dict[str, tuple[int, dict[str, Any]]] = {}
    completed_output_items: dict[int, dict[str, Any]] = {}
    internal_bridge_item_ids: set[str] = set()
    completion_state = _ResponsesStreamCompletionState(request_data)
    saw_image_generation_activity = False
    saw_web_search_call_activity = False
    saw_visible_assistant_output_after_web_search = False
    seen_web_search_activity_keys: set[str] = set()
    namespace_by_name = _responses_output_module._responses_namespace_tool_map(
        request_data.get("input"),
        request_data,
    )
    custom_tool_names = _responses_output_module._responses_custom_tool_names(
        request_data.get("input"),
        request_data,
    )
    custom_tool_item_ids: set[str] = set()
    completed_custom_tool_input_item_ids: set[str] = set()
    custom_tool_input_delta_tracker = _responses_output_module._CustomToolInputDeltaTracker()
    raw_tool_call_text_filter = _responses_web_search_bridge_module._RawToolCallTextFilter()

    def normalize_tool_bridge_chunk(chunk: Any) -> Any:
        return _responses_output_module._normalize_response_stream_tool_bridge_chunk(
            chunk,
            namespace_by_name,
            custom_tool_names,
            custom_tool_item_ids,
            custom_tool_input_delta_tracker,
        )

    def sanitize_visible_text_chunk(chunk: Any) -> Optional[Any]:
        return _responses_web_search_bridge_module._sanitize_raw_tool_call_text_stream_chunk(
            chunk,
            raw_tool_call_text_filter,
        )

    def should_consume_bridge_calls() -> bool:
        return _tools_module._request_should_consume_web_search_function_call(request_data)

    def internal_bridge_item_from_chunk(chunk: Any) -> Optional[dict[str, Any]]:
        dumped = _stream_chunk_dump(chunk)
        if _stream_chunk_type(dumped) not in {
            "response.output_item.added",
            "response.output_item.done",
        }:
            return None
        item = _jsonable(dumped.get("item"))
        if isinstance(item, dict) and _responses_web_search_bridge_module._is_web_search_function_call_item(item):
            return item
        return None

    def remember_internal_bridge_item_id(chunk: Any) -> None:
        item = internal_bridge_item_from_chunk(chunk)
        if item is None:
            return
        item_id = _stream_output_item_key(item)
        if isinstance(item_id, str) and item_id:
            internal_bridge_item_ids.add(item_id)

    def should_suppress_internal_bridge_chunk(chunk: Any) -> bool:
        if not should_consume_bridge_calls():
            return False
        if internal_bridge_item_from_chunk(chunk) is not None:
            return True
        dumped = _stream_chunk_dump(chunk)
        if _stream_chunk_type(dumped) not in {
            "response.function_call_arguments.delta",
            "response.function_call_arguments.done",
        }:
            return False
        item_id = _stream_function_arguments_key(dumped)
        return isinstance(item_id, str) and item_id in internal_bridge_item_ids

    def bridge_payload_from_state(*, require_finished_arguments: bool) -> Optional[dict[str, Any]]:
        if not should_consume_bridge_calls():
            return None
        if require_finished_arguments and not (
            internal_bridge_item_ids & completion_state.finished_argument_item_ids
        ):
            return None
        payload = completion_state.completed_payload(request_data)
        if _responses_web_search_bridge_module._has_web_search_actions_for_request(payload, request_data):
            return payload
        return None

    def bridge_payload_for_chunk(chunk: Any) -> Optional[dict[str, Any]]:
        if not should_consume_bridge_calls():
            return None
        if _responses_web_search_bridge_module._has_web_search_actions_for_request(chunk, request_data):
            return completion_state.completed_payload(request_data)
        dumped = _stream_chunk_dump(chunk)
        if _stream_chunk_type(dumped) in {
            "response.function_call_arguments.done",
            "response.output_item.done",
        }:
            return bridge_payload_from_state(require_finished_arguments=True)
        return None

    async def yield_resolved_bridge_stream(
        trigger_chunk: Any,
        payload: dict[str, Any],
    ) -> AsyncIterator[Any]:
        actions = _responses_web_search_bridge_module._web_search_actions_for_request(payload, request_data)
        _trace_module._route_trace(
            "external_web_search_bridge_guarded_stream_function_call_intercept",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            deployment_id=_routing_module._deployment_id_from_request(request_data),
            route_key=_routing_module._deployment_route_key_from_request(request_data),
            request=_trace_module._trace_request_summary(request_data),
            response=_trace_module._trace_response_summary(payload, request_data),
            actions=actions,
            stream_event_count=completion_state.event_count,
        )
        original_function = _responses_execution_module._responses_bridge_original_function(request_data)
        async for resolved_chunk in _computer_facade_module._resolve_web_search_function_calls_stream_rounds(
            payload,
            request_data,
            original_function,
        ):
            yield resolved_chunk

    def remember(chunk: Any) -> None:
        nonlocal saw_image_generation_activity, saw_web_search_call_activity
        nonlocal saw_visible_assistant_output_after_web_search
        event_tail.append(chunk)
        if len(event_tail) > _STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS:
            del event_tail[0]
        completion_state.remember(chunk)
        remember_internal_bridge_item_id(chunk)
        saw_image_generation_activity = (
            saw_image_generation_activity
            or _image_generation_module._response_has_image_generation_activity(chunk)
        )
        _remember_stream_output_item_ids(
            chunk,
            seen_output_item_ids,
            pending_tool_items,
            completed_output_items,
        )
        dumped = _stream_chunk_dump(chunk)
        if _stream_chunk_type(dumped) == "response.custom_tool_call_input.done":
            item_id = dumped.get("item_id") or dumped.get("id")
            input_text = dumped.get("input")
            if isinstance(item_id, str) and item_id and isinstance(input_text, str):
                completed_custom_tool_input_item_ids.add(item_id)
                for _output_index, item in pending_tool_items.values():
                    if item_id in _stream_output_item_identity_keys(item):
                        item["input"] = input_text
        if _responses_stream_chunk_is_completed(chunk):
            _remember_completed_response_output_items(
                chunk,
                completed_output_items,
            )
        web_search_activity_keys = stream_chunk_web_search_activity_keys(chunk)
        chunk_has_web_search = bool(web_search_activity_keys) or stream_chunk_has_web_search_call_activity(chunk)
        if web_search_activity_keys:
            new_web_search_keys = web_search_activity_keys - seen_web_search_activity_keys
            seen_web_search_activity_keys.update(web_search_activity_keys)
        else:
            new_web_search_keys = set()
        if new_web_search_keys or (chunk_has_web_search and not web_search_activity_keys):
            _routing_module._mark_external_web_search_started_for_request(request_data)
            saw_web_search_call_activity = True
            saw_visible_assistant_output_after_web_search = False
        if saw_web_search_call_activity and stream_chunk_has_visible_assistant_text_output(chunk):
            saw_visible_assistant_output_after_web_search = True

    def synthesized_text() -> str:
        return completion_state.synthesized_text()

    def completed_output_is_tool_call_only() -> bool:
        return bool(completed_output_items) and all(
            _stream_output_item_is_tool_call(_responses_web_search_bridge_module._response_item_get(item, "type"))
            for item in completed_output_items.values()
        )

    def completed_output_has_visible_assistant_text() -> bool:
        return any(
            _response_item_has_assistant_text(item)
            for item in completed_output_items.values()
        )

    def output_item_is_web_search_only_tool(item: Any) -> bool:
        json_item = _jsonable(item)
        if not isinstance(json_item, dict):
            return False
        if json_item.get("type") == "web_search_call":
            return True
        return _responses_web_search_bridge_module._is_web_search_function_call_item(json_item)

    def output_item_has_visible_assistant_text(item: Any) -> bool:
        return _response_item_has_assistant_text(item)

    def web_search_activity_key_from_item(item: Any, output_index: Any = None) -> Optional[str]:
        json_item = _jsonable(item)
        if not isinstance(json_item, dict) or not output_item_is_web_search_only_tool(json_item):
            return None
        item_id = _stream_output_item_key(json_item)
        if isinstance(item_id, str) and item_id:
            return f"id:{item_id}"
        action = web_search_call_action_from_item(json_item)
        if action is not None:
            return "action:" + _responses_web_search_bridge_module._external_web_search_action_key(action)
        if isinstance(output_index, int):
            return f"output_index:{output_index}"
        return None

    def stream_chunk_web_search_activity_keys(chunk: Any) -> set[str]:
        dumped = _stream_chunk_dump(chunk)
        chunk_type = _stream_chunk_type(dumped)
        keys: set[str] = set()
        if chunk_type in {"response.output_item.added", "response.output_item.done"}:
            key = web_search_activity_key_from_item(dumped.get("item"), dumped.get("output_index"))
            if key:
                keys.add(key)
            return keys
        if chunk_type == "response.web_search_call.completed":
            item_id = dumped.get("item_id") or dumped.get("id") or dumped.get("call_id")
            if isinstance(item_id, str) and item_id:
                keys.add(f"id:{item_id}")
                return keys
            action = dumped.get("action")
            if isinstance(action, dict):
                action_type = _responses_web_search_bridge_module._external_web_search_action_name(action.get("type"))
                keyed_action: Optional[dict[str, str]] = None
                if action_type == "search":
                    query = action.get("query")
                    if isinstance(query, str) and query.strip():
                        keyed_action = {"type": "search", "query": query.strip()}
                elif action_type == "openPage":
                    url = action.get("url")
                    if isinstance(url, str) and url.strip():
                        keyed_action = {"type": "openPage", "url": url.strip()}
                elif action_type == "findInPage":
                    url = action.get("url")
                    pattern = action.get("pattern") or action.get("text") or action.get("needle")
                    if isinstance(url, str) and url.strip() and isinstance(pattern, str) and pattern.strip():
                        keyed_action = {"type": "findInPage", "url": url.strip(), "pattern": pattern.strip()}
                if keyed_action is not None:
                    keys.add("action:" + _responses_web_search_bridge_module._external_web_search_action_key(keyed_action))
                    return keys
            output_index = dumped.get("output_index")
            if isinstance(output_index, int):
                keys.add(f"output_index:{output_index}")
            return keys
        if _responses_stream_chunk_is_completed(dumped):
            response = dumped.get("response")
            output = response.get("output") if isinstance(response, dict) else None
            if isinstance(output, list):
                for output_index, item in enumerate(output):
                    key = web_search_activity_key_from_item(item, output_index)
                    if key:
                        keys.add(key)
            return keys
        return keys

    def stream_chunk_has_web_search_call_activity(chunk: Any) -> bool:
        dumped = _stream_chunk_dump(chunk)
        chunk_type = _stream_chunk_type(dumped)
        if chunk_type in {"response.output_item.added", "response.output_item.done"}:
            return output_item_is_web_search_only_tool(dumped.get("item"))
        if chunk_type == "response.web_search_call.completed":
            return True
        if _responses_stream_chunk_is_completed(dumped):
            response = dumped.get("response")
            output = response.get("output") if isinstance(response, dict) else None
            return isinstance(output, list) and any(
                output_item_is_web_search_only_tool(item) for item in output
            )
        return False

    def stream_chunk_has_visible_assistant_text_output(chunk: Any) -> bool:
        dumped = _stream_chunk_dump(chunk)
        chunk_type = _stream_chunk_type(dumped)
        text_fragment, _is_done_text = _stream_chunk_text_fragment(dumped)
        if text_fragment.strip():
            return True
        if chunk_type in {"response.output_item.added", "response.output_item.done"}:
            return output_item_has_visible_assistant_text(dumped.get("item"))
        if _responses_stream_chunk_is_completed(dumped):
            response = dumped.get("response")
            return _responses_web_search_bridge_module._external_web_search_has_completed_assistant_message(
                response,
            )
        return False

    def completed_output_is_web_search_call_only() -> bool:
        if completed_output_has_visible_assistant_text() or synthesized_text().strip():
            return False
        items = [item for item in completed_output_items.values() if isinstance(item, dict)]
        items.extend(
            item
            for _index, item in pending_tool_items.values()
            if isinstance(item, dict)
        )
        return bool(items) and all(output_item_is_web_search_only_tool(item) for item in items)

    def completed_output_has_web_search_call() -> bool:
        items = [item for item in completed_output_items.values() if isinstance(item, dict)]
        items.extend(
            item
            for _index, item in pending_tool_items.values()
            if isinstance(item, dict)
        )
        return any(output_item_is_web_search_only_tool(item) for item in items)

    def missing_answer_after_web_search() -> bool:
        return (
            saw_web_search_call_activity
            and not saw_visible_assistant_output_after_web_search
            and not completed_output_has_visible_assistant_text()
            and completed_output_is_web_search_call_only()
        )

    def external_web_search_recovery_allowed(exception: Optional[Exception]) -> bool:
        if _responses_web_search_bridge_module._external_web_search_is_recovery_payload(
            request_data,
        ):
            return True
        metadata = _request_context_module._request_metadata_dict(
            request_data,
            "litellm_metadata",
        ) or {}
        if metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
            return True
        if _responses_web_search_bridge_module._external_web_search_has_recovery_context(
            request_data,
            exception,
        ):
            return True
        if exception is None:
            return False
        return _routing_module._is_native_responses_web_search_unsupported_error(
            exception,
            request_data,
        )

    def web_search_missing_answer_failed_event(
        reason: str,
        exception: Optional[Exception] = None,
    ) -> _JSONStreamEvent:
        if exception is not None:
            _routing_module._mark_exception_for_deployment_failover(
                exception,
                request_data,
            )
        return failed_terminal_event(reason, exception)

    def current_completed_output() -> list[dict[str, Any]]:
        return [
            copy.deepcopy(item)
            for _index, item in sorted(completed_output_items.items(), key=lambda entry: entry[0])
            if isinstance(item, dict)
        ]

    def web_search_call_action_from_item(item: dict[str, Any]) -> Optional[dict[str, str]]:
        if item.get("type") != "web_search_call":
            return None
        raw_action = item.get("action")
        raw_action = raw_action if isinstance(raw_action, dict) else {}
        bridge_action = raw_action.get("bridge_action")
        lookup_action = bridge_action if isinstance(bridge_action, dict) else raw_action
        clean_item = _responses_web_search_bridge_module._sanitize_web_search_call_item(item)
        if not isinstance(clean_item, dict):
            return None
        clean_action = clean_item.get("action")
        if not isinstance(clean_action, dict):
            return None
        action_type = _responses_web_search_bridge_module._external_web_search_action_name(
            lookup_action.get("type") or clean_action.get("type")
        )
        if action_type == "openPage":
            url = lookup_action.get("url") or clean_action.get("url")
            if isinstance(url, str) and url.strip():
                return {"type": "openPage", "url": url.strip()}
            return None
        if action_type == "findInPage":
            url = lookup_action.get("url") or clean_action.get("url")
            pattern = (
                lookup_action.get("pattern")
                or lookup_action.get("text")
                or lookup_action.get("needle")
                or clean_action.get("pattern")
                or clean_action.get("text")
                or clean_action.get("needle")
            )
            if (
                isinstance(url, str)
                and url.strip()
                and isinstance(pattern, str)
                and pattern.strip()
            ):
                return {
                    "type": "findInPage",
                    "url": url.strip(),
                    "pattern": pattern.strip(),
                }
            return None
        query = lookup_action.get("query") or clean_action.get("query") or clean_item.get("query")
        if isinstance(query, str) and query.strip():
            return {"type": "search", "query": query.strip()}
        return None

    def web_search_call_actions_from_output() -> list[dict[str, str]]:
        actions: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in current_completed_output():
            action = web_search_call_action_from_item(item)
            if action is None:
                continue
            key = _responses_web_search_bridge_module._external_web_search_action_key(action)
            if key in seen:
                continue
            seen.add(key)
            actions.append(action)
        return actions

    async def hidden_web_search_evidence_for_recovery() -> tuple[
        str,
        list[str],
        list[dict[str, str]],
        list[str],
    ]:
        actions = web_search_call_actions_from_output()
        if not actions:
            return "", [], [], []
        try:
            run_search_action = _responses_web_search_bridge_module._external_web_search_run_action
            page_cache: dict[str, str] = {}
            page_fetch_tasks: dict[str, asyncio.Task[str]] = {}
            action_results = [
                await run_search_action(action, page_cache, page_fetch_tasks)
                for action in actions
            ]
        except Exception as exc:
            _trace_module._route_trace(
                "responses_web_search_call_recovery_evidence_error",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(request_data),
                actions=actions,
                exception=_routing_module._trace_exception(exc),
            )
            return (
                "",
                _responses_web_search_bridge_module._external_web_search_action_labels(actions),
                actions,
                [],
            )
        sections = [section for section, _urls, _action in action_results]
        completed_actions = [action for _section, _urls, action in action_results]
        source_urls: list[str] = []
        for _section, urls, _action in action_results:
            for url in urls:
                if url not in source_urls:
                    source_urls.append(url)
        message = "\n\n".join(section for section in sections if section.strip())
        labels = _responses_web_search_bridge_module._external_web_search_action_labels(completed_actions or actions)
        _trace_module._route_trace(
            "responses_web_search_call_recovery_evidence_collected",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            actions=labels,
            evidence_chars=len(message or ""),
        )
        return message, labels, completed_actions or actions, source_urls

    def visible_message_items_from_payload(payload: Any) -> list[dict[str, Any]]:
        json_payload = _jsonable(payload)
        output = json_payload.get("output") if isinstance(json_payload, dict) else None
        if not isinstance(output, list):
            return []
        messages: list[dict[str, Any]] = []
        for item in output:
            if not isinstance(item, dict) or item.get("type") != "message":
                continue
            if _responses_output_module._response_text(item).strip():
                messages.append(item)
        return messages

    def message_stream_events(item: dict[str, Any], index: int) -> list[_JSONStreamEvent]:
        content_items = item.get("content")
        content = (
            content_items[0]
            if isinstance(content_items, list)
            and content_items
            and isinstance(content_items[0], dict)
            else {"type": "output_text", "text": "", "annotations": []}
        )
        text = content.get("text") if isinstance(content.get("text"), str) else ""
        added_message = copy.deepcopy(item)
        added_message["status"] = "in_progress"
        added_message["content"] = []
        events: list[dict[str, Any]] = [
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": added_message,
            },
            {
                "type": "response.content_part.added",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ]
        if text:
            events.append(
                {
                    "type": "response.output_text.delta",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "delta": text,
                }
            )
        events.extend(
            [
                {
                    "type": "response.output_text.done",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "text": text,
                },
                {
                    "type": "response.content_part.done",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "part": content,
                },
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                },
            ]
        )
        return [_JSONStreamEvent(event) for event in events]

    def search_tool_missing_answer_exception(
        reason: str,
        exception: Optional[Exception] = None,
    ) -> Exception:
        recovery_request = (
            _responses_web_search_bridge_module._external_web_search_recovery_request_from_exception(
                exception,
            )
            if exception is not None
            else None
        )
        if exception is not None and _external_web_search_recovery_poll_error(exception):
            _routing_module._mark_exception_for_deployment_failover(exception, request_data)
            return exception
        recovery_exception = RuntimeError(
            f"Responses web_search stream ended without a visible assistant answer: {reason}"
        )
        if isinstance(recovery_request, dict):
            _responses_web_search_bridge_module._external_web_search_set_recovery_request(
                recovery_exception,
                recovery_request,
            )
        try:
            recovery_exception.status_code = 503  # type: ignore[attr-defined]
        except Exception:
            pass
        try:
            recovery_exception.body = {  # type: ignore[attr-defined]
                "reason": "responses_web_search_missing_final_answer",
                "stream_reason": reason,
            }
        except Exception:
            pass
        _routing_module._mark_exception_for_deployment_failover(recovery_exception, request_data)
        return recovery_exception

    async def yield_recovered_search_tool_answer(
        reason: str,
        exception: Optional[Exception] = None,
    ) -> AsyncIterator[Any]:
        if _responses_web_search_bridge_module._request_has_provider_native_web_search_event(
            request_data
        ):
            yield web_search_missing_answer_failed_event(reason, exception)
            return
        if not external_web_search_recovery_allowed(exception):
            yield web_search_missing_answer_failed_event(reason, exception)
            return
        recovery_exception = search_tool_missing_answer_exception(reason, exception)
        bridge_payload = bridge_payload_from_state(require_finished_arguments=False)
        if bridge_payload is not None:
            try:
                async for resolved_chunk in yield_resolved_bridge_stream(
                    bridge_payload,
                    bridge_payload,
                ):
                    yield resolved_chunk
                return
            except Exception as bridge_exc:
                recovery_exception = search_tool_missing_answer_exception(
                    "bridge resolution after web_search-only stream failed",
                    bridge_exc,
                )

        evidence_task = asyncio.create_task(hidden_web_search_evidence_for_recovery())
        try:
            while True:
                try:
                    (
                        search_results,
                        completed_labels,
                        completed_actions_for_recovery,
                        source_urls_for_recovery,
                    ) = await asyncio.wait_for(
                        asyncio.shield(evidence_task),
                        timeout=_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS,
                    )
                    break
                except asyncio.TimeoutError:
                    yield _route_recovery_sse_keepalive(
                        0,
                        request_data=request_data,
                        phase="web_search_evidence",
                    )
        finally:
            if not evidence_task.done():
                evidence_task.cancel()
        recovered_payload: Optional[dict[str, Any]] = None
        if (
            _routing_module._recovery_max_seconds_for_request(request_data) > 0
            and (
                _external_web_search_recovery_poll_error(recovery_exception)
                or search_results.strip()
            )
        ):
            events: list[Any] = []
            try:
                if search_results.strip():
                    existing_recovery_request = (
                        _responses_web_search_bridge_module._external_web_search_recovery_request_from_exception(
                            recovery_exception
                        )
                        or _responses_web_search_bridge_module._external_web_search_pending_recovery_request(
                            request_data
                        )
                    )
                    if (
                        _responses_web_search_bridge_module._external_web_search_recovery_payload_phase(
                            existing_recovery_request
                        )
                        == "continuation"
                    ):
                        recovery_request = (
                            _responses_web_search_bridge_module._external_web_search_recovery_kwargs(
                                request_data,
                                search_results=search_results,
                                exception=recovery_exception,
                            )
                        )
                    else:
                        continuation_request = (
                            _responses_web_search_bridge_module._external_web_search_prepare_continuation_recovery_request(
                                request_kwargs=request_data,
                                search_results=search_results,
                                queries=completed_labels,
                                completed_actions=completed_actions_for_recovery,
                                round_number=1,
                            )
                        )
                        _responses_web_search_bridge_module._external_web_search_set_recovery_request(
                            recovery_exception,
                            continuation_request,
                        )
                        recovery_request = continuation_request
                else:
                    recovery_request = (
                        _responses_web_search_bridge_module._external_web_search_safe_request_base(
                            request_data
                        )
                    )
                    recovery_request["stream"] = True
                async for recovery_chunk in _stream_route_recovery_poll(
                    recovery_request,
                    recovery_exception,
                ):
                    if _is_route_recovery_sse_keepalive(recovery_chunk):
                        yield recovery_chunk
                        continue
                    events.append(recovery_chunk)
            except Exception as recovery_exc:
                _trace_module._route_trace(
                    "responses_web_search_call_route_recovery_error",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    request=_trace_module._trace_request_summary(request_data),
                    recovery_request=(
                        _trace_module._trace_request_summary(recovery_request)
                        if "recovery_request" in locals()
                        else None
                    ),
                    original_exception=_routing_module._trace_exception(
                        recovery_exception
                    ),
                    exception=_routing_module._trace_exception(recovery_exc),
                )
                recovery_exception = search_tool_missing_answer_exception(
                    "route recovery after web_search-only stream failed",
                    recovery_exc,
                )
            if events:
                try:
                    recovered_payload = _responses_stream_events_to_completed_payload(
                        events,
                        recovery_request,
                    )
                except Exception:
                    recovered_payload = None
        if not isinstance(recovered_payload, dict):
            yield _synthesized_failed_response_event(request_data, recovery_exception)
            return

        final_output = current_completed_output()
        for item in recovered_payload.get("output", []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "web_search_call" or _responses_web_search_bridge_module._is_web_search_function_call_item(item):
                continue
            item = _responses_web_search_bridge_module._final_answer_message_item(item)
            final_output.append(item)
            if item.get("type") == "message":
                index = len(final_output) - 1
                for event in message_stream_events(item, index):
                    yield event
        recovered_payload = copy.deepcopy(recovered_payload)
        recovered_payload["status"] = "completed"
        recovered_payload["output"] = final_output
        yield _JSONStreamEvent({"type": "response.completed", "response": recovered_payload})

    def terminal_completed_output_items(
        *,
        allow_tool_call_only: bool = False,
    ) -> Optional[dict[int, dict[str, Any]]]:
        if not completed_output_items:
            return completed_output_items
        if completed_output_has_visible_assistant_text():
            return completed_output_items
        if completed_output_is_tool_call_only():
            if allow_tool_call_only:
                return completed_output_items
            if synthesized_text().strip():
                return {}
            return None
        if synthesized_text().strip():
            return {}
        return None

    def prune_internal_bridge_items() -> None:
        for item_id in list(internal_bridge_item_ids):
            pending_tool_items.pop(item_id, None)
        for output_index, item in list(completed_output_items.items()):
            if _responses_web_search_bridge_module._is_web_search_function_call_item(item):
                completed_output_items.pop(output_index, None)

    def completed_terminal_event(
        reason: str,
        *,
        allow_tool_call_only: bool = False,
    ) -> Optional[_JSONStreamEvent]:
        terminal_items = terminal_completed_output_items(
            allow_tool_call_only=allow_tool_call_only,
        )
        if terminal_items is None:
            return None
        completed = _synthesized_completed_response_event(
            terminal_items,
            completion_state.created_response,
            completion_state.model,
            fallback_text=synthesized_text(),
        )
        if completed is None:
            return None
        _trace_module._route_trace(
            "responses_active_stream_synthesized_completed_event",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            reason=reason,
            already_yielded_visible_output=True,
        )
        return completed

    def completed_image_generation_terminal_event(
        reason: str,
    ) -> Optional[_JSONStreamEvent]:
        if not _image_generation_module._response_has_image_generation_result_payload(
            list(completed_output_items.values())
        ):
            return None
        return completed_terminal_event(reason, allow_tool_call_only=True)

    def completed_custom_tool_terminal_events() -> list[_JSONStreamEvent]:
        if completed_output_has_visible_assistant_text() or synthesized_text().strip():
            return []
        completed_custom_tool_items = [
            item
            for item in completed_output_items.values()
            if item.get("type") == "custom_tool_call"
        ]
        if not pending_tool_items and not completed_custom_tool_items:
            return []
        if any(
            item.get("type") not in {"reasoning", "custom_tool_call"}
            for item in completed_output_items.values()
        ):
            return []
        for _output_index, item in pending_tool_items.values():
            if item.get("type") != "custom_tool_call":
                return []
            if not any(
                item_id in completed_custom_tool_input_item_ids
                for item_id in _stream_output_item_identity_keys(item)
            ):
                return []
        if pending_tool_items:
            events = _synthesized_pending_tool_completion_events(
                pending_tool_items,
                completed_output_items,
                completion_state.created_response,
                completion_state.model,
            )
        else:
            completed = _synthesized_completed_response_event(
                completed_output_items,
                completion_state.created_response,
                completion_state.model,
            )
            events = [completed] if completed is not None else []
        if events:
            _trace_module._route_trace(
                "responses_active_stream_synthesized_completed_event",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(request_data),
                reason="completed custom tool call before terminal response event",
                already_yielded_visible_output=True,
            )
        return events

    def failed_terminal_event(reason: str, exception: Optional[Exception] = None) -> _JSONStreamEvent:
        failure = exception or _responses_incomplete_stream_exception(reason, buffer=event_tail)
        # Once part of a Responses stream has reached Codex, replaying that
        # stream could duplicate a tool call or visible output.  Keep the
        # terminal failure for this response, but quarantine the route before
        # Codex reconnects so the next attempt selects a healthy deployment.
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            failure,
            request_data,
        ):
            _routing_module._mark_exception_for_deployment_failover(
                failure,
                request_data,
            )
        _trace_module._route_trace(
            "responses_active_stream_failed_terminal_event",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            exception=_routing_module._trace_exception(failure),
            reason=reason,
            already_yielded_visible_output=True,
        )
        return _synthesized_failed_response_event(request_data, failure)

    for chunk in buffer:
        _responses_web_search_bridge_module._mark_provider_native_web_search_event(
            request_data, chunk
        )
        sanitized_chunk = _responses_web_search_bridge_module._sanitize_web_search_stream_chunk(chunk)
        if sanitized_chunk is None:
            continue
        sanitized_chunk = sanitize_visible_text_chunk(sanitized_chunk)
        if sanitized_chunk is None:
            continue
        chunk = normalize_tool_bridge_chunk(sanitized_chunk)
        if chunk is None:
            continue
        remember(chunk)
        visible_output_seen = visible_output_seen or _stream_chunk_has_visible_output(chunk)
        bridge_payload = bridge_payload_for_chunk(chunk)
        if bridge_payload is not None:
            async for resolved_chunk in yield_resolved_bridge_stream(chunk, bridge_payload):
                yield resolved_chunk
            return
        completed_compat_chunk = _responses_output_limit_incomplete_as_completed_chunk(
            chunk,
            request_data,
        )
        if completed_compat_chunk is not None:
            chunk = completed_compat_chunk
            remember(chunk)
        if _responses_stream_chunk_is_completed(chunk):
            if _responses_completed_chunk_is_empty(chunk, request_data) and not visible_output_seen:
                break
            saw_responses_completed = True
            visible_output_seen = visible_output_seen or _responses_completed_chunk_has_usable_output(
                chunk,
                request_data,
            )
            for synthetic_chunk in _synthesized_missing_completed_tool_events(
                chunk,
                seen_output_item_ids,
                pending_tool_items,
            ):
                remember(synthetic_chunk)
                yield synthetic_chunk
            if missing_answer_after_web_search():
                async for recovered_chunk in yield_recovered_search_tool_answer(
                    "completed response event after web_search without final answer",
                ):
                    yield recovered_chunk
                return
            if should_suppress_internal_bridge_chunk(chunk):
                await _close_async_iterator_safely(response)
                return
            yield _responses_stream_chunk_for_delivery(
                completion_state.chunk_for_delivery(chunk, request_data),
                request_data,
            )
            await _close_async_iterator_safely(response)
            return
        elif _responses_stream_chunk_is_incomplete_terminal(chunk):
            if missing_answer_after_web_search():
                async for recovered_chunk in yield_recovered_search_tool_answer(
                    "terminal response event after web_search-only output",
                ):
                    yield recovered_chunk
                return
            terminal_events = completed_custom_tool_terminal_events()
            for synthetic_chunk in terminal_events:
                remember(synthetic_chunk)
                yield synthetic_chunk
            if terminal_events:
                return
            output_limit_terminal = (
                _responses_incomplete_terminal_reason(chunk)
                in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS
            )
            if output_limit_terminal:
                completed = completed_terminal_event(
                    "output token limit terminal response event",
                    allow_tool_call_only=True,
                )
                if completed is not None:
                    remember(completed)
                    yield completed
                    return
                yield failed_terminal_event(
                    "output token limit terminal response event without usable output"
                )
                return
            if (
                not completed_output_has_visible_assistant_text()
                and not synthesized_text().strip()
            ):
                completed = completed_terminal_event(
                    "tool-call terminal response event before response.completed",
                    allow_tool_call_only=True,
                )
                if completed is not None:
                    remember(completed)
                    yield completed
                    return
            if visible_output_seen:
                yield failed_terminal_event("terminal response event before response.completed")
                return
            raise _responses_incomplete_stream_exception(
                "terminal response event before response.completed",
                buffer=event_tail,
                request_data=request_data,
            )
        if should_suppress_internal_bridge_chunk(chunk):
            continue
        yield _responses_stream_chunk_for_delivery(
            completion_state.chunk_for_delivery(chunk, request_data),
            request_data,
        )

    try:
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
            stream_started_at=stream_started_at,
            saw_visible_output=visible_output_seen,
            initial_chunk_count=len(buffer),
        ):
            _responses_web_search_bridge_module._mark_provider_native_web_search_event(
                request_data, chunk
            )
            sanitized_chunk = _responses_web_search_bridge_module._sanitize_web_search_stream_chunk(chunk)
            if sanitized_chunk is None:
                continue
            sanitized_chunk = sanitize_visible_text_chunk(sanitized_chunk)
            if sanitized_chunk is None:
                continue
            chunk = normalize_tool_bridge_chunk(sanitized_chunk)
            if chunk is None:
                continue
            chunk_exception = _stream_chunk_priority_error_exception(
                chunk,
                request_data,
            )
            if chunk_exception is not None:
                if saw_image_generation_activity:
                    completed = completed_image_generation_terminal_event(
                        "stream error after completed image generation output"
                    )
                    if completed is not None:
                        remember(completed)
                        yield completed
                        return
                if missing_answer_after_web_search():
                    async for recovered_chunk in yield_recovered_search_tool_answer(
                        "stream error after web_search-only output",
                        chunk_exception,
                    ):
                        yield recovered_chunk
                    return
                terminal_events = completed_custom_tool_terminal_events()
                for synthetic_chunk in terminal_events:
                    remember(synthetic_chunk)
                    yield synthetic_chunk
                if terminal_events:
                    return
                if visible_output_seen and not saw_responses_completed:
                    yield failed_terminal_event("stream error after visible output", chunk_exception)
                    return
                raise chunk_exception
            remember(chunk)
            visible_output_seen = visible_output_seen or _stream_chunk_has_visible_output(chunk)
            bridge_payload = bridge_payload_for_chunk(chunk)
            if bridge_payload is not None:
                async for resolved_chunk in yield_resolved_bridge_stream(chunk, bridge_payload):
                    yield resolved_chunk
                return
            completed_compat_chunk = _responses_output_limit_incomplete_as_completed_chunk(
                chunk,
                request_data,
            )
            if completed_compat_chunk is not None:
                chunk = completed_compat_chunk
                remember(chunk)
            if _responses_stream_chunk_is_completed(chunk):
                if _responses_completed_chunk_is_empty(chunk, request_data) and not visible_output_seen:
                    break
                saw_responses_completed = True
                for synthetic_chunk in _synthesized_missing_completed_tool_events(
                    chunk,
                    seen_output_item_ids,
                    pending_tool_items,
                ):
                    remember(synthetic_chunk)
                    yield synthetic_chunk
                if missing_answer_after_web_search():
                    async for recovered_chunk in yield_recovered_search_tool_answer(
                        "completed response event after web_search without final answer",
                    ):
                        yield recovered_chunk
                    return
                if should_suppress_internal_bridge_chunk(chunk):
                    await _close_async_iterator_safely(response)
                    return
                yield _responses_stream_chunk_for_delivery(
                    completion_state.chunk_for_delivery(chunk, request_data),
                    request_data,
                )
                await _close_async_iterator_safely(response)
                return
            elif _responses_stream_chunk_is_incomplete_terminal(chunk):
                if missing_answer_after_web_search():
                    async for recovered_chunk in yield_recovered_search_tool_answer(
                        "terminal response event after web_search-only output",
                    ):
                        yield recovered_chunk
                    return
                terminal_events = completed_custom_tool_terminal_events()
                for synthetic_chunk in terminal_events:
                    remember(synthetic_chunk)
                    yield synthetic_chunk
                if terminal_events:
                    return
                output_limit_terminal = (
                    _responses_incomplete_terminal_reason(chunk)
                    in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS
                )
                if output_limit_terminal:
                    completed = completed_terminal_event(
                        "output token limit terminal response event",
                        allow_tool_call_only=True,
                    )
                    if completed is not None:
                        remember(completed)
                        yield completed
                        return
                    yield failed_terminal_event(
                        "output token limit terminal response event without usable output"
                    )
                    return
                if (
                    not completed_output_has_visible_assistant_text()
                    and not synthesized_text().strip()
                ):
                    completed = completed_terminal_event(
                        "tool-call terminal response event before response.completed",
                        allow_tool_call_only=True,
                    )
                    if completed is not None:
                        remember(completed)
                        yield completed
                        return
                if visible_output_seen:
                    yield failed_terminal_event("terminal response event before response.completed")
                    return
                raise _responses_incomplete_stream_exception(
                    "terminal response event before response.completed",
                    buffer=event_tail,
                    request_data=request_data,
                )
            if should_suppress_internal_bridge_chunk(chunk):
                continue
            yield _responses_stream_chunk_for_delivery(
                completion_state.chunk_for_delivery(chunk, request_data),
                request_data,
            )
    except Exception as exc:
        if saw_image_generation_activity:
            completed = completed_image_generation_terminal_event(
                "stream error after completed image generation output"
            )
            if completed is not None:
                remember(completed)
                yield completed
                return
        if missing_answer_after_web_search():
            async for recovered_chunk in yield_recovered_search_tool_answer(
                "stream error after web_search-only output",
                exc,
            ):
                yield recovered_chunk
            return
        terminal_events = completed_custom_tool_terminal_events()
        for synthetic_chunk in terminal_events:
            remember(synthetic_chunk)
            yield synthetic_chunk
        if terminal_events:
            return
        if visible_output_seen and not saw_responses_completed:
            yield failed_terminal_event("stream error after visible output", exc)
            return
        raise

    if not saw_responses_completed and saw_image_generation_activity:
        completed = completed_image_generation_terminal_event(
            "image generation output without response.completed"
        )
        if completed is not None:
            remember(completed)
            yield completed
        return

    if not saw_responses_completed:
        bridge_payload = bridge_payload_from_state(require_finished_arguments=True)
        if bridge_payload is not None:
            async for resolved_chunk in yield_resolved_bridge_stream(
                bridge_payload,
                bridge_payload,
            ):
                yield resolved_chunk
            return

    prune_internal_bridge_items()

    if not saw_responses_completed:
        terminal_events = completed_custom_tool_terminal_events()
        for synthetic_chunk in terminal_events:
            remember(synthetic_chunk)
            yield synthetic_chunk
        if terminal_events:
            return

    if not saw_responses_completed and pending_tool_items:
        if missing_answer_after_web_search():
            async for recovered_chunk in yield_recovered_search_tool_answer(
                "stream ended before response.completed",
            ):
                yield recovered_chunk
            return
        for synthetic_chunk in _synthesized_pending_tool_completion_events(
            pending_tool_items,
            completed_output_items,
            completion_state.created_response,
            completion_state.model,
        ):
            remember(synthetic_chunk)
            yield synthetic_chunk
        return

    if not saw_responses_completed:
        if missing_answer_after_web_search():
            async for recovered_chunk in yield_recovered_search_tool_answer(
                "stream ended before response.completed",
            ):
                yield recovered_chunk
            return
        if visible_output_seen:
            if synthesize_completed_on_clean_eof_after_visible_output:
                terminal_items = terminal_completed_output_items()
                synthetic_completed = _synthesized_completed_response_event(
                    terminal_items,
                    completion_state.created_response,
                    completion_state.model,
                    fallback_text=synthesized_text(),
                )
                remember(synthetic_completed)
                yield synthetic_completed
                return
            yield failed_terminal_event("stream ended before response.completed")
            return
        terminal_items = terminal_completed_output_items()
        synthetic_completed = (
            _synthesized_completed_response_event(
                terminal_items,
                completion_state.created_response,
                completion_state.model,
                fallback_text=synthesized_text(),
            )
            if terminal_items is not None
            else None
        )
        if synthetic_completed is not None:
            remember(synthetic_completed)
            yield synthetic_completed
            return
        raise _responses_incomplete_stream_exception(
            "stream ended before response.completed",
            buffer=event_tail,
            request_data=request_data,
        )

async def _yield_streaming_error_fallback_or_raise(
    request_data: dict,
    exception: Exception,
) -> AsyncIterator[Any]:
    is_responses_stream = _request_is_responses_stream(request_data)
    network_recovery = (
        _routing_module._is_network_recovery_exception(exception)
        and _routing_module._recovery_policy_for_exception(exception)
        != _routing_module._RECOVERY_POLICY_ERROR
    )
    supports_streaming_fallback = _request_supports_streaming_error_fallback(
        request_data
    )
    litellm_metadata = _request_context_module._request_metadata_dict(
        request_data,
        "litellm_metadata",
    ) or {}
    route_recovery_poll_payload = litellm_metadata.get(_ROUTE_RECOVERY_POLL_METADATA_KEY) is True
    if is_responses_stream and _routing_module._is_context_size_error(exception):
        raise exception
    native_web_search_unsupported = (
        is_responses_stream
        and _routing_module._is_native_responses_web_search_unsupported_error(
            exception,
            request_data,
        )
        and not _responses_web_search_bridge_module._request_has_provider_native_web_search_event(
            request_data
        )
    )
    if (
        _routing_module._should_block_external_web_search_original_recovery(
            request_data
        )
        and not native_web_search_unsupported
    ):
        _trace_module._route_trace(
            "external_web_search_original_stream_fallback_blocked",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            request=_trace_module._trace_request_summary(request_data),
            exception=_routing_module._trace_exception(exception),
        )
        yield _external_web_search_missing_answer_failed_event(request_data, exception)
        return
    if native_web_search_unsupported:
        from . import responses_surfaces as _responses_surfaces_module

        external_web_search_bridge_kwargs = (
            _responses_surfaces_module._with_responses_external_web_search_bridge_after_native_error(
                exception,
                request_data,
            )
        )
        original_function = _responses_execution_module._responses_bridge_original_function(
            request_data,
        )
        if external_web_search_bridge_kwargs is not None and original_function is not None:
            try:
                bridge_response = await _responses_execution_module._execute_responses_native_web_search_bridge_call(
                    original_function,
                    external_web_search_bridge_kwargs,
                    original_request_kwargs=request_data,
                    outer_request_kwargs=request_data,
                )
            except Exception as bridge_exception:
                exception = bridge_exception
            else:
                async for chunk in _yield_start_buffered_stream_with_error_fallback(
                    bridge_response,
                    external_web_search_bridge_kwargs,
                ):
                    yield chunk
                return
        if (
            _routing_module._should_block_external_web_search_original_recovery(
                request_data
            )
            and not native_web_search_unsupported
        ):
            _trace_module._route_trace(
                "external_web_search_original_stream_fallback_blocked",
                request_id=_routing_module._trace_request_id(request_data),
                session=_routing_module._trace_session_context(request_data),
                model_group=_responses_execution_module._request_model_group(
                    request_data
                ),
                request=_trace_module._trace_request_summary(request_data),
                exception=_routing_module._trace_exception(exception),
            )
            yield _external_web_search_missing_answer_failed_event(
                request_data,
                exception,
            )
            return
    fallback_start_buffer: List[Any] = []
    fallback_started_delivery = False
    fallback_delivered_terminal_or_visible = False
    yielded_fallback = False
    fallback_exception: Optional[Exception] = None

    async def route_recovery_chunks(exception: Exception) -> AsyncIterator[Any]:
        def with_full_route_pool(recovery_request: dict) -> dict:
            payload = recovery_request.copy()
            payload["_route_recovery_ignore_local_constraints"] = True
            return payload

        if _responses_web_search_bridge_module._external_web_search_has_recovery_context(
            request_data,
            exception,
        ):
            recovery_request = _responses_web_search_bridge_module._external_web_search_recovery_kwargs(
                request_data,
                exception=exception,
            )
            async for chunk in _stream_route_recovery_poll(
                with_full_route_pool(recovery_request),
                exception,
            ):
                yield chunk
            return

        async for chunk in _stream_route_recovery_poll(
            with_full_route_pool(request_data),
            exception,
        ):
            yield chunk

    if (
        # Route recovery runs the same failover path under a keepalive-aware
        # iterator.  Start it immediately for an offline upstream so Codex
        # sees an established stream while the first replacement route is
        # probed, rather than waiting for the ordinary one-shot fallback to
        # finish or fail.
        supports_streaming_fallback
        and network_recovery
        and not route_recovery_poll_payload
        and _routing_module._recovery_max_seconds_for_request(request_data) > 0
    ):
        async for chunk in route_recovery_chunks(exception):
            yield chunk
        return

    if (
        supports_streaming_fallback
        and not route_recovery_poll_payload
        and _routing_module._recovery_max_seconds_for_request(request_data) > 0
        # A normal priority failover still needs its ordinary one-shot router
        # fallback.  Skip that call only when routing has already established
        # that the group has no eligible deployment; then the shared cooldown
        # check can keep the original stream alive without an upstream probe.
        and _routing_module._is_no_deployments_available_error(exception)
        and _external_web_search_recovery_poll_error(exception)
        and _route_recovery_poll_cooldown_wait(
            request_data,
            ignore_constraints=True,
        )
        is not None
    ):
        async for chunk in route_recovery_chunks(exception):
            yield chunk
        return

    try:
        async for chunk in _stream_streaming_error_fallback(request_data, exception):
            if is_responses_stream and not fallback_started_delivery:
                chunk_has_visible_output = _stream_chunk_has_visible_output(chunk)
                chunk_is_completed = _responses_completed_chunk_has_usable_output(
                    chunk,
                    request_data,
                )
                chunk_is_incomplete_terminal = _responses_stream_chunk_is_incomplete_terminal(chunk)
                if not (
                    chunk_has_visible_output
                    or chunk_is_completed
                    or chunk_is_incomplete_terminal
                ):
                    fallback_start_buffer.append(chunk)
                    continue
                fallback_started_delivery = True
                for buffered_chunk in fallback_start_buffer:
                    yielded_fallback = True
                    yield buffered_chunk
                fallback_start_buffer.clear()
                fallback_delivered_terminal_or_visible = (
                    chunk_has_visible_output
                    or chunk_is_completed
                    or chunk_is_incomplete_terminal
                )
            elif is_responses_stream:
                fallback_delivered_terminal_or_visible = (
                    fallback_delivered_terminal_or_visible
                    or _stream_chunk_has_visible_output(chunk)
                    or _responses_completed_chunk_has_usable_output(chunk, request_data)
                    or _responses_stream_chunk_is_incomplete_terminal(chunk)
                )
            yielded_fallback = True
            yield chunk
    except Exception as exc:
        if yielded_fallback:
            if (
                supports_streaming_fallback
                and not fallback_delivered_terminal_or_visible
                and _routing_module._recovery_max_seconds_for_request(request_data) > 0
                and _external_web_search_recovery_poll_error(exc)
            ):
                _trace_module._route_trace(
                    "stream_fallback_route_recovery_poll",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    request=_trace_module._trace_request_summary(request_data),
                    original_exception=_routing_module._trace_exception(exception),
                    exception=_routing_module._trace_exception(exc),
                )
                yielded_recovery = False
                recovery_exception: Optional[Exception] = None
                try:
                    async for chunk in route_recovery_chunks(exc):
                        yielded_recovery = True
                        yield chunk
                except Exception as recovery_exc:
                    if yielded_recovery:
                        raise
                    recovery_exception = recovery_exc
                if yielded_recovery:
                    return
                if recovery_exception is not None:
                    exc = recovery_exception
            if (
                is_responses_stream
                and _routing_module._is_terminal_responses_stream_failure_error(exc)
            ):
                _trace_module._route_trace(
                    "responses_stream_fallback_failed_terminal_event",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    request=_trace_module._trace_request_summary(request_data),
                    original_exception=_routing_module._trace_exception(exception),
                    exception=_routing_module._trace_exception(exc),
                )
                yield _synthesized_failed_response_event(request_data, exc)
                return
            raise
        fallback_start_buffer.clear()
        fallback_exception = exc
    if yielded_fallback:
        return
    final_exception = fallback_exception or exception
    recovery_exception: Optional[Exception] = None
    network_recovery = network_recovery or (
        _routing_module._is_network_recovery_exception(final_exception)
        and _routing_module._recovery_policy_for_exception(final_exception)
        != _routing_module._RECOVERY_POLICY_ERROR
    )
    if (
        supports_streaming_fallback
        and _routing_module._recovery_max_seconds_for_request(request_data) > 0
        and (
            network_recovery
            or _external_web_search_recovery_poll_error(final_exception)
        )
    ):
        yielded_recovery = False
        recovery_seed = exception if network_recovery else final_exception
        try:
            async for chunk in route_recovery_chunks(recovery_seed):
                yielded_recovery = True
                yield chunk
        except Exception as exc:
            if yielded_recovery:
                raise
            recovery_exception = exc
        if yielded_recovery:
            return
        if recovery_exception is not None:
            final_exception = recovery_exception
    if (
        network_recovery
        and not route_recovery_poll_payload
        and (
            recovery_exception is None
            or _routing_module._is_network_recovery_exception(recovery_exception)
            or _external_web_search_recovery_poll_error(final_exception)
        )
    ):
        # Never turn an offline upstream into the CancelledError escape hatch
        # that asks Codex to reconnect.  The route-recovery iterator normally
        # remains open until a route succeeds; if a bridge declined recovery,
        # close quietly instead of exposing the transport exception.
        return
    if is_responses_stream and (
        _routing_module._is_request_scoped_priority_deployment_failover_error(
            final_exception,
            request_data,
        )
        or _routing_module._is_no_deployments_available_error(final_exception)
        or _routing_module._is_request_scoped_priority_deployment_failover_error(
            exception,
            request_data,
        )
        or _routing_module._is_no_deployments_available_error(exception)
    ):
        if route_recovery_poll_payload:
            raise final_exception
        _trace_module._route_trace(
            "responses_stream_failed_terminal_event",
            request_id=_routing_module._trace_request_id(request_data),
            session=_routing_module._trace_session_context(request_data),
            model_group=_responses_execution_module._request_model_group(request_data),
            request=_trace_module._trace_request_summary(request_data),
            original_exception=_routing_module._trace_exception(exception),
            exception=_routing_module._trace_exception(final_exception),
        )
        yield _synthesized_failed_response_event(request_data, final_exception)
        return
    if _routing_module._is_no_deployments_available_error(
        fallback_exception or exception
    ) or _routing_module._is_no_deployments_available_error(exception):
        _routing_module._raise_retryable_stream_disconnect(
            request_data,
            original_exception=exception,
            fallback_exception=fallback_exception,
        )
    raise exception


async def _yield_start_buffered_stream_with_error_fallback(
    response: Any,
    request_data: dict,
) -> AsyncIterator[Any]:
    if not _responses_output_module._response_is_async_iterable(response):
        try:
            async for chunk in _non_streaming_response_as_stream(response, request_data):
                yield chunk
        except Exception as exception:
            async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                request_data,
                exception,
            ):
                yield fallback_chunk
        return
    if _routing_module._is_route_recovery_stream_response(response):
        async for chunk in response:
            yield chunk
        return
    if _routing_module._is_failed_responses_stream_response(response):
        failed_exception = getattr(response, "exception", None)
        failed_request = getattr(response, "request_data", None)
        if (
            isinstance(failed_exception, Exception)
            and isinstance(failed_request, dict)
            and _routing_module._is_native_responses_web_search_unsupported_error(
                failed_exception,
                failed_request,
            )
            and not _responses_web_search_bridge_module._request_has_provider_native_web_search_event(
                failed_request
            )
        ):
            from . import responses_surfaces as _responses_surfaces_module

            bridge_kwargs = (
                _responses_surfaces_module._with_responses_external_web_search_bridge_after_native_error(
                    failed_exception,
                    failed_request,
                    failed_request,
                )
            )
            original_function = _responses_execution_module._responses_bridge_original_function(
                failed_request
            )
            if bridge_kwargs is not None and original_function is not None:
                bridge_response = await _responses_execution_module._execute_responses_native_web_search_bridge_call(
                    original_function,
                    bridge_kwargs,
                    original_request_kwargs=failed_request,
                    outer_request_kwargs=failed_request,
                )
                async for chunk in _yield_start_buffered_stream_with_error_fallback(
                    bridge_response,
                    bridge_kwargs,
                ):
                    yield chunk
                return
        async for chunk in response:
            yield chunk
        return
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        stream_started_at = time.monotonic()
        try:
            async for chunk in _yield_validated_structured_codex_compaction_stream(
                [],
                response,
                request_data,
                stream_started_at=stream_started_at,
            ):
                yield chunk
        except Exception as exception:
            if _routing_module._is_request_scoped_priority_deployment_failover_error(
                exception,
                request_data,
            ):
                _routing_module._mark_exception_for_deployment_failover(
                    exception,
                    request_data,
                )
            async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                request_data,
                exception,
            ):
                yield fallback_chunk
        return

    buffer: List[Any] = []
    is_responses_stream = _request_is_responses_stream(request_data)
    saw_responses_completed = False
    saw_visible_output = False
    should_delay_web_search_preamble = (
        is_responses_stream
        and _tools_module._request_should_consume_web_search_function_call(request_data)
    )
    stream_exhausted = True
    stream_started_at = time.monotonic()
    completion_state = _ResponsesStreamCompletionState(request_data)
    raw_tool_call_text_filter = _responses_web_search_bridge_module._RawToolCallTextFilter()

    try:
        stream = _stream_with_idle_timeout(
            response,
            request_data,
            stream_started_at=stream_started_at,
        )
        if _should_emit_codex_responses_initial_sse_keepalive(request_data):
            stream = _yield_codex_responses_initial_keepalive_stream(
                response,
                request_data,
                stream_started_at=stream_started_at,
            )
        async for chunk in stream:
            _responses_web_search_bridge_module._mark_provider_native_web_search_event(
                request_data, chunk
            )
            sanitized_chunk = _responses_web_search_bridge_module._sanitize_web_search_stream_chunk(chunk)
            if sanitized_chunk is None:
                continue
            sanitized_chunk = _responses_web_search_bridge_module._sanitize_raw_tool_call_text_stream_chunk(
                sanitized_chunk,
                raw_tool_call_text_filter,
            )
            if sanitized_chunk is None:
                continue
            chunk = sanitized_chunk
            if _is_codex_responses_initial_sse_keepalive(chunk):
                yield chunk
                continue
            if (
                not saw_visible_output
                and _stream_chunk_type(chunk) == "response.output_text.delta"
                and not str(_stream_chunk_dump(chunk).get("delta") or "").strip()
            ):
                continue
            chunk_exception = _stream_chunk_priority_error_exception(
                chunk,
                request_data,
            )
            if chunk_exception is not None:
                async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                    request_data,
                    chunk_exception,
                ):
                    yield fallback_chunk
                return
            if is_responses_stream:
                completed_compat_chunk = _responses_output_limit_incomplete_as_completed_chunk(
                    chunk,
                    request_data,
                )
                if completed_compat_chunk is not None:
                    chunk = completed_compat_chunk
            buffer.append(chunk)
            if is_responses_stream:
                completion_state.remember(chunk)
            if (
                is_responses_stream
                and _tools_module._request_should_consume_web_search_function_call(request_data)
                and _responses_web_search_bridge_module._has_web_search_actions_for_request(
                    completion_state.completed_payload(request_data),
                    request_data,
                )
            ):
                payload = completion_state.completed_payload(request_data)
                _trace_module._route_trace(
                    "external_web_search_bridge_stream_function_call_intercept",
                    request_id=_routing_module._trace_request_id(request_data),
                    session=_routing_module._trace_session_context(request_data),
                    model_group=_responses_execution_module._request_model_group(request_data),
                    deployment_id=_routing_module._deployment_id_from_request(request_data),
                    route_key=_routing_module._deployment_route_key_from_request(request_data),
                    request=_trace_module._trace_request_summary(request_data),
                    response=_trace_module._trace_response_summary(payload, request_data),
                    actions=_responses_web_search_bridge_module._web_search_actions_for_request(payload, request_data),
                    buffered_chunks=len(buffer),
                )
                await _close_async_iterator_safely(response)
                original_function = _responses_execution_module._responses_bridge_original_function(request_data)
                async for resolved_chunk in _computer_facade_module._resolve_web_search_function_calls_stream_rounds(
                    payload,
                    request_data,
                    original_function,
                ):
                    yield resolved_chunk
                return
            saw_visible_output = saw_visible_output or (
                _stream_chunk_has_visible_output(chunk)
            )
            if is_responses_stream:
                if _responses_stream_chunk_is_completed(chunk):
                    if _responses_completed_chunk_is_empty(chunk, request_data) and not saw_visible_output:
                        break
                    saw_responses_completed = True
                    saw_visible_output = saw_visible_output or _responses_completed_chunk_has_usable_output(
                        chunk,
                        request_data,
                    )
                elif _responses_stream_chunk_is_incomplete_terminal(chunk):
                    completed_compat_chunk = (
                        _responses_output_limit_terminal_state_as_completed_chunk(
                            chunk,
                            completion_state,
                            request_data,
                        )
                    )
                    if completed_compat_chunk is not None:
                        for buffered_chunk in buffer[:-1]:
                            yield _responses_stream_chunk_for_delivery(buffered_chunk)
                        yield completed_compat_chunk
                        await _close_async_iterator_safely(response)
                        return
                    output_limit_terminal = (
                        _responses_incomplete_terminal_reason(chunk)
                        in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS
                    )
                    incomplete_exception = _responses_incomplete_stream_exception(
                        "terminal response event before response.completed",
                        buffer=buffer,
                        request_data=request_data,
                    )
                    if output_limit_terminal:
                        _trace_module._route_trace(
                            "responses_stream_output_limit_fallback_start",
                            request_id=_routing_module._trace_request_id(request_data),
                            session=_routing_module._trace_session_context(request_data),
                            model_group=_responses_execution_module._request_model_group(request_data),
                            request=_trace_module._trace_request_summary(request_data),
                            exception=_routing_module._trace_exception(incomplete_exception),
                        )
                        async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                            request_data,
                            incomplete_exception,
                        ):
                            yield fallback_chunk
                        await _close_async_iterator_safely(response)
                        return
                    async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                        request_data,
                        incomplete_exception,
                    ):
                        yield fallback_chunk
                    return
            chunk_is_delayed_web_search_preamble = (
                should_delay_web_search_preamble
                and _stream_chunk_is_assistant_text_without_tool_activity(chunk)
            )
            if (
                (
                    saw_visible_output
                    and not chunk_is_delayed_web_search_preamble
                )
                or saw_responses_completed
                or (
                    len(buffer) >= _STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS
                    and not should_delay_web_search_preamble
                    and not is_responses_stream
                )
            ):
                stream_exhausted = False
                break
    except Exception as exc:
        completed_compat = _codex_compaction_done_item_completed_compat(
            completion_state,
            request_data,
            reason="stream_error_after_completed_compaction_item",
            exception=exc,
        )
        if completed_compat is not None:
            for buffered_chunk in buffer:
                yield _responses_stream_chunk_for_delivery(buffered_chunk)
            yield completed_compat
            await _close_async_iterator_safely(response)
            return
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            exc,
            request_data,
        ):
            _routing_module._mark_exception_for_deployment_failover(
                exc,
                request_data,
            )
        async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
            request_data,
            exc,
        ):
            yield fallback_chunk
        return

    if is_responses_stream and stream_exhausted and not saw_responses_completed:
        completed_compat = _codex_compaction_done_item_completed_compat(
            completion_state,
            request_data,
            reason="clean_eof_after_completed_compaction_item",
        )
        if completed_compat is not None:
            for buffered_chunk in buffer:
                yield _responses_stream_chunk_for_delivery(buffered_chunk)
            yield completed_compat
            return
        incomplete_exception = _responses_incomplete_stream_exception(
            "stream ended before response.completed",
            buffer=buffer,
            request_data=request_data,
        )
        async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
            request_data,
            incomplete_exception,
        ):
            yield fallback_chunk
        return

    if not is_responses_stream and stream_exhausted:
        finished_choice_indices: set[int] = set()
        saw_native_terminal = any(
            _native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            )
            for chunk in buffer
        )
        if not saw_native_terminal:
            incomplete_exception = _native_stream_incomplete_exception(
                request_data,
                buffered_chunks=len(buffer),
            )
            async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                request_data,
                incomplete_exception,
            ):
                yield fallback_chunk
            return

    async for chunk in _yield_guarded_original_stream(
        buffer,
        response,
        request_data,
        saw_responses_completed=saw_responses_completed,
        stream_started_at=stream_started_at,
        saw_visible_output=saw_visible_output,
    ):
        yield chunk
