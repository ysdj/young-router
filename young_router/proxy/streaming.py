from __future__ import annotations

import re
from typing import Callable

from . import codex_fast_tier as _codex_fast_tier_module
from . import codex_contract as _codex_contract_module
from . import image_generation as _image_generation_module
from . import image_inputs as _image_inputs_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_output as _responses_output_module
from . import responses_tools as _responses_tools_module
from . import web_search_native as _web_search_native_module
from . import routing as _routing_module
from . import state as _state_module
from . import stream_continuation as _stream_continuation_module
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
    request_data: Optional[dict] = None,
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
    _responses_output_module._sanitize_reasoning_wrapper_event(payload, request_data)
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
    request_data: Optional[dict] = None,
) -> str:
    if (
        "data:" not in text
        or (
            "response.completed" not in text
            and "image_generation_call" not in text
            and "response.output_item.added" not in text
            and "output_text" not in text
            and "content_part" not in text
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
                    request_data=request_data,
                )
            )
            break
        position, delimiter = min(candidates, key=lambda item: item[0])
        output.append(
            _normalize_sse_response_completed_block(
                text[index:position],
                delimiter,
                input_token_upper_bound=input_token_upper_bound,
                request_data=request_data,
            )
        )
        index = position + len(delimiter)
    return "".join(output)

def _normalize_sse_response_completed_chunk(
    chunk: str | bytes,
    *,
    input_token_upper_bound: Optional[int] = None,
    request_data: Optional[dict] = None,
) -> str | bytes:
    if isinstance(chunk, bytes):
        try:
            text = chunk.decode("utf-8")
        except Exception:
            return chunk
        normalized_text = _normalize_sse_response_completed_text(
            text,
            input_token_upper_bound=input_token_upper_bound,
            request_data=request_data,
        )
        if normalized_text == text:
            return chunk
        return normalized_text.encode("utf-8")
    return _normalize_sse_response_completed_text(
        chunk,
        input_token_upper_bound=input_token_upper_bound,
        request_data=request_data,
    )

def _function_call_arguments_valid(value: Any) -> bool:
    normalized, valid = _responses_request_module._codex_normalized_function_arguments(
        value,
        empty_is_object=True,
    )
    return bool(valid and isinstance(normalized, str))

def _payload_invalid_function_call_reason(payload: Any) -> Optional[str]:
    """Where a Responses event still carries function-call arguments nothing can read.

    Reached *after* normalization has had its turn, so a value reported here is
    one no parser accepts even with the containers an output limit left open
    closed (see ``_normalize_response_function_call_arguments``).  The reason is
    recorded rather than turned into a terminal: the value travels to the
    client exactly as the upstream wrote it, and a client that cannot read it
    runs the call with no arguments and reports that itself, which leaves the
    turn intact instead of ending it for a tool call the model was still in the
    middle of writing.
    """

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
    """Adapt one chunk to what this client must receive, or ``None`` to drop it.

    ``None`` is an answer, not a payload: the caller skips that chunk.  Yielding
    it instead reaches LiteLLM's serializer as neither a model, bytes, a string,
    nor a dict, so the frame is written with the Python literal ``data: None`` --
    a body every Responses client rejects as malformed JSON.  The request travels
    with the chunk because the decisions here are the request's own: the
    reasoning-visibility check reads ``request_data``, and without it a client
    that asked for a summary had its thinking silently dropped.
    """
    input_token_upper_bound = _codex_request_input_token_upper_bound(request_data)
    if isinstance(chunk, _JSONStreamEvent):
        _normalize_response_function_call_arguments(chunk)
        invalid_reason = _payload_invalid_function_call_reason(chunk)
        if invalid_reason is not None:
            # A tool call whose arguments no parser accepts is the request's own
            # shape, not a route fault and not a reason to end the turn: the
            # event is delivered as the upstream wrote it so the client's own
            # tolerant reader runs the call and reports the arguments it could
            # not use.  Failing the response here threw that work away — the
            # Codex client treats a failed terminal as fatal, so a turn the
            # model was still writing died with it.
            _trace_module._request_route_trace(
                "responses_stream_unreadable_tool_call_arguments",
                request_data,
                reason=invalid_reason,
                event_type=_stream_chunk_type(chunk),
                arguments_length=len(str(chunk.get("arguments") or "")) if isinstance(chunk.get("arguments"), str) else None,
            )
        _image_generation_module._normalize_image_generation_result_status(chunk)
        _normalize_response_completed_event_usage(
            chunk,
            input_token_upper_bound=input_token_upper_bound,
        )
        _responses_output_module._sanitize_reasoning_wrapper_event(
            chunk,
            request_data,
        )
        if (
            _stream_chunk_type(chunk).startswith("response.reasoning_summary_")
            and not _web_search_native_module._public_reasoning_is_allowed(
                request_data
            )
        ):
            return None
        return chunk
    if isinstance(chunk, (str, bytes)):
        normalized = _normalize_sse_response_completed_chunk(
            chunk,
            input_token_upper_bound=input_token_upper_bound,
            request_data=request_data,
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
            # Delivered, not failed: see the dict-event path above.
            _trace_module._request_route_trace(
                "responses_stream_unreadable_tool_call_arguments",
                request_data,
                reason=invalid_reason,
                event_type=_stream_chunk_type(json_chunk),
            )
        _image_generation_module._normalize_image_generation_result_status(json_chunk)
        _normalize_response_completed_event_usage(
            json_chunk,
            input_token_upper_bound=input_token_upper_bound,
        )
        _responses_output_module._sanitize_reasoning_wrapper_event(
            json_chunk,
            request_data,
        )
        if (
            _stream_chunk_type(json_chunk).startswith(
                "response.reasoning_summary_"
            )
            and not _web_search_native_module._public_reasoning_is_allowed(
                request_data
            )
        ):
            return None
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

def _normalized_item_arguments(value: Any) -> Optional[str]:
    """One function call's arguments as a parseable object, or ``None``.

    A value an output limit cut short is closed rather than discarded: the
    fragment holds every byte the model wrote and only its closing brackets are
    missing, so the arguments that did arrive are usable.  A fragment cut
    inside a value is left alone — closing that would mean inventing a tail the
    model never wrote.
    """

    normalized, valid = _responses_request_module._codex_normalized_function_arguments(
        value,
        empty_is_object=True,
    )
    if valid and normalized is not None:
        return normalized
    closed = _responses_request_module._codex_closed_function_arguments(value)
    if closed is None:
        return None
    return json.dumps(closed, ensure_ascii=False, separators=(",", ":"))

def _normalize_response_function_call_arguments(payload: Any) -> None:
    """Normalize function-call arguments before a Responses event is delivered."""

    if not isinstance(payload, dict):
        return
    _normalize_response_function_call_added_arguments(payload)

    def normalize_item(item: Any) -> Any:
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return item
        normalized = _normalized_item_arguments(item.get("arguments"))
        if normalized is None:
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
        normalized = _normalized_item_arguments(payload.get("arguments"))
        if normalized is not None:
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
    # A read that carries an ``event: error`` frame beside provider output is
    # still an error: joining the read's ``data:`` lines and parsing them as one
    # payload answered nothing, so a route that failed mid-read looked healthy.
    for payload in _sse_stream_chunk_payloads(chunk):
        error = _stream_chunk_error_payload(payload)
        if error is not None:
            return error
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
    # Synthesized route/compaction failures may use ``invalid_request_error``
    # as their wire-level type while carrying the exact upstream status in the
    # message. Parse that explicit status before the generic invalid-request
    # inference, otherwise an upstream 404 is incorrectly downgraded to 400.
    # A wire payload's code is not necessarily text: a malformed upstream
    # error that carries an object or list there must not turn this mapping
    # into an unhandled TypeError inside the stream path.
    if status_code is None and (error_code if isinstance(error_code, str) else "") in {
        "upstream_compaction_failure",
        "upstream_route_failure",
    }:
        match = re.search(r"\bhttp\s+([45]\d\d)\b", lowered)
        if match is not None:
            status_code = int(match.group(1))
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

def _sse_stream_chunk_text(chunk: Any) -> Optional[str]:
    """The wire text of an encoded stream chunk, or ``None`` for any other shape."""

    if isinstance(chunk, bytes):
        try:
            return chunk.decode("utf-8")
        except UnicodeDecodeError:
            return None
    if isinstance(chunk, str):
        return chunk
    return None

def _sse_stream_frame_payloads(text: str) -> list[dict[str, Any]]:
    """Decode every complete SSE frame a transport read carries, in order.

    A provider transport hands over whatever bytes one socket read returned, so
    a single chunk routinely carries several frames -- ``message_delta`` and
    ``message_stop`` are usually written together, and so are the lifecycle
    frames of a fast answer.  Classifying only the last ``data:`` line of such a
    read (what a flattened line scan does) sees the terminal frame but drops the
    ceiling reason beside it, and the reverse: the terminal frame is invisible
    to a scan that stops at the first payload.  Every predicate downstream --
    terminal detection, the output-ceiling reason, visible output, and the
    error classifier -- asks about one frame, so the frames are decoded here and
    the caller asks each of them.

    A read that is not a frame sequence (a bare JSON body, or a fragment still
    missing its terminator) keeps the previous single-payload reading.
    """

    frames: list[dict[str, Any]] = []
    for block in re.split(r"\r\n\r\n|\n\n|\r\r", text):
        if not block.strip():
            continue
        payload = _sse_frame_payload_from_block(block)
        if payload is not None:
            frames.append(payload)
    return frames

def _sse_frame_payload_from_block(block: str) -> Optional[dict[str, Any]]:
    event_type: Optional[str] = None
    data_lines: list[str] = []
    for line in block.splitlines():
        if line.startswith("event:"):
            value = line.split(":", 1)[1].strip()
            event_type = value or None
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].strip())
    payload_text = "\n".join(data_lines) if data_lines else block.strip()
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

def _sse_stream_chunk_payloads(chunk: Any) -> list[dict[str, Any]]:
    """Decode a structured wire chunk for local classification, never delivery.

    Returns one payload per frame the read carries, so a coalesced read is read
    as the sequence of frames it is rather than as whichever frame a flattened
    scan happened to keep.
    """

    text = _sse_stream_chunk_text(chunk)
    if text is None:
        return []
    return _sse_stream_frame_payloads(text)

def _sse_stream_chunk_payload(chunk: Any) -> Optional[dict[str, Any]]:
    """The first structured payload a wire chunk carries, if any."""

    payloads = _sse_stream_chunk_payloads(chunk)
    return payloads[0] if payloads else None

def _stream_chunk_dumps(chunk: Any) -> list[dict[str, Any]]:
    """Every frame this chunk carries, in wire order.

    A dict (or a Responses event) is one frame.  An encoded native read is
    whatever one socket read returned, which is usually several frames; the
    yes/no questions asked below -- is this terminal, does it carry output, does
    it carry an error -- are asked about the read as a whole, so they must see
    every frame instead of only whichever one a flattened scan kept.
    """

    if isinstance(chunk, dict):
        return [chunk]
    payloads = _sse_stream_chunk_payloads(chunk)
    if payloads:
        return payloads
    if hasattr(chunk, "model_dump"):
        try:
            dumped = chunk.model_dump()
        except Exception:
            return []
        return [dumped] if isinstance(dumped, dict) else []
    return []

def _stream_chunk_type(chunk: Any) -> str:
    """The frame type this chunk is read as.

    A coalesced transport read is named by its most advanced frame: that is the
    one whose arrival released a start buffer or closed a lifecycle, and naming
    the read after an earlier frame in the same read (or after nothing at all,
    which is what an unparseable joined payload produced) hid the frame that
    actually decided.
    """

    dumps = _stream_chunk_dumps(chunk)
    for dumped in reversed(dumps):
        chunk_type = dumped.get("type")
        if isinstance(chunk_type, str) and chunk_type:
            return chunk_type
    return ""

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

def _is_young_router_terminal_failure_event(chunk: Any) -> bool:
    if bool(getattr(chunk, "_young_router_terminal_failure", False)):
        return True
    dumped = _stream_chunk_dump(chunk)
    if _stream_chunk_type(dumped) != "response.failed":
        return False
    response = dumped.get("response")
    response = response if isinstance(response, dict) else {}
    error = response.get("error")
    error = error if isinstance(error, dict) else {}
    # Only codes this router alone emits belong here.  ``context_length_exceeded``
    # and ``invalid_prompt`` are the client's own vocabulary (see the Codes it
    # maps in ``codex-api/src/sse/responses_error.rs``), so an upstream sending
    # them must keep its ordinary meaning instead of being taken for a terminal
    # this router already decided.  Our own events are recognised above by the
    # attribute, which survives even when the code is a shared one.
    return error.get("code") in {
        "image_generation_tool_unavailable",
        "router_restarting",
        "upstream_account_restricted",
        "upstream_client_outdated",
        "insufficient_quota",
        "upstream_compaction_failure",
        "upstream_compaction_unsupported",
        "upstream_relay_capacity",
        "upstream_route_failure",
    }

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
    _trace_module._request_route_trace(
        "responses_stream_output_limit_incomplete_completed_compat", request_data,
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
    _trace_module._request_route_trace(
        "responses_stream_output_limit_state_completed_compat", request_data,
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
    item_id = _web_search_native_module._response_item_get(item, "id")
    if not isinstance(item_id, str) or not item_id:
        item_id = _web_search_native_module._response_item_get(item, "call_id")
    return item_id if isinstance(item_id, str) and item_id else None

def _stream_output_item_identity_keys(item: Any) -> list[str]:
    keys: list[str] = []
    item_type = _web_search_native_module._response_item_get(item, "type")
    for key_name in ("id", "call_id"):
        if key_name == "call_id" and not _stream_output_item_is_tool_call(item_type):
            continue
        value = _web_search_native_module._response_item_get(item, key_name)
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
    item_call_id = _web_search_native_module._response_item_get(item, "call_id")
    if isinstance(item_call_id, str) and item_call_id:
        for pending_key, (_index, pending_item) in pending_tool_items.items():
            pending_call_id = _web_search_native_module._response_item_get(
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
    item_type = _web_search_native_module._response_item_get(item, "type")
    if item_type == "web_search_call":
        json_item = _jsonable(item)
        if not isinstance(json_item, dict):
            return
        sanitized_item = _web_search_native_module._sanitize_web_search_call_item(json_item)
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
        item_type = _web_search_native_module._response_item_get(
            json_item,
            "type",
        )
        if item_type == "web_search_call":
            sanitized_item = _web_search_native_module._sanitize_web_search_call_item(
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
        item_type = _web_search_native_module._response_item_get(item, "type")
        if not _stream_output_item_is_tool_call(item_type):
            continue
        if any(identity_key in seen_item_ids for identity_key in _stream_output_item_identity_keys(item)):
            continue

        done_item = _jsonable(item)
        if not isinstance(done_item, dict):
            continue
        if done_item.get("type") == "web_search_call":
            sanitized_item = _web_search_native_module._sanitize_web_search_call_item(done_item)
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
            sanitized_item = _web_search_native_module._sanitize_web_search_call_item(done_item)
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
    response = _web_search_native_module._sanitize_response_web_search_call_items(response)
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
    response = _web_search_native_module._sanitize_response_web_search_call_items(response)
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

def _local_shutdown_completed_response_event(
    request_data: dict,
    completion_state: Any,
) -> Optional[_JSONStreamEvent]:
    """Close a stream this worker itself cut, as a completed turn.

    A replacement worker is stopped while it is draining, so a turn that is
    still being written can lose its upstream mid-answer.  The upstream said
    nothing, so there is no failure to report and nothing for the user to
    answer: the only honest outcome is the part of the answer that already
    arrived.  Whatever the completion state has recorded is emitted as the
    turn's output, which is how the client keeps the conversation instead of
    rendering ``upstream_route_failure`` and asking the user to send it again.

    ``None`` means nothing usable had been produced yet, and the caller falls
    back to ending the stream quietly rather than inventing an empty answer.
    """

    if completion_state is None:
        return None
    try:
        completed_response = completion_state.completed_payload(request_data)
    except Exception:  # pragma: no cover - a partial state that cannot be read
        return None
    if not isinstance(completed_response, dict):
        return None
    output = completed_response.get("output")
    if not isinstance(output, list) or not output:
        if not str(completed_response.get("output_text") or "").strip():
            return None
    completed_event: dict[str, Any] = {
        "type": "response.completed",
        "response": completed_response,
    }
    model = (
        _responses_execution_module._request_model_group(request_data)
        or request_data.get("model")
    )
    if isinstance(model, str) and model:
        completed_event["model"] = model
    _trace_module._request_route_trace(
        "responses_stream_local_shutdown_completed_compat",
        request_data,
        output_items=len(output) if isinstance(output, list) else 0,
    )
    return _json_stream_event(completed_event)

def _synthesized_failed_response_event(
    request_data: dict,
    exception: Exception,
    completion_state: Any = None,
    *,
    after_visible_output: bool = False,
) -> _JSONStreamEvent:
    """Build the terminal event for a turn that ended without an answer.

    ``after_visible_output`` says the client already rendered part of this
    answer, so neither the fallback nor the recovery poll can be used: replaying
    the turn would duplicate what the reader has already seen.  A cause with its
    own classifier gets the same branch and code either way -- only the sentence
    may mention streaming -- because which way the router answers a stop has
    nothing to do with whether the client happened to render bytes first.  A
    classified stop that fell through to the unclassified branch was reported
    as one the router could not explain, and Pi's retry classifier matches none
    of that sentence, so the turn died with no resend at all.

    ``after_visible_output`` also is not a statement about the router's own
    willingness to wait: the route-recovery poll reaches this builder with the
    exception that ended its window, and that window expiring is what makes the
    stop terminal *here* -- not something the client can be told to sit through
    again.
    """
    if _routing_module._is_local_shutdown_exception(exception):
        # This worker truncated the stream because it is the one being replaced,
        # not because the upstream refused.  A synthetic ``response.failed`` is
        # terminal to Pi/Codex and turns that teardown into a user-visible
        # ``upstream_route_failure`` the user has to answer by hand, so the
        # stream is closed the way a completed turn is: whatever the model had
        # already produced is delivered as the turn's output and the client
        # keeps the conversation instead of rendering a retry tip.
        completed = _local_shutdown_completed_response_event(request_data, completion_state)
        if completed is not None:
            return completed
        # Nothing had been produced yet, so there is no partial turn to keep and
        # no honest completion to report.  The connection is already being
        # closed by Uvicorn, so end the stream quietly rather than naming a
        # failure the user cannot act on.
        raise asyncio.CancelledError
    model = _responses_execution_module._request_model_group(request_data) or request_data.get("model") or "unknown"
    is_structured_compaction = (
        _responses_request_module._request_has_structured_codex_compaction(
            request_data
        )
    )
    if is_structured_compaction:
        _routing_module._mark_real_compaction_failure_capability_unsupported(
            request_data,
            exception,
        )
        status_code = _routing_module._exception_status_code(exception)
        status_suffix = f" (HTTP {status_code})" if status_code is not None else ""
        if _routing_module._is_local_shutdown_exception(exception):
            # The structured compaction request was cut off by this worker's
            # own teardown, not by the route it selected.
            message = (
                "The router was restarting while the context compaction request "
                "was in flight, so it was cut short. Compact again."
            )
            error_type = "server_error"
            error_code = "router_restarting"
        elif _routing_module._is_codex_compaction_capability_unsupported_error(exception):
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
    elif _routing_module._is_local_shutdown_exception(exception):
        # The failure every restart used to report as `upstream_route_failure`.
        # The route is healthy; this process closed the stream while it was
        # being replaced, so the turn just needs to be sent again.
        # The replacement worker is already listening, so a resend is exactly
        # the right answer -- and it has to be worded in a way the client's
        # retry classifier recognises, or the turn dies on a restart the router
        # recovered from a moment later.  "terminated" is on that list and is
        # also the literal truth here.
        message = (
            "The router was restarting while this turn was streaming, so the "
            "turn was terminated. Send the message again."
        )
        error_type = "server_error"
        error_code = "router_restarting"
    elif _routing_module._is_upstream_relay_capacity_error(exception):
        message = _routing_module._sanitized_upstream_route_failure_message(
            model,
            exception,
            request_data,
        )
        error_type = "server_error"
        error_code = "upstream_relay_capacity"
    elif _routing_module._is_upstream_account_restricted_error(exception):
        # The route answered, and its answer is that the account paying for it
        # is restricted.  Resending the same bytes cannot change that, so the
        # stop names the account and the wire type says it is the configuration
        # that must change: ``server_error`` here is what made the client
        # reconnect five times over a request no route would accept.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_account_restricted_message(model_group)
        error_type = "invalid_request_error"
        error_code = "upstream_account_restricted"
    elif _routing_module._is_upstream_client_version_rejected_error(exception):
        # The route's own channel client is what is stale; the same bytes meet
        # the same answer, so this stop must not read as a retry tip.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_client_version_rejected_message(
            model_group
        )
        error_type = "invalid_request_error"
        error_code = "upstream_client_outdated"
    elif _routing_module._is_upstream_tool_history_rejected_error(exception):
        # The route refused this replay because a tool call and its output no
        # longer pair.  The client can only fix that by starting a new task, so
        # the stop says so and uses a wire type that never invites the same
        # history to be sent again.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_tool_history_rejected_message(
            model_group
        )
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    elif _routing_module._is_upstream_deployment_failover_error(exception):
        message = _routing_module._sanitized_upstream_route_failure_message(
            model,
            exception,
            request_data,
        )
        error_type = "server_error"
        error_code = "insufficient_quota"
    elif _routing_module._is_context_size_error(exception):
        # The conversation itself is what the upstream refused, not the route:
        # every candidate route would refuse the same bytes, so a generic
        # "route failed / retry later" sentence sends the user into a retry
        # loop the client keeps re-running.  Naming the limit is the one thing
        # the reader can act on, and ``invalid_request_error`` is what stops a
        # client from treating it as a transport hiccup worth reconnecting.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"The upstream rejected this conversation as longer than "
            f"{model_group}'s context window on every candidate route. Compact "
            "or start a new task so the history sent upstream is smaller."
        )
        error_type = "invalid_request_error"
        # Codex classifies a `response.failed` by exact `error.code` and treats
        # every unrecognised one as retryable, so the codes on this wire are its
        # vocabulary, not ours: an unrepresentable refusal here is a turn the
        # client reconnects over.  The sentence still names our cause.
        error_code = "context_length_exceeded"
    elif _routing_module._is_route_cut_stream_error(exception):
        # The upstream started this answer and then closed the body before its
        # own framing finished.  The turn is over -- the bytes the client
        # already read cannot be replayed -- but the cause is knowable and it
        # belongs to the route, so the sentence names it instead of falling
        # through to the unclassified-stop branch.  That branch used to be the
        # answer here, which told the reader nothing while the same route was
        # reselected for the next request.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"{model_group} closed the response before finishing it, so the "
            "stream ended without a terminal response event. The route has "
            "been parked; send the message again and another route will answer."
        )
        error_type = "server_error"
        error_code = "upstream_stream_truncated"
    elif _routing_module._is_local_stream_timeout_error(exception):
        # This worker's stall watchdog gave up on a route that had gone quiet.
        # ``after_visible_output`` is deliberately absent from this test: the
        # watchdog fires both while an answer streams and when the route never
        # produced a first event, and the recovery poll arrives here with the
        # same verdict once its window expired against a silent route.  Gating
        # on visible output left the second case -- the common one, since a
        # route that answers nothing is what the start watchdog exists for --
        # falling through to the unclassified branch, which said the router
        # could not classify a stop whose trace recorded ``stream_start_timeout``
        # exactly.  Pi matches its retry phrases against that sentence, matched
        # none, and ended a turn a resend would have served: observed as a live
        # 390s turn that died on a silent route.
        #
        # So the stop is named with its own code either way.  Both sentences
        # carry "ended without", one of the client's retry phrases, because the
        # router's wait being over says nothing about the client's next request:
        # that one is a fresh attempt whose cooled route is no longer first.
        #
        # The recovery poll does stop here rather than replaying the same silent
        # route (see ``_route_recovery_poll_keep_going``); only the sentence the
        # client reads is unaffected by that decision.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        if _routing_module._is_local_stream_start_timeout_error(exception):
            message = (
                f"{model_group} produced no response in time, so the stream "
                "ended without a terminal response event. Send the message "
                "again and it will continue on a working route."
            )
            error_code = "upstream_stream_start_timeout"
        else:
            message = (
                f"{model_group} stopped sending this response, so the stream "
                "ended without a terminal response event. Send the message "
                "again and it will continue on a working route."
            )
            error_code = "upstream_stream_idle_timeout"
        error_type = "server_error"
    elif _routing_module._request_body_size_rejected(request_data):
        # Every candidate route refused these bytes.  The rejection is the
        # request's own size, not a route outage, so the client is told what to
        # change instead of being invited to retry an identical body.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"The upstream rejected this request body as too large for "
            f"{model_group} on every candidate route. Start a new task so the "
            "conversation sent upstream is smaller than the upstream limit."
        )
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    elif (
        _routing_module._is_route_recovery_poll_error(exception)
        # This worker's own stall watchdog already names its cause and keeps its
        # own terminal code; reporting its verdict as an upstream drop would
        # invent a connection failure that never happened.
        and not _routing_module._is_local_stream_timeout_error(exception)
    ):
        # The route failed with a policy the router calls retryable, so the
        # cause is known even when nothing was rendered yet.
        #
        # ``after_visible_output`` used to gate this branch as well, which made
        # every such stop before the first byte -- a cooled-down pool, a route
        # that answered 5xx, a dropped connection the recovery poll finished its
        # window on -- read as the unclassified stop below.  The trace recorded
        # ``upstream-network-connectivity`` while the client was told nothing
        # could explain the stop, so Pi's classifier matched nothing and the
        # turn ended.  The sentence now always names the cause; only its second
        # clause differs, because "while the answer was streaming" is false when
        # nothing had streamed.
        #
        # The wording carries a client contract, not just a prose choice.  Pi
        # classifies a failed turn with ``isRetryableAssistantError`` in
        # ``@earendil-works/pi-ai/utils/retry.js``, which regex-matches the
        # rendered ``<code>: <message>`` against a phrase list. A sentence that
        # matches nothing ends the turn with no retry at all -- which is exactly
        # what ``upstream_route_failure`` + "could not classify the stop" did to
        # a live session. "stream ended without" is in that list and is also the
        # literal truth here, so naming the truncation this way is what lets the
        # client resend automatically instead of dropping the turn.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        if _routing_module._is_network_recovery_exception(exception):
            if after_visible_output:
                message = (
                    f"{model_group} closed this connection while the answer was "
                    "streaming, so the stream ended without a terminal response "
                    "event. Send the message again and it will continue on a "
                    "working route."
                )
            else:
                message = (
                    f"{model_group} closed the connection before this turn could "
                    "answer, so the stream ended without a terminal response "
                    "event. Send the message again and it will continue on a "
                    "working route."
                )
            error_code = "upstream_connection_dropped"
        else:
            if after_visible_output:
                message = (
                    f"{model_group} stopped answering while this turn was "
                    "streaming, so the stream ended without a terminal response "
                    "event. Send the message again and it will continue on a "
                    "working route."
                )
            else:
                message = (
                    f"{model_group} produced no answer for this turn, so the "
                    "stream ended without a terminal response event. Send the "
                    "message again and it will continue on a working route."
                )
            error_code = "upstream_stream_interrupted"
        error_type = "server_error"
    else:
        # Nothing this router knows can explain the stop, so the sentence says
        # exactly that and no more.  The generic "the route failed before an
        # answer was available" wording this branch used to carry is banned
        # repo-wide (tests/test_proxy_module_integrity.py): clients render it as
        # a retry tip and re-send a request the router already knows is dead,
        # which is the reconnect loop a generic sentence causes.  A failure that
        # has a cause gets its own branch above; one that does not says so
        # plainly and stops.
        message = (
            "The turn ended before the assistant answered, and the router "
            "could not classify the stop. The route trace for this request "
            "records the upstream status and the exception that ended it."
        )
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
    terminal_event = _json_stream_event(event)
    try:
        terminal_event._young_router_terminal_failure = True  # type: ignore[attr-defined]
    except Exception:
        pass
    return terminal_event

def _synthesized_native_stream_error_chunk(
    request_data: dict,
    exception: Exception,
) -> Any:
    """Return a terminal error in the client's native streaming format.

    The message and code come from the same classifiers the Responses event
    uses.  A hardcoded "upstream_route_failure" told a Chat Completions client
    nothing about which of the very different causes it was looking at, so an
    idle timeout, a relay channel outage, and a restart all arrived as the same
    unexplained stop at whatever text had already streamed.
    """

    model = (
        _responses_execution_module._request_model_group(request_data)
        or request_data.get("model")
    )
    error_code = _native_stream_error_code(exception)
    message = _native_stream_error_message(model, exception, request_data)
    # A refusal the client can act on is not a server failure.  Labelling a
    # context-window refusal ``server_error`` is what made a client reconnect
    # and resend the same oversized history; the wire type is what stops it.
    error_type = (
        "invalid_request_error"
        if error_code
        in {
            "context_length_exceeded",
            "invalid_prompt",
            "upstream_prompt_rejected",
            "image_generation_tool_unavailable",
            "upstream_account_restricted",
            "upstream_client_outdated",
        }
        else "server_error"
    )
    if _request_is_anthropic_messages_stream(request_data):
        payload = {
            "type": "error",
            "error": {
                "type": "invalid_request_error"
                if error_type == "invalid_request_error"
                else "api_error",
                "message": message,
            },
        }
        encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        return f"event: error\ndata: {encoded}\n\n".encode("utf-8")
    return {
        "error": {
            "type": error_type,
            "code": error_code,
            "message": message,
        }
    }

def _native_stream_error_code(exception: Exception) -> str:
    if _routing_module._is_local_shutdown_exception(exception):
        return "router_restarting"
    if _routing_module._is_context_size_error(exception):
        # Codex keys its retry decision on `error.code`; these are its names.
        return "context_length_exceeded"
    if _routing_module._is_terminal_prompt_or_policy_error(exception):
        return "upstream_prompt_rejected"
    if _routing_module._is_image_generation_all_deployments_unsupported_error(
        exception
    ):
        return "image_generation_tool_unavailable"
    if _routing_module._is_upstream_relay_capacity_error(exception):
        return "upstream_relay_capacity"
    if _routing_module._is_upstream_account_restricted_error(exception):
        return "upstream_account_restricted"
    if _routing_module._is_upstream_client_version_rejected_error(exception):
        # A code of its own: the route's channel client is stale, which is a
        # configuration change, not a transport hiccup worth reconnecting for.
        return "upstream_client_outdated"
    if _routing_module._is_upstream_tool_history_rejected_error(exception):
        return "invalid_prompt"
    if _routing_module._is_upstream_deployment_failover_error(exception):
        return "insufficient_quota"
    # The router's own timeout predicate answers for both reasons, so the
    # narrower start timeout is read first.
    if _routing_module._is_local_stream_start_timeout_error(exception):
        return "upstream_stream_start_timeout"
    if _routing_module._is_local_stream_timeout_error(exception):
        return "upstream_stream_idle_timeout"
    if getattr(exception, "stream_incomplete", False):
        return "upstream_stream_incomplete"
    if _routing_module._is_route_cut_stream_error(exception):
        return "upstream_stream_truncated"
    return "upstream_route_failure"

def _native_stream_error_message(
    model: Optional[str],
    exception: Exception,
    request_data: dict,
) -> str:
    if _routing_module._is_local_shutdown_exception(exception):
        # Same contract as the Responses branch: the replacement is listening,
        # so the sentence has to read as the transient restart it is.
        return (
            "The router was restarting while this turn was streaming, so the "
            "turn was terminated. Send the message again."
        )
    if _routing_module._is_local_stream_start_timeout_error(exception):
        # The sentence itself has to carry a phrase Pi's retry classifier knows:
        # the Anthropic wire shape below sends only this message (no error
        # code), so a sentence that reads as advice ends the turn there even
        # though the cause is transient and the router already cooled the route.
        return (
            "The upstream model route produced no response in time, so the "
            "stream ended without a terminal response event. Send the message "
            "again or choose another model route."
        )
    if _routing_module._is_local_stream_timeout_error(exception):
        return (
            "The upstream model route stopped sending this response, so the "
            "stream ended without a terminal response event. Send the message "
            "again or choose another model route."
        )
    if _routing_module._is_context_size_error(exception):
        # A terminal ``response.failed`` event for a context refusal is read by
        # the client as an ordinary route failure unless this event says
        # otherwise, which is what produced the endless "reconnecting 5/5"
        # loop over a request no retry can ever fit.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return (
            f"The upstream rejected this conversation as longer than "
            f"{model_group}'s context window. Compact or start a new task so "
            "the history sent upstream is smaller."
        )
    if _routing_module._is_upstream_account_restricted_error(exception):
        # Same trap as the context refusal: the refusal belongs to the account
        # behind the route, so a resent turn meets the identical answer. It is
        # stated as the configuration change it needs instead of a retry tip.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_account_restricted_message(model_group)
    if _routing_module._is_upstream_client_version_rejected_error(exception):
        # The route's bundled client is stale, so the same bytes meet the same
        # refusal: name the route's own problem, never a resend.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_client_version_rejected_message(model_group)
    if _routing_module._is_upstream_tool_history_rejected_error(exception):
        # The replay cannot be repaired on the client's side by sending it
        # again: the interrupted tool call stays in the history forever.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_tool_history_rejected_message(model_group)
    if getattr(exception, "stream_incomplete", False):
        # Keeps the phrase the chat-completions terminal test pins, and adds the
        # one the client's retry classifier needs.  This stop is transient -- the
        # peer simply stopped mid-answer -- so letting the sentence end the turn
        # would throw away a request the next attempt can serve.
        return (
            "The upstream model route ended the stream before it reported the "
            "response as finished, so the stream ended without a terminal "
            "response event. Send the message again and it will continue on a "
            "working route."
        )
    if _routing_module._is_route_cut_stream_error(exception):
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return (
            f"{model_group} closed the response before finishing it, so the "
            "stream ended without a terminal response event. The route has "
            "been parked; send the message again and another route will answer."
        )
    return _routing_module._sanitized_upstream_route_failure_message(
        model,
        exception,
        request_data,
    )

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
    text = _responses_output_module._response_text(chunk)
    # A relay that finishes a turn early can emit a whitespace-only output
    # text.  That is not usable output: treat it the same as an empty
    # completion so the buffered-stream wrapper fails over instead of
    # forwarding a silent, message-less turn to the client.
    if not text or not text.strip():
        return False
    types = [item_type.lower() for item_type in _responses_output_module._response_types(chunk)]
    if not any("reasoning" in item_type for item_type in types):
        return True
    return any("output_text" in item_type for item_type in types)

def _stream_chunk_has_visible_output(chunk: Any) -> bool:
    """Whether any frame in this chunk carries output the client can read.

    The answer is about the read, not about one frame in it: a transport read
    that carries a delta beside its own lifecycle frames has shown the reader
    that delta, and reading only the first frame of such a read is how a
    delivered answer was mistaken for a route that had produced nothing.
    """

    dumps = _stream_chunk_dumps(chunk)
    candidates = dumps if dumps else [chunk]
    for candidate in candidates:
        if _stream_chunk_has_visible_text_output(candidate):
            return True
        if _image_generation_module._response_has_image_generation_activity(candidate):
            return True
        if _response_has_tool_call_activity(candidate):
            return True
    return False

def _stream_chunk_is_public_reasoning_delta(chunk: Any) -> bool:
    """Whether this frame is thinking the client can already read.

    A reasoning summary delta is user-visible output even though it is not the
    answer: the client renders it the moment it arrives.  It is kept apart from
    ``_stream_chunk_has_visible_output`` because that predicate answers a
    different question -- "has the answer begun" -- which decides whether a route
    that produced nothing but a preamble may be replaced.
    """

    dumps = _stream_chunk_dumps(chunk)
    for visible_chunk in dumps if dumps else [chunk]:
        if not _web_search_native_module._is_public_reasoning_summary_stream_event(
            _stream_chunk_type(visible_chunk)
        ):
            continue
        delta = visible_chunk.get("delta") if isinstance(visible_chunk, dict) else None
        if isinstance(delta, str) and delta.strip():
            return True
    return False


def _stream_chunk_has_delivered_output(chunk: Any) -> bool:
    """Whether the client has now been shown something it cannot un-see.

    Once a frame has reached the reader, neither a replay nor a silent failover
    can put the turn back the way it was, so the start buffer must stop holding
    frames and a later failure belongs to this turn.  Thinking counts: holding it
    until the first answer token is what made a whole reasoning block appear in
    one burst after the model had finished thinking.
    """

    return _stream_chunk_has_visible_output(
        chunk
    ) or _stream_chunk_is_public_reasoning_delta(chunk)

def _response_item_is_web_search_call(item: Any) -> bool:
    json_item = _jsonable(item)
    if not isinstance(json_item, dict):
        return False
    return json_item.get("type") == "web_search_call"

def _response_item_is_codex_compaction(item: Any) -> bool:
    """Recognize both canonical and server-reference compaction item names."""
    json_item = _jsonable(item)
    if not isinstance(json_item, dict):
        return False
    item_type = _web_search_native_module._response_item_get(
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
    encrypted_content = _web_search_native_module._response_item_get(
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
    item_type = _web_search_native_module._response_item_get(json_item, "type")
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
def _stream_start_timeout_exception(
    request_data: dict,
    *,
    start_seconds: float,
    saw_chunk: bool,
    buffered_chunks: int,
) -> Exception:
    exception = TimeoutError(
        f"Young Router stream start timeout after {start_seconds:g}s without the first stream event"
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
    _trace_module._request_route_trace(
        "stream_start_timeout", request_data,
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
        f"Young Router stream idle timeout after {idle_seconds:g}s without a new chunk"
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
    _trace_module._request_route_trace(
        "stream_idle_timeout", request_data,
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

def _resolved_stream_timeout_seconds(
    request_data: dict,
    key: str,
    default_resolver: Any,
) -> float:
    """Honor an explicit per-request timeout override when present.

    Internal callers (the external web-search bridge continuations) know their
    own upstream silence profile: a reasoning model digesting a large evidence
    block can legitimately exceed the global stall budget.  An explicit
    ``stream_idle_timeout_seconds`` / ``stream_start_timeout_seconds`` entry
    wins over the global defaults; ``0.0`` keeps its existing meaning and
    disables the cap.
    """

    if isinstance(request_data, dict):
        raw = request_data.get(key)
        if raw is not None:
            try:
                explicit = float(raw)
            except (TypeError, ValueError):
                explicit = None
            if explicit is not None:
                return explicit
    return default_resolver(request_data)

async def _stream_with_idle_timeout(
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float] = None,
    saw_visible_output: bool = False,
    initial_chunk_count: int = 0,
) -> AsyncIterator[Any]:
    idle_timeout_seconds = _resolved_stream_timeout_seconds(
        request_data,
        "stream_idle_timeout_seconds",
        _routing_module._stream_idle_timeout_seconds_for_request,
    )
    start_timeout_seconds = _resolved_stream_timeout_seconds(
        request_data,
        "stream_start_timeout_seconds",
        _routing_module._stream_start_timeout_seconds_for_request,
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
    # payload (``data: : young_router ...``), aborting the client stream
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
        return chunk.startswith(": young_router initial_wait ")
    return False

async def _cancel_pending_read(task: Optional[asyncio.Task[Any]]) -> None:
    """Cancel and settle one in-flight upstream read without letting it raise."""

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
        task, next_chunk_task = next_chunk_task, None
        await _cancel_pending_read(task)

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

def _stream_chunk_reasoning_only_text(chunk: Any) -> str:
    """Return the thinking an arriving non-Responses frame carries, if any.

    Chat Completions and Anthropic Messages name their thinking carriers
    differently from a Responses ``*.delta`` event, so the generic branch in
    ``_stream_chunk_has_meaningful_delta`` cannot read them.  A route that
    streams ``reasoning_content`` while it thinks is producing the frames a
    client renders as a live thinking block: that is stream activity, and read
    as silence it tears the turn down as ``stream_idle_timeout`` while the
    route is healthy and still working.
    """

    dumped = _stream_chunk_dump(chunk)
    if not isinstance(dumped, dict):
        return ""
    if _stream_chunk_type(dumped) == "content_block_delta":
        # Anthropic states the thinking carrier in the delta's own ``type``.
        delta = dumped.get("delta")
        if isinstance(delta, dict) and delta.get("type") in {
            "thinking_delta",
            "signature_delta",
        }:
            value = delta.get("thinking") or delta.get("signature")
            return value if isinstance(value, str) else ""
        return ""
    choices = dumped.get("choices")
    if not isinstance(choices, (list, tuple)):
        return ""
    for choice in choices:
        delta = _web_search_native_module._response_item_get(
            choice, "delta"
        )
        if not isinstance(delta, dict):
            continue
        for field in (
            "reasoning_content",
            "reasoning",
            "thinking_blocks",
            "reasoning_items",
        ):
            value = _web_search_native_module._response_item_get(
                delta, field
            )
            if isinstance(value, str):
                if value.strip():
                    return value
            elif value not in (None, "", [], {}):
                return "reasoning"
    return ""

def _stream_chunk_has_meaningful_delta(chunk: Any) -> bool:
    dumps = _stream_chunk_dumps(chunk)
    for dumped in dumps if dumps else [chunk]:
        chunk_type = _stream_chunk_type(dumped)
        if isinstance(chunk_type, str) and chunk_type.endswith(".delta"):
            delta = dumped.get("delta") if isinstance(dumped, dict) else None
            if isinstance(delta, str) and delta.strip():
                return True
            if delta not in (None, "", [], {}):
                return True
    if _stream_chunk_reasoning_only_text(chunk).strip():
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
    # The replay states its protocol here because routing reads this key to
    # classify the retry's client surface; the router dispatch below strips it
    # again before calling (see ``_streaming_error_fallback_dispatch_kwargs``).
    if method_name == "aresponses":
        payload.setdefault("call_type", "aresponses")
    elif method_name == "anthropic_messages":
        payload.setdefault("call_type", "anthropic_messages")
    # This fallback payload does not pass through the ordinary deployment
    # pre-call hook, so the Codex contract steps run here too.  They set the
    # lifecycle state and the tool choice; nothing is written into the prompt.
    for contract_step in _codex_contract_module.CODEX_CONTRACT_REQUEST_STEPS:
        contract_payload = contract_step(payload)
        if contract_payload is not None:
            payload = contract_payload
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

def _streaming_error_fallback_dispatch_kwargs(payload: dict) -> dict:
    """The kwargs one fallback dispatch hands to the router method.

    ``call_type`` is the router's own binding, not a request field: every method
    here was built by ``factory_function(..., call_type=...)``, and the generic
    dispatch ``anthropic_messages`` goes through re-passes that same value by
    keyword.  Sending our copy as well raised ``_dispatch_generic_call_type()
    got multiple values for keyword argument 'call_type'`` before any upstream
    call was made, so the one failover that could have replaced a truncated
    Anthropic stream failed as a ``TypeError`` and the client saw the original
    truncation instead.  Routing still needs the key on the payload (it is what
    classifies the retry's client surface), so only the dispatch drops it.
    """

    return {key: value for key, value in payload.items() if key != "call_type"}

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
    # A streaming response can fail only when its iterator is consumed.  By
    # the time a fallback round reaches this helper the generic router has
    # already returned, so classify a real compaction 400 against the
    # selected deployment before deciding whether another route is eligible.
    if selected_deployment_box is not None:
        _routing_module._apply_current_selected_deployment_to_request(
            request_data,
            selected_box=selected_deployment_box,
        )
    _routing_module._mark_real_compaction_failure_capability_unsupported(
        request_data,
        exception,
    )
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
                payload.get("input"),
                payload.get("tools"),
            )
        )
        if input_stats.get("changed"):
            payload["input"] = bridge_input
        payload["litellm_metadata"] = retry_metadata
    router_method = getattr(llm_router, method_name, None)
    if router_method is None:
        raise RuntimeError(f"LiteLLM router does not support {method_name} streaming fallback")
    dispatch_kwargs = _streaming_error_fallback_dispatch_kwargs(payload)

    _trace_module._request_route_trace(
        "streaming_error_fallback_start", request_data,
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
                router_method(**dispatch_kwargs),
                timeout=stream_start_timeout_seconds,
            )
        else:
            response = await router_method(**dispatch_kwargs)
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
        _trace_module._request_route_trace(
            "streaming_error_fallback_error", request_data,
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

def _request_with_selected_deployment(
    request_data: dict,
    *,
    selected_box: Any,
) -> dict:
    """Name the route a fallback round actually reached, or nothing.

    A fallback round still carries the failed route on its request object, so
    crediting that request would clear the failure it just recorded. The
    selected-deployment marker holds the route the replay reached; without one
    the replay named no route of its own, so this answers with no request at
    all rather than with the failed one.
    """

    success_request = request_data.copy()
    if _routing_module._apply_current_selected_deployment_to_request(
        success_request,
        selected_box=selected_box,
    ):
        return success_request
    return {}

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
        if getattr(
            fallback_response,
            "_young_router_terminal_failed_stream",
            False,
        ) is True:
            failed_exception = getattr(fallback_response, "exception", None)
            failed_request = getattr(fallback_response, "request_data", None)
            if isinstance(failed_exception, Exception):
                failure_request = (
                    failed_request
                    if isinstance(failed_request, dict)
                    else fallback_payload
                )
                yield _synthesized_failed_response_event(
                    failure_request,
                    failed_exception,
                )
                return
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
        # The replayed route delivered the whole answer without raising, which
        # is what makes it the route that served this request. The native
        # branch records its own success from its terminal frame.
        if not _request_uses_native_event_stream(fallback_payload):
            served_request = _request_with_selected_deployment(
                fallback_payload,
                selected_box=selected_deployment_box,
            )
            if served_request:
                _routing_module._record_deployment_success_for_cooldown(
                    served_request
                )
    except Exception as fallback_exception:
        fallback_request = (
            fallback_payload if "fallback_payload" in locals() else request_data
        )
        _routing_module._apply_current_selected_deployment_to_request(
            fallback_request,
            selected_box=selected_deployment_box,
        )
        # The second route can also return an iterator successfully and then
        # reject the encrypted compaction on its first/next read.  Record that
        # route-local capability failure before priority/failover classification
        # or the outer recovery loop sees the raw HTTP 400.
        _routing_module._mark_real_compaction_failure_capability_unsupported(
            fallback_request,
            fallback_exception,
        )
        _routing_module._apply_current_selected_deployment_to_request(
            request_data,
            selected_box=selected_deployment_box,
            update_top_level=False,
        )
        if (
            _routing_module._is_request_scoped_priority_deployment_failover_error(
                fallback_exception,
                fallback_request,
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
                fallback_request,
            )
            and not _routing_module._should_retry_same_deployment_before_fallback(fallback_exception)
        ):
            failed_deployment_id = (
                _responses_execution_module._failed_deployment_id(fallback_exception)
                or _routing_module._deployment_id_from_request(
                    fallback_request
                )
                or _routing_module._deployment_id_from_request(request_data)
            )
            _routing_module._mark_exception_for_deployment_failover(
                fallback_exception,
                fallback_request,
            )
            _routing_module._sync_failed_deployment_exclusions(
                request_data,
                fallback_exception,
                deployment_id=failed_deployment_id,
            )
        raise fallback_exception

_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS = 15.0
_ROUTE_RECOVERY_SSE_KEEPALIVE_MIN_DELAY_SECONDS = 1.0
_CODEX_RESPONSES_INITIAL_SSE_GRACE_SECONDS = 5.0

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
    payload = _web_search_native_module._sanitize_response_stream_payload(payload)
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
        # The call's own alias groups.  An item states both its ``id`` and its
        # ``call_id`` while the argument events name only one of them, so the
        # relation has to be remembered where it is declared or the call's
        # accumulated arguments stay under whichever key the events used.
        self._identity_aliases: dict[str, set[str]] = {}
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
        if item_id and json_item.get("type") == "function_call":
            argument_parts = self.argument_parts_by_item_id.get(item_id)
            existing_args = json_item.get("arguments")
            if existing_args is None:
                existing_args = json_item.get("input")
            # One call owns two identity keys: the item's ``id`` and its
            # ``call_id``.  A bridge may state the streamed arguments under
            # either, so the accumulated value is read and written through
            # every key this item answers to; a mirror that states the same
            # call under the other key is the same call, not a second one.
            identity_keys = self._identity_keys_for_item(json_item, item_id)
            accumulated = self._valid_arguments_for_keys(identity_keys)
            if argument_parts:
                accumulated = "".join(argument_parts)
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
                if valid and normalized_args is not None:
                    accumulated = normalized_args
            if accumulated is not None:
                for identity_key in identity_keys:
                    self.arguments_by_item_id[identity_key] = accumulated

        if chunk_type == "response.output_item.done":
            self.output_by_index[index] = _apply_stream_function_arguments(
                json_item,
                self.arguments_by_item_id,
            )
            self.pending_by_index.pop(index, None)
        elif json_item.get("type") == "function_call":
            if json_item.get("type") == "function_call":
                json_item.setdefault("arguments", "")
            self.pending_by_index[index] = json_item

    def _identity_keys_for_item(self, json_item: dict[str, Any], item_id: str) -> list[str]:
        """Every key this call answers to, its own ``id`` first.

        ``_stream_output_item_identity_keys`` already answers this for a tool
        call; the leading ``item_id`` keeps a non-tool item's one key.  The
        declared keys are then unified so an argument event that names one of
        them reaches the value the call owns.
        """
        declared = _stream_output_item_identity_keys(json_item)
        if not declared:
            declared = [item_id]
        elif item_id not in declared:
            declared.insert(0, item_id)
        return self._remember_identity_aliases(declared)

    def _remember_identity_aliases(self, keys: list[str]) -> list[str]:
        """Unify one call's declared keys and return the whole alias group."""
        group = {key for key in keys if isinstance(key, str) and key}
        for key in list(group):
            group |= self._identity_aliases.get(key, set())
        for key in group:
            self._identity_aliases[key] = group
        ordered = [key for key in keys if key in group]
        ordered.extend(sorted(group - set(ordered)))
        return ordered

    def _alias_identity_keys(self, keys: list[str]) -> list[str]:
        """Expand keys with whatever alias group this stream declared."""
        expanded: list[str] = []
        for key in keys:
            if key and key not in expanded:
                expanded.append(key)
        for key in list(keys):
            for alias in sorted(self._identity_aliases.get(key, ())):
                if alias and alias not in expanded:
                    expanded.append(alias)
        return expanded

    def _valid_arguments_for_keys(self, identity_keys: list[str]) -> Optional[str]:
        for identity_key in identity_keys:
            value = self.arguments_by_item_id.get(identity_key)
            if isinstance(value, str):
                return value
        return None

    def _remember_function_arguments(self, dumped: dict[str, Any], chunk_type: str) -> None:
        item_id = _stream_function_arguments_key(dumped)
        if not item_id:
            return
        # The event's own key and the call's sibling key, because a bridge may
        # state the item by ``id`` and these events by ``call_id``; a value the
        # stream delivered is the same value under either name.
        identity_keys = [item_id]
        for key_name in ("item_id", "call_id", "id"):
            value = dumped.get(key_name)
            if isinstance(value, str) and value and value not in identity_keys:
                identity_keys.append(value)
        identity_keys = self._alias_identity_keys(identity_keys)
        if chunk_type.endswith(".delta"):
            delta = dumped.get("delta")
            if isinstance(delta, str):
                for identity_key in identity_keys:
                    self.argument_parts_by_item_id.setdefault(identity_key, []).append(delta)
            return
        arguments = dumped.get("arguments")
        if not isinstance(arguments, str):
            for identity_key in identity_keys:
                parts = self.argument_parts_by_item_id.get(identity_key)
                if isinstance(parts, list):
                    arguments = "".join(parts)
                    break
        if isinstance(arguments, str):
            normalized_args, valid = (
                _responses_request_module._codex_normalized_function_arguments(
                    arguments,
                    empty_is_object=True,
                )
            )
            # Only a usable value is recorded, and only a usable value marks
            # the call finished.  A malformed value used to be stored as
            # ``None``, which then shadowed the call's other identity key and
            # made an already-streamed valid value look absent; an unusable
            # mirror states nothing, so it must not erase what the stream
            # delivered nor claim the call is done before a usable value did.
            if valid and normalized_args is not None:
                for identity_key in identity_keys:
                    self.arguments_by_item_id[identity_key] = normalized_args
                    self.finished_argument_item_ids.add(identity_key)

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

    _trace_module._request_route_trace(
        "codex_compaction_missing_terminal_completed_compat", request_data,
        reason=reason,
        buffered_events=completion_state.event_count,
        completed_output_types=[
            _web_search_native_module._response_item_get(item, "type")
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
        sanitized_chunk = _web_search_native_module._sanitize_web_search_stream_chunk(
            chunk,
            request_data,
        )
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
        async for chunk in _web_search_native_module._external_web_search_bridge_stream(payload):
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
    raw_tool_call_text_filter = _web_search_native_module._RawToolCallTextFilter()

    def remember(chunk: Any) -> Optional[str]:
        sanitized_chunk = _web_search_native_module._sanitize_web_search_stream_chunk(
            chunk,
            request_data,
        )
        if sanitized_chunk is None:
            return None
        sanitized_chunk = _web_search_native_module._sanitize_raw_tool_call_text_stream_chunk(
            sanitized_chunk,
            raw_tool_call_text_filter,
            request_data,
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
                yield _responses_stream_chunk_for_delivery(event, request_data)
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
                    yield _responses_stream_chunk_for_delivery(event, request_data)
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
            yield _responses_stream_chunk_for_delivery(event, request_data)
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
            yield _responses_stream_chunk_for_delivery(event, request_data)
        yield completed_compat
        return
    raise _responses_incomplete_stream_exception(
        "stream ended before response.completed",
        buffer=events,
        request_data=request_data,
    )

async def _yield_native_stream_with_output_limit_continuation(
    buffer: List[Any],
    response: Any,
    request_data: dict,
    *,
    stream_started_at: Optional[float] = None,
    saw_visible_output: bool = False,
) -> AsyncIterator[Any]:
    """Deliver a Chat/Anthropic stream, continuing one its route cut off.

    A route that reaches its own output ceiling closes the answer -- not the
    turn -- with a terminal frame whose reason names the limit.  That frame is
    withheld and the same route is asked for the rest, so a full-document
    translation arrives as one answer instead of stopping mid-sentence.  The
    withheld frame is re-emitted only when no continuation is possible, which
    keeps a client that reads the reason (rather than the text) from being told
    an unfinished answer was finished.
    """

    finished_choice_indices: set[int] = set()
    saw_terminal = False
    success_recorded = False
    native_chunk_count = 0
    # Whether any part of the *answer* reached the client.  Thinking does
    # not count: a client renders it as a live activity block and needs no
    # reconciliation, so a route that only ever streamed thinking can still
    # be replaced by a peer.
    visible_output_delivered = saw_visible_output
    # The terminal frame this route emitted when it hit its output ceiling.
    # Held back until the answer is finished so the client is never told an
    # unfinished answer was complete.
    withheld_ceiling_terminal: Optional[Any] = None
    # The frames that close the answer the withheld ceiling frame belongs to.
    # They are withheld with it and re-emitted only when no continuation runs,
    # so the client always receives a complete terminal sequence.
    withheld_ceiling_closers: List[Any] = []
    # Everything this answer has delivered, so a continuation resumes from the
    # end of it instead of restarting the answer.
    delivered_text_parts: List[str] = []

    def remember_delivered_text(chunk: Any) -> Any:
        text = _responses_output_module._response_text(chunk)
        if text:
            delivered_text_parts.append(text)
        return chunk

    def inspect_native_chunk(chunk: Any) -> bool:
        nonlocal saw_terminal
        nonlocal success_recorded
        nonlocal native_chunk_count
        nonlocal visible_output_delivered
        chunk_exception = _stream_chunk_error_exception(chunk)
        if chunk_exception is not None:
            if saw_terminal:
                return False
            raise chunk_exception
        native_chunk_count += 1
        visible_output_delivered = (
            visible_output_delivered or _stream_chunk_has_visible_output(chunk)
        )
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

    async def deliver_route() -> AsyncIterator[Any]:
        nonlocal withheld_ceiling_terminal
        nonlocal native_chunk_count
        for chunk in buffer:
            if not inspect_native_chunk(chunk):
                return
            yield remember_delivered_text(chunk)
        # Whether the ceiling frame has been withheld.  Once it has, the
        # frames that close the same answer belong to it: ``message_stop``
        # and the ``finish_reason`` terminal state the same completion the
        # withheld frame described, so they are withheld with it rather than
        # delivered ahead of the continuation that has not run yet.
        ceiling_withheld = False
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
            stream_started_at=stream_started_at,
            saw_visible_output=visible_output_delivered,
            initial_chunk_count=len(buffer),
        ):
            chunk_exception = _stream_chunk_error_exception(chunk)
            if chunk_exception is not None:
                if saw_terminal:
                    return
                raise chunk_exception
            if _stream_continuation_module._native_stream_output_limit_reason(chunk) is not None:
                # This frame carries the answer's own completion state, so the
                # delivery below is what decides whether it is ever sent.
                withheld_ceiling_terminal = chunk
                ceiling_withheld = True
                native_chunk_count += 1
                # The terminal frame that closes this answer arrives in its own
                # transport read on some routes.  Returning here read the
                # ceiling and dropped the stop: with no continuation possible
                # the client was handed the withheld ``max_tokens`` frame and
                # never the ``message_stop`` that ends it, so a finished answer
                # rendered as one cut off at the route's output limit.  Keep
                # reading and withhold the closing frames instead.
                continue
            if ceiling_withheld and _native_stream_chunk_is_terminal(
                chunk,
                request_data,
                finished_choice_indices,
            ):
                # Same answer, so the same fate as the ceiling frame.
                withheld_ceiling_closers.append(chunk)
                native_chunk_count += 1
                continue
            if not inspect_native_chunk(chunk):
                return
            yield remember_delivered_text(chunk)

    try:
        async for chunk in deliver_route():
            yield chunk
    except Exception as exception:
        if _routing_module._is_local_shutdown_exception(exception):
            # The worker is being replaced.  Once the client has received
            # answer text, a synthetic terminal failure would be rendered
            # as ``router_restarting`` and stop the turn; let the response
            # close so the client-side reconnect path can take over.
            return
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
        # Committing the buffered start to the client -- which the chunk
        # cap does after 20 frames -- must not cost a thinking-only turn
        # its route recovery.  An answer that was never delivered leaves
        # nothing to reconcile, so a peer route may still serve the turn;
        # a stream that already showed answer text keeps its pinned
        # behaviour and reports the failure itself.
        if visible_output_delivered:
            raise
        async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
            request_data,
            exception,
        ):
            yield fallback_chunk
        return

    if withheld_ceiling_terminal is not None:
        # The route stopped at its own ceiling.  Ask it for the rest and splice
        # the continuation into this same answer; the withheld frame is sent
        # only when there is nothing more to ask for.
        continued = False
        async for chunk in _stream_continuation_module._stream_native_continuation_rounds(
            request_data,
            "".join(delivered_text_parts),
            stream_idle_timeout_seconds=_resolved_stream_timeout_seconds(
                request_data,
                "stream_idle_timeout_seconds",
                _routing_module._stream_idle_timeout_seconds_for_request,
            ),
        ):
            continued = True
            yield remember_delivered_text(chunk)
        # The continuation owns the answer's last frame: when it reached a
        # natural stop it already emitted that frame, and when it ran out of
        # rounds the withheld "length" frame is the honest report that the
        # answer is still unfinished.  Re-emitting the withheld frame on top of
        # a natural stop would tell the client an answer that finished had hit
        # the ceiling.
        if not continued:
            yield withheld_ceiling_terminal
            # The frame that closes the withheld one travels with it.  Sending
            # the ceiling alone left the client on an answer that never ended,
            # which it renders as a reply cut off at the route's output limit.
            for closer in withheld_ceiling_closers:
                yield closer
        return

    if not saw_terminal:
        incomplete_exception = _native_stream_incomplete_exception(
            request_data,
            buffered_chunks=native_chunk_count,
        )
        # Same rule for the stream that simply ended: the route is only
        # pinned once it has delivered part of the answer.  A clean EOF
        # after thinking alone advances to the next route instead of
        # ending the turn with a route the user never saw an answer from.
        if visible_output_delivered:
            if _routing_module._is_local_shutdown_exception(
                incomplete_exception
            ):
                return
            raise incomplete_exception
        async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
            request_data,
            incomplete_exception,
        ):
            yield fallback_chunk
        return

async def _yield_guarded_original_stream(
    buffer: List[Any],
    response: Any,
    request_data: dict,
    *,
    saw_responses_completed: bool = False,
    stream_started_at: Optional[float] = None,
    saw_visible_output: bool = False,
    synthesize_completed_on_clean_eof_after_visible_output: bool = False,
    on_responses_completed: Optional[Callable[[], None]] = None,
) -> AsyncIterator[Any]:
    if not _request_is_responses_stream(request_data):
        async for chunk in _yield_native_stream_with_output_limit_continuation(
            buffer,
            response,
            request_data,
            stream_started_at=stream_started_at,
            saw_visible_output=saw_visible_output,
        ):
            yield chunk
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
    client_open_tool_output_indexes: set[int] = set()
    # A Responses stream reports its outcome through a terminal ``completed``
    # event rather than through LiteLLM's stream-completion callback, so this
    # branch has to name the serving route itself. Without it a Responses
    # request that answered perfectly never looks like a route that worked.
    success_recorded = False

    def record_responses_stream_success() -> None:
        nonlocal success_recorded
        if success_recorded or on_responses_completed is None:
            return
        success_recorded = True
        on_responses_completed()

    completion_state = _ResponsesStreamCompletionState(request_data)
    saw_image_generation_activity = False
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
    raw_tool_call_text_filter = _web_search_native_module._RawToolCallTextFilter()

    def normalize_tool_bridge_chunk(chunk: Any) -> Any:
        return _responses_output_module._normalize_response_stream_tool_bridge_chunk(
            chunk,
            namespace_by_name,
            custom_tool_names,
            custom_tool_item_ids,
            custom_tool_input_delta_tracker,
        )

    def sanitize_visible_text_chunk(chunk: Any) -> Optional[Any]:
        return _web_search_native_module._sanitize_raw_tool_call_text_stream_chunk(
            chunk,
            raw_tool_call_text_filter,
            request_data,
        )

    def remember_client_tool_call_exposure(chunk: Any) -> None:
        """Track which tool calls the client was actually told about.

        A Responses client opens a tool call on ``response.output_item.added`` and
        closes it on ``response.output_item.done``; the bridge takeover replays
        neither for items the router consumed.  The router's own pending map is
        not enough: a synthesized or replayed item can be closed in that map
        without the client ever having seen the opening event.
        """
        dumped = _stream_chunk_dump(chunk)
        if not isinstance(dumped, dict):
            return
        chunk_type = _stream_chunk_type(dumped)
        if chunk_type not in {"response.output_item.added", "response.output_item.done"}:
            return
        item = dumped.get("item")
        if not _stream_output_item_is_tool_call(
            _web_search_native_module._response_item_get(item, "type")
        ):
            return
        output_index = dumped.get("output_index")
        if not isinstance(output_index, int):
            return
        if chunk_type == "response.output_item.added":
            client_open_tool_output_indexes.add(output_index)
        else:
            client_open_tool_output_indexes.discard(output_index)

    def deliver_client_chunk(chunk: Any) -> Any:
        remember_client_tool_call_exposure(chunk)
        return _responses_stream_chunk_for_delivery(
            completion_state.chunk_for_delivery(chunk, request_data),
            request_data,
        )

    def undeclared_tool_call_name(chunk: Any) -> Optional[str]:
        """The name of a call naming a tool this request never declared.

        The declared set is carried by the request itself, so a call outside it
        is a protocol error rather than a capability gap: the client cannot
        execute the name, and letting it through ends the turn on a call that
        nothing will ever answer.
        """

        if not _codex_contract_module.declared_tool_names(request_data):
            return None
        dumped = _stream_chunk_dump(chunk)
        if not isinstance(dumped, dict):
            return None
        chunk_type = _stream_chunk_type(dumped)
        item: Any = None
        if chunk_type in {"response.output_item.added", "response.output_item.done"}:
            item = dumped.get("item")
        elif chunk_type == "response.completed":
            return None
        elif isinstance(dumped.get("item"), dict):
            item = dumped.get("item")
        if not isinstance(item, dict) or item.get("type") != "function_call":
            return None
        name = _codex_contract_module._tool_definition_name(item)
        if name is None or not _codex_contract_module.is_undeclared_tool_call(
            request_data,
            name,
        ):
            return None
        return name

    def remember(chunk: Any) -> None:
        nonlocal saw_image_generation_activity
        event_tail.append(chunk)
        if len(event_tail) > _STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS:
            del event_tail[0]
        completion_state.remember(chunk)
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
    def synthesized_text() -> str:
        return completion_state.synthesized_text()

    def completed_output_is_tool_call_only() -> bool:
        return bool(completed_output_items) and all(
            _stream_output_item_is_tool_call(_web_search_native_module._response_item_get(item, "type"))
            for item in completed_output_items.values()
        )

    def completed_output_has_visible_assistant_text() -> bool:
        return any(
            _response_item_has_assistant_text(item)
            for item in completed_output_items.values()
        )

    def stream_chunk_has_visible_assistant_text_output(chunk: Any) -> bool:
        dumped = _stream_chunk_dump(chunk)
        chunk_type = _stream_chunk_type(dumped)
        text_fragment, _is_done_text = _stream_chunk_text_fragment(dumped)
        if text_fragment.strip():
            return True
        if chunk_type in {"response.output_item.added", "response.output_item.done"}:
            return _response_item_has_assistant_text(dumped.get("item"))
        return False

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
        _trace_module._request_route_trace(
            "responses_active_stream_synthesized_completed_event", request_data,
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
            not isinstance(item.get("type"), str) or item.get("type") not in {"reasoning", "custom_tool_call"}
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
            _trace_module._request_route_trace(
                "responses_active_stream_synthesized_completed_event", request_data,
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
        _trace_module._request_route_trace(
            "responses_active_stream_failed_terminal_event", request_data,
            exception=_routing_module._trace_exception(failure),
            reason=reason,
            already_yielded_visible_output=True,
        )
        # This terminal is only built once the stream has already delivered part
        # of the answer, so the recovery poll is unavailable and the sentence
        # must say what the reader can actually do.
        return _synthesized_failed_response_event(
            request_data,
            failure,
            after_visible_output=True,
        )

    for chunk in buffer:
        _web_search_native_module._mark_provider_native_web_search_event(
            request_data, chunk
        )
        sanitized_chunk = _web_search_native_module._sanitize_web_search_stream_chunk(
            chunk,
            request_data,
        )
        if sanitized_chunk is None:
            continue
        sanitized_chunk = sanitize_visible_text_chunk(sanitized_chunk)
        if sanitized_chunk is None:
            continue
        chunk = normalize_tool_bridge_chunk(sanitized_chunk)
        if chunk is None:
            continue
        remember(chunk)
        visible_output_seen = visible_output_seen or _stream_chunk_has_delivered_output(chunk)
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
            record_responses_stream_success()
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
                remember_client_tool_call_exposure(synthetic_chunk)
                yield synthetic_chunk
            yield deliver_client_chunk(chunk)
            await _close_async_iterator_safely(response)
            return
        elif _responses_stream_chunk_is_incomplete_terminal(chunk):
            if _is_young_router_terminal_failure_event(chunk):
                yield deliver_client_chunk(chunk)
                await _close_async_iterator_safely(response)
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
        undeclared_name = undeclared_tool_call_name(chunk)
        if undeclared_name is not None:
            _trace_module._request_route_trace(
                "codex_undeclared_tool_call_rejected", request_data,
                include_route_identity=True,
                tool_name=undeclared_name,
                declared_tool_names=sorted(
                    _codex_contract_module.declared_tool_names(request_data)
                ),
            )
            terminal = _json_stream_event(
                _codex_contract_module.undeclared_tool_response_event(
                    undeclared_name,
                    _stream_output_item_key(
                        _stream_chunk_dump(chunk).get("item")
                        if isinstance(_stream_chunk_dump(chunk), dict)
                        else None
                    ),
                    request_data,
                )
            )
            yield terminal
            await _close_async_iterator_safely(response)
            return
        yield deliver_client_chunk(chunk)

    try:
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
            stream_started_at=stream_started_at,
            saw_visible_output=visible_output_seen,
            initial_chunk_count=len(buffer),
        ):
            _web_search_native_module._mark_provider_native_web_search_event(
                request_data, chunk
            )
            sanitized_chunk = _web_search_native_module._sanitize_web_search_stream_chunk(
                chunk,
                request_data,
            )
            if sanitized_chunk is None:
                continue
            sanitized_chunk = sanitize_visible_text_chunk(sanitized_chunk)
            if sanitized_chunk is None:
                continue
            chunk = normalize_tool_bridge_chunk(sanitized_chunk)
            if chunk is None:
                continue
            if _is_young_router_terminal_failure_event(chunk):
                yield deliver_client_chunk(chunk)
                await _close_async_iterator_safely(response)
                return
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
            visible_output_seen = visible_output_seen or _stream_chunk_has_delivered_output(chunk)
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
                record_responses_stream_success()
                for synthetic_chunk in _synthesized_missing_completed_tool_events(
                    chunk,
                    seen_output_item_ids,
                    pending_tool_items,
                ):
                    remember(synthetic_chunk)
                    yield synthetic_chunk
                yield deliver_client_chunk(chunk)
                await _close_async_iterator_safely(response)
                return
            elif _responses_stream_chunk_is_incomplete_terminal(chunk):
                if _is_young_router_terminal_failure_event(chunk):
                    yield deliver_client_chunk(chunk)
                    await _close_async_iterator_safely(response)
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
            yield deliver_client_chunk(chunk)
    except (Exception, asyncio.CancelledError) as exc:
        if isinstance(exc, asyncio.CancelledError) and not _routing_module._is_local_shutdown_exception(exc):
            # A cancellation is the runtime taking this task down for its own
            # reasons, so it stays cooperative and is never read as a finished
            # turn.  Only a cancellation that belongs to this worker's own
            # app-exit teardown is turned into a terminal below.
            raise
        if _routing_module._is_local_shutdown_exception(exc):
            # This worker is the one being replaced, so the failure says nothing
            # about any upstream and there is no route to quarantine.  Ending
            # the stream with no terminal is what the client renders as
            # ``upstream_route_failure``, which interrupts a turn the user was
            # already reading; the output that did arrive is delivered as a
            # completed response so the conversation continues instead.
            terminal_items = terminal_completed_output_items()
            synthetic_completed = _synthesized_completed_response_event(
                terminal_items,
                completion_state.created_response,
                completion_state.model,
                fallback_text=synthesized_text(),
            )
            if synthetic_completed is not None:
                remember(synthetic_completed)
                yield synthetic_completed
            return
        if saw_image_generation_activity:
            completed = completed_image_generation_terminal_event(
                "stream error after completed image generation output"
            )
            if completed is not None:
                remember(completed)
                yield completed
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
        terminal_events = completed_custom_tool_terminal_events()
        for synthetic_chunk in terminal_events:
            remember(synthetic_chunk)
            yield synthetic_chunk
        if terminal_events:
            return

    if not saw_responses_completed and pending_tool_items:
        for synthetic_chunk in _synthesized_pending_tool_completion_events(
            pending_tool_items,
            completed_output_items,
            completion_state.created_response,
            completion_state.model,
        ):
            remember(synthetic_chunk)
            remember_client_tool_call_exposure(synthetic_chunk)
            yield synthetic_chunk
        return

    if not saw_responses_completed:
        if visible_output_seen:
            incomplete_exception = _responses_incomplete_stream_exception(
                "stream ended before response.completed",
                buffer=event_tail,
                request_data=request_data,
            )
            if _routing_module._is_local_shutdown_exception(incomplete_exception):
                # This worker is being replaced, so the stream ended because the
                # process did -- not because any upstream refused.  Ending with
                # no terminal at all is what the client renders as
                # ``upstream_route_failure``, interrupting a turn the user was
                # already reading and asking them to send it again.  The answer
                # text that did arrive is real output, so the turn is closed the
                # way a finished one is: whatever was delivered becomes the
                # response and the conversation continues.
                terminal_items = terminal_completed_output_items()
                synthetic_completed = _synthesized_completed_response_event(
                    terminal_items,
                    completion_state.created_response,
                    completion_state.model,
                    fallback_text=synthesized_text(),
                )
                if synthetic_completed is not None:
                    remember(synthetic_completed)
                    yield synthetic_completed
                return
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
            yield failed_terminal_event(
                "stream ended before response.completed",
                incomplete_exception,
            )
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
    # Uvicorn closes accepted connections before waiting for in-flight
    # requests during a rolling restart. Classify that teardown before any
    # failover or recovery path can quarantine a healthy deployment or turn
    # the interruption into a reconnect-triggering exception.
    if _routing_module._is_local_shutdown_exception(exception):
        # A rolling worker replacement must never surface a synthetic failure
        # event to the client: that event is rendered as ``router_restarting``
        # and aborts the turn even though the replacement worker can continue
        # it. Leave the stream open for the client-side retry/reconnect path.
        return

    # A provider may return an async iterator successfully and raise the
    # upstream 400 only on a later iterator read. That bypasses LiteLLM's
    # generic-call exception path, so record the route-local compaction
    # capability evidence before any recovery/fallback policy is evaluated.
    _routing_module._apply_current_selected_deployment_to_request(request_data)
    _routing_module._mark_real_compaction_failure_capability_unsupported(
        request_data,
        exception,
    )
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
        and _routing_module._is_route_recovery_poll_error(exception)
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
                and _routing_module._is_route_recovery_poll_error(exc)
            ):
                _trace_module._request_route_trace(
                    "stream_fallback_route_recovery_poll", request_data,
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
                _trace_module._request_route_trace(
                    "responses_stream_fallback_failed_terminal_event", request_data,
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
            or _routing_module._is_route_recovery_poll_error(final_exception)
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
            or _routing_module._is_route_recovery_poll_error(final_exception)
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
        _trace_module._request_route_trace(
            "responses_stream_failed_terminal_event", request_data,
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
    if (
        _routing_module._is_failed_responses_stream_response(response)
        or bool(getattr(response, "_young_router_terminal_failed_stream", False))
    ):
        failed_exception = getattr(response, "exception", None)
        failed_request = getattr(response, "request_data", None)
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
    saw_delivered_output = False
    stream_exhausted = True
    stream_started_at = time.monotonic()
    completion_state = _ResponsesStreamCompletionState(request_data)
    raw_tool_call_text_filter = _web_search_native_module._RawToolCallTextFilter()

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
            _web_search_native_module._mark_provider_native_web_search_event(
                request_data, chunk
            )
            sanitized_chunk = _web_search_native_module._sanitize_web_search_stream_chunk(
                chunk,
                request_data,
            )
            if sanitized_chunk is None:
                continue
            sanitized_chunk = _web_search_native_module._sanitize_raw_tool_call_text_stream_chunk(
                sanitized_chunk,
                raw_tool_call_text_filter,
                request_data,
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
            if _is_young_router_terminal_failure_event(chunk):
                for buffered_chunk in [*buffer, chunk]:
                    yield _responses_stream_chunk_for_delivery(
                        buffered_chunk,
                        request_data,
                    )
                await _close_async_iterator_safely(response)
                return
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
            saw_visible_output = saw_visible_output or (
                _stream_chunk_has_visible_output(chunk)
            )
            # The start buffer exists so a route that fails before showing
            # anything can be replaced without the reader noticing.  Thinking is
            # shown, so it ends that window: holding it back until the first
            # answer token made a whole reasoning block arrive at once, after the
            # model had already finished thinking.
            saw_delivered_output = saw_delivered_output or (
                _stream_chunk_is_public_reasoning_delta(chunk)
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
                    if _is_young_router_terminal_failure_event(chunk):
                        for buffered_chunk in buffer:
                            yield _responses_stream_chunk_for_delivery(
                                buffered_chunk,
                                request_data,
                            )
                        await _close_async_iterator_safely(response)
                        return
                    completed_compat_chunk = (
                        _responses_output_limit_terminal_state_as_completed_chunk(
                            chunk,
                            completion_state,
                            request_data,
                        )
                    )
                    if completed_compat_chunk is not None:
                        for buffered_chunk in buffer[:-1]:
                            yield _responses_stream_chunk_for_delivery(
                                buffered_chunk,
                                request_data,
                            )
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
                        _trace_module._request_route_trace(
                            "responses_stream_output_limit_fallback_start", request_data,
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
            if (
                saw_visible_output
                or saw_delivered_output
                or saw_responses_completed
                or (
                    len(buffer) >= _STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS
                    and not is_responses_stream
                )
            ):
                # Which frame ends the start buffer decides whether a thinking
                # block can stream at all, and "the reader saw nothing for the
                # whole reasoning phase" is otherwise indistinguishable from a
                # slow route in the trace.
                _trace_module._request_route_trace(
                    "responses_start_buffer_released", request_data,
                    released_by=_stream_chunk_type(chunk),
                    buffered_chunks=len(buffer),
                    buffered_types=[
                        chunk_type
                        for chunk_type in dict.fromkeys(
                            _stream_chunk_type(buffered) for buffered in buffer
                        )
                    ][:12],
                    answer_started=saw_visible_output,
                    thinking_seen=saw_delivered_output,
                )
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
                yield _responses_stream_chunk_for_delivery(
                    buffered_chunk,
                    request_data,
                )
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
                yield _responses_stream_chunk_for_delivery(
                    buffered_chunk,
                    request_data,
                )
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
        saw_visible_output=saw_visible_output or saw_delivered_output,
        on_responses_completed=(
            lambda: _routing_module._record_deployment_success_for_cooldown(
                request_data
            )
        ),
    ):
        yield chunk

# The recovery conversation moved to young_router.proxy.streaming_recovery; the
# names stay importable here so every caller keeps its one entry point.
from .streaming_recovery import (  # noqa: E402
    _anthropic_messages_stream_chunk_is_terminal,
    _chat_completions_stream_chunk_is_terminal,
    _configured_deployment_ids,
    _configured_deployment_orders,
    _is_route_recovery_sse_keepalive,
    _is_stream_keepalive_chunk,
    _native_stream_chunk_is_terminal,
    _native_stream_incomplete_exception,
    _next_configured_order,
    _reset_route_exhaustion_retry_state,
    _route_recovery_cooldown_wait_delay,
    _route_recovery_exhausted_order,
    _route_recovery_next_poll_order,
    _route_recovery_poll_cooldown_wait,
    _route_recovery_poll_has_no_available_deployments,
    _route_recovery_poll_keep_going,
    _route_recovery_sse_keepalive,
    _sleep_route_recovery_poll_interval,
    _stream_keepalive_chunk,
    _stream_native_route_recovery_poll_attempt,
    _stream_route_recovery_poll,
    _stream_route_recovery_poll_attempt,
    _stream_streaming_error_fallback,
    _yield_downstream_keepalive_stream,
    _yield_native_stream_after_terminal,
)
