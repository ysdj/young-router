from __future__ import annotations

import base64
import copy
import re
import time
import uuid
from typing import Callable

from . import codex_fast_tier as _codex_fast_tier_module
from . import codex_contract as _codex_contract_module
from . import anthropic_dsml as _anthropic_dsml_module
from . import image_generation as _image_generation_module
from . import image_inputs as _image_inputs_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_execution as _responses_execution_module
from . import responses_output as _responses_output_module
from . import historical_reasoning_note as _historical_note_module
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
    _CHAT_REASONING_DELTA_KEYS,
    _CURRENT_SELECTED_DEPLOYMENT_BOX,
    _IMAGE_GENERATION_TOOL_FALLBACK_ATTEMPTS_METADATA_KEY,
    _JSONStreamEvent,
    _RESPONSES_STREAM_COMPLETED_TYPES,
    _RESPONSES_STREAM_INCOMPLETE_STATUSES,
    _RESPONSES_STREAM_INCOMPLETE_TYPES,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _HEAD_HEDGE_METADATA_KEY,
    _ROUTE_RECOVERY_POLL_METADATA_KEY,
    _RouteOrder,
    _STREAM_ERROR_FALLBACK_METADATA_KEY,
    _STREAM_ERROR_FALLBACK_START_BUFFER_CHUNKS,
    _STREAM_FALLBACK_METADATA_KEY,
    _STREAM_IDLE_TIMEOUT_METADATA_KEY,
    _STREAM_START_TIMEOUT_METADATA_KEY,
    _START_TIMEOUT_OBSERVED_KEY,
    _TERMINAL_FAILURE_REQUEST_KEY,
    _UPSTREAM_INPUT_ITEM_LIMIT_ATTR,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _normalize_response_completed_event_usage,
    _proxy_shutdown_in_progress,
    asyncio,
    datetime,
    inspect,
    json,
    os,
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
            and "response.output_item.done" not in text
            and "function_call_arguments" not in text
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

_ANNOUNCED_RESPONSE_ID_KEY = "_young_router_announced_response_id"
_ANNOUNCED_FRAMES_SENT_KEY = "_young_router_announced_frames_sent"

# The two frames that say "this turn exists" and carry no content of their own.
_RESPONSE_OPENING_FRAME_TYPES = frozenset(
    {"response.created", "response.in_progress"}
)


def _announced_response_id(request_data: Optional[dict]) -> Optional[str]:
    """The id this proxy already gave the client, if it has spoken."""

    if not isinstance(request_data, dict):
        return None
    value = request_data.get(_ANNOUNCED_RESPONSE_ID_KEY)
    return value if isinstance(value, str) and value else None


def _announce_response_id(request_data: dict) -> str:
    """Mint the id this turn is announced under, once per request."""

    existing = _announced_response_id(request_data)
    if existing is not None:
        return existing
    minted = "resp_" + uuid.uuid4().hex
    request_data[_ANNOUNCED_RESPONSE_ID_KEY] = minted
    return minted


def _response_opening_events(
    request_data: dict,
    completion_state: Any,
) -> list[Any]:
    """The frames that open a Responses turn before the upstream has spoken.

    A Responses client's clock starts at ``response.created``.  On this router
    that frame cannot come from the upstream: the routes serving these models
    answer on the Chat wire, where the first thing LiteLLM yields is the first
    token.  Measured on the live host, the upstream wrote its own
    ``response.created`` 230 ms in while the client still held nothing at
    900-1300 ms; the official endpoint opens the same turn at 170 ms.

    So the proxy opens it, on the route it has already chosen, and keeps the
    id: every later frame is rewritten to it, so the client sees one turn with
    one id no matter which deployment ends up answering.
    """

    announced_id = _announce_response_id(request_data)
    model = (
        request_data.get("model")
        if isinstance(request_data.get("model"), str)
        else None
    )
    payload: dict[str, Any] = {
        "id": announced_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "in_progress",
        "output": [],
    }
    if model:
        payload["model"] = model
    if not isinstance(getattr(completion_state, "created_response", None), dict):
        completion_state.created_response = copy.deepcopy(payload)
    _trace_module._request_route_trace(
        "responses_response_announced_before_upstream",
        request_data,
        response_id=announced_id,
        model=model,
    )
    return [
        _json_stream_event({"type": "response.created", "response": payload}),
        _json_stream_event({"type": "response.in_progress", "response": payload}),
    ]


def _align_sse_response_identity_block(
    block: str,
    announced_id: str,
) -> Optional[str]:
    """One encoded SSE block, rewritten under ``announced_id`` or dropped."""

    if "data:" not in block:
        return block
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
        return block
    try:
        payload = json.loads(payload_text)
    except Exception:
        return block
    if not isinstance(payload, dict):
        return block
    response = payload.get("response")
    response_id = response.get("id") if isinstance(response, dict) else None
    if payload.get("type") in _RESPONSE_OPENING_FRAME_TYPES and response_id != announced_id:
        # A second opening carrying the upstream's id would give the client
        # two ids for one turn.
        return None
    if not isinstance(response, dict) or response_id == announced_id:
        return block
    rewritten = dict(payload)
    response = dict(response)
    response["id"] = announced_id
    rewritten["response"] = response
    normalized_data = json.dumps(rewritten, ensure_ascii=False)
    next_lines: list[str] = []
    replaced_data = False
    for line in block.splitlines():
        if line.startswith("data:"):
            if not replaced_data:
                next_lines.append(f"data: {normalized_data}")
                replaced_data = True
            continue
        next_lines.append(line)
    line_separator = "\r\n" if "\r\n" in block else "\n"
    return line_separator.join(next_lines)

def _align_sse_response_identity_text(
    text: str,
    announced_id: str,
) -> Optional[str]:
    """Every encoded SSE block in ``text``, aligned; ``None`` when none is left."""

    output: list[str] = []
    dropped = False
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
            block, delimiter = text[index:], ""
        else:
            position, delimiter = min(candidates, key=lambda item: item[0])
            block = text[index:position]
        aligned = _align_sse_response_identity_block(block, announced_id)
        if aligned is None:
            dropped = True
        else:
            output.append(aligned + delimiter)
        if not delimiter:
            break
        index = position + len(delimiter)
    if dropped and not output:
        return None
    return "".join(output)

def _align_sse_response_identity_chunk(
    chunk: str | bytes,
    announced_id: str,
) -> Optional[str | bytes]:
    if not isinstance(chunk, bytes):
        return _align_sse_response_identity_text(chunk, announced_id)
    try:
        text = chunk.decode("utf-8")
    except Exception:
        return chunk
    aligned = _align_sse_response_identity_text(text, announced_id)
    if aligned is None:
        return None
    return chunk if aligned == text else aligned.encode("utf-8")

def _align_announced_response_identity(
    delivered: Any,
    request_data: Optional[dict] = None,
) -> Any:
    """Keep every frame of a turn under the id the client was told about."""

    announced_id = _announced_response_id(request_data)
    if announced_id is None or delivered is None:
        return delivered
    if not _request_is_responses_stream(request_data):
        return delivered
    if isinstance(delivered, (str, bytes)):
        return _align_sse_response_identity_chunk(delivered, announced_id)
    if not isinstance(delivered, dict):
        return delivered
    frame_type = _stream_chunk_type(delivered)
    response = delivered.get("response")
    response_id = response.get("id") if isinstance(response, dict) else None
    if frame_type in _RESPONSE_OPENING_FRAME_TYPES and response_id != announced_id:
        # The turn is already open under our id.  A second opening carrying the
        # upstream's id would give the client two ids for one turn.
        return None
    if isinstance(response, dict) and response_id != announced_id:
        rewritten = dict(delivered)
        response = dict(response)
        response["id"] = announced_id
        rewritten["response"] = response
        return type(delivered)(rewritten) if type(delivered) is not dict else rewritten
    return delivered


def _delivered_frames(
    chunks: Any,
    request_data: Optional[dict] = None,
):
    """Deliver each frame, skipping the ones this router decides not to send."""

    for chunk in chunks:
        delivered = _responses_stream_chunk_for_delivery(chunk, request_data)
        if delivered is not None:
            yield delivered


def _responses_stream_chunk_for_delivery(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> Any:
    """Adapt one chunk, then keep it under the id the client was given."""

    return _align_announced_response_identity(
        _responses_stream_chunk_adapted_for_delivery(chunk, request_data),
        request_data,
    )


def _responses_stream_chunk_adapted_for_delivery(
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
    # The bound rebuilds and serializes the whole request; only a frame that
    # states the completion can use it, and this runs once per delivered frame.
    input_token_upper_bound = (
        _codex_request_input_token_upper_bound(request_data)
        if _responses_stream_chunk_is_completed(chunk)
        else None
    )
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
            _trace_module._request_route_trace(
                "responses_summary_frame_dropped",
                request_data,
                event_type=_stream_chunk_type(chunk),
                reasoning=request_data.get("reasoning")
                if isinstance(request_data, dict)
                else None,
                model_reasoning_summary=request_data.get("model_reasoning_summary")
                if isinstance(request_data, dict)
                else None,
            )
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
            _trace_module._request_route_trace(
                "responses_summary_frame_dropped",
                request_data,
                event_type=_stream_chunk_type(json_chunk),
                reasoning=request_data.get("reasoning")
                if isinstance(request_data, dict)
                else None,
                model_reasoning_summary=request_data.get("model_reasoning_summary")
                if isinstance(request_data, dict)
                else None,
            )
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
    elif (
        status_code is None
        and error_type == "upstream_error"
        and error_code != "stream_read_error"
    ):
        # The station's own envelope for "my upstream failed" (New API /
        # sub2api): ``{"type":"upstream_error","message":"Upstream access
        # forbidden, please contact administrator"}``.  It is an answer about
        # the route, not about the bytes, so a stream that ends on it must park
        # the route and advance to a sibling.  Without a status the frame read
        # as an unclassified stop: the route stayed first in the order, and the
        # client's own non-streaming fallback replayed the same 655k-byte turn
        # into that route's ~2-minute silent-connection ceiling (live
        # 2026-11-11, nine identical attempts).  A refusal the message itself
        # names (a field the route's validator rejected, a context ceiling)
        # still wins by its wording in the classifiers below.
        status_code = 502

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

def _stream_chunk_completed_dump(chunk: Any) -> dict[str, Any]:
    """The frame that states the completion, not the first frame of the read.

    A coalesced read is named by its last frame, so its ``response`` payload
    belongs to that frame; looking in the first frame finds nothing when a
    delta or lifecycle event travelled ahead of ``response.completed``.
    """

    dumps = _stream_chunk_dumps(chunk)
    for dumped in reversed(dumps):
        if dumped.get("type") in _RESPONSES_STREAM_COMPLETED_TYPES:
            return dumped
    return dumps[-1] if dumps else {}

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

def _stream_chunk_is_native_terminal(chunk: Any) -> bool:
    """True for the frame that ends a Chat or Anthropic Messages stream.

    ``_responses_stream_chunk_is_completed`` only reads Responses events.  A
    native Messages stream ends on ``message_stop`` (bytes or a dict), and a
    Chat stream ends on ``[DONE]`` or a choice with a finish reason.  The
    cooldown's success accounting asks this question once per turn, so a
    streamed answer that completed must clear its route's failure count just
    like a whole-body answer does.
    """

    text = _sse_stream_chunk_text(chunk)
    if text is not None:
        return "event: message_stop" in text or "data: [DONE]" in text
    dumped = _stream_chunk_dump(chunk)
    if dumped.get("type") == "message_stop":
        return True
    choices = dumped.get("choices")
    if isinstance(choices, list):
        return any(
            isinstance(choice, dict) and choice.get("finish_reason") is not None
            for choice in choices
        )
    return False

def _responses_stream_chunk_is_completed(chunk: Any) -> bool:
    chunk_type = _stream_chunk_type(chunk)
    return chunk_type in _RESPONSES_STREAM_COMPLETED_TYPES or (
        chunk_type == "response"
        and _stream_chunk_response_status(chunk).lower() == "completed"
    )

def _responses_completed_chunk_has_usable_output(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    if not _responses_stream_chunk_is_completed(chunk):
        return False
    dumped = _stream_chunk_completed_dump(chunk)
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

def _responses_stream_requires_terminal_delivery(
    request_data: Optional[dict],
) -> bool:
    """Whether this pipeline owes the reader a frame that states the outcome.

    A Responses reader has no end-of-body signal of its own: the shape it parses
    ends a turn only at ``response.completed``/``incomplete``/``failed``.  A body
    that closes without one leaves the client waiting on a turn nobody will ever
    finish -- observed live 2026-11-03, a deepseek route answered for seventeen
    minutes and then ended the stream with no terminal event, and the reader's
    own error was "stream ended before a terminal response event".
    """

    return _request_is_responses_stream(request_data)


def _stream_failure_ended_without_terminal(exception: Exception) -> bool:
    """Whether this exception is the stream *ending*, not a refusal of the turn.

    Only the shapes that cut a stream this proxy was already delivering owe the
    reader a terminal frame here: a body that stopped short of its terminal
    event, a read of an accepted response that failed, or a route that closed
    mid-answer.  A refusal that names the request itself (a malformed local
    chunk, an oversized prompt) already has a cause the reader must act on, and
    the layers above own it -- synthesizing there would replace a tested
    classified stop with a generic one.
    """

    if _routing_module._is_context_size_error(exception):
        # A prompt the route refused for its size has a cause the reader must
        # fix, and the truncation retry above owns it: naming it here would
        # replace that answer with a generic stop.
        return False
    ended_without_terminal = bool(
        getattr(exception, "responses_stream_incomplete", False)
        or getattr(exception, "stream_incomplete", False)
        or _routing_module._is_route_cut_stream_error(exception)
        or _routing_module._is_upstream_body_read_failure(exception)
        # This proxy's own watchdogs: their budgets expired with the body still
        # open, which is the same obligation -- the reader must be told, not
        # left waiting on a stream nobody will finish.
        or _routing_module._is_local_stream_timeout_error(exception)
        or _routing_module._is_local_stream_start_timeout_error(exception)
    )
    if not ended_without_terminal:
        return False
    # A policy of ``error`` says no replay can change this answer; those stops
    # already have a classified sentence and are the caller's to surface.
    return (
        _routing_module._recovery_policy_for_exception(exception)
        != _routing_module._RECOVERY_POLICY_ERROR
    )


def _responses_stream_chunk_state_terminal(chunk: Any) -> bool:
    """Whether this frame already states how the turn ended."""

    dumps = _stream_chunk_dumps(chunk)
    for candidate in dumps if dumps else [chunk]:
        if _responses_stream_chunk_is_completed(candidate):
            return True
        if _responses_stream_chunk_is_incomplete_terminal(candidate):
            return True
        if _is_young_router_terminal_failure_event(candidate):
            return True
    return False


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
        "upstream_model_unpriced",
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

def _request_output_token_budget(request_data: Optional[dict]) -> Optional[int]:
    """The output budget the client stated, when it stated one."""

    data = request_data or {}
    for key in ("max_output_tokens", "max_completion_tokens", "max_tokens"):
        value = data.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        if value > 0:
            return int(value)
    return None


def _responses_incomplete_output_tokens(chunk: Any) -> Optional[int]:
    """The output tokens a terminal event reports, from either wire shape."""

    event = _jsonable(chunk)
    if not isinstance(event, dict):
        event = _stream_chunk_dump(chunk)
    response = event.get("response") if isinstance(event, dict) else None
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict) and isinstance(event, dict):
        usage = event.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    value = usage.get("output_tokens")
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        value = usage.get("completion_tokens")
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return int(value)


def _incomplete_stopped_below_client_budget(
    chunk: Any,
    request_data: Optional[dict],
) -> bool:
    """True when the route stopped well short of the budget the client set.

    A relay that resells a smaller model reports its own cap as
    ``reason: max_output_tokens``.  The client asked for far more, so the
    terminal is the route's truncation, not the client's limit; presenting it
    as a normal completion is how a half answer arrives with no error.
    """

    budget = _request_output_token_budget(request_data)
    if budget is None:
        return False
    output_tokens = _responses_incomplete_output_tokens(chunk)
    if output_tokens is None or output_tokens <= 0:
        return False
    return output_tokens < max(1, budget - 128)

# The reason must stand as its own token: ``max_tokens`` inside a longer word
# names something else, and ``length`` is also how an input ceiling is worded.
_OUTPUT_TOKEN_LIMIT_REASON_PATTERNS = tuple(
    (reason, re.compile(rf"(?<![a-z0-9_]){re.escape(reason)}(?![a-z0-9_])"))
    for reason in sorted(_OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS)
)

# A message about the *input* ceiling is not an output limit, and several of
# its spellings (``context_length_exceeded``, ``maximum context length is N
# tokens``) contain ``length``; taken for an output limit they would let a
# truncated turn leave as a normal completion.
_CONTEXT_LENGTH_INCOMPLETE_RE = re.compile(
    r"context[\s_-]*(?:length|window|limit)"
    r"|(?:maximum|exceeds?|exceeded)[\s_-]+context"
    r"|(?:input|prompt)[\s_-]*length"
    r"|reduce[\s_-]+(?:the[\s_-]+)?length"
)

def _output_limit_incomplete_reason_from_text(value: str) -> Optional[str]:
    lowered = value.strip().lower()
    if not lowered:
        return None
    if _CONTEXT_LENGTH_INCOMPLETE_RE.search(lowered):
        return None
    for reason, pattern in _OUTPUT_TOKEN_LIMIT_REASON_PATTERNS:
        if pattern.search(lowered):
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
    if _incomplete_stopped_below_client_budget(chunk, request_data):
        # The route capped the answer below the budget this client asked for.
        # Leave the terminal event alone so the delivery path reports the
        # truncation instead of dressing it as a normal completion.
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
    if _incomplete_stopped_below_client_budget(chunk, request_data):
        # The route capped the answer below the budget this client asked for.
        # Leave the terminal event alone so the delivery path reports the
        # truncation instead of dressing it as a normal completion.
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
        if isinstance(json_item, dict) and not _historical_note_module.is_note_item(
            json_item
        ):
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
        if _historical_note_module.is_note_item(json_item):
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
    dumped = _stream_chunk_completed_dump(chunk)
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

def _synthesized_incomplete_response_event(
    created_response: Optional[dict[str, Any]],
    model: Optional[str],
    *,
    terminal: Any,
    output_items: Optional[dict[int, dict[str, Any]]] = None,
) -> _JSONStreamEvent:
    """Hand the client the outcome the route itself stated.

    Codex's Responses reader maps ``response.incomplete`` to
    ``ApiError::ResponseIncomplete`` and ``is_terminal_sampling_outcome`` keeps
    it out of the retry loop: the turn ends, the partial output stays, and the
    bytes are not sent again.  Every unrecognised ``response.failed`` code is
    the opposite -- retried as a transport failure.  Turning a route's own
    ``response.incomplete`` into a failure is what made every capped turn read
    as ``正在重新连接 1/5`` (live 2026-11-20, thread ``01a1ee60``).
    """

    dumped = _stream_chunk_dump(terminal)
    upstream_response = dumped.get("response") if isinstance(dumped, dict) else None
    upstream_response = (
        upstream_response if isinstance(upstream_response, dict) else {}
    )
    response = (
        copy.deepcopy(created_response) if isinstance(created_response, dict) else {}
    )
    response.setdefault("id", upstream_response.get("id") or "resp_synthesized")
    response.setdefault("object", "response")
    if model and not response.get("model"):
        response["model"] = model
    response["status"] = "incomplete"
    details = upstream_response.get("incomplete_details")
    if not isinstance(details, dict) or not details.get("reason"):
        details = {
            "reason": _responses_incomplete_terminal_reason(dumped) or "incomplete"
        }
    response["incomplete_details"] = details
    # The spend is real and the client records it from this frame.
    usage = upstream_response.get("usage")
    if isinstance(usage, dict):
        response["usage"] = usage
    if output_items:
        response["output"] = [
            item for _index, item in sorted(output_items.items(), key=lambda entry: entry[0])
        ]
    else:
        response.setdefault("output", [])
    return _json_stream_event({"type": "response.incomplete", "response": response})

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

_SYNTHETIC_TIMEOUT_NOTICE_ID = "chatcmpl-timeout-guard"
_SYNTHETIC_TIMEOUT_NOTICE_MARKERS = ("**Request exceeded ", " limit**")
_SYNTHETIC_TIMEOUT_NOTICE_TOOL_MARKERS = (
    "Recommended tools:",
    "opencode, Claude Code, and Codex",
)

def _synthetic_timeout_notice_id(value: Any) -> bool:
    """Whether a response id names a relay's synthetic timeout guard.

    A relay answers an over-long turn with an ordinary completion whose id is
    ``chatcmpl-timeout-guard``; LiteLLM re-encodes that id into the
    ``resp_<base64>`` Responses id, so both spellings have to be read.
    """

    if not isinstance(value, str) or not value:
        return False
    if value.endswith(_SYNTHETIC_TIMEOUT_NOTICE_ID):
        return True
    if not value.startswith("resp_"):
        return False
    encoded = value[5:]
    try:
        decoded = base64.b64decode(
            encoded + "=" * (-len(encoded) % 4)
        ).decode("utf-8", "replace")
    except Exception:
        return False
    return _SYNTHETIC_TIMEOUT_NOTICE_ID in decoded

def _synthetic_timeout_notice_text(text: Any) -> bool:
    """Whether this text is the notice, not an answer that quotes it.

    The guard's whole message opens with its own request tag and states the
    limit; a real answer may mention the same words, so only a fragment that
    starts at the tag counts.
    """

    if not isinstance(text, str):
        return False
    stripped = text.lstrip()
    if not stripped.startswith("[req_"):
        return False
    if not all(marker in stripped for marker in _SYNTHETIC_TIMEOUT_NOTICE_MARKERS):
        return False
    return any(marker in stripped for marker in _SYNTHETIC_TIMEOUT_NOTICE_TOOL_MARKERS)

def _stream_chunk_synthetic_timeout_notice(chunk: Any) -> bool:
    dumped = _stream_chunk_dump(chunk)
    if not isinstance(dumped, dict):
        return False
    response = dumped.get("response")
    for value in (
        dumped.get("id"),
        response.get("id") if isinstance(response, dict) else None,
    ):
        if _synthetic_timeout_notice_id(value):
            return True
    for choice in dumped.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for key in ("delta", "message"):
            part = choice.get(key)
            if isinstance(part, dict) and _synthetic_timeout_notice_text(
                part.get("content")
            ):
                return True
    for key in ("delta", "text", "output_text"):
        if _synthetic_timeout_notice_text(dumped.get(key)):
            return True
    return False

def _synthetic_timeout_notice_exception(chunk: Any, request_data: Any) -> Exception:
    from litellm.exceptions import APIError

    dumped = _stream_chunk_dump(chunk)
    upstream_id = dumped.get("id") if isinstance(dumped, dict) else None
    exception = APIError(
        status_code=502,
        message=(
            "upstream returned a synthetic timeout notice instead of an answer"
            + (
                f" ({upstream_id})"
                if isinstance(upstream_id, str) and upstream_id
                else ""
            )
        ),
        llm_provider="openai",
        model=str(dumped.get("model") or "") if isinstance(dumped, dict) else "",
    )
    if isinstance(request_data, dict):
        try:
            _routing_module._apply_current_selected_deployment_to_request(request_data)
            _routing_module._mark_exception_for_deployment_failover(
                exception, request_data
            )
        except Exception:
            pass
    _trace_module._request_route_trace(
        "upstream_synthetic_timeout_notice_rejected",
        request_data,
        upstream_response_id=upstream_id,
        response_id_type=_stream_chunk_type(dumped),
    )
    return exception

def _raise_if_synthetic_timeout_notice(chunk: Any, request_data: Any) -> None:
    if _stream_chunk_synthetic_timeout_notice(chunk):
        raise _synthetic_timeout_notice_exception(chunk, request_data)

# A DeepSeek-V4 chat route must parse its native DSML tool-call markup into
# ``tool_calls``.  flux-code's ``-mod`` variant does not: the closing tags of
# its own dialect arrive as answer text (observed 2026-11-03 on a Codex turn,
# ``</| DSLM | invoke>`` / ``</| DSLM | calls>``), so the client renders a
# call it cannot execute and the turn stops.  These exact tags are the leak's
# signature; the model's documentation of the format is not.
_LEAKED_TOOL_CALL_MARKUP_MARKERS = (
    "</| DSLM | calls>",
    "</| DSLM | invoke>",
)

# A route trained on the Responses wire sometimes returns the request's own
# content array serialized as the answer text (observed 2026-11-03 from
# flux-code's ``-mod`` variant: ``[{"type":"text","text":"..."}]``).  The
# client renders that JSON instead of the answer, so the bridge unwraps it.
_CONTENT_ARRAY_TEXT_PREFIX = '[{"type":"text","text":"'

def _unwrap_content_array_text(text: str) -> str:
    if not isinstance(text, str):
        return text
    bare = text.strip()
    if not bare.startswith(_CONTENT_ARRAY_TEXT_PREFIX):
        return text
    try:
        parts = json.loads(bare)
    except Exception:
        return text
    if not isinstance(parts, list) or not parts:
        return text
    texts: list[str] = []
    for item in parts:
        if not isinstance(item, dict) or item.get("type") != "text":
            return text
        value = item.get("text")
        if not isinstance(value, str):
            return text
        texts.append(value)
    return "\n".join(texts)

def _text_has_leaked_tool_call_markup(text: Any) -> bool:
    if not isinstance(text, str):
        return False
    return any(marker in text for marker in _LEAKED_TOOL_CALL_MARKUP_MARKERS)

def _stream_chunk_leaked_tool_call_markup(chunk: Any) -> bool:
    dumped = _stream_chunk_dump(chunk)
    if not isinstance(dumped, dict):
        return False
    for choice in dumped.get("choices") or []:
        if not isinstance(choice, dict):
            continue
        for key in ("delta", "message"):
            part = choice.get(key)
            if isinstance(part, dict) and _text_has_leaked_tool_call_markup(
                part.get("content")
            ):
                return True
    for key in ("delta", "text", "output_text"):
        if _text_has_leaked_tool_call_markup(dumped.get(key)):
            return True
    return False

def _leaked_tool_call_markup_exception(chunk: Any, request_data: Any) -> Exception:
    from litellm.exceptions import APIError

    dumped = _stream_chunk_dump(chunk)
    exception = APIError(
        status_code=502,
        message=(
            "upstream leaked its native tool-call markup as answer text "
            "instead of a tool call"
        ),
        llm_provider="openai",
        model=str(dumped.get("model") or "") if isinstance(dumped, dict) else "",
    )
    if isinstance(request_data, dict):
        try:
            _routing_module._apply_current_selected_deployment_to_request(request_data)
            _routing_module._mark_exception_for_deployment_failover(
                exception, request_data
            )
        except Exception:
            pass
    _trace_module._request_route_trace(
        "upstream_tool_call_markup_leak_rejected",
        request_data,
        upstream_response_id=(
            dumped.get("id") if isinstance(dumped, dict) else None
        ),
    )
    return exception

def _raise_if_leaked_tool_call_markup(chunk: Any, request_data: Any) -> None:
    if _stream_chunk_leaked_tool_call_markup(chunk):
        raise _leaked_tool_call_markup_exception(chunk, request_data)

def _unclassified_stop_message() -> str:
    """The one sentence for a stop no branch in this module can name.

    Both wire formats return it, because the same stop may reach either client
    and the sentence, not the code, is what decides whether the turn survives.
    Pi matches ``isRetryableAssistantError``'s phrase list against the rendered
    ``<code>: <message>``, and "ended without" is on that list; Codex never
    reads the sentence and retries every code it does not recognise, which is
    why ``upstream_route_failure`` is never given a name of our own invention.
    """

    return (
        "The turn ended before the assistant answered, and the router could "
        "not classify the stop, so the stream ended without a terminal "
        "response event. The route trace for this request records the upstream "
        "status and the exception that ended it."
    )


def _sanitized_failure_wire_pair(exception: Exception) -> tuple[str, str]:
    """The wire type/code pair that agrees with a sentence already decided.

    A sanitized route failure carries the policy of the refusal it was built
    from, and the two clients read different halves of the terminal event: Pi
    the sentence, Codex the code.  The wrapper's own class says nothing about
    the cause, so the pair comes from that policy -- ``invalid_prompt`` is
    Codex's name for a stop no resend changes, and an unrecognised code would
    be retried forever.
    """

    if (
        _routing_module._recovery_policy_for_exception(exception)
        == _routing_module._RECOVERY_POLICY_ERROR
    ):
        return "invalid_request_error", "invalid_prompt"
    return "server_error", "upstream_route_failure"


def _synthesized_failed_response_event(
    request_data: dict,
    exception: Exception,
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
    elif _routing_module._is_sanitized_upstream_route_failure_error(exception):
        # The sentence was already decided where the turn ended; choosing a new
        # one from this wrapper replaces its cause with the wrapper's own status.
        # Live 2026-11-12: the route-recovery poll's terminal event re-worded a
        # station refusal into "temporary upstream server error", so the reader
        # was told a bare 5xx while the trace still named the station.
        message = str(exception)
        error_type, error_code = _sanitized_failure_wire_pair(exception)
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
    elif _routing_module._is_upstream_model_unpriced_error(exception):
        # The station's price table is the thing that has to change, so this
        # stop names it and never reads as a transport blip worth resending.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_model_unpriced_message(model_group)
        error_type = "invalid_request_error"
        error_code = "upstream_model_unpriced"
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
    elif _routing_module._is_thinking_replay_contract_error(exception):
        # A thinking-mode route refused the replay because the reasoning text of
        # earlier turns is not in it any more.  ``reasoning_replay`` answers that
        # on the same route for a history that still carries its thinking in any
        # form; reaching here means there was nothing left to repair, and the
        # same bytes meet the same refusal.  Reading it as an unclassified stop
        # told the reader the route "produced no answer" and invited a resend of
        # exactly that history, so the turn looped on a refusal it could never
        # clear.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_thinking_replay_rejected_message(
            model_group
        )
        # Same pair as the deterministic siblings: a wire type that stops the
        # client and the Codex code for "no resend changes this".
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    elif _routing_module._is_encrypted_replay_refused_error(exception):
        # Every candidate said it cannot read the signed history.  The items are
        # bound to the organization that minted them, so the reader's action is
        # a new task -- but the stop still names the cause instead of falling
        # through to the unclassified sentence, which is what the station's raw
        # envelope used to become.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_encrypted_replay_refused_message(
            model_group
        )
        # ``server_error`` keeps a retry possible: the same history is readable
        # on the route whose account minted it, and the session's affinity now
        # steers a resend there.  ``invalid_prompt`` would end the conversation
        # on a history the client can still get answered.
        error_type = "server_error"
        error_code = "encrypted_replay_unreadable"
    elif _routing_module._is_upstream_request_field_rejected_error(exception):
        # The route refused the *fields* this proxy sent, not the conversation:
        # an ``extra_body`` container, a ``litellm_params`` container, or one of
        # this proxy's own markers reached a payload validator.  Live session
        # 2026-11-01: an Anthropic route answered HTTP 400 ``extra_body: extra
        # inputs are not permitted`` and the reader was told the router could
        # not classify the stop, because nothing here owned that answer.  No
        # resend meets a different validator, and no codex/Pi retry phrase may
        # be attached, so the stop names the defect and stops.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = _routing_module._upstream_request_field_rejected_message(
            model_group
        )
        # ``invalid_request_error`` is the wire type that stops a client from
        # reconnecting over a body it would send again unchanged, and
        # ``invalid_prompt`` is the Codex code for "no resend changes this" --
        # every unrecognised code is retryable there, so an invented one would
        # loop the same refused payload.  The two sibling branches for a
        # deterministic refusal (body size, tool history) use the same pair.
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    elif _routing_module._is_terminal_prompt_or_policy_error(exception):
        # A route refused the prompt itself under its own content policy.  The
        # trace has named this cause for sessions (``terminal-prompt-or-policy``)
        # and the chat-completions wire already gave it a code, but this
        # Responses branch did not exist: the stop fell through to the
        # unclassified sentence, so the client was told the router could not
        # classify a refusal whose own words say what it was.  Nothing a resend
        # changes reaches a different answer, so the wire type is the one that
        # stops the client and the sentence says what to change.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"{model_group} refused this prompt under the route's own content "
            "policy, and no candidate route would accept it. Change the prompt "
            "or start a new task; the same text meets the same refusal."
        )
        # ``invalid_prompt`` is Codex's own name for a refusal no resend can
        # clear, and it is deliberately not one of Pi's retry phrases.
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    elif _routing_module._is_upstream_model_not_found_error(exception):
        # The route answered 404 for the model it was asked to serve.  That is a
        # configuration change, not a transport hiccup, so it may not read as an
        # unclassified stop: the reader is told which model no route serves.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"No candidate route serves {model_group} any more (the upstream "
            "answered that the model does not exist). Disable the stale "
            "provider route or choose another model; sending this turn again "
            "meets the same answer."
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
    elif _routing_module._is_upstream_incomplete_terminal_error(exception):
        # The route did not fall silent: it *ended* the turn with its own
        # terminal event and no error -- a Responses stream that reports the
        # response incomplete (or cancelled) before ``response.completed``.
        # The generic sentence this shape used to reach said the route
        # "stopped answering while this turn was streaming", which is not what
        # happened: the upstream answered with an outcome, and the reader is
        # owed that outcome plus the one action that continues the turn.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"{model_group} ended this turn with an incomplete response of its "
            "own, so the stream ended without a terminal response event. Send "
            "the message again and it will continue on a working route."
        )
        error_type = "server_error"
        error_code = "upstream_stream_incomplete"
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
    elif _routing_module._is_upstream_request_shape_rejected_error(exception):
        # The route refused the body this router (or litellm) built, and every
        # candidate route answered the same way.  Neither sentence this stop used
        # to reach was true: it was not an unclassified stop, and it was not the
        # conversation the route rejected.  A resend rebuilds the identical
        # body, so the wire type is the one that stops the client.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"{model_group} rejected the request body this router sent, on every "
            "candidate route: the route answered a request-shape refusal rather "
            "than anything about the conversation. Sending the same turn again "
            "meets the same refusal; choose another route for this model."
        )
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    elif _routing_module._is_upstream_channel_refused_error(exception):
        # The cause is specific -- the station's selected channel refused -- and
        # the generic transient branch below would replace it with "produced no
        # answer", inviting a resend without saying why one works.  The routing
        # sentence carries the station's scope and the client's retry phrase.
        message = _routing_module._sanitized_upstream_route_failure_message(
            model,
            exception,
            request_data,
        )
        error_type = "server_error"
        error_code = "upstream_route_failure"
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
    elif _routing_module._stop_cause_is_named(exception):
        # The router's own trace names this cause, but no branch above owns it.
        # Reaching here means the recovery policy already called it terminal --
        # that decision is what routes a transient cause to the poll branch --
        # so the honest sentence is a refusal, and it may not claim the router
        # could not classify a stop its trace just classified.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        message = (
            f"{model_group} refused this request on every candidate route, and "
            "the route trace for this request names the refusal. No resend "
            "changes that answer; change the route or the request before "
            "sending it again."
        )
        error_type = "invalid_request_error"
        error_code = "invalid_prompt"
    else:
        # Nothing here can name the stop, so the sentence says exactly that.
        # The generic "the route failed before an answer was available" wording
        # this branch used to carry is banned repo-wide
        # (tests/test_proxy_module_integrity.py), and the sentence may not claim
        # a cause it does not have.
        #
        # It may not end the turn either.  An unknown cause is not the same fact
        # as a refusal no resend can clear, and only Codex reads the code: every
        # code it does not recognise is retried there, while Pi reads the
        # sentence, and a sentence it matches nothing in ends the turn.  So the
        # same stop survived on one client and killed a live gpt-6.1-sol turn on
        # the other.  The truncation clause below is the literal truth (this
        # attempt produced no terminal response event) and puts the stop on the
        # retry list of both clients, which is where a stop nobody can explain
        # belongs: bounded by each client's own budget, never by a sentence that
        # reads as final.
        message = _unclassified_stop_message()
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
    if isinstance(request_data, dict):
        # This turn's client-visible outcome is a failure this proxy built, and
        # the upstream call underneath it closes normally.  LiteLLM's success
        # callback is what would otherwise record a delivered answer -- a
        # success row and a cleared cooldown for a route that just cut the turn.
        # The callback reads both keys instead of the transport.
        try:
            request_data[_TERMINAL_FAILURE_REQUEST_KEY] = exception
            request_data.setdefault("exception", exception)
        except Exception:
            pass
    # The dict above only reaches a callback handed *this* copy, and LiteLLM
    # hands the logging callback a different one.  Live 2026-11-19 09:18 the
    # gateway still recorded a success and cleared the cooldown 0.4 s after the
    # failure.  The verdict therefore also goes to the shared registry, keyed by
    # the turn's request identifiers.
    _routing_module._mark_terminal_failure_for_request(request_data, exception)
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
    error_code = _native_stream_error_code(exception, request_data)
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
            "image_generation_tool_unavailable",
            "upstream_account_restricted",
            "upstream_client_outdated",
            "upstream_model_unpriced",
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
    # A Chat event is written by LiteLLM's SSE generator as ``f"data: {chunk}"``,
    # so a plain dict reaches the client as its Python repr (``data: {'error':
    # ...}``) -- not JSON, which every Chat client refuses to parse (Cherry
    # Studio reports it as ``Invalid JSON data``).  ``_JSONStreamEvent``
    # stringifies as JSON, the same way this repo's Responses events do.
    return _json_stream_event(
        {
            "error": {
                "type": error_type,
                "code": error_code,
                "message": message,
            }
        }
    )

def _native_stream_error_code(
    exception: Exception,
    request_data: Optional[dict] = None,
) -> str:
    if _routing_module._is_sanitized_upstream_route_failure_error(exception):
        # The sentence is the one the router decided on; the code has to agree
        # with it.  A wrapper's own class says nothing about the cause, and a
        # transient sentence carrying a terminal code (or the reverse) makes the
        # two clients disagree about the same stop.
        return _sanitized_failure_wire_pair(exception)[1]
    if _routing_module._is_local_shutdown_exception(exception):
        return "router_restarting"
    if _routing_module._is_context_size_error(exception):
        # Codex keys its retry decision on `error.code`; these are its names.
        return "context_length_exceeded"
    if _routing_module._is_terminal_prompt_or_policy_error(exception):
        # ``invalid_prompt`` is Codex's own name for a refusal no resend
        # changes.  ``upstream_prompt_rejected`` was this router's own spelling,
        # and Codex treats every code it does not recognise as retryable, so a
        # content-policy refusal was resent until the client's budget ran out.
        return "invalid_prompt"
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
    if _routing_module._is_upstream_model_unpriced_error(exception):
        # A code of its own, for the same reason: the station's missing price
        # entry is a configuration change, not a transport hiccup.
        return "upstream_model_unpriced"
    if _routing_module._is_thinking_replay_contract_error(exception):
        # The replayed thinking is gone from this history and no resend adds it
        # back, so the client must not reconnect over the same body.
        return "invalid_prompt"
    if _routing_module._is_encrypted_replay_refused_error(exception):
        # A resend can be answered by the route that minted the signed items, so
        # this stop is retryable; it still gets a code of its own so the trace
        # and the client say what the route could not read.
        return "encrypted_replay_unreadable"
    if _routing_module._is_upstream_tool_history_rejected_error(exception):
        return "invalid_prompt"
    if _routing_module._is_upstream_request_field_rejected_error(exception):
        # ``invalid_prompt`` is the Codex code for a refusal no resend changes;
        # a code of this router's own invention would be retried there forever.
        return "invalid_prompt"
    if _routing_module._is_upstream_request_shape_rejected_error(exception):
        # The same reading as the request-field branch: the body this side built
        # is what was refused, and ``invalid_prompt`` is the Codex code that
        # stops a client resending it.
        return "invalid_prompt"
    if _routing_module._is_upstream_model_not_found_error(exception):
        # A route that 404s the model is a configuration change: ``invalid_prompt``
        # is what stops Codex resending a turn no candidate can serve, and a code
        # of our own would be retried there until the budget ran out.
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
    if _routing_module._request_body_size_rejected(request_data):
        # Every candidate route refused these bytes, so ``invalid_prompt`` is
        # the code that stops a client resending the identical body.
        return "invalid_prompt"
    return "upstream_route_failure"

def _native_stream_error_message(
    model: Optional[str],
    exception: Exception,
    request_data: dict,
) -> str:
    if _routing_module._is_sanitized_upstream_route_failure_error(exception):
        # Same contract as the Responses builder: the sentence was decided where
        # the turn ended, and re-deriving one from the wrapper loses its cause.
        original = str(exception).strip()
        if original:
            return original
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
    if _routing_module._is_upstream_channel_refused_error(exception):
        # Same contract as the Responses builder: name the station's channel and
        # keep the retry phrase; the generic transient wording would drop the
        # cause the trace already named.
        return _routing_module._sanitized_upstream_route_failure_message(
            model,
            exception,
            request_data,
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
    if _routing_module._is_upstream_model_unpriced_error(exception):
        # Same reading: the station has no price for this model, so a resend
        # carries the identical request to the identical refusal.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_model_unpriced_message(model_group)
    if _routing_module._is_thinking_replay_contract_error(exception):
        # Same reading as the Responses branch: the thinking this route needs
        # back is not in the history any more, so a resend carries the same gap.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_thinking_replay_rejected_message(model_group)
    if _routing_module._is_encrypted_replay_refused_error(exception):
        # Same reading as the Responses branch: no candidate route could read
        # the signed items, and the client has to start a new task.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_encrypted_replay_refused_message(
            model_group
        )
    if _routing_module._is_upstream_tool_history_rejected_error(exception):
        # The replay cannot be repaired on the client's side by sending it
        # again: the interrupted tool call stays in the history forever.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return _routing_module._upstream_tool_history_rejected_message(model_group)
    if _routing_module._is_terminal_prompt_or_policy_error(exception):
        # The Responses branch states the same refusal for the same reason: the
        # route judged the prompt, and a resend carries the identical prompt.
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return (
            f"{model_group} refused this prompt under the route's own content "
            "policy, and no candidate route would accept it. Change the prompt "
            "or start a new task; the same text meets the same refusal."
        )
    if _routing_module._is_upstream_request_shape_rejected_error(exception):
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return (
            f"{model_group} rejected the request body this router sent, on every "
            "candidate route: the route answered a request-shape refusal rather "
            "than anything about the conversation. Sending the same turn again "
            "meets the same refusal; choose another route for this model."
        )
    if _routing_module._is_upstream_model_not_found_error(exception):
        model_group = (
            _responses_execution_module._request_model_group(request_data)
            or request_data.get("model")
            or "the requested model"
        )
        return (
            f"No candidate route serves {model_group} any more (the upstream "
            "answered that the model does not exist). Disable the stale "
            "provider route or choose another model; sending this turn again "
            "meets the same answer."
        )
    if _routing_module._is_image_generation_all_deployments_unsupported_error(
        exception
    ):
        # The wire type says ``invalid_request_error``, and the Anthropic shape
        # below carries no error code at all: this sentence is the only thing
        # that tells the reader the tool itself was refused.
        return (
            "All configured deployments for this model rejected the "
            "image_generation tool."
        )
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
    if not _routing_module._stop_cause_is_named(exception):
        # The same stop, in the same words as the Responses event: one nobody can
        # name is not a refusal the client's own bytes earned, and the sentence it
        # reads is what decides whether the turn survives.
        return _unclassified_stop_message()
    return _routing_module._sanitized_upstream_route_failure_message(
        model,
        exception,
        request_data,
    )

# Terminal reasons that can mean "the replay carried too many items", not "the
# model cannot hold this conversation".  Live 2026-11-19 the tken relay ended a
# 311 s turn with ``response.incomplete`` and ``reason: max_messages``; the same
# payload had been served minutes earlier, so the router treats this as a
# refusal to resend smaller first, and only then as a stop.
#
# The reason alone does not say *which* cap was hit.  Both relays state
# ``max_messages`` when the model spends a whole response's item budget
# thinking, and that terminal arrives after the reasoning has been streamed:
# live 2026-11-20 thread ``01a1ee60`` was cut that way after four minutes, and
# each of those terminals was remembered as "this replay is too large".  The
# thread walked the entire ladder in one afternoon -- 5,160 items became 197
# with no reasoning left -- so the model answered blind with the same sentence,
# Codex asked again, and the next turn was cut the same way.  A mark is
# therefore only written when the route had delivered nothing before its
# terminal: a route that had already started this response was counting what it
# generated, and a shorter replay cannot clear that cap.
_UPSTREAM_INPUT_ITEM_LIMIT_REASONS = frozenset(
    {
        "max_messages",
        "max_input_items",
        "max_items",
        "too_many_messages",
        "too_many_items",
    }
)


# How much answer text may already have reached the reader when a route's item
# cap ends the turn and a sibling route takes it over.  Below this the cost is
# a repeated progress line; above it the reader is reading an answer that must
# not be written twice.
_CAP_CUT_RESUMABLE_TEXT_CHARS = 400


def _incomplete_terminal_capped_generated_output(
    buffer: Optional[List[Any]],
    request_data: Optional[dict] = None,
) -> bool:
    """Whether this response had already shown the reader something.

    The frames before the terminal are the evidence.  A reasoning summary delta
    reaches the client the moment it arrives, so a cap that follows one was
    counting the items this response generated -- not the replay it was given.
    """

    if not buffer:
        return False
    for chunk in buffer[:-1]:
        if _stream_chunk_has_delivered_output(chunk, request_data):
            return True
    return False


def _mark_generated_output_item_cap(
    exception: Exception,
    terminal_chunk: Any,
    *,
    delivered: bool,
) -> None:
    """Mark the route's own item budget as the cause of this stop.

    The exception builder can only look at the frames it was handed; the guards
    that saw the whole stream know whether the reader had already been shown
    output, which is the fact that separates "this response ran out of items"
    from "this replay was refused".  Only the first shape is a rate to charge
    to the route (two inside five minutes).
    """

    if not delivered:
        return
    if _responses_incomplete_terminal_reason(terminal_chunk) not in _UPSTREAM_INPUT_ITEM_LIMIT_REASONS:
        return
    try:
        exception.item_cap_cut = True  # type: ignore[attr-defined]
    except Exception:
        pass


def _responses_incomplete_stream_exception(
    reason: str,
    *,
    buffer: Optional[List[Any]] = None,
    request_data: Optional[dict] = None,
    delivered_output: Optional[bool] = None,
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
        terminal_reason = str(terminal_summary.get("reason") or "")
        if terminal_reason in _UPSTREAM_INPUT_ITEM_LIMIT_REASONS:
            # The caller that watched the whole stream knows whether anything
            # reached the reader; the buffer it can hand over is often only the
            # tail.  Live 2026-11-20 11:34: a hop whose buffer held just the
            # terminal was read as a refused replay, wrote stage 3 on the
            # thread, and every later turn was cut to 422 items with no
            # reasoning -- the model then re-derived the same work for hours.
            capped_generated = (
                delivered_output
                if delivered_output is not None
                else _incomplete_terminal_capped_generated_output(buffer, request_data)
            )
            if capped_generated:
                # The cap was on what this response generated.  Saying so keeps
                # it out of the thread's ladder -- and keeps the two stops
                # distinguishable for whoever reads the trace.
                _trace_module._request_route_trace(
                    "responses_item_cap_after_delivered_output",
                    request_data,
                    reason=terminal_reason,
                    buffered_event_count=len(buffer or []),
                )
            else:
                # The upstream said the replay carries too many items.  The
                # router answers that by resending a shorter history, which is a
                # different verdict from "this conversation no longer fits" --
                # Codex must not be told to compact over a cap that a shorter
                # replay clears.
                setattr(exception, _UPSTREAM_INPUT_ITEM_LIMIT_ATTR, True)
                # Whatever this turn can still do about it, the thread is now
                # known to be too large for this route -- and the client replays
                # the same history on its next turn.
                _routing_module._remember_replay_too_large(request_data)
    except Exception:
        pass
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

def _stream_chunk_is_public_reasoning_delta(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    """Whether this frame is thinking the client can already read.

    A reasoning summary delta is user-visible output even though it is not the
    answer: the client renders it the moment it arrives.  It is kept apart from
    ``_stream_chunk_has_visible_output`` because that predicate answers a
    different question -- "has the answer begun" -- which decides whether a route
    that produced nothing but a preamble may be replaced.
    """

    dumps = _stream_chunk_dumps(chunk)
    for visible_chunk in dumps if dumps else [chunk]:
        if not isinstance(visible_chunk, dict):
            continue
        chunk_type = _stream_chunk_type(visible_chunk)
        delta = visible_chunk.get("delta")
        if _web_search_native_module._is_public_reasoning_summary_stream_event(
            chunk_type
        ):
            if isinstance(delta, str) and delta.strip():
                return True
            if isinstance(delta, dict) and str(delta.get("thinking") or "").strip():
                return True
            continue
        if chunk_type == "response.reasoning_text.delta":
            # A Responses upstream may stream the thinking raw; the delivery
            # loop mirrors it into summary frames for a client that asked for
            # a summary.  That conversion happens downstream of the start
            # buffer, so the raw frame itself must open the window or the
            # whole block is held until the answer.
            if (
                isinstance(delta, str)
                and delta.strip()
                and _web_search_native_module._public_reasoning_is_allowed(
                    request_data
                )
            ):
                return True
            continue
        # Anthropic reports thinking as a ``content_block_delta`` whose own
        # ``delta`` is a ``thinking_delta`` carrying ``thinking``.
        if isinstance(delta, dict) and str(delta.get("thinking") or "").strip():
            return True
        # A chat-completions stream carries the same thinking in its choice
        # delta: ``reasoning_content`` (deepseek-style routes) or ``reasoning``
        # (some relays).  It is the shape a ``fallback`` route produces, so
        # leaving it out held the first twenty frames of every thinking phase
        # in the start buffer and handed the client the whole block at once --
        # the burst this predicate exists to prevent, one layer lower than the
        # Responses bridge.
        if chunk_type:
            continue
        for choice in visible_chunk.get("choices") or []:
            if not isinstance(choice, dict):
                continue
            choice_delta = choice.get("delta")
            if not isinstance(choice_delta, dict):
                continue
            for key in _CHAT_REASONING_DELTA_KEYS:
                value = choice_delta.get(key)
                if isinstance(value, str) and value.strip():
                    return True
    return False


_DELIVERED_TEXT_DELTA_KEYS = ("content", "text", "reasoning_content", "reasoning", "thinking")


def _stream_event_delta_is_text(chunk: Any) -> bool:
    """Whether an event-shaped frame carries text its reader can already see.

    Covers the Anthropic ``content_block_delta`` frame (its payload is a dict
    under ``delta``) and the Responses delta events (a string under ``delta``).
    """

    if not isinstance(chunk, dict):
        return False
    delta = chunk.get("delta")
    if isinstance(delta, dict):
        return any(delta.get(key) for key in _DELIVERED_TEXT_DELTA_KEYS)
    if isinstance(delta, str) and delta:
        event_type = chunk.get("type")
        return isinstance(event_type, str) and event_type.endswith(".delta")
    return False


def _stream_chunk_delivered_text_delta(chunk: Any) -> bool:
    """The positive-only fast path for ``_stream_chunk_has_delivered_output``.

    Read the delta straight off the frame instead of walking JSON: the two full
    predicates below cost about 90 microseconds per chunk, they run on every
    frame of every stream, and for the chat shape the answer is one attribute
    lookup.  False means "ask the full predicates", never "nothing was
    delivered", so an unusual shape still gets the careful answer.
    """

    choices = getattr(chunk, "choices", None)
    if choices is None and isinstance(chunk, dict):
        choices = chunk.get("choices")
    if not choices:
        return _stream_event_delta_is_text(chunk)
    for choice in choices:
        delta = choice.get("delta") if isinstance(choice, dict) else getattr(choice, "delta", None)
        if delta is None:
            continue
        for key in _DELIVERED_TEXT_DELTA_KEYS:
            value = delta.get(key) if isinstance(delta, dict) else getattr(delta, key, None)
            if value:
                return True
    return False


def _stream_chunk_has_delivered_output(
    chunk: Any,
    request_data: Optional[dict] = None,
) -> bool:
    """Whether the client has now been shown something it cannot un-see.

    Once a frame has reached the reader, neither a replay nor a silent failover
    can put the turn back the way it was, so the start buffer must stop holding
    frames and a later failure belongs to this turn.  Thinking counts: holding it
    until the first answer token is what made a whole reasoning block appear in
    one burst after the model had finished thinking.
    """

    return _stream_chunk_delivered_text_delta(
        chunk
    ) or _stream_chunk_has_visible_output(chunk) or _stream_chunk_is_public_reasoning_delta(
        chunk, request_data
    )

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
    if _stream_chunk_is_public_reasoning_delta(dumped, request_data):
        # Thinking is shown the moment it arrives, so it ends the poll's
        # buffer window exactly as it ends the ordinary start buffer's.  While
        # this predicate omitted it, a recovery poll held every summary delta
        # until the first answer token and then released the whole reasoning
        # block at once.
        return True
    if _stream_chunk_has_visible_output(chunk):
        return True
    if _responses_completed_chunk_has_route_recovery_output(dumped, request_data):
        return True
    if _responses_stream_chunk_is_incomplete_terminal(dumped):
        return True
    return False


async def _await_upstream_dispatch(
    awaitable: Any,
    request_data: dict,
    *,
    fallback_budget_seconds: float = 0.0,
) -> Any:
    """Await one upstream attempt's *dispatch*, bounded.

    The helper LiteLLM calls per hop returns the response object: for a
    streaming call that moment is exactly "the host was reached, the body was
    uploaded, and the response head arrived".  Every stream budget in this
    module starts after it, so until now nothing timed it, and an unreachable
    host was bounded only by the operating system's SYN ladder times LiteLLM's
    own retry -- 271 s on the live gateway on 2026-11-15, on a route that had
    sent and received nothing.

    The budget is ``_upstream_dispatch_budget_seconds``: the connect budget plus
    an upload allowance for the bytes this request carries, so a heavy replay
    is not cut and an absent host is not waited on.  A timeout is this proxy's
    own verdict about the route, so it is raised as the start-timeout shape the
    failover, cooldown and recovery machinery already agree on: a sibling may
    answer, the route is parked, and the client is told no upstream event
    arrived rather than that its bytes were refused.

    ``fallback_budget_seconds`` is for the callers that already bounded this
    await with the first-event budget: when the operator turns the connect
    budget off, that older bound is what should remain -- not "wait forever".
    """

    budget_seconds = _routing_module._upstream_dispatch_budget_seconds(request_data)
    if budget_seconds <= 0:
        budget_seconds = fallback_budget_seconds
    if budget_seconds <= 0:
        return await awaitable
    # Cancellation unwinds the router's selected-route ContextVar before this
    # outer budget catches its timeout. Keep the attempt's marker in the shared
    # box so that the timeout still names (and releases) the route it contacted.
    if not isinstance(_CURRENT_SELECTED_DEPLOYMENT_BOX.get(), dict):
        _CURRENT_SELECTED_DEPLOYMENT_BOX.set({})
    # Two wrong tools, both rejected by measurement rather than taste:
    #
    # ``asyncio.wait_for`` and a hand-rolled ``ensure_future`` both move the
    # dispatch into a *child task*, whose context is a copy -- so every
    # ``ContextVar`` write the attempt makes stays inside it.  This proxy keeps
    # the route that answered in one of those (``_CURRENT_SELECTED_DEPLOYMENT_BOX``),
    # and a copy silently turns "route-peer answered" into "no route identity":
    # the log row, the cooldown key and the delivery marker all lose their
    # route.  ``tests/test_hooks_streaming_compaction.py`` catches it.
    #
    # ``asyncio.timeout`` cancels the *current* task's await instead, so the
    # dispatch keeps running in the caller's own context.  It also separates the
    # two ways a ``TimeoutError`` can appear: since 3.11 ``asyncio.TimeoutError``
    # is the builtin one, and ``wait_for`` would rewrite an upstream that raised
    # its own ``TimeoutError`` into this proxy's verdict, losing the real cause.
    # ``asyncio.timeout`` raises only on its own expiry and passes an inner
    # ``TimeoutError`` through untouched.
    try:
        async with asyncio.timeout(budget_seconds):
            return await awaitable
    except TimeoutError as exc:
        if _is_dispatch_budget_expiry(exc):
            raise _stream_start_timeout_exception(
                request_data,
                start_seconds=budget_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            ) from None
        raise


def _is_dispatch_budget_expiry(exception: TimeoutError) -> bool:
    """Whether this ``TimeoutError`` is the dispatch budget's own verdict.

    ``asyncio.timeout`` raises its ``TimeoutError`` with no message of its own,
    while the failures this proxy already classifies (and every upstream that
    wraps one) carry a reason on ``body`` or at least a sentence.  The check
    exists so a route's own timeout keeps its own reason and its own route
    identity instead of being reported as this proxy's silence.
    """

    if getattr(exception, "body", None) is not None:
        return False
    return not str(exception).strip()


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

async def _dispatch_stream_hedge(
    request_data: dict,
    *,
    stage: str = "first_frame",
) -> Optional[tuple[Any, dict]]:
    """Start the same turn on a sibling route, keeping the first attempt alive.

    This is the hedge the first-frame wait uses: a route that has sent nothing
    looks exactly like a route that is thinking, so the answer is to *also* ask
    a sibling and let the first frame decide -- never to decide on the first
    route's behalf.  The sibling is chosen the ordinary way with this attempt's
    deployment excluded; nothing is marked failed here, because this attempt
    has not failed.
    """

    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return None
    if llm_router is None:
        return None
    method_name = _streaming_error_fallback_method_name(request_data)
    if method_name is None:
        return None
    payload = _build_streaming_error_fallback_payload(
        request_data,
        method_name=method_name,
        allow_repeated_attempt=True,
    )
    if payload is None:
        return None
    excluded = set(
        _responses_request_module._request_excluded_deployment_ids(payload)
    )
    current_id = _routing_module._deployment_id_from_request(request_data)
    if current_id:
        excluded.add(current_id)
    if not excluded:
        # Without a route to exclude the "sibling" would be the same deployment,
        # which is a second paid prefill for this route and not a hedge.
        return None
    payload["_excluded_deployment_ids"] = sorted(excluded)
    # The point of the hedge is the healthiest route, not the next order: a
    # slow order-0 route is exactly what the hedge is there to overtake.
    payload.pop("_target_order", None)
    hedge_metadata = _request_context_module._request_metadata_dict(
        payload, "litellm_metadata"
    ) or {}
    updated_hedge_metadata = hedge_metadata.copy()
    updated_hedge_metadata[_HEAD_HEDGE_METADATA_KEY] = True
    payload["litellm_metadata"] = updated_hedge_metadata
    router_method = getattr(llm_router, method_name, None)
    if router_method is None:
        return None
    _trace_module._request_route_trace(
        "stream_hedge_start", request_data,
        stage=stage,
        excluded_deployment_ids=sorted(excluded),
    )
    try:
        response = await _await_upstream_dispatch(
            router_method(**_streaming_error_fallback_dispatch_kwargs(payload)),
            payload,
            fallback_budget_seconds=_routing_module._stream_start_timeout_seconds_for_request(
                payload
            ),
        )
    except Exception as exc:
        _trace_module._request_route_trace(
            "stream_hedge_error", request_data,
            stage=stage,
            exception=_routing_module._trace_exception(exc),
        )
        return None
    return response, payload


def _head_hedge_eligible(request_data: dict) -> bool:
    """Whether this attempt's *dispatch* may be raced with a sibling's.

    The frame hedge below only sees a route that has already answered with a
    response head, and that is not where this gateway waits: measured over 25
    minutes of the live 1.17 MB Codex replay, selection-to-head was p50 10 s /
    p95 23 s / max 232 s while head-to-first-frame was p50 1 s.  So the silence
    a turn actually pays happens before the timer the frame hedge starts.  This
    hedge races the dispatch itself: after ``YOUNG_ROUTER_STREAM_HEDGE_DELAY_SECONDS``
    of no head a sibling is asked as well, and the first head wins.
    """

    if not isinstance(request_data, dict):
        return False
    if request_data.get("stream") is not True:
        return False
    if not _responses_request_module._request_is_responses_api(request_data):
        return False
    if _routing_module._stream_hedge_delay_seconds() <= 0:
        return False
    metadata = _request_context_module._request_metadata_dict(
        request_data, "litellm_metadata"
    ) or {}
    if metadata.get(_HEAD_HEDGE_METADATA_KEY) is True:
        # This attempt *is* the hedge. One slow turn must not fan out.
        return False
    if _responses_request_module._request_already_attempted_streaming_error_fallback(
        request_data
    ):
        return False
    if metadata.get(_ROUTE_RECOVERY_POLL_METADATA_KEY) is True:
        return False
    return _routing_module._request_group_deployment_count(request_data) != 1


async def _head_hedge_after(delay: float, request_data: dict) -> Optional[tuple[Any, dict]]:
    await asyncio.sleep(delay)
    return await _dispatch_stream_hedge(request_data, stage="head")


def _adopt_head_hedge_winner(
    request_data: dict,
    response: Any,
    payload: dict,
) -> None:
    """Make the sibling that answered first this turn's route.

    The winning dispatch ran in its own task, so the route it selected lives on
    the response's marker rather than in this context.  Everything downstream --
    cooldowns, exclusions, the request log's route key -- reads that identity
    from the request, so it is written back onto this turn here.
    """

    marker = _routing_module._selected_deployment_marker_from_response(response)
    if marker is None:
        marker = _routing_module._selected_deployment_marker_from_box()
    if isinstance(marker, dict):
        _routing_module._merge_request_routing_state_into_selected_deployment_marker(
            marker,
            payload,
        )
        _routing_module._apply_selected_deployment_marker_to_request(
            request_data,
            marker,
        )
        _routing_module._remember_attempt_route(request_data, marker)
    _trace_module._request_route_trace(
        "stream_hedge_won",
        request_data,
        stage="head",
        deployment_id=_routing_module._deployment_id_from_request(request_data),
    )


async def _await_upstream_dispatch_with_head_hedge(
    awaitable: Any,
    request_data: dict,
    *,
    fallback_budget_seconds: float = 0.0,
) -> Any:
    """Await one attempt's dispatch, racing a sibling once the delay passes.

    The first *response head* wins, not the first frame: for this gateway's
    heavy replays the head is the whole wait.  The attempt that loses is closed
    and its concurrency slot released; it was never a failure, so no route is
    parked and nothing is charged to it beyond the prefill it spent.
    """

    delay = _routing_module._stream_hedge_delay_seconds()
    budget_seconds = _routing_module._upstream_dispatch_budget_seconds(request_data)
    if budget_seconds <= 0:
        budget_seconds = fallback_budget_seconds
    if (
        delay <= 0
        or budget_seconds <= 0
        or not _head_hedge_eligible(request_data)
    ):
        return await _await_upstream_dispatch(
            awaitable,
            request_data,
            fallback_budget_seconds=fallback_budget_seconds,
        )

    # Keep the attempt's marker where this task can still read it: the primary
    # below runs in a child task, whose context is a copy.
    if not isinstance(_CURRENT_SELECTED_DEPLOYMENT_BOX.get(), dict):
        _CURRENT_SELECTED_DEPLOYMENT_BOX.set({})
    primary = asyncio.ensure_future(awaitable)
    hedge: Optional["asyncio.Future[Any]"] = asyncio.ensure_future(
        _head_hedge_after(delay, request_data)
    )
    try:
        async with asyncio.timeout(budget_seconds):
            while True:
                done, _pending = await asyncio.wait(
                    {task for task in (primary, hedge) if task is not None},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if hedge is not None and hedge in done:
                    hedge_result = hedge.result()
                    hedge = None
                    if hedge_result is not None:
                        winner_response, winner_payload = hedge_result
                        primary.cancel()
                        await asyncio.gather(primary, return_exceptions=True)
                        # The loser's slot is not released here: the release
                        # this proxy has is keyed by the turn, and the winner
                        # shares that key.  Its own completion gives the slot
                        # back, and a lease that outlives that ages out -- the
                        # documented cost of a hop this proxy did not observe.
                        _adopt_head_hedge_winner(
                            request_data,
                            winner_response,
                            winner_payload,
                        )
                        return winner_response
                if primary in done:
                    return primary.result()
    except TimeoutError as exc:
        for task in (primary, hedge):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in (primary, hedge) if task is not None],
            return_exceptions=True,
        )
        if _is_dispatch_budget_expiry(exc):
            raise _stream_start_timeout_exception(
                request_data,
                start_seconds=budget_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            ) from None
        raise
    except BaseException:
        for task in (primary, hedge):
            if task is not None:
                task.cancel()
        await asyncio.gather(
            *[task for task in (primary, hedge) if task is not None],
            return_exceptions=True,
        )
        raise


def _stream_hedge_eligible(request_data: dict) -> bool:
    if not isinstance(request_data, dict):
        return False
    if request_data.get("stream") is not True:
        return False
    if not _responses_request_module._request_is_responses_api(request_data):
        return False
    if _routing_module._stream_hedge_delay_seconds() <= 0:
        return False
    # One hedge belongs to the turn's first attempt.  A fallback or recovery
    # round is already a second route, and hedging it would pay two more
    # prefills for the same frame.
    if _responses_request_module._request_already_attempted_streaming_error_fallback(
        request_data
    ):
        return False
    metadata = _request_context_module._request_metadata_dict(
        request_data, "litellm_metadata"
    ) or {}
    if metadata.get(_ROUTE_RECOVERY_POLL_METADATA_KEY) is True:
        return False
    # A group of one has no sibling to hedge with; asking twice would only pay
    # this route's prefill twice.
    return _routing_module._request_group_deployment_count(request_data) != 1


async def _await_first_frame(
    task: "asyncio.Task[Any]",
    budget: Optional[float],
) -> Any:
    if budget is None:
        return await task
    return await asyncio.wait_for(task, timeout=budget)


async def _settle_task(task: Optional["asyncio.Task[Any]"]) -> None:
    if task is None or task.done():
        return
    task.cancel()
    await asyncio.gather(task, return_exceptions=True)


async def _hedged_first_chunk(
    iterator: Any,
    request_data: dict,
    *,
    start_timeout_seconds: float,
) -> tuple[Any, Any, Optional[dict]]:
    """Read the first frame, hedging onto a sibling once the delay passes.

    Returns the iterator the rest of this attempt must read (the first one, or
    the hedge that won), that first frame, and the hedge's payload when the
    hedge won -- its route identity is applied to the request there.

    The hedge is dispatched *concurrently* with this attempt: its own dispatch
    (upload plus the wait for the response head) is the same slow phase, so
    awaiting it here would stop reading the attempt that may answer first.  A
    hedge that cannot be dispatched, raises, or ends without a frame is not a
    turn failure: the first attempt is still running and the caller keeps
    waiting on it.
    """

    delay = _routing_module._stream_hedge_delay_seconds()
    current_deployment_id = _routing_module._deployment_id_from_request(request_data)
    started_at = time.monotonic()

    def remaining() -> Optional[float]:
        if start_timeout_seconds <= 0:
            return None
        return max(0.0, start_timeout_seconds - (time.monotonic() - started_at))

    primary_task: asyncio.Task[Any] = asyncio.ensure_future(iterator.__anext__())
    hedge_dispatch_task: Optional[asyncio.Task[Any]] = None
    hedge_task: Optional[asyncio.Task[Any]] = None
    hedge_iterator = None
    hedge_response = None
    hedge_payload: Optional[dict] = None
    hedge_handed_over = False

    async def abandon_hedge() -> None:
        nonlocal hedge_iterator, hedge_payload, hedge_task
        await _settle_task(hedge_task)
        if hedge_iterator is not None:
            await _close_async_iterator_safely(hedge_iterator)
        if hedge_payload is not None:
            _routing_module._release_request_concurrency_slots(hedge_payload)
        hedge_iterator = None
        hedge_payload = None
        hedge_task = None

    try:
        # Phase one: wait the hedge delay (never past this attempt's own first
        # frame budget) for the attempt that is already running.
        budget = remaining()
        first_wait = min(delay, budget) if budget is not None else delay
        if first_wait > 0:
            done, _pending = await asyncio.wait({primary_task}, timeout=first_wait)
            if done:
                return iterator, primary_task.result(), None
        hedge_dispatch_task = asyncio.ensure_future(
            _dispatch_stream_hedge(request_data)
        )
        pending: set = {primary_task, hedge_dispatch_task}
        while True:
            budget = remaining()
            if budget is not None and budget <= 0:
                raise asyncio.TimeoutError()
            done, _pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_COMPLETED,
                timeout=budget,
            )
            if not done:
                raise asyncio.TimeoutError()
            if primary_task in done:
                await abandon_hedge()
                return iterator, primary_task.result(), None
            if hedge_dispatch_task in done:
                pending.discard(hedge_dispatch_task)
                hedge = None
                try:
                    hedge = hedge_dispatch_task.result()
                except Exception as exc:
                    _trace_module._request_route_trace(
                        "stream_hedge_error", request_data,
                        exception=_routing_module._trace_exception(exc),
                    )
                if hedge is None:
                    # Nothing to race with; the first attempt still owns the
                    # turn and its remaining budget is unchanged.
                    continue
                hedge_response, hedge_payload = hedge
                hedge_iterator = hedge_response.__aiter__()
                hedge_task = asyncio.ensure_future(hedge_iterator.__anext__())
                pending.add(hedge_task)
                continue
            if hedge_task is not None and hedge_task in done:
                pending.discard(hedge_task)
                hedge_error = (
                    None if hedge_task.cancelled() else hedge_task.exception()
                )
                if hedge_error is None:
                    chunk = hedge_task.result()
                    marker = _routing_module._selected_deployment_marker_from_response(
                        hedge_response
                    )
                    _routing_module._apply_selected_deployment_marker_to_request(
                        request_data, marker
                    )
                    if isinstance(marker, dict):
                        # This task's context still names the first attempt; the
                        # frames that follow belong to the hedge, so the context
                        # has to say so as well.
                        _routing_module._remember_selected_deployment(
                            marker,
                            surface=str(marker.get("surface") or ""),
                        )
                    await _settle_task(primary_task)
                    await _close_async_iterator_safely(iterator)
                    hedge_handed_over = True
                    _trace_module._request_route_trace(
                        "stream_hedge_won", request_data,
                        hedge_deployment_id=_routing_module._deployment_id_from_request(
                            hedge_payload or {}
                        ),
                        primary_deployment_id=current_deployment_id,
                    )
                    return hedge_iterator, chunk, hedge_payload
                # The hedge's own failure says nothing about this turn.
                _trace_module._request_route_trace(
                    "stream_hedge_error", request_data,
                    exception=_routing_module._trace_exception(hedge_error),
                )
                await abandon_hedge()
                continue
    finally:
        await _settle_task(hedge_dispatch_task)
        await _settle_task(primary_task)
        if not hedge_handed_over:
            await abandon_hedge()


async def _stream_with_idle_timeout(
    response: Any,
    request_data: dict,
    *,
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
    hedge_pending = (not saw_chunk) and _stream_hedge_eligible(request_data)
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
            if hedge_pending:
                hedge_pending = False
                # The first frame decides between this attempt and the hedge;
                # the helper owns the delay, the race and the loser's cleanup.
                iterator, chunk, hedge_payload = await _hedged_first_chunk(
                    iterator,
                    request_data,
                    start_timeout_seconds=start_timeout_seconds,
                )
                if hedge_payload is not None:
                    _routing_module._apply_current_selected_deployment_to_request(
                        request_data,
                    )
            elif effective_timeout is not None:
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
        _raise_if_synthetic_timeout_notice(chunk, request_data)
        _raise_if_leaked_tool_call_markup(chunk, request_data)
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

def _dsml_trace(request_data: Optional[dict], **fields: Any) -> None:
    """Record that this answer carried markup the filter rewrote.

    A Messages answer whose ``text_delta`` frames were rewritten is otherwise
    invisible: the client sees the visible half either way, so a filter that
    never ran looks exactly like a route that wrote no markup.
    """

    from . import trace as _trace_module

    try:
        _trace_module._route_trace(
            _anthropic_dsml_module.TRACE_EVENT,
            model=(request_data or {}).get("model"),
            **fields,
        )
    except Exception:
        return


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
        # A replay is the same caller's silence profile, not this process's
        # default: a continuation that raised its idle budget would otherwise
        # be torn down mid-reasoning on the route that took over.
        "stream_idle_timeout_seconds",
        "stream_start_timeout_seconds",
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
    # A route that refused the *ciphertext* is not excluded: this attempt's body
    # has already been repaired, and the repaired replay belongs in the same
    # pool.  Every other exclusion in the request stays.
    repaired_route = (
        _responses_execution_module._failed_deployment_id(exception)
        if _routing_module._request_encrypted_replay_was_stripped(request_data)
        else None
    )
    if not repaired_route:
        _routing_module._sync_failed_deployment_exclusions(request_data, exception)

    def cumulative_excluded_ids() -> set[str]:
        excluded_ids = set(_CURRENT_EXCLUDED_DEPLOYMENT_IDS.get() or ())
        excluded_ids.update(
            _routing_module._exception_excluded_deployment_ids(exception)
        )
        excluded_ids.update(
            _responses_request_module._request_excluded_deployment_ids(request_data)
        )
        if repaired_route:
            excluded_ids.discard(repaired_route)
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
        for container in (request_data, payload):
            metadata = container.get("litellm_metadata")
            if isinstance(metadata, dict):
                metadata = metadata.copy()
                metadata["excluded_deployment_ids"] = serialized_excluded_ids
                container["litellm_metadata"] = metadata
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
    if repaired_route:
        excluded_ids.discard(repaired_route)
    if excluded_ids:
        preserve_cumulative_exclusions(
            {"_excluded_deployment_ids": sorted(excluded_ids)}
        )
    elif repaired_route:
        # The repaired body carries no verdict against the route that refused
        # the ciphertext, so the exclusion this hop inherited is dropped from
        # both containers -- otherwise the retry still cannot see that route.
        for container in (request_data, payload):
            if container.get("_excluded_deployment_ids") == [repaired_route]:
                container.pop("_excluded_deployment_ids", None)

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
    if not _routing_module._is_streaming_error_fallback_eligible(
        exception,
        request_data,
    ):
        # One predicate owns "can a round act on this": the recovery poll uses
        # the same answer before opening its window, so the two cannot disagree
        # about a seed that produces empty rounds.
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
        # A refusal about the ciphertext says nothing about the route: the body
        # has just been repaired, and the repaired replay goes back to the same
        # pool.  Marking it here would exclude the route the order would pick.
        and not _routing_module._request_encrypted_replay_was_stripped(request_data)
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
        # The retry hop dispatches through the same budget as the first one: the
        # route this round picks is just as capable of being absent, and the
        # first-event clock still cannot see the phase that decides that.  The
        # start budget stays as the fallback for an operator who turned the
        # connect budget off.
        response = await _await_upstream_dispatch(
            router_method(**dispatch_kwargs),
            payload,
            fallback_budget_seconds=stream_start_timeout_seconds,
        )
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
    # This round is a new attempt in the same client turn: from here the turn's
    # outcome is whatever this route delivers.  The marker the failed attempt
    # left would otherwise make a successful replay log itself as a failure (and
    # keep its route parked).
    if isinstance(request_data, dict):
        failed_attempt = request_data.pop(_TERMINAL_FAILURE_REQUEST_KEY, None)
        if failed_attempt is None:
            # The previous attempt may have logged its verdict through the
            # shared registry instead of this dict; a new round starts clean.
            _routing_module._take_terminal_failure_for_request(request_data)
        if failed_attempt is not None and request_data.get("exception") is failed_attempt:
            request_data.pop("exception", None)
        if _routing_module._is_local_stream_start_timeout_error(exception):
            # One attempt spent the heavy-replay first-event budget and produced
            # nothing.  The routes after it get the ordinary budget: the prefix
            # that earned the larger one is the same on every route, so another
            # five-minute silence is a dead route, not a slow replay.
            request_data[_START_TIMEOUT_OBSERVED_KEY] = True
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
                # The turn's client-visible terminal is a failure, and the
                # caller's request object is the one LiteLLM's own callbacks
                # hold.  Without this the replay's HTTP close still reads as a
                # delivered answer -- success row, cleared cooldown.
                if isinstance(request_data, dict):
                    request_data[_TERMINAL_FAILURE_REQUEST_KEY] = (
                        failed_exception
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

async def _empty_async_iterator() -> AsyncIterator[Any]:
    for chunk in ():
        yield chunk

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
        # The backend's historical-reasoning note is context, not an answer.
        # A note that streams through here is dropped before delivery, and an
        # input replay of one is removed by the request step, so neither the
        # client nor the next request keeps a copy to imitate.
        self.suppressed_note_item_ids: set[str] = set()
        self.note_text_by_item_id: dict[str, str] = {}
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

        if chunk_type == "response.output_text.delta":
            note_item_id = dumped.get("item_id") or dumped.get("id")
            delta = dumped.get("delta")
            if isinstance(note_item_id, str) and isinstance(delta, str):
                candidate = self.note_text_by_item_id.get(note_item_id, "") + delta
                self.note_text_by_item_id[note_item_id] = candidate
                if _historical_note_module.is_note_text(candidate) or candidate.startswith(
                    _historical_note_module.NOTE_PREFIX
                ):
                    self.suppressed_note_item_ids.add(note_item_id)
        elif chunk_type == "response.output_item.done":
            done_item = dumped.get("item")
            if _historical_note_module.is_note_item(done_item):
                note_item_id = _stream_output_item_key(done_item)
                if note_item_id:
                    self.suppressed_note_item_ids.add(note_item_id)

        text_fragment, is_done_text = _stream_chunk_text_fragment(dumped)
        if text_fragment and not self._is_suppressed_note_chunk(dumped):
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

    def _is_suppressed_note_chunk(self, dumped: Any) -> bool:
        if not isinstance(dumped, dict):
            return False
        chunk_type = _stream_chunk_type(dumped)
        item_id = dumped.get("item_id") or dumped.get("id")
        if isinstance(item_id, str) and item_id in self.suppressed_note_item_ids:
            return True
        if chunk_type == "response.output_text.done":
            return _historical_note_module.is_note_text(dumped.get("text"))
        if chunk_type == "response.output_item.done":
            return _historical_note_module.is_note_item(dumped.get("item"))
        return False

    def suppresses(self, chunk: Any) -> bool:
        """Whether this frame belongs to a note the client must not record."""

        return self._is_suppressed_note_chunk(_stream_chunk_dump(chunk))

    def _drop_note_output_items(self, response: Any) -> bool:
        if not isinstance(response, dict):
            return False
        output = response.get("output")
        if not isinstance(output, list):
            return False
        filtered = [
            item for item in output if not _historical_note_module.is_note_item(item)
        ]
        if len(filtered) == len(output):
            return False
        if not filtered:
            # A response that is *only* the note must still arrive: dropping
            # every item leaves the client a completion with no output, which
            # it reads as a failed turn and reconnects over.  The request step
            # keeps the replay clean; this frame is the one case where the
            # note is all the model produced.
            return False
        response["output"] = filtered
        return True

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
                if self._drop_note_output_items(response):
                    return _JSONStreamEvent(dumped)
                return chunk
            dumped["response"] = self.completed_payload(request_data)
            self._drop_note_output_items(dumped.get("response"))
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
    _raise_if_synthetic_timeout_notice(response, request_data)
    _raise_if_leaked_tool_call_markup(response, request_data)
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
            for delivered_frame in _delivered_frames(events, request_data):
                yield delivered_frame
            await _close_async_iterator_safely(response)
            return

    try:
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
            initial_chunk_count=len(buffer),
        ):
            if remember(chunk) is not None:
                for delivered_frame in _delivered_frames(events, request_data):
                    yield delivered_frame
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
        for delivered_frame in _delivered_frames(events, request_data):
            yield delivered_frame
        yield completed_compat
        await _close_async_iterator_safely(response)
        return

    completed_compat = _codex_compaction_done_item_completed_compat(
        completion_state,
        request_data,
        reason="clean_eof_after_completed_compaction_item",
    )
    if completed_compat is not None:
        for delivered_frame in _delivered_frames(events, request_data):
            yield delivered_frame
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
    # The output tokens this route reported for the answer it just ended.  A
    # route that honours ``stream_options.include_usage`` states them in a
    # trailing frame of their own, *after* the ``finish_reason`` one, so the
    # ceiling frame alone carries no count -- and a decision that could only
    # read that frame refused every route that answers a ceiling this way.
    answered_output_tokens: Optional[int] = None

    def remember_route_accounting(chunk: Any) -> None:
        nonlocal answered_output_tokens
        tokens = _responses_incomplete_output_tokens(chunk)
        if tokens is not None:
            answered_output_tokens = tokens

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
        remember_route_accounting(chunk)
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

    # DeepSeek-V4 answers a tool request with its own markup instead of
    # ``tool_calls``.  On the Responses wire this proxy already parses that
    # markup; a Chat/Anthropic stream had no such path, so the call arrived as
    # answer text and the client rendered XML it could not execute (measured on
    # the live gateway 2026-11-16, replaying the PIK3CA thread: the answer began
    # ``\n\n<｜｜DSML｜｜ calls>\n<｜｜DSML｜｜ invoke name="Bash">``).  The
    # filter holds the markup back while it streams and hands it over as real
    # tool calls once the block closes -- see ``anthropic_dsml``.
    dsml_filter = _anthropic_dsml_module.MessageStreamFilter()
    is_anthropic_stream = _request_is_anthropic_messages_stream(request_data)
    if not _request_is_responses_stream(request_data):
        _dsml_trace(
            request_data,
            stage="open",
            anthropic=is_anthropic_stream,
            surface=_routing_module._request_client_surface(request_data),
            stream=_stream_chunk_type(buffer[0]) if buffer else None,
            choices=bool(isinstance(_stream_chunk_dump(buffer[0]), dict)
                        and (_stream_chunk_dump(buffer[0]) or {}).get("choices")) if buffer else None,
        )
    # Whether the filter is looking at this answer at all is the difference
    # between "the route wrote no markup" and "nothing rewrote it", and the two
    # are indistinguishable from the client's side.  One trace per answer.
    dsml_text_frames = [0]
    dsml_yielded: dict = {}
    # Which content blocks this answer has open.  The frames a DSML block turns
    # into are appended at the end of the answer, and a client that receives a
    # ``content_block_start`` while an earlier block is still open reads the
    # earlier block as never finished -- so the text block has to be closed
    # before the call's block opens.
    dsml_open_blocks: List[int] = []

    def dsml_clean(chunk: Any) -> Optional[Any]:
        """The visible half of one frame, or ``None`` when it held only markup."""

        if not is_anthropic_stream:
            return chunk
        dumped = _stream_chunk_dump(chunk)
        if not isinstance(dumped, dict):
            return chunk
        if dumped.get("type") == "content_block_start":
            index = dumped.get("index")
            if isinstance(index, int) and index not in dsml_open_blocks:
                dsml_open_blocks.append(index)
            return chunk
        if dumped.get("type") == "content_block_stop":
            index = dumped.get("index")
            if index in dsml_open_blocks:
                dsml_open_blocks.remove(index)
            return chunk
        if dumped.get("type") != "content_block_delta":
            return chunk
        delta = dumped.get("delta")
        if not isinstance(delta, dict) or delta.get("type") != "text_delta":
            return chunk
        text = delta.get("text")
        if not isinstance(text, str) or not text:
            return chunk
        visible = dsml_filter.feed(text)
        dsml_text_frames[0] += 1
        if visible == text:
            return chunk
        if not visible:
            return None
        clean = dict(dumped)
        clean_delta = dict(delta)
        clean_delta["text"] = visible
        clean["delta"] = clean_delta
        return _json_stream_event(clean)

    def dsml_tool_use_events() -> List[Any]:
        """The calls the filter collected, as Messages ``tool_use`` frames."""

        events: List[Any] = []
        calls = dsml_filter.take_calls()
        if calls:
            # Close what the answer left open (the text block) before the
            # synthesized blocks begin; a client that never saw this stop keeps
            # the text block dangling while the tool call is already running.
            for open_index in list(dsml_open_blocks):
                events.append(
                    _json_stream_event(
                        {"type": "content_block_stop", "index": open_index}
                    )
                )
            dsml_open_blocks.clear()
        for call_index, call in enumerate(calls):
            # Indices the answer never used, so a client cannot confuse a
            # synthesized block with one the route wrote itself.
            index = 900 + call_index
            events.append(
                _json_stream_event(
                    {
                        "type": "content_block_start",
                        "index": index,
                        "content_block": {
                            "type": "tool_use",
                            "id": call["id"],
                            "name": call["name"],
                            "input": {},
                        },
                    }
                )
            )
            events.append(
                _json_stream_event(
                    {
                        "type": "content_block_delta",
                        "index": index,
                        "delta": {
                            "type": "input_json_delta",
                            "partial_json": json.dumps(call["input"], ensure_ascii=False),
                        },
                    }
                )
            )
            events.append(
                _json_stream_event({"type": "content_block_stop", "index": index})
            )
        return events

    async def deliver_route() -> AsyncIterator[Any]:
        nonlocal withheld_ceiling_terminal
        nonlocal native_chunk_count
        for chunk in buffer:
            if not inspect_native_chunk(chunk):
                return
            cleaned = dsml_clean(chunk)
            if cleaned is not None:
                yielded = remember_delivered_text(cleaned)
                kind = str(_stream_chunk_type(yielded))
                dsml_yielded[kind] = dsml_yielded.get(kind, 0) + 1
                yield yielded
        # Whether the ceiling frame has been withheld.  Once it has, the
        # frames that close the same answer belong to it: ``message_stop``
        # and the ``finish_reason`` terminal state the same completion the
        # withheld frame described, so they are withheld with it rather than
        # delivered ahead of the continuation that has not run yet.
        ceiling_withheld = False
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
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
                remember_route_accounting(chunk)
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
                remember_route_accounting(chunk)
                continue
            if not inspect_native_chunk(chunk):
                return
            cleaned = dsml_clean(chunk)
            if cleaned is not None:
                yielded = remember_delivered_text(cleaned)
                kind = str(_stream_chunk_type(yielded))
                dsml_yielded[kind] = dsml_yielded.get(kind, 0) + 1
                yield yielded
        # The answer is over: whatever markup is still held is all there will
        # be.  Its visible tail is delivered first (an unterminated block still
        # ends the answer the route wrote), then the calls it parsed to.  They
        # go before the terminal frames, which the caller emits afterwards.
        tail = dsml_filter.drain()
        if tail:
            yield _json_stream_event(
                {
                    "type": "content_block_delta",
                    "index": 0,
                    "delta": {"type": "text_delta", "text": tail},
                }
            )
        for event in dsml_tool_use_events():
            yield event
        if is_anthropic_stream:
            _dsml_trace(
                request_data,
                frames=dsml_text_frames[0],
                calls=len(dsml_filter.calls_parsed()),
                marker=dsml_filter.saw_marker,
                tail=len(tail),
                yielded=dsml_yielded,
            )

    try:
        async for chunk in deliver_route():
            yield chunk
    except (Exception, asyncio.CancelledError) as exception:
        if (
            isinstance(exception, asyncio.CancelledError)
            and not _routing_module._is_local_shutdown_exception(exception)
        ):
            # A cancellation that is not this worker's own teardown is the
            # runtime taking the task down for its own reasons, so it stays
            # cooperative and is never read as a finished turn.
            raise
        if _routing_module._is_local_shutdown_exception(exception):
            # The worker is being replaced.  A clean close would be read as a
            # finished turn by a client that waits for a terminal frame, so
            # state the restart and let the client resend onto the replacement.
            # The shutdown patch ends a turn the drain could not finish by
            # cancelling it (``base._retire_remaining_streams``), and the
            # cancellation surfaces here as a ``BaseException`` the error
            # branches below never see.
            yield _synthesized_native_stream_error_chunk(request_data, exception)
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
        continuation_closed = False
        async for chunk in _stream_continuation_module._stream_native_continuation_rounds(
            request_data,
            "".join(delivered_text_parts),
            stream_idle_timeout_seconds=_resolved_stream_timeout_seconds(
                request_data,
                "stream_idle_timeout_seconds",
                _routing_module._stream_idle_timeout_seconds_for_request,
            ),
            ceiling_chunk=withheld_ceiling_terminal,
            answered_output_tokens=answered_output_tokens,
        ):
            # The same test the continuation applies before it hands over a
            # route's closing frame: that frame is the only terminal it yields.
            if _native_stream_chunk_is_terminal(chunk, request_data, set()):
                continuation_closed = True
            yield remember_delivered_text(chunk)
        # The continuation owns the answer's last frame: when it reached a
        # natural stop it already emitted that frame, and when it ran out of
        # rounds the withheld "length" frame is the honest report that the
        # answer is still unfinished.  Re-emitting the withheld frame on top of
        # a natural stop would tell the client an answer that finished had hit
        # the ceiling.
        #
        # "It closed the answer" is not "it yielded something": rounds that add
        # text and hit the ceiling again, or one that fails after it started
        # writing, yield frames and no terminal.  Reading those as closed left
        # the client a stream with no end.
        if not continuation_closed:
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
                # The same ending the exception branch above reports, for the
                # same reason: this body is about to close with no terminal
                # frame, and a Chat client that reads that close as a finished
                # turn keeps a half-written answer it will never be told is
                # unfinished (Cherry Studio reports it as ``Invalid response:
                # the provider response could not be processed``).  The restart
                # is the one stop that resends onto the replacement.
                yield _synthesized_native_stream_error_chunk(
                    request_data,
                    incomplete_exception,
                )
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
        ):
            yield chunk
        return

    event_tail: List[Any] = []
    visible_output_seen = saw_visible_output
    # Whether this answer has *begun*, which is a different question from
    # whether the client was shown anything: thinking is delivered the moment
    # it arrives but it never answers the turn.  A bridged chat route that is
    # cut while it is still thinking reports itself as ``response.completed``
    # with a reasoning item and nothing else (LiteLLM's completion bridge
    # fabricates that terminal from whatever it collected when the chat stream
    # simply stops).  Handing that to the client is a finished turn that
    # states no outcome at all, so the empty-completion check below reads this
    # flag rather than the delivered-output one, whose thinking would waive it.
    answer_output_seen = False
    seen_output_item_ids: set[str] = set()
    pending_tool_items: dict[str, tuple[int, dict[str, Any]]] = {}
    completed_output_items: dict[int, dict[str, Any]] = {}
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

    def deliver_client_chunk(chunk: Any) -> Any:
        if completion_state.suppresses(chunk):
            return None
        return _responses_stream_chunk_for_delivery(
            completion_state.chunk_for_delivery(chunk, request_data),
            request_data,
        )

    def deliverable_client_chunks(chunk: Any):
        """Deliver one upstream frame unless this router withholds it."""

        delivered = deliver_client_chunk(chunk)
        if delivered is not None:
            yield delivered

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

    def completed_response_has_output_items(chunk: Any) -> bool:
        """Whether this terminal states an output list with any item at all.

        A fabricated terminal that omits the answer carries a reasoning item
        (and possibly an empty message), while a relay may legitimately state a
        terminal with no output list at all.  Only the former can silently
        replace an answer the reader already saw.
        """

        dumped = _stream_chunk_dump(chunk)
        response = dumped.get("response") if isinstance(dumped, dict) else None
        output = response.get("output") if isinstance(response, dict) else None
        return isinstance(output, list) and bool(output)

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

    # An empty completion is this proxy's own verdict, not an upstream cut.  It
    # ends the read loop the same way, but the two must not be conflated: a cut
    # stream is evidence against the route and is quarantined on the first
    # occurrence, while an answer that spent its whole budget thinking is a
    # request that asked for too little.  Conflated, one ``max_output_tokens:
    # 16`` probe on a thinking route evicted the fastest route for five minutes,
    # and every request in that window fell to a relay seven times slower.
    empty_completion_ended_stream = False

    def reopen_the_turn_after_a_thinking_only_cut(failure: Exception) -> bool:
        """Ask the route machinery for one more route instead of the reader.

        Only thinking has reached the client: answer text and tool calls cannot
        be sent twice, thinking can be derived again, and a sibling route is a
        far cheaper answer than the reconnect Codex does when this terminal
        reaches it (``stream disconnected before completion``, ``1/5``).  The
        route is parked first so the hop that follows picks a different one.
        This is offered to a turn once -- the same body reaching the same cap
        twice is a stop the reader is owed, and the window this would otherwise
        open is hours of thinking nobody can read.
        """

        if not _routing_module._claim_thinking_only_cut_retry(request_data):
            return False
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            failure,
            request_data,
        ):
            _routing_module._mark_exception_for_deployment_failover(
                failure,
                request_data,
            )
        _trace_module._request_route_trace(
            "responses_cut_after_thinking_only_retried", request_data,
            exception=_routing_module._trace_exception(failure),
        )
        return True

    def item_cap_leaves_the_turn_unanswered(chunk: Any) -> bool:
        """Whether a route's item cap ended a turn that has not answered yet.

        ``max_messages`` counts what one response may generate, so a route that
        hits it mid-thinking has not answered anything -- the reader got a few
        progress lines and a stop.  Measured 2026-11-20 on the tken relay: two
        capped turns had produced 100 and 98 reasoning items when the terminal
        arrived (1m49s and 3m45s in) while every turn that finished stayed
        around ten, and the same model finished the same kind of turn on
        flux/youyangai without ever stating this reason.  So this is a route
        limit, and a sibling route continues the turn instead of the reader
        being told to send it again.

        A finished tool call is work that already happened and a long answer is
        text the reader is reading; neither may be produced twice, so both keep
        the stop.  What remains is the short-progress case, where the duplicate
        is a line or two of narration.
        """

        if _responses_incomplete_terminal_reason(chunk) not in _UPSTREAM_INPUT_ITEM_LIMIT_REASONS:
            return False
        if pending_tool_items:
            return False
        if completion_state.finished_argument_item_ids:
            return False
        return len(synthesized_text().strip()) < _CAP_CUT_RESUMABLE_TEXT_CHARS

    def stated_incomplete_terminal_event(
        chunk: Any,
        failure: Exception,
    ) -> Optional[_JSONStreamEvent]:
        """The route's own ``response.incomplete``, as the outcome it is.

        Codex's Responses reader maps an incomplete response to a terminal
        sampling outcome -- it records the spend, keeps what it already
        rendered and does not re-send the bytes -- while a failure with a code
        it does not know is retried as transport.  Handing it a failure instead
        is what made every capped turn read as ``正在重新连接 1/5`` and replay
        the same history (live 2026-11-20, thread ``01a1ee60``).
        """

        if _stream_chunk_type(_stream_chunk_dump(chunk)) != "response.incomplete":
            return None
        if pending_tool_items:
            # A half-written tool call is not an outcome: the work it stands for
            # never started, so this turn has to be resent instead of accepted
            # as a partial answer.
            return None
        if _routing_module._is_request_scoped_priority_deployment_failover_error(
            failure,
            request_data,
        ):
            _routing_module._mark_exception_for_deployment_failover(
                failure,
                request_data,
            )
        _trace_module._request_route_trace(
            "responses_upstream_incomplete_delivered",
            request_data,
            reason=_responses_incomplete_terminal_reason(chunk),
            exception=_routing_module._trace_exception(failure),
            already_yielded_visible_output=True,
        )
        return _synthesized_incomplete_response_event(
            completion_state.created_response,
            completion_state.model,
            terminal=chunk,
            output_items=completed_output_items,
        )

    def failed_terminal_event(reason: str, exception: Optional[Exception] = None) -> _JSONStreamEvent:
        failure = exception or _responses_incomplete_stream_exception(
            reason,
            buffer=event_tail,
            # The event is built here when no lower layer raised one, and the
            # constructor is where an item-cap refusal (`max_messages`) is
            # recorded against the thread.  Without the request it has no
            # thread to record, so the client's reconnect replayed the same
            # 2,000 items and every request ended this way (2026-11-20).
            request_data=request_data,
        )
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
            # The frame that ended this turn is the only evidence of *why* the
            # route cut it, and it used to live only inside a truncated
            # exception text.  Name it here: type, status, the reason the
            # upstream stated, and any error it attached.
            terminal=(
                _responses_incomplete_terminal_summary(event_tail[-1])
                if event_tail
                else None
            ),
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
        visible_output_seen = visible_output_seen or _stream_chunk_has_delivered_output(
            chunk, request_data
        )
        answer_output_seen = answer_output_seen or _stream_chunk_has_visible_output(chunk)
        completed_compat_chunk = _responses_output_limit_incomplete_as_completed_chunk(
            chunk,
            request_data,
        )
        if completed_compat_chunk is not None:
            chunk = completed_compat_chunk
            remember(chunk)
        if _responses_stream_chunk_is_completed(chunk):
            empty_completed = _responses_completed_chunk_is_empty(chunk, request_data)
            if empty_completed and _proxy_shutdown_in_progress():
                # The terminal was fabricated while this worker was exiting, so
                # it is not the answer's outcome.  Even an output list the
                # sanitizer has already emptied is the same fact: a clean close
                # here is a finished turn to the client, so state the restart
                # and let the turn be resent.
                yield _synthesized_failed_response_event(
                    request_data,
                    RuntimeError("router restarting"),
                )
                await _close_async_iterator_safely(response)
                return
            if empty_completed and completed_response_has_output_items(chunk):
                if not answer_output_seen:
                    empty_completion_ended_stream = True
                    break
                # Answer text was streamed, but the fabricated terminal carries
                # none of it.  Close with what the client actually read instead
                # of handing it a completion whose output is a blank reasoning
                # item.
                completed = completed_terminal_event(
                    "empty completed response after a streamed answer"
                )
                if completed is not None:
                    remember(completed)
                    yield completed
                    await _close_async_iterator_safely(response)
                    return
                break
            if empty_completed and not answer_output_seen:
                empty_completion_ended_stream = True
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
                yield synthetic_chunk
            for delivered_chunk in deliverable_client_chunks(chunk):
                yield delivered_chunk
            await _close_async_iterator_safely(response)
            return
        elif _responses_stream_chunk_is_incomplete_terminal(chunk):
            if _is_young_router_terminal_failure_event(chunk):
                for delivered_chunk in deliverable_client_chunks(chunk):
                    yield delivered_chunk
                await _close_async_iterator_safely(response)
                return
            terminal_events = completed_custom_tool_terminal_events()
            for synthetic_chunk in terminal_events:
                remember(synthetic_chunk)
                yield synthetic_chunk
            if terminal_events:
                await _close_async_iterator_safely(response)
                return
            output_limit_terminal = (
                _responses_incomplete_terminal_reason(chunk)
                in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS
            )
            if output_limit_terminal:
                if _incomplete_stopped_below_client_budget(chunk, request_data):
                    _trace_module._request_route_trace(
                        "responses_stream_upstream_output_limit_truncated",
                        request_data,
                        reason=_responses_incomplete_terminal_reason(chunk),
                        requested_max_output_tokens=_request_output_token_budget(request_data),
                        output_tokens=_responses_incomplete_output_tokens(chunk),
                    )
                    if (
                        completed_output_has_visible_assistant_text()
                        or synthesized_text().strip()
                    ):
                        yield failed_terminal_event(
                            "upstream stopped at its own output limit below the requested budget"
                        )
                        await _close_async_iterator_safely(response)
                        return
                    raise _responses_incomplete_stream_exception(
                        "upstream stopped at its own output limit below the requested budget",
                        request_data=request_data,
                    )
                completed = completed_terminal_event(
                    "output token limit terminal response event",
                    allow_tool_call_only=True,
                )
                if completed is not None:
                    remember(completed)
                    yield completed
                    await _close_async_iterator_safely(response)
                    return
                yield failed_terminal_event(
                    "output token limit terminal response event without usable output"
                )
                await _close_async_iterator_safely(response)
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
                    await _close_async_iterator_safely(response)
                    return
            if visible_output_seen:
                failure = _responses_incomplete_stream_exception(
                    "terminal response event before response.completed",
                    buffer=event_tail,
                    request_data=request_data,
                )
                if (
                    not answer_output_seen
                    and reopen_the_turn_after_a_thinking_only_cut(failure)
                ):
                    raise failure
                stated_outcome = stated_incomplete_terminal_event(chunk, failure)
                if stated_outcome is not None:
                    yield stated_outcome
                    await _close_async_iterator_safely(response)
                    return
                yield failed_terminal_event(
                    "terminal response event before response.completed",
                    failure,
                )
                await _close_async_iterator_safely(response)
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
        for delivered_chunk in deliverable_client_chunks(chunk):
            yield delivered_chunk

    try:
        async for chunk in _stream_with_idle_timeout(
            response,
            request_data,
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
                for delivered_chunk in deliverable_client_chunks(chunk):
                    yield delivered_chunk
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
                        await _close_async_iterator_safely(response)
                        return
                terminal_events = completed_custom_tool_terminal_events()
                for synthetic_chunk in terminal_events:
                    remember(synthetic_chunk)
                    yield synthetic_chunk
                if terminal_events:
                    await _close_async_iterator_safely(response)
                    return
                if visible_output_seen and not saw_responses_completed:
                    yield failed_terminal_event("stream error after visible output", chunk_exception)
                    await _close_async_iterator_safely(response)
                    return
                raise chunk_exception
            remember(chunk)
            visible_output_seen = visible_output_seen or _stream_chunk_has_delivered_output(
                chunk, request_data
            )
            answer_output_seen = answer_output_seen or _stream_chunk_has_visible_output(chunk)
            completed_compat_chunk = _responses_output_limit_incomplete_as_completed_chunk(
                chunk,
                request_data,
            )
            if completed_compat_chunk is not None:
                chunk = completed_compat_chunk
                remember(chunk)
            if _responses_stream_chunk_is_completed(chunk):
                empty_completed = _responses_completed_chunk_is_empty(chunk, request_data)
                if empty_completed and _proxy_shutdown_in_progress():
                    # See the buffer branch above: a terminal fabricated by this
                    # worker's own teardown must not close the turn.
                    yield _synthesized_failed_response_event(
                        request_data,
                        RuntimeError("router restarting"),
                    )
                    await _close_async_iterator_safely(response)
                    return
                if empty_completed and completed_response_has_output_items(chunk):
                    if not answer_output_seen:
                        empty_completion_ended_stream = True
                        break
                    completed = completed_terminal_event(
                        "empty completed response after a streamed answer"
                    )
                    if completed is not None:
                        remember(completed)
                        yield completed
                        await _close_async_iterator_safely(response)
                        return
                    break
                if empty_completed and not answer_output_seen:
                    empty_completion_ended_stream = True
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
                for delivered_chunk in deliverable_client_chunks(chunk):
                    yield delivered_chunk
                await _close_async_iterator_safely(response)
                return
            elif _responses_stream_chunk_is_incomplete_terminal(chunk):
                if _is_young_router_terminal_failure_event(chunk):
                    for delivered_chunk in deliverable_client_chunks(chunk):
                        yield delivered_chunk
                    await _close_async_iterator_safely(response)
                    return
                terminal_events = completed_custom_tool_terminal_events()
                for synthetic_chunk in terminal_events:
                    remember(synthetic_chunk)
                    yield synthetic_chunk
                if terminal_events:
                    await _close_async_iterator_safely(response)
                    return
                output_limit_terminal = (
                    _responses_incomplete_terminal_reason(chunk)
                    in _OUTPUT_TOKEN_LIMIT_INCOMPLETE_REASONS
                )
                if output_limit_terminal:
                    if _incomplete_stopped_below_client_budget(chunk, request_data):
                        _trace_module._request_route_trace(
                            "responses_stream_upstream_output_limit_truncated",
                            request_data,
                            reason=_responses_incomplete_terminal_reason(chunk),
                            requested_max_output_tokens=_request_output_token_budget(request_data),
                            output_tokens=_responses_incomplete_output_tokens(chunk),
                        )
                        if (
                            completed_output_has_visible_assistant_text()
                            or synthesized_text().strip()
                        ):
                            yield failed_terminal_event(
                                "upstream stopped at its own output limit below the requested budget"
                            )
                            await _close_async_iterator_safely(response)
                            return
                        raise _responses_incomplete_stream_exception(
                            "upstream stopped at its own output limit below the requested budget",
                            request_data=request_data,
                        )
                    completed = completed_terminal_event(
                        "output token limit terminal response event",
                        allow_tool_call_only=True,
                    )
                    if completed is not None:
                        remember(completed)
                        yield completed
                        await _close_async_iterator_safely(response)
                        return
                    yield failed_terminal_event(
                        "output token limit terminal response event without usable output"
                    )
                    await _close_async_iterator_safely(response)
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
                        await _close_async_iterator_safely(response)
                        return
                if visible_output_seen:
                    failure = _responses_incomplete_stream_exception(
                        "terminal response event before response.completed",
                        buffer=event_tail,
                        request_data=request_data,
                        delivered_output=True,
                    )
                    _mark_generated_output_item_cap(failure, chunk, delivered=True)
                    if item_cap_leaves_the_turn_unanswered(chunk) and (
                        reopen_the_turn_after_a_thinking_only_cut(failure)
                    ):
                        # The route's cap is not this turn's answer: park it and
                        # let the same turn continue on a sibling route, inside
                        # this one client stream.  The capped attempt's body
                        # closes normally once the hop starts, and that close
                        # would otherwise read as a delivered answer and clear
                        # the failure this cap just earned.
                        async for fallback_chunk in _yield_streaming_error_fallback_or_raise(
                            request_data,
                            failure,
                        ):
                            yield fallback_chunk
                        await _close_async_iterator_safely(response)
                        return
                    stated_outcome = stated_incomplete_terminal_event(chunk, failure)
                    if stated_outcome is not None:
                        yield stated_outcome
                        await _close_async_iterator_safely(response)
                        return
                    yield failed_terminal_event(
                        "terminal response event before response.completed",
                        failure,
                    )
                    await _close_async_iterator_safely(response)
                    return
                raise _responses_incomplete_stream_exception(
                    "terminal response event before response.completed",
                    buffer=event_tail,
                    request_data=request_data,
                )
            for delivered_chunk in deliverable_client_chunks(chunk):
                yield delivered_chunk
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
                await _close_async_iterator_safely(response)
                return
            # Nothing usable arrived, so there is no partial turn to keep.  A
            # clean body close is still read as a finished turn, so state the
            # restart as the retryable stop it is.
            yield _synthesized_failed_response_event(request_data, exc)
            await _close_async_iterator_safely(response)
            return
        if saw_image_generation_activity:
            completed = completed_image_generation_terminal_event(
                "stream error after completed image generation output"
            )
            if completed is not None:
                remember(completed)
                yield completed
                await _close_async_iterator_safely(response)
                return
        terminal_events = completed_custom_tool_terminal_events()
        for synthetic_chunk in terminal_events:
            remember(synthetic_chunk)
            yield synthetic_chunk
        if terminal_events:
            await _close_async_iterator_safely(response)
            return
        if visible_output_seen and not saw_responses_completed:
            yield failed_terminal_event("stream error after visible output", exc)
            await _close_async_iterator_safely(response)
            return
        raise

    if not saw_responses_completed and saw_image_generation_activity:
        completed = completed_image_generation_terminal_event(
            "image generation output without response.completed"
        )
        if completed is not None:
            remember(completed)
            yield completed
        await _close_async_iterator_safely(response)
        return

    if not saw_responses_completed:
        terminal_events = completed_custom_tool_terminal_events()
        for synthetic_chunk in terminal_events:
            remember(synthetic_chunk)
            yield synthetic_chunk
        if terminal_events:
            await _close_async_iterator_safely(response)
            return

    if not saw_responses_completed and pending_tool_items:
        for synthetic_chunk in _synthesized_pending_tool_completion_events(
            pending_tool_items,
            completed_output_items,
            completion_state.created_response,
            completion_state.model,
        ):
            remember(synthetic_chunk)
            yield synthetic_chunk
        await _close_async_iterator_safely(response)
        return

    if not saw_responses_completed:
        if visible_output_seen:
            incomplete_exception = _responses_incomplete_stream_exception(
                (
                    "the completion carried no answer"
                    if empty_completion_ended_stream
                    else "stream ended before response.completed"
                ),
                buffer=event_tail,
                request_data=request_data,
            )
            if empty_completion_ended_stream:
                # Nothing was cut: the read loop reached its own end because
                # the answer was empty.  The client-facing code stays what it
                # was -- an empty answer still fails the turn -- but the route
                # is told not to put this on its health record, because the
                # first-strike quarantine is for a stream that was cut.
                try: incomplete_exception.empty_completion = True  # type: ignore[attr-defined]
                except Exception: pass
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
                    await _close_async_iterator_safely(response)
                    return
                # The answer never began.  Closing the body without a terminal
                # is read as a finished turn by the client, which is how a
                # restart ended a main thread that still had subagents running.
                # State the restart so the turn is resent instead.
                yield _synthesized_failed_response_event(request_data, incomplete_exception)
                await _close_async_iterator_safely(response)
                return
            if (
                not answer_output_seen
                and reopen_the_turn_after_a_thinking_only_cut(incomplete_exception)
            ):
                raise incomplete_exception
            if synthesize_completed_on_clean_eof_after_visible_output:
                terminal_items = terminal_completed_output_items()
                synthetic_completed = _synthesized_completed_response_event(
                    terminal_items,
                    completion_state.created_response,
                    completion_state.model,
                    fallback_text=synthesized_text(),
                )
                # Thinking alone leaves nothing to complete, so fall through to
                # the failed terminal instead of yielding None to the client.
                if synthetic_completed is not None:
                    remember(synthetic_completed)
                    yield synthetic_completed
                    await _close_async_iterator_safely(response)
                    return
            yield failed_terminal_event(
                "stream ended before response.completed",
                incomplete_exception,
            )
            await _close_async_iterator_safely(response)
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
            await _close_async_iterator_safely(response)
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
        # A rolling worker replacement must not close the body without a
        # terminal: a client that reads a clean end of body as a finished turn
        # ends the conversation instead of resending it.  The replacement is
        # already listening, so this is the retryable restart stop.
        if _request_is_responses_stream(request_data):
            yield _synthesized_failed_response_event(request_data, exception)
        else:
            yield _synthesized_native_stream_error_chunk(request_data, exception)
        return

    # A provider may return an async iterator successfully and raise the
    # upstream 400 only on a later iterator read. That bypasses LiteLLM's
    # generic-call exception path, so record the route-local compaction
    # capability evidence before any recovery/fallback policy is evaluated.
    _routing_module._apply_current_selected_deployment_to_request(request_data)
    if (
        (
            _routing_module._is_route_cut_stream_error(exception)
            or _routing_module._is_upstream_body_read_failure(exception)
        )
        and not _routing_module._is_network_recovery_exception(exception)
    ):
        # A body that fails while it is being read never passes through
        # LiteLLM's failure callbacks, and the wrapper that consumed it has
        # already reset the selected-route marker by the time the exception
        # arrives here.  This is the last place that can still name the route:
        # without the mark, the fallback excludes nothing and the recovery poll
        # replays the same bytes at the same deployment forever (live
        # 2026-11-18: 213 rounds, every one with a null failed_deployment_id).
        _routing_module._mark_exception_for_deployment_failover(
            exception,
            request_data,
        )
    # A route that could not decrypt this history is answering about the bytes,
    # not about itself: the same conversation is readable once the ciphertext no
    # route can read is gone.  So the repair is a resend, not a hop -- the failed
    # route goes back into the pool and the repaired body is sent again at once
    # (order decides which route that is).  Dropping the ciphertext here, before
    # the fallback and the recovery poll rebuild their requests, is what makes
    # every later hop carry the repaired body too.
    if _routing_module._strip_unreadable_encrypted_replay(request_data, exception):
        _routing_module._allow_the_failed_deployment_again(exception, request_data)
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
                # Thinking is shown the moment it arrives, exactly as in the
                # ordinary start buffer.  A fallback that held summary deltas
                # until the answer handed the reader the whole reasoning block
                # at once; it is delivered output, so it ends this window.
                chunk_has_public_reasoning = _stream_chunk_is_public_reasoning_delta(
                    chunk, request_data
                )
                chunk_is_completed = _responses_completed_chunk_has_usable_output(
                    chunk,
                    request_data,
                )
                chunk_is_incomplete_terminal = _responses_stream_chunk_is_incomplete_terminal(chunk)
                if not (
                    chunk_has_visible_output
                    or chunk_has_public_reasoning
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
                    or chunk_has_public_reasoning
                    or chunk_is_completed
                    or chunk_is_incomplete_terminal
                )
            elif is_responses_stream:
                fallback_delivered_terminal_or_visible = (
                    fallback_delivered_terminal_or_visible
                    or _stream_chunk_has_visible_output(chunk)
                    or _stream_chunk_is_public_reasoning_delta(chunk, request_data)
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
    if _routing_module._is_failed_responses_stream_response(response):
        async for chunk in response:
            yield chunk
        return
    if _responses_request_module._request_has_structured_codex_compaction(
        request_data
    ):
        try:
            async for chunk in _yield_validated_structured_codex_compaction_stream(
                [],
                response,
                request_data,
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
    empty_completion_ended_stream = False
    stream_started_at = time.monotonic()
    completion_state = _ResponsesStreamCompletionState(request_data)
    raw_tool_call_text_filter = _web_search_native_module._RawToolCallTextFilter()

    # The turn is opened here, not by the upstream: see
    # ``_response_opening_events`` for the measurement behind it.
    if is_responses_stream and not request_data.get(_ANNOUNCED_FRAMES_SENT_KEY):
        request_data[_ANNOUNCED_FRAMES_SENT_KEY] = True
        for opening_event in _response_opening_events(request_data, completion_state):
            yield opening_event

    try:
        stream = _stream_with_idle_timeout(
            response,
            request_data,
        )
        if _should_emit_codex_responses_initial_sse_keepalive(request_data):
            stream = _yield_codex_responses_initial_keepalive_stream(
                response,
                request_data,
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
                for delivered_frame in _delivered_frames(
                    [*buffer, chunk],
                    request_data,
                ):
                    yield delivered_frame
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
                _stream_chunk_is_public_reasoning_delta(chunk, request_data)
            )
            if is_responses_stream:
                if _responses_stream_chunk_is_completed(chunk):
                    if _responses_completed_chunk_is_empty(chunk, request_data) and not saw_visible_output:
                        empty_completion_ended_stream = True
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
                        for delivered_frame in _delivered_frames(
                            buffer[:-1],
                            request_data,
                        ):
                            yield delivered_frame
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
                        delivered_output=bool(saw_visible_output or saw_delivered_output),
                    )
                    _mark_generated_output_item_cap(
                        incomplete_exception,
                        chunk,
                        delivered=bool(saw_visible_output or saw_delivered_output),
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
            for delivered_frame in _delivered_frames(buffer, request_data):
                yield delivered_frame
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
            for delivered_frame in _delivered_frames(buffer, request_data):
                yield delivered_frame
            yield completed_compat
            return
        incomplete_exception = _responses_incomplete_stream_exception(
            (
                "the completion carried no answer"
                if empty_completion_ended_stream
                else "stream ended before response.completed"
            ),
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
        # A Responses summary is a committed item in the client's transcript, so
        # showing one ends the replay window there.  On the native chat shape a
        # thinking-only turn must still reach a peer route -- a replay is the
        # lesser cost there, and the recovery branch reads this flag to decide
        # exactly that.
        saw_visible_output=(
            saw_visible_output
            or (saw_delivered_output and _request_is_responses_stream(request_data))
        ),
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
    _native_stream_chunk_is_terminal,
    _native_stream_incomplete_exception,
    _next_configured_order,
    _reset_route_exhaustion_retry_state,
    _route_recovery_cooldown_wait_delay,
    _route_recovery_exhausted_order,
    _route_recovery_next_poll_order,
    _route_recovery_poll_cooldown_wait,
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
