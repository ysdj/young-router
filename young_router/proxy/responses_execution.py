from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_output as _responses_output_module
from . import image_inputs as _image_inputs_module
from . import responses_request as _responses_request_module
from . import request_context as _request_context_module
from . import responses_surfaces as _responses_surfaces_module
from . import responses_tools as _responses_tools_module
from . import web_search_native as _web_search_native_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    Optional,
    _GENERIC_HELPER_PATCH_ATTR,
    _HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
    _PROTOCOL_FALLBACK_CACHE_HIT_KEY,
    _PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY,
    _PROTOCOL_FALLBACK_FROM_SURFACE_KEY,
    _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
    _RESTORED_REASONING_REPLAY_KEY,
    _RouteOrder,
    _UPSTREAM_URL_SURFACE_OPENAI_CHAT,
    _UPSTREAM_URL_SURFACE_ANTHROPIC,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY,
    _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY,
    asyncio,
    inspect,
    time,
    _normalize_response_completed_event_usage,
)


def _chat_stream_object_get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


def _chat_stream_first_choice(value: Any) -> Any:
    choices = _chat_stream_object_get(value, "choices")
    if isinstance(choices, list) and choices:
        return choices[0]
    return None


def _chat_stream_delta_text(chunk: Any) -> str:
    choice = _chat_stream_first_choice(chunk)
    delta = _chat_stream_object_get(choice, "delta")
    content = _chat_stream_object_get(delta, "content")
    return content if isinstance(content, str) else ""


def _chat_stream_delta_reasoning(chunk: Any) -> str:
    """The thinking one Chat delta carries, whichever field names it."""

    choice = _chat_stream_first_choice(chunk)
    delta = _chat_stream_object_get(choice, "delta")
    reasoning = _chat_stream_object_get(delta, "reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = _chat_stream_object_get(delta, "reasoning")
    return reasoning if isinstance(reasoning, str) else ""


def _chat_message_reasoning(response: Any) -> str:
    """The same thinking, from a Chat answer that did not stream."""

    choice = _chat_stream_first_choice(response)
    message = _chat_stream_object_get(choice, "message")
    reasoning = _chat_stream_object_get(message, "reasoning_content")
    if not isinstance(reasoning, str):
        reasoning = _chat_stream_object_get(message, "reasoning")
    return reasoning if isinstance(reasoning, str) else ""


def _chat_bridge_reasoning_effort(bridge_kwargs: dict) -> Optional[str]:
    """The client's reasoning level as the Chat field an upstream reads.

    A Responses request states its level inside ``reasoning`` (a Chat surface
    reads ``reasoning_effort``), and an OpenAI-chat gateway ignores the object
    whole: forwarding it verbatim left every route served by the text bridge
    answering without thinking at all.  Only the level travels — the summary
    the object also carries is a visibility request this bridge answers
    itself.
    """

    reasoning = bridge_kwargs.get("reasoning")
    effort = reasoning.get("effort") if isinstance(reasoning, dict) else reasoning
    if not isinstance(effort, str) or not effort.strip():
        return None
    return effort.strip()


def _chat_completion_message_text(response: Any) -> str:
    choice = _chat_stream_first_choice(response)
    message = _chat_stream_object_get(choice, "message")
    content = _chat_stream_object_get(message, "content")
    return content if isinstance(content, str) else ""


def _chat_stream_usage(chunk: Any) -> Any:
    return _chat_stream_object_get(chunk, "usage")


def _forced_tool_choice_auto_retry_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[dict]:
    """Build one same-surface retry after an explicit forced-choice rejection."""

    if not _routing_module._is_forced_tool_choice_auto_retry_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    if not isinstance(request_kwargs, dict):
        return None
    metadata = _request_context_module._request_metadata_dict(
        request_kwargs,
        "litellm_metadata",
    ) or {}
    if metadata.get("responses_forced_tool_choice_auto_retry") is True:
        return None
    _routing_module._mark_protocol_fallback_relax_tool_choice(request_kwargs)
    _routing_module._mark_protocol_fallback_relax_tool_choice(
        outer_request_kwargs
    )
    retry_kwargs = request_kwargs.copy()
    retry_kwargs["tool_choice"] = "auto"
    for container_key in ("extra_body", "litellm_params"):
        container = request_kwargs.get(container_key)
        if not isinstance(container, dict):
            continue
        retry_container = container.copy()
        # The request normalizers may mirror a forced choice into one of
        # LiteLLM's nested parameter containers.  Leaving that stale mirror
        # in place can make the upstream still see a named choice even after
        # the top-level retry is explicitly ``auto``.
        retry_container.pop("tool_choice", None)
        retry_container.pop("function_call", None)
        retry_kwargs[container_key] = retry_container
    retry_metadata = metadata.copy()
    retry_metadata["responses_forced_tool_choice_auto_retry"] = True
    retry_metadata["responses_forced_tool_choice_auto_retry_reason"] = (
        "upstream_rejected_forced_choice"
    )
    retry_kwargs["litellm_metadata"] = retry_metadata
    return retry_kwargs


def _function_tool_schema_compat_retry_kwargs(
    exception: Exception,
    request_kwargs: Optional[dict],
) -> Optional[dict]:
    """Build one same-surface retry for the observed strict=false shape."""

    if _routing_module._exception_status_code(exception) not in {400, 422}:
        return None
    text = _routing_module._exception_text(exception)
    if not any(
        marker in text
        for marker in (
            "请求参数组合无效",
            "参数组合无效",
            "invalid parameter combination",
            "invalid parameters combination",
            "invalid combination of parameters",
            "invalid schema",
            "schema validation",
            "schema is invalid",
        )
    ):
        return None
    return _responses_tools_module._responses_function_tool_schema_compat_retry_kwargs(
        request_kwargs
    )


def _chat_bridge_stream_payload(
    bridge_kwargs: dict,
) -> Optional[dict[str, Any]]:
    if bridge_kwargs.get("stream") is not True:
        return None
    if not _web_search_native_module._external_web_search_chat_only_route(
        bridge_kwargs,
    ):
        return None
    tools = bridge_kwargs.get("tools")
    if isinstance(tools, list) and tools:
        return None
    if bridge_kwargs.get("web_search_options") is not None:
        return None
    tool_choice = bridge_kwargs.get("tool_choice")
    if tool_choice not in (None, "auto", "none"):
        return None

    model_group = (
        _request_selected_deployment_model_group(bridge_kwargs)
        or _request_metadata_model_group(bridge_kwargs)
        or _request_model_group(bridge_kwargs)
    )
    if not isinstance(model_group, str) or not model_group.strip():
        return None

    payload: dict[str, Any] = {
        "model": model_group,
        "messages": _web_search_native_module._external_web_search_chat_tool_messages(
            bridge_kwargs,
        ),
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    max_completion_tokens = _request_context_module._positive_int_value(
        bridge_kwargs.get("max_completion_tokens")
    )
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens
    else:
        max_output_tokens = _request_context_module._positive_int_value(
            bridge_kwargs.get("max_output_tokens")
        )
        if max_output_tokens is not None:
            payload["max_completion_tokens"] = max_output_tokens

    for key in (
        "temperature",
        "top_p",
        "user",
        "service_tier",
        "seed",
        "stop",
        "response_format",
        "metadata",
        "litellm_metadata",
        "api_base",
        "api_key",
        "api_version",
        "custom_llm_provider",
        "extra_body",
        "extra_headers",
        "_target_order",
        "_excluded_deployment_ids",
    ):
        value = bridge_kwargs.get(key)
        if value is not None:
            payload[key] = value
    reasoning_effort = _chat_bridge_reasoning_effort(bridge_kwargs)
    if reasoning_effort is not None:
        payload["reasoning_effort"] = reasoning_effort
    return payload


async def _responses_chat_bridge_text_stream_from_chat_stream(
    chat_stream: Any,
    bridge_kwargs: dict,
) -> Any:
    """Relay a Chat stream as a Responses stream, thinking included.

    A thinking route streams ``reasoning_content`` before its answer, and a
    Responses client renders that as one reasoning item's summary part.  The
    item is opened on the first reasoning frame and closed before the answer's
    own message item, so the two arrive in the order their ``output_index``
    states; buffering the thinking into one item emitted after the message left
    a client's live thinking block empty for the whole turn, and put its item
    after the answer it preceded.
    """
    response_id = f"resp_chat_bridge_{time.time_ns()}"
    reasoning_id = f"rs_chat_bridge_{time.time_ns()}"
    message_id = f"msg_chat_bridge_{time.time_ns()}"
    model = (
        _request_selected_deployment_model_group(bridge_kwargs)
        or _request_metadata_model_group(bridge_kwargs)
        or _request_model_group(bridge_kwargs)
        or "unknown"
    )
    deliver_reasoning = (
        _web_search_native_module._public_reasoning_is_allowed(
            bridge_kwargs
        )
    )
    text_parts: list[str] = []
    reasoning_parts: list[str] = []
    usage = None
    reasoning_open = False
    message_index: Optional[int] = None

    def reasoning_added_events() -> list[dict[str, Any]]:
        return [
            {
                "type": "response.output_item.added",
                "output_index": 0,
                "item": {
                    "id": reasoning_id,
                    "type": "reasoning",
                    "status": "in_progress",
                    "summary": [],
                },
            },
            {
                "type": "response.reasoning_summary_part.added",
                "item_id": reasoning_id,
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": ""},
            },
        ]

    def reasoning_done_events() -> list[dict[str, Any]]:
        reasoning = "".join(reasoning_parts)
        return [
            {
                "type": "response.reasoning_summary_text.done",
                "item_id": reasoning_id,
                "output_index": 0,
                "summary_index": 0,
                "text": reasoning,
            },
            {
                "type": "response.reasoning_summary_part.done",
                "item_id": reasoning_id,
                "output_index": 0,
                "summary_index": 0,
                "part": {"type": "summary_text", "text": reasoning},
            },
            {
                "type": "response.output_item.done",
                "output_index": 0,
                "item": {
                    "id": reasoning_id,
                    "type": "reasoning",
                    "status": "completed",
                    "summary": [{"type": "summary_text", "text": reasoning}],
                },
            },
        ]

    def message_added_events(index: int) -> list[dict[str, Any]]:
        return [
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": {
                    "id": message_id,
                    "type": "message",
                    "status": "in_progress",
                    "role": "assistant",
                    "content": [],
                },
            },
            {
                "type": "response.content_part.added",
                "item_id": message_id,
                "output_index": index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            },
        ]

    yield {
        "type": "response.created",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "in_progress",
            "model": model,
            "output": [],
        },
    }

    async for chunk in chat_stream:
        chunk_usage = _chat_stream_usage(chunk)
        if chunk_usage is not None:
            usage = chunk_usage
        reasoning_delta = _chat_stream_delta_reasoning(chunk)
        delta = _chat_stream_delta_text(chunk)
        # Reasoning frames only ever precede the answer, and the item order the
        # client reads is fixed once the message opens: a route that resumed
        # thinking after its first answer frame has no place to put it.
        if reasoning_delta and deliver_reasoning and message_index is None:
            if not reasoning_open:
                reasoning_open = True
                for event in reasoning_added_events():
                    yield event
            reasoning_parts.append(reasoning_delta)
            yield {
                "type": "response.reasoning_summary_text.delta",
                "item_id": reasoning_id,
                "output_index": 0,
                "summary_index": 0,
                "delta": reasoning_delta,
            }
        if not delta:
            continue
        if message_index is None:
            if reasoning_open:
                for event in reasoning_done_events():
                    yield event
                reasoning_open = False
            message_index = 1 if reasoning_parts else 0
            for event in message_added_events(message_index):
                yield event
        text_parts.append(delta)
        yield {
            "type": "response.output_text.delta",
            "item_id": message_id,
            "output_index": message_index,
            "content_index": 0,
            "delta": delta,
        }

    if reasoning_open:
        for event in reasoning_done_events():
            yield event
        reasoning_open = False
    if message_index is None:
        message_index = 1 if reasoning_parts else 0
        for event in message_added_events(message_index):
            yield event
    text = "".join(text_parts)
    message = {
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
    yield {
        "type": "response.output_text.done",
        "item_id": message_id,
        "output_index": message_index,
        "content_index": 0,
        "text": text,
    }
    yield {
        "type": "response.content_part.done",
        "item_id": message_id,
        "output_index": message_index,
        "content_index": 0,
        "part": {"type": "output_text", "text": text, "annotations": []},
    }
    yield {
        "type": "response.output_item.done",
        "output_index": message_index,
        "item": message,
    }
    output: list[dict[str, Any]] = []
    if reasoning_parts:
        reasoning = "".join(reasoning_parts)
        output.append(
            {
                "id": reasoning_id,
                "type": "reasoning",
                "status": "completed",
                "summary": [{"type": "summary_text", "text": reasoning}],
            }
        )
    output.append(message)
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output_text": text,
            "output": output,
        },
    }
    if usage is not None:
        completed["response"]["usage"] = usage
    yield _normalize_response_completed_event_usage(completed)


def _chat_bridge_reasoning_included(bridge_kwargs: dict) -> bool:
    bridge_input = bridge_kwargs.get("input")
    return _responses_request_module._request_has_codex_client_evidence(
        bridge_kwargs
    ) and isinstance(bridge_input, list) and any(
        isinstance(item, dict)
        and item.get("type") == "reasoning"
        and isinstance(item.get("encrypted_content"), str)
        and bool(item["encrypted_content"].strip())
        for item in bridge_input
    )


REASONING_INCLUDED_HEADER = "x-reasoning-included"
_RESTORED_REASONING_ATTR = "_young_router_restored_reasoning"


class _RestoredReasoningStream:
    """A Responses stream whose usage already covered replayed reasoning.

    Codex decides whether to re-estimate earlier encrypted reasoning from the
    ``x-reasoning-included`` *HTTP response header*: its SSE client reads the
    header once, before the first event, and nothing inside the event body can
    replace it.  The adapted stream is created here, far below LiteLLM's
    request processing, so the fact travels on the object LiteLLM hands to its
    response-header hook (see
    ``YoungRouterHook.async_post_call_response_headers_hook``).

    The adapted Chat request replays the client's context as ordinary
    messages, so the upstream reports every restored token in
    ``usage.input_tokens``.  Codex must therefore not add its own estimate of
    the encrypted reasoning items on top, which is what made a 185k-token
    context look like 400k and triggered a needless pre-turn compaction.
    """

    def __init__(self, response: Any) -> None:
        self._response = response
        self._iterator = response.__aiter__()
        setattr(self, _RESTORED_REASONING_ATTR, True)
        # This wrapper sits outside the selected-deployment marker stream, and
        # the hooks that handle a failed initial stream read that marker off
        # the response object.  Without it the route that failed is never
        # excluded and the retry picks the same route again.
        marker = getattr(response, "_young_router_selected_deployment_marker", None)
        if marker is not None:
            self._young_router_selected_deployment_marker = marker

    def __aiter__(self) -> Any:
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


def _restored_reasoning_included(response: Any) -> bool:
    """Whether this response object says it covered replayed reasoning."""

    return getattr(response, _RESTORED_REASONING_ATTR, False) is True


def _restored_reasoning_replayed(request_kwargs: dict) -> bool:
    """Whether this answer replays a Codex history that carries reasoning.

    Every route this router serves receives that history — as encrypted
    reasoning items on a Responses surface, or as the restored messages a
    bridge writes for a Chat one — and reports the usage it actually billed.
    Codex's own estimate of those items is therefore a second count of the same
    context, which is what turned a 185k-token conversation into a 400k one and
    started a pre-turn compaction nobody needed.
    """

    if request_kwargs.get(_RESTORED_REASONING_REPLAY_KEY) is True:
        return True
    # The client's own request is the only place the reasoning items can still
    # be seen once a bridge or a surface adapter has rewritten its input.
    return _routing_module._request_replays_restored_reasoning(
        request_kwargs
    ) or _chat_bridge_reasoning_included(request_kwargs)


def _with_restored_reasoning_marker(response: Any, request_kwargs: dict) -> Any:
    """Tag an answer that replayed the client's encrypted reasoning."""

    already = _restored_reasoning_included(response)
    replayed = _restored_reasoning_replayed(request_kwargs)
    stream = callable(getattr(response, "__aiter__", None))
    marked = already or (replayed and stream)
    _trace_module._route_trace(
        "responses_restored_reasoning_marker",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_request_model_group(request_kwargs),
        replayed=replayed,
        codex_evidence=_responses_request_module._request_has_codex_client_evidence(
            request_kwargs
        ),
        surface=_responses_request_module._codex_request_responses_surface(
            request_kwargs
        ),
        flagged=request_kwargs.get(_RESTORED_REASONING_REPLAY_KEY) is True,
        request_evidence=_routing_module._request_replays_restored_reasoning(
            request_kwargs
        ),
        adapted_surface=_routing_module._request_adapted_surface(request_kwargs),
        stream=stream,
        already=already,
        marked=marked,
        response_type=type(response).__name__,
    )
    if already or not marked:
        return response
    return _RestoredReasoningStream(response)


def responses_chat_bridge_stream(
    bridge_kwargs: dict,
    upstream_response: Any,
) -> Any:
    return _with_restored_reasoning_marker(
        _responses_chat_bridge_text_stream_from_chat_stream(
            upstream_response,
            bridge_kwargs,
        ),
        bridge_kwargs,
    )


async def _responses_chat_bridge_direct_stream_response(
    bridge_kwargs: dict,
) -> Optional[Any]:
    # The adapter still emits a Responses stream. Codex compacts from that same
    # history, so its header must describe the replay the adapter really sends
    # to the upstream, rather than the legacy Chat-shaped request it replaces.
    payload = _chat_bridge_stream_payload(bridge_kwargs)
    if payload is None:
        return None
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return None
    acompletion = getattr(llm_router, "acompletion", None)
    if not callable(acompletion):
        return None
    _trace_module._request_route_trace(
        "responses_chat_bridge_direct_stream_start", bridge_kwargs, include_route_identity=True,
        retry_request=_trace_module._trace_request_summary(
            payload,
            method_name="acompletion",
        ),
    )
    start_timeout_seconds = _routing_module._stream_start_timeout_seconds_for_request(
        bridge_kwargs
    )
    if start_timeout_seconds > 0:
        try:
            chat_response = await asyncio.wait_for(
                acompletion(**payload),
                timeout=start_timeout_seconds,
            )
        except asyncio.TimeoutError as exc:
            raise _streaming_module._stream_start_timeout_exception(
                bridge_kwargs,
                start_seconds=start_timeout_seconds,
                saw_chunk=False,
                buffered_chunks=0,
            ) from exc
    else:
        chat_response = await acompletion(**payload)
    if _responses_output_module._response_is_async_iterable(chat_response):
        return responses_chat_bridge_stream(bridge_kwargs, chat_response)

    text = _chat_completion_message_text(chat_response)
    reasoning = _chat_message_reasoning(chat_response)
    if not text and not reasoning:
        return None

    return responses_chat_bridge_stream(
        bridge_kwargs,
        _single_chat_completion_chunk_stream(chat_response),
    )


async def _single_chat_completion_chunk_stream(response: Any) -> Any:
    text = _chat_completion_message_text(response)
    delta: dict[str, Any] = {"content": text}
    reasoning = _chat_message_reasoning(response)
    if reasoning:
        delta["reasoning_content"] = reasoning
    yield {
        "choices": [
            {
                "delta": delta,
            }
        ],
        "usage": _chat_stream_usage(response),
    }


def _inherit_protocol_fallback_state_for_bridge(
    bridge_kwargs: dict,
    *request_sources: Optional[dict],
) -> dict:
    """Keep fallback state across LiteLLM's bridge kwargs copy boundary."""
    metadata = _request_context_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    updated_metadata = metadata.copy()
    changed = False
    for key in (
        _PROTOCOL_FALLBACK_FROM_SURFACE_KEY,
        _PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY,
        _PROTOCOL_FALLBACK_CACHE_HIT_KEY,
        _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
    ):
        if key in bridge_kwargs:
            continue
        value = metadata.get(key)
        if value is None:
            for source in request_sources:
                if not isinstance(source, dict):
                    continue
                value = source.get(key)
                if value is None:
                    source_metadata = _request_context_module._request_metadata_dict(
                        source,
                        "litellm_metadata",
                    ) or {}
                    value = source_metadata.get(key)
                if value is not None:
                    break
        if value is not None:
            bridge_kwargs[key] = value
            updated_metadata[key] = value
            changed = True
    if changed:
        bridge_kwargs["litellm_metadata"] = updated_metadata
    return bridge_kwargs


async def _execute_responses_chat_bridge_call(
    original_function: Any,
    bridge_kwargs: dict,
    *,
    original_request_kwargs: Optional[dict] = None,
    outer_request_kwargs: Optional[dict] = None,
    original_exception: Optional[Exception] = None,
    start_event: str,
    error_event: str,
) -> Any:
    bridge_metadata = _request_context_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    trace_request = original_request_kwargs or bridge_kwargs
    trace_request_summary = _trace_module._trace_request_summary(trace_request)
    bridge_request_summary = _trace_module._trace_request_summary(bridge_kwargs)
    trace_payload = {
        "request_id": _routing_module._trace_request_id(trace_request)
        or _routing_module._trace_request_id(outer_request_kwargs),
        "session": _routing_module._trace_session_context(trace_request or outer_request_kwargs),
        "model_group": _request_model_group(trace_request)
        or _request_model_group(outer_request_kwargs),
        "deployment_id": _routing_module._deployment_id_from_request(trace_request),
        "route_key": _routing_module._deployment_route_key_from_request(trace_request),
        "request": trace_request_summary,
        "retry_request": bridge_request_summary,
        "retry_tool_types": _trace_module._trace_tool_types(bridge_kwargs.get("tools")),
        "retry_tool_names": _trace_module._trace_tool_names(bridge_kwargs.get("tools")),
        "retry_has_web_search_options": "web_search_options" in bridge_kwargs,
        "responses_chat_bridge_tool_sanitized": bridge_metadata.get(
            "responses_chat_bridge_tool_sanitized"
        ),
        "preemptive_reason": bridge_metadata.get(
            "responses_chat_bridge_preemptive_reason"
        ),
    }
    if original_exception is not None:
        trace_payload["exception"] = _routing_module._trace_exception(original_exception)
    _trace_module._route_trace(start_event, **trace_payload)

    async def execute_once(active_bridge_kwargs: dict) -> Any:
        active_bridge_metadata = _request_context_module._request_metadata_dict(
            active_bridge_kwargs,
            "litellm_metadata",
        ) or {}
        # A Chat-only route is served directly from the Chat stream: the
        # Responses call has no endpoint to reach on that deployment.
        direct_stream_response = await _responses_chat_bridge_direct_stream_response(
            active_bridge_kwargs,
        )
        if direct_stream_response is not None:
            return direct_stream_response
        response = original_function(**active_bridge_kwargs)
        if inspect.isawaitable(response):
            response = await response
        response = _image_inputs_module._sanitize_response_echoed_request_images_for_delivery(
            response,
            active_bridge_kwargs,
        )
        response = await _responses_surfaces_module._ensure_responses_chat_bridge_non_empty_response(
            response,
            active_bridge_kwargs,
            active_bridge_metadata,
            original_function,
        )
        return response

    async def execute_once_and_remember_fallback(
        active_bridge_kwargs: dict,
    ) -> Any:
        _inherit_protocol_fallback_state_for_bridge(
            active_bridge_kwargs,
            original_request_kwargs,
            outer_request_kwargs,
        )
        response = await execute_once(active_bridge_kwargs)
        # A streaming provider call is established before its iterator is
        # consumed. Record the working alternate protocol now so the next
        # request does not repeat the known-incompatible client surface. Use a
        # copy because the live request must retain its fallback state: a later
        # stream error needs that state to clear the optimistic cache entry.
        _routing_module._record_protocol_fallback_success(
            active_bridge_kwargs.copy()
        )
        # Every adapted answer leaves this function through here, so the
        # restored-reasoning fact is attached once instead of at each return.
        return _with_restored_reasoning_marker(
            response,
            active_bridge_kwargs,
        )

    try:
        return await execute_once_and_remember_fallback(bridge_kwargs)
    except Exception as bridge_exc:
        forced_choice_retry_kwargs = _forced_tool_choice_auto_retry_kwargs(
            bridge_exc,
            bridge_kwargs,
            outer_request_kwargs,
        )
        if forced_choice_retry_kwargs is not None:
            bridge_kwargs = forced_choice_retry_kwargs
            try:
                return await execute_once_and_remember_fallback(bridge_kwargs)
            except Exception as retry_exc:
                bridge_exc = retry_exc
        xhigh_retry_kwargs = _responses_request_module._xhigh_reasoning_compat_retry_kwargs(
            bridge_exc,
            bridge_kwargs,
        )
        if xhigh_retry_kwargs is not None:
            try:
                return await execute_once_and_remember_fallback(
                    xhigh_retry_kwargs
                )
            except Exception as retry_exc:
                _trace_module._request_route_trace(
                    "xhigh_reasoning_compat_retry_error", outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
                    request=trace_request_summary,
                    retry_request=_trace_module._trace_request_summary(xhigh_retry_kwargs),
                    original_exception=_routing_module._trace_exception(bridge_exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                bridge_exc = retry_exc
        _trace_module._request_route_trace(
            error_event, outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
            request=trace_request_summary,
            retry_request=bridge_request_summary,
            original_exception=(
                _routing_module._trace_exception(original_exception)
                if original_exception is not None
                else None
            ),
            exception=_routing_module._trace_exception(bridge_exc),
            preemptive_reason=bridge_metadata.get(
                "responses_chat_bridge_preemptive_reason"
            ),
        )
        raise bridge_exc


async def _postprocess_generic_bridge_response(
    response: Any,
    request_kwargs: dict,
    original_function: Any,
) -> Any:
    return _image_inputs_module._sanitize_response_echoed_request_images_for_delivery(
        response,
        request_kwargs,
    )


async def _execute_responses_context_truncation_fallback(
    original_function: Any,
    exception: Exception,
    request_kwargs: dict,
    *,
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[tuple[Any, dict]]:
    """Replay the selected native Responses call once with truncation=auto."""
    retry_kwargs = (
        _responses_request_module._responses_context_truncation_fallback_kwargs(
            exception,
            request_kwargs,
        )
    )
    if retry_kwargs is None:
        return None
    try:
        response = original_function(**retry_kwargs)
        response = await _responses_request_module._await_streaming_fallback_candidate_response(
            response,
            retry_kwargs,
            outer_request_kwargs,
        )
        response = await _postprocess_generic_bridge_response(
            response,
            retry_kwargs,
            original_function,
        )
    except Exception as retry_exception:
        _trace_module._request_route_trace(
            "responses_context_truncation_fallback_error", request_kwargs, include_route_identity=True,
            request=_trace_module._trace_request_summary(request_kwargs),
            retry_request=_trace_module._trace_request_summary(retry_kwargs),
            original_exception=_routing_module._trace_exception(exception),
            exception=_routing_module._trace_exception(retry_exception),
        )
        raise retry_exception
    return response, retry_kwargs


class _ResponsesContextTruncationStream:
    """Keep completion metadata and close the source through the retry wrapper."""

    def __init__(self, stream: Any, source: Any) -> None:
        self._stream = stream
        self._source = source
        self._closed = False
        self.completed_response = getattr(source, "completed_response", None)

    def __aiter__(self) -> "_ResponsesContextTruncationStream":
        return self

    def _remember_completion(self, chunk: Any) -> None:
        if (
            _streaming_module._responses_stream_chunk_is_completed(chunk)
            or _streaming_module._responses_stream_chunk_is_incomplete_terminal(chunk)
        ):
            # This chunk can come from a retry after the source stored an older
            # failure. The delivered terminal event is authoritative.
            dumped = _streaming_module._stream_chunk_dump(chunk)
            response = dumped.get("response")
            self.completed_response = response if response is not None else chunk
            return
        source_completed = getattr(self._source, "completed_response", None)
        if source_completed is None:
            return
        source_response = getattr(source_completed, "response", None)
        if source_response is None and isinstance(source_completed, dict):
            source_response = source_completed.get("response")
        self.completed_response = (
            source_response if source_response is not None else source_completed
        )

    async def __anext__(self) -> Any:
        if self._closed:
            raise StopAsyncIteration
        try:
            chunk = await self._stream.__anext__()
        except StopAsyncIteration:
            self._remember_completion(None)
            await self.aclose()
            raise
        except Exception:
            await self.aclose()
            raise
        self._remember_completion(chunk)
        if (
            _streaming_module._responses_stream_chunk_is_completed(chunk)
            or _streaming_module._responses_stream_chunk_is_incomplete_terminal(chunk)
        ):
            # The terminal event is the protocol end-of-stream marker. Do not
            # make the caller perform another read just to discover EOF.
            await self.aclose()
        return chunk

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        await _streaming_module._close_async_iterator_safely(self._stream)
        if self._source is not self._stream:
            await _streaming_module._close_async_iterator_safely(self._source)


def _with_responses_context_truncation_fallback_stream(
    response: Any,
    request_kwargs: dict,
    original_function: Any,
    *,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    """Hold pre-answer events so a context error can retry the same call."""
    if request_kwargs.get("stream") is not True:
        return response
    if not _responses_output_module._response_is_async_iterable(response):
        return response
    if not _responses_request_module._request_can_attempt_responses_context_truncation_fallback(
        request_kwargs
    ):
        return response

    async def guarded_stream() -> Any:
        buffered: list[Any] = []
        fallback_attempted = False
        delivery_started = False

        async def yield_retry(exception: Exception) -> Any:
            nonlocal fallback_attempted
            if fallback_attempted:
                raise exception
            fallback_attempted = True
            await _streaming_module._close_async_iterator_safely(response)
            fallback = await _execute_responses_context_truncation_fallback(
                original_function,
                exception,
                request_kwargs,
                outer_request_kwargs=outer_request_kwargs,
            )
            if fallback is None:
                raise exception
            fallback_response, _fallback_kwargs = fallback
            if _responses_output_module._response_is_async_iterable(
                fallback_response
            ):
                fallback_visible_output = False
                try:
                    async for fallback_chunk in fallback_response:
                        fallback_visible_output = (
                            fallback_visible_output
                            or _streaming_module._stream_chunk_has_visible_output(
                                fallback_chunk
                            )
                        )
                        if (
                            _streaming_module._responses_completed_chunk_is_empty(
                                fallback_chunk
                            )
                            and not fallback_visible_output
                        ):
                            raise _streaming_module._responses_incomplete_stream_exception(
                                "response.completed without usable output",
                                buffer=[fallback_chunk],
                                request_data=_fallback_kwargs,
                            )
                        yield fallback_chunk
                        if (
                            _streaming_module._responses_stream_chunk_is_completed(
                                fallback_chunk
                            )
                            or _streaming_module._responses_stream_chunk_is_incomplete_terminal(
                                fallback_chunk
                            )
                        ):
                            await _streaming_module._close_async_iterator_safely(
                                fallback_response
                            )
                            return
                finally:
                    await _streaming_module._close_async_iterator_safely(
                        fallback_response
                    )
            else:
                yield fallback_response

        try:
            async for chunk in response:
                chunk_exception = _streaming_module._stream_chunk_error_exception(
                    chunk
                )
                if (
                    chunk_exception is not None
                    and _routing_module._is_context_size_error(chunk_exception)
                ):
                    async for retry_chunk in yield_retry(chunk_exception):
                        yield retry_chunk
                    return

                buffered.append(chunk)
                if _streaming_module._stream_chunk_has_visible_output(chunk):
                    delivery_started = True
                if (
                    _streaming_module._responses_completed_chunk_is_empty(chunk)
                    and not any(
                        _streaming_module._stream_chunk_has_visible_output(
                            buffered_chunk
                        )
                        for buffered_chunk in buffered
                    )
                ):
                    raise _streaming_module._responses_incomplete_stream_exception(
                        "response.completed without usable output",
                        buffer=buffered,
                        request_data=request_kwargs,
                    )
                if _streaming_module._responses_stream_chunk_is_incomplete_terminal(
                    chunk
                ):
                    terminal_exception = (
                        _streaming_module._responses_incomplete_stream_exception(
                            "terminal response event before response.completed",
                            buffer=buffered,
                            request_data=request_kwargs,
                        )
                    )
                    if _routing_module._is_context_size_error(
                        terminal_exception
                    ):
                        async for retry_chunk in yield_retry(terminal_exception):
                            yield retry_chunk
                        return

                if (
                    _streaming_module._stream_chunk_has_visible_output(chunk)
                    or _streaming_module._responses_stream_chunk_is_completed(chunk)
                    or _streaming_module._responses_stream_chunk_is_incomplete_terminal(
                        chunk
                    )
                    or len(buffered) >= 20
                ):
                    delivery_started = True
                    terminal_in_buffer = any(
                        _streaming_module._responses_stream_chunk_is_completed(
                            buffered_chunk
                        )
                        or _streaming_module._responses_stream_chunk_is_incomplete_terminal(
                            buffered_chunk
                        )
                        for buffered_chunk in buffered
                    )
                    for buffered_chunk in buffered:
                        yield buffered_chunk
                    buffered.clear()
                    if terminal_in_buffer:
                        await _streaming_module._close_async_iterator_safely(response)
                        return
                    async for remaining_chunk in response:
                        yield remaining_chunk
                        if (
                            _streaming_module._responses_stream_chunk_is_completed(
                                remaining_chunk
                            )
                            or _streaming_module._responses_stream_chunk_is_incomplete_terminal(
                                remaining_chunk
                            )
                        ):
                            await _streaming_module._close_async_iterator_safely(
                                response
                            )
                            return
                    return
        except Exception as exception:
            if (
                not fallback_attempted
                and not delivery_started
                and _routing_module._is_context_size_error(exception)
            ):
                async for retry_chunk in yield_retry(exception):
                    yield retry_chunk
                return
            raise

        for buffered_chunk in buffered:
            yield buffered_chunk

    return _ResponsesContextTruncationStream(guarded_stream(), response)


def _responses_router_function() -> Optional[Any]:
    try:
        from litellm.proxy.proxy_server import llm_router
    except Exception:
        return None
    if llm_router is None:
        return None
    aresponses = getattr(llm_router, "aresponses", None)
    if not callable(aresponses):
        return None

    async def call_router(**kwargs: Any) -> Any:
        return await aresponses(**kwargs)

    return call_router


def _responses_bridge_original_function(request_data: dict) -> Optional[Any]:
    original_function = request_data.get("original_generic_function")
    if callable(original_function):
        return original_function
    return _responses_router_function()


async def _execute_bridged_upstream_attempt(
    original_function: Any,
    active_bridge_kwargs: dict,
    outer_request_kwargs: Any,
) -> Any:
    """Run one bridged upstream attempt and post-process the answer."""

    response = original_function(**active_bridge_kwargs)
    response = await _responses_request_module._await_streaming_fallback_candidate_response(
        response,
        active_bridge_kwargs,
        outer_request_kwargs,
    )
    return await _postprocess_generic_bridge_response(
        response,
        active_bridge_kwargs,
        original_function,
    )


async def _execute_responses_function_tool_bridge_call(
    original_function: Any,
    bridge_kwargs: dict,
    *,
    original_request_kwargs: Optional[dict] = None,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    bridge_metadata = _request_context_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    trace_request = original_request_kwargs or bridge_kwargs
    trace_request_summary = _trace_module._trace_request_summary(trace_request)
    bridge_request_summary = _trace_module._trace_request_summary(bridge_kwargs)
    _trace_module._request_route_trace(
        "responses_function_tool_bridge_start", outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
        request=trace_request_summary,
        retry_request=bridge_request_summary,
        retry_tool_types=_trace_module._trace_tool_types(bridge_kwargs.get("tools")),
        retry_tool_names=_trace_module._trace_tool_names(bridge_kwargs.get("tools")),
        retry_has_web_search_options="web_search_options" in bridge_kwargs,
        responses_function_tool_bridge_tool_sanitized=bridge_metadata.get(
            "responses_function_tool_bridge_tool_sanitized"
        ),
        preemptive_reason=bridge_metadata.get(
            "responses_function_tool_bridge_preemptive_reason"
        ),
        fallback_reason=bridge_metadata.get(
            _RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY
        ),
    )

    async def execute_once(active_bridge_kwargs: dict) -> Any:
        return await _execute_bridged_upstream_attempt(
            original_function,
            active_bridge_kwargs,
            outer_request_kwargs,
        )

    error_request_summary = bridge_request_summary
    try:
        return await execute_once(bridge_kwargs)
    except Exception as exc:
        original_exc = exc
        forced_choice_retry_kwargs = _forced_tool_choice_auto_retry_kwargs(
            exc,
            bridge_kwargs,
            outer_request_kwargs,
        )
        if forced_choice_retry_kwargs is not None:
            bridge_kwargs = forced_choice_retry_kwargs
            try:
                return await execute_once(bridge_kwargs)
            except Exception as retry_exc:
                exc = retry_exc
        schema_retry_kwargs = _function_tool_schema_compat_retry_kwargs(
            exc,
            bridge_kwargs,
        )
        if schema_retry_kwargs is not None:
            _trace_module._request_route_trace(
                "responses_function_tool_schema_compat_retry_start", outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
                tool_types=_trace_module._trace_tool_types(
                    schema_retry_kwargs.get("tools")
                ),
            )
            bridge_kwargs = schema_retry_kwargs
            try:
                return await execute_once(bridge_kwargs)
            except Exception as retry_exc:
                _trace_module._request_route_trace(
                    "responses_function_tool_schema_compat_retry_error", outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
                    exception=_routing_module._trace_exception(retry_exc),
                )
                exc = retry_exc
        xhigh_retry_kwargs = _responses_request_module._xhigh_reasoning_compat_retry_kwargs(exc, bridge_kwargs)
        if xhigh_retry_kwargs is not None:
            try:
                return await execute_once(xhigh_retry_kwargs)
            except Exception as retry_exc:
                error_request_summary = _trace_module._trace_request_summary(xhigh_retry_kwargs)
                _trace_module._request_route_trace(
                    "xhigh_reasoning_compat_retry_error", outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
                    request=trace_request_summary,
                    retry_request=error_request_summary,
                    original_exception=_routing_module._trace_exception(exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                exc = retry_exc
        native_error_bridge_kwargs = _responses_surfaces_module._with_responses_external_web_search_bridge_after_native_error(
            exc,
            bridge_kwargs,
            outer_request_kwargs,
        )
        if native_error_bridge_kwargs is not None:
            return await _execute_responses_chat_bridge_call(
                original_function,
                native_error_bridge_kwargs,
                original_request_kwargs=original_request_kwargs or bridge_kwargs,
                outer_request_kwargs=outer_request_kwargs,
                start_event="responses_chat_bridge_native_web_search_fallback_start",
                error_event="responses_chat_bridge_native_web_search_fallback_error",
            )
        _trace_module._request_route_trace(
            "responses_function_tool_bridge_error", outer_request_kwargs, trace_request=trace_request, include_route_identity=True,
            request=trace_request_summary,
            retry_request=error_request_summary,
            exception=_routing_module._trace_exception(exc),
            preemptive_reason=bridge_metadata.get(
                "responses_function_tool_bridge_preemptive_reason"
            ),
            fallback_reason=bridge_metadata.get(
                _RESPONSES_FUNCTION_TOOL_BRIDGE_FALLBACK_REASON_KEY
            ),
        )
        if exc is original_exc:
            raise
        raise exc

def _hosted_computer_unsupported_retry_response(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> Optional[Any]:
    """Answer a hosted computer-use request when the route refuses that tool.

    Young Router runs no computer-use executor of its own: a hosted ``computer``
    tool only exists when an upstream serves it natively, and every route here
    is a relay.  A refusal therefore ends the turn with an explicit unavailable
    message, because falling back to the chat bridge would drop the hosted tool
    and answer as if computer use had never been requested.
    """

    if not isinstance(request_kwargs, dict):
        return None
    if not (
        _responses_request_module._request_is_responses_api(request_kwargs)
        or _responses_request_module._request_is_responses_api(outer_request_kwargs)
    ):
        return None
    if not _responses_tools_module._native_hosted_computer_unsupported_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    response = _web_search_native_module._hosted_tool_unsupported_response(
        request_kwargs,
        _HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
    )
    if request_kwargs.get("stream") is True:
        return _web_search_native_module._hosted_tool_unsupported_stream(response)
    return response


def _hosted_computer_unavailable_preemptive_response(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> Optional[Any]:
    """Answer a hosted computer-use request before shipping it to a chat route.

    A route whose Responses endpoint does not exist can only be served by the
    chat bridge, and the bridge drops hosted tools.  When the client brings no
    computer use of its own, the request would either fail against the chat
    endpoint or silently lose the capability, so the honest answer is the
    explicit unavailability message.
    """

    if not _responses_request_module._request_is_responses_api(request_kwargs):
        return None
    if not _responses_surfaces_module._current_route_responses_endpoint_unsupported(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    if not _responses_tools_module._request_hosted_computer_blocks_chat_bridge(
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    response = _web_search_native_module._hosted_tool_unsupported_response(
        request_kwargs,
        _HOSTED_COMPUTER_UNSUPPORTED_MESSAGE,
    )
    if request_kwargs.get("stream") is True:
        return _web_search_native_module._hosted_tool_unsupported_stream(
            response
        )
    return response


def _wrap_generic_function_for_deployment_failover(
    original_function: Any,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    if getattr(original_function, _GENERIC_HELPER_PATCH_ATTR, False):
        return original_function

    async def wrapped_generic_function(**kwargs: Any) -> Any:
        # LiteLLM may rebuild the generic callback kwargs from the raw
        # deployment and omit the selected deployment's model_info/surface.
        # Restore it before capability-aware request adapters run, otherwise
        # an explicitly unsupported route is misclassified as unknown and a
        # native-capable route can receive the local search pair.
        _routing_module._apply_current_selected_deployment_to_request(kwargs)
        for update_request in (
            _responses_request_module._with_codex_openrouter_native_web_search_tool,
            *_responses_request_module._CODEX_REQUEST_INSTRUCTION_STEPS,
        ):
            updated_kwargs = update_request(kwargs)
            if updated_kwargs is not None:
                kwargs = updated_kwargs
        async def surface_adapted_original_function(**call_kwargs: Any) -> Any:
            dispatch_kwargs = _routing_module._surface_adapted_dispatch_kwargs(
                call_kwargs
            )
            response = original_function(**dispatch_kwargs)
            if inspect.isawaitable(response):
                return await response
            return response

        native_client_tool_kwargs = (
            _responses_surfaces_module._with_responses_native_client_tool_passthrough(
                kwargs,
                outer_request_kwargs,
            )
        )
        if native_client_tool_kwargs is not None:
            kwargs = native_client_tool_kwargs
        responses_function_bridge_kwargs = (
            _responses_surfaces_module._responses_function_tool_bridge_preemptive_kwargs(
                kwargs,
                outer_request_kwargs,
            )
        )
        if responses_function_bridge_kwargs is not None:
            try:
                return await _execute_responses_function_tool_bridge_call(
                    surface_adapted_original_function,
                    responses_function_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                )
            except Exception as exc:
                # Function-tool bridging is a pre-dispatch path for Codex
                # namespace/custom tools.  Preserve the same protocol
                # failover classification used by the ordinary upstream call
                # path; otherwise a Responses 400 raised here would bypass
                # the configured same-deployment Chat/Anthropic surface.
                _routing_module._apply_current_selected_deployment_to_request(
                    kwargs
                )
                decision_kwargs = (
                    outer_request_kwargs
                    if isinstance(outer_request_kwargs, dict)
                    else kwargs
                )
                if decision_kwargs is not kwargs:
                    _routing_module._apply_current_selected_deployment_to_request(
                        decision_kwargs
                    )
                if _routing_module._protocol_fallback_attempt_active(
                    decision_kwargs
                ):
                    _routing_module._mark_exception_for_deployment_failover(
                        exc,
                        decision_kwargs,
                    )
                    raise
                if _routing_module._is_current_upstream_surface_incompatible_error(
                    exc,
                    responses_function_bridge_kwargs,
                    decision_kwargs,
                ):
                    relax_tool_choice = (
                        _routing_module._is_forced_tool_choice_unsupported_error(
                            exc,
                            responses_function_bridge_kwargs,
                            decision_kwargs,
                        )
                    )
                    _routing_module._clear_protocol_fallback_cache_for_request(
                        decision_kwargs,
                        preserve_relaxed_tool_choice=True,
                    )
                    if relax_tool_choice:
                        _routing_module._mark_protocol_fallback_relax_tool_choice(
                            kwargs
                        )
                        _routing_module._mark_protocol_fallback_relax_tool_choice(
                            responses_function_bridge_kwargs
                        )
                        _routing_module._mark_protocol_fallback_relax_tool_choice(
                            decision_kwargs
                        )
                    _routing_module._mark_exception_for_upstream_surface_failover(
                        exc,
                        decision_kwargs,
                    )
                    raise
                if _routing_module._request_current_upstream_surface(kwargs):
                    raise
                bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_retry_kwargs(
                    exc,
                    kwargs,
                    outer_request_kwargs,
                )
                if bridge_kwargs is None:
                    bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_retry_kwargs(
                        exc,
                        responses_function_bridge_kwargs,
                        outer_request_kwargs,
                    )
                if bridge_kwargs is None:
                    raise
                return await _execute_responses_chat_bridge_call(
                    surface_adapted_original_function,
                    bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_function_tool_bridge_chat_retry_start",
                    error_event="responses_function_tool_bridge_chat_retry_error",
                )
        hosted_computer_unavailable = _hosted_computer_unavailable_preemptive_response(
            kwargs,
            outer_request_kwargs,
        )
        if hosted_computer_unavailable is not None:
            return _image_inputs_module._sanitize_response_echoed_request_images_for_delivery(
                hosted_computer_unavailable,
                kwargs,
            )
        preemptive_bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_preemptive_kwargs(
            kwargs,
            outer_request_kwargs,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
            allow_selected_marker=True,
        )
        if preemptive_bridge_kwargs is not None:
            try:
                return await _execute_responses_chat_bridge_call(
                    surface_adapted_original_function,
                    preemptive_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    start_event="responses_chat_bridge_preemptive_start",
                    error_event="responses_chat_bridge_preemptive_error",
                )
            except Exception as exc:
                if not _routing_module._is_responses_endpoint_not_found_error(
                    exc,
                    preemptive_bridge_kwargs,
                    outer_request_kwargs,
                ):
                    raise
                return await _execute_responses_chat_bridge_call(
                    surface_adapted_original_function,
                    preemptive_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_chat_bridge_preemptive_retry_start",
                    error_event="responses_chat_bridge_preemptive_retry_error",
                )
        try:
            response = surface_adapted_original_function(**kwargs)
            response = await _responses_request_module._await_streaming_fallback_candidate_response(
                response,
                kwargs,
                outer_request_kwargs,
            )
            response = await _postprocess_generic_bridge_response(
                response,
                kwargs,
                surface_adapted_original_function,
            )
            return _with_responses_context_truncation_fallback_stream(
                response,
                kwargs,
                surface_adapted_original_function,
                outer_request_kwargs=outer_request_kwargs,
            )
        except Exception as exc:
            original_exception = exc
            # The router has selected a deployment by this point, but LiteLLM
            # does not always preserve that selection on the generic callback
            # kwargs.  Restore it before classifying an upstream 400 so the
            # configured alternate protocol can be tried on the same route.
            _routing_module._apply_current_selected_deployment_to_request(kwargs)
            decision_kwargs = (
                outer_request_kwargs
                if isinstance(outer_request_kwargs, dict)
                else kwargs
            )
            if decision_kwargs is not kwargs:
                _routing_module._apply_current_selected_deployment_to_request(
                    decision_kwargs
                )
            forced_choice_retry_kwargs = _forced_tool_choice_auto_retry_kwargs(
                original_exception,
                kwargs,
                outer_request_kwargs,
            )
            if forced_choice_retry_kwargs is not None:
                try:
                    response = surface_adapted_original_function(
                        **forced_choice_retry_kwargs
                    )
                    response = await _responses_request_module._await_streaming_fallback_candidate_response(
                        response,
                        forced_choice_retry_kwargs,
                        outer_request_kwargs,
                    )
                    return await _postprocess_generic_bridge_response(
                        response,
                        forced_choice_retry_kwargs,
                        surface_adapted_original_function,
                    )
                except Exception as retry_exc:
                    exc = retry_exc
                    original_exception = retry_exc
                    # Keep the auto-choice form active for every remaining
                    # same-deployment compatibility attempt.  In particular,
                    # a namespace/custom -> function bridge must not restore
                    # the named choice that the upstream just rejected.
                    kwargs = forced_choice_retry_kwargs
            if _routing_module._is_context_size_error(original_exception):
                # Context-size errors are deterministic input errors until the
                # dedicated native truncation fallback chooses to repair them.
                # They must never enter an unrelated protocol bridge.
                context_truncation_fallback = (
                    await _execute_responses_context_truncation_fallback(
                        surface_adapted_original_function,
                        exc,
                        kwargs,
                        outer_request_kwargs=outer_request_kwargs,
                    )
                )
                if context_truncation_fallback is not None:
                    response, _retry_kwargs = context_truncation_fallback
                    return response
                raise original_exception
            context_truncation_fallback = (
                await _execute_responses_context_truncation_fallback(
                    surface_adapted_original_function,
                    exc,
                    kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                )
            )
            if context_truncation_fallback is not None:
                response, _retry_kwargs = context_truncation_fallback
                return response
            if _routing_module._is_context_size_error(original_exception):
                raise original_exception
            if _routing_module._protocol_fallback_attempt_active(decision_kwargs):
                _routing_module._mark_exception_for_deployment_failover(
                    exc,
                    decision_kwargs,
                )
                raise
            responses_function_bridge_retry_kwargs = (
                _responses_surfaces_module._responses_function_tool_bridge_retry_kwargs(
                    exc,
                    kwargs,
                    outer_request_kwargs,
                )
            )
            if responses_function_bridge_retry_kwargs is not None:
                try:
                    return await _execute_responses_function_tool_bridge_call(
                        surface_adapted_original_function,
                        responses_function_bridge_retry_kwargs,
                        original_request_kwargs=kwargs,
                        outer_request_kwargs=outer_request_kwargs,
                    )
                except Exception as bridge_exc:
                    # Tool representation is a same-protocol fallback.  Only
                    # after it also fails may this deployment advance to its
                    # configured alternate protocol.
                    if _routing_module._protocol_fallback_attempt_active(
                        decision_kwargs
                    ):
                        _routing_module._mark_exception_for_deployment_failover(
                            bridge_exc,
                            decision_kwargs,
                        )
                        raise
                    if _routing_module._is_current_upstream_surface_incompatible_error(
                        bridge_exc,
                        responses_function_bridge_retry_kwargs,
                        decision_kwargs,
                    ):
                        _routing_module._clear_protocol_fallback_cache_for_request(
                            decision_kwargs,
                            preserve_relaxed_tool_choice=True,
                        )
                        _routing_module._mark_exception_for_upstream_surface_failover(
                            bridge_exc,
                            decision_kwargs,
                        )
                    raise
            if _routing_module._is_current_upstream_surface_incompatible_error(
                exc,
                kwargs,
                decision_kwargs,
            ):
                _routing_module._clear_protocol_fallback_cache_for_request(
                    decision_kwargs,
                    preserve_relaxed_tool_choice=True,
                )
                _routing_module._mark_exception_for_upstream_surface_failover(
                    exc,
                    decision_kwargs,
                )
                raise
            hosted_computer_response = _hosted_computer_unsupported_retry_response(
                exc,
                kwargs,
                outer_request_kwargs,
            )
            if hosted_computer_response is not None:
                return _image_inputs_module._sanitize_response_echoed_request_images_for_delivery(
                    hosted_computer_response,
                    kwargs,
                )
            xhigh_retry_kwargs = _responses_request_module._xhigh_reasoning_compat_retry_kwargs(exc, kwargs)
            if xhigh_retry_kwargs is not None:
                try:
                    response = surface_adapted_original_function(**xhigh_retry_kwargs)
                    response = await _responses_request_module._await_streaming_fallback_candidate_response(
                        response,
                        xhigh_retry_kwargs,
                        outer_request_kwargs,
                    )
                    return await _postprocess_generic_bridge_response(
                        response,
                        xhigh_retry_kwargs,
                        surface_adapted_original_function,
                    )
                except Exception as retry_exc:
                    _trace_module._request_route_trace(
                        "xhigh_reasoning_compat_retry_error", kwargs, include_route_identity=True,
                        original_exception=_routing_module._trace_exception(exc),
                        exception=_routing_module._trace_exception(retry_exc),
                    )
                    exc = retry_exc
            bridge_kwargs = None
            if not _routing_module._request_current_upstream_surface(kwargs):
                bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_retry_kwargs(
                    exc, kwargs, outer_request_kwargs
                )
            if bridge_kwargs is not None:
                return await _execute_responses_chat_bridge_call(
                    surface_adapted_original_function,
                    bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_chat_bridge_retry_start",
                    error_event="responses_chat_bridge_retry_error",
                )
            if (
                _routing_module._is_request_scoped_priority_deployment_failover_error(
                    exc,
                    kwargs,
                )
                and not _routing_module._should_retry_with_browser_compatible_headers(exc, kwargs)
            ):
                # This wrapper has completed the selected deployment call. It
                # is not the explicit retry loop, so its error must advance on
                # the next router decision when the configured budget is zero.
                if _routing_module._same_deployment_retries() <= 0:
                    _routing_module._mark_same_deployment_retry_exhausted(exc)
                _routing_module._mark_exception_for_deployment_failover(exc, kwargs)
                if isinstance(outer_request_kwargs, dict):
                    _routing_module._sync_failed_deployment_exclusions(outer_request_kwargs, exc)
            raise

    setattr(wrapped_generic_function, _GENERIC_HELPER_PATCH_ATTR, True)
    setattr(wrapped_generic_function, "_wrapped_function", original_function)
    return wrapped_generic_function


def _with_generic_deployment_failover_wrapper(request_kwargs: Optional[dict]) -> None:
    if not isinstance(request_kwargs, dict):
        return
    original_function = request_kwargs.get("original_generic_function")
    if original_function is None:
        return
    request_kwargs["original_generic_function"] = _wrap_generic_function_for_deployment_failover(
        original_function,
        outer_request_kwargs=request_kwargs,
    )


def _restore_routing_constraints(
    request_kwargs: dict,
    *,
    target_order: Any,
    excluded_deployment_ids: Any,
) -> None:
    if target_order is not None and "_target_order" not in request_kwargs:
        request_kwargs["_target_order"] = target_order
    if excluded_deployment_ids is not None and "_excluded_deployment_ids" not in request_kwargs:
        request_kwargs["_excluded_deployment_ids"] = excluded_deployment_ids


def _failed_deployment_id(exception: Exception) -> Optional[str]:
    deployment_id = getattr(exception, "failed_deployment_id", None)
    return deployment_id if isinstance(deployment_id, str) and deployment_id.strip() else None


def _failed_deployment_route_key(exception: Exception) -> Optional[str]:
    route_key = getattr(exception, "failed_deployment_route_key", None)
    return route_key if isinstance(route_key, str) and route_key.strip() else None


def _failed_deployment_order(exception: Exception) -> Optional[_RouteOrder]:
    return _routing_module._coerce_order(getattr(exception, "failed_deployment_order", None))


def _failed_deployment_surface(exception: Exception) -> Optional[str]:
    surface = _routing_module._normalized_request_surface(
        getattr(exception, "failed_deployment_surface", None)
    )
    return surface or None


def _request_model_group(request_kwargs: Optional[dict]) -> Optional[str]:
    request_kwargs = request_kwargs or {}
    model = request_kwargs.get("model")
    if isinstance(model, str) and model.strip():
        return model
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if not metadata:
            continue
        model_group = metadata.get("model_group")
        if isinstance(model_group, str) and model_group.strip():
            return model_group
    return None


def _request_metadata_model_group(request_kwargs: Optional[dict]) -> Optional[str]:
    for key in ("litellm_metadata", "metadata"):
        metadata = _request_context_module._request_metadata_dict(request_kwargs, key)
        if not metadata:
            continue
        for model_key in (
            _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
            "original_model_group",
            "model_group",
        ):
            model_group = metadata.get(model_key)
            if isinstance(model_group, str) and model_group.strip():
                return model_group
    return None


def _request_selected_deployment_model_group(
    request_kwargs: Optional[dict],
) -> Optional[str]:
    model_info = _request_context_module._request_model_info(request_kwargs)
    for model_key in ("model_group", "model_name"):
        model_group = model_info.get(model_key)
        if isinstance(model_group, str) and model_group.strip():
            return model_group
    return None


def _request_kwargs_with_model_group(
    model_group: Optional[str],
    request_kwargs: dict,
) -> dict:
    if not isinstance(model_group, str) or not model_group.strip():
        return request_kwargs
    if isinstance(request_kwargs.get("model"), str) and request_kwargs["model"].strip():
        return request_kwargs
    updated = request_kwargs.copy()
    updated["model"] = model_group
    return updated


def _remember_responses_chat_bridge_model_group(
    metadata: dict,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> None:
    if metadata.get(_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY):
        return
    model_group = (
        _request_metadata_model_group(request_kwargs)
        or _request_model_group(request_kwargs)
        or _request_metadata_model_group(outer_request_kwargs)
        or _request_model_group(outer_request_kwargs)
    )
    if isinstance(model_group, str) and model_group.strip():
        metadata[_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY] = model_group


def _remember_request_model_group_before_deployment_update(
    request_kwargs: Optional[dict],
) -> None:
    if not isinstance(request_kwargs, dict):
        return
    model_group = _request_metadata_model_group(request_kwargs) or _request_model_group(
        request_kwargs
    )
    if not isinstance(model_group, str) or not model_group.strip():
        return
    litellm_metadata = _request_context_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    if litellm_metadata.get(_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY):
        return
    updated_metadata = litellm_metadata.copy()
    updated_metadata[_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY] = model_group
    request_kwargs["litellm_metadata"] = updated_metadata


def _external_web_search_router_model_group(request_kwargs: Optional[dict]) -> Optional[str]:
    model_info = _request_context_module._request_model_info(request_kwargs)
    model = model_info.get("model")
    if isinstance(model, str) and model.strip():
        return model

    litellm_params = (request_kwargs or {}).get("litellm_params")
    if isinstance(litellm_params, dict):
        model = litellm_params.get("model")
        if isinstance(model, str) and model.strip():
            return model

    request_model = _request_model_group(request_kwargs)
    if isinstance(request_model, str) and request_model.strip():
        return request_model

    metadata_model_group = _request_metadata_model_group(request_kwargs)
    if isinstance(metadata_model_group, str) and metadata_model_group.strip():
        return metadata_model_group

    route_key = _routing_module._deployment_route_key_from_request(request_kwargs)
    if isinstance(route_key, str) and route_key.strip():
        route_parts = [part.strip() for part in route_key.split(" / ")]
        for part in route_parts:
            if part.startswith("model=") and part.removeprefix("model=").strip():
                return part.removeprefix("model=").strip()
        if len(route_parts) >= 2 and route_parts[1] and "=" not in route_parts[1]:
            return route_parts[1]

    return None


def _request_selected_route_upstream_model(request_kwargs: Optional[dict]) -> Optional[str]:
    model_info = _request_context_module._request_model_info(request_kwargs)
    model = model_info.get("model")
    if isinstance(model, str) and model.strip():
        return model.strip()

    litellm_params = (request_kwargs or {}).get("litellm_params")
    if isinstance(litellm_params, dict):
        model = litellm_params.get("model")
        if isinstance(model, str) and model.strip():
            return model.strip()

    route_key = _routing_module._deployment_route_key_from_request(request_kwargs)
    if not isinstance(route_key, str) or not route_key.strip():
        return None
    route_parts = [part.strip() for part in route_key.split(" / ")]
    for part in route_parts:
        if part.startswith("upstream=") and part.removeprefix("upstream=").strip():
            return part.removeprefix("upstream=").strip()
    if len(route_parts) >= 2 and route_parts[1] and "=" not in route_parts[1]:
        return route_parts[1]
    return None


def _normalize_external_web_search_router_kwargs(
    call_kwargs: dict[str, Any],
    request_kwargs: Optional[dict],
) -> dict[str, Any]:
    model_group = (
        _request_selected_deployment_model_group(request_kwargs)
        or _request_metadata_model_group(request_kwargs)
        or _request_model_group(request_kwargs)
    )
    if not (isinstance(model_group, str) and model_group.strip()):
        model_group = _external_web_search_router_model_group(request_kwargs)
    if isinstance(model_group, str) and model_group.strip():
        call_kwargs["model"] = model_group
    return call_kwargs


def _with_failed_order_constraint(request_kwargs: dict, exception: Exception) -> None:
    failed_order = _failed_deployment_order(exception)
    if failed_order is not None and "_target_order" not in request_kwargs:
        request_kwargs["_target_order"] = failed_order
        _trace_module._request_route_trace(
            "fallback_target_order_constraint", request_kwargs,
            target_order=failed_order,
            request=_trace_module._trace_request_summary(request_kwargs),
            exception=_routing_module._trace_exception(exception),
        )


def _ordered_deployment_fallback_entry(
    router: Any,
    exception: Exception,
    request_kwargs: dict,
) -> Optional[dict]:
    if _routing_module._is_terminal_prompt_or_policy_error(exception):
        _trace_module._request_route_trace(
            "terminal_error_fallback_suppressed", request_kwargs,
            request=_trace_module._trace_request_summary(request_kwargs),
            exception=_routing_module._trace_exception(exception),
        )
        return None

    is_image_tool_runtime_probe = (
        _tools_module._request_has_image_generation_tool(request_kwargs)
        and (
            _routing_module._is_image_parameter_or_capability_bad_request_error(exception)
            or _routing_module._is_image_generation_tool_runtime_fallback_error(exception)
        )
    )
    if is_image_tool_runtime_probe:
        attempts = _image_generation_module._with_incremented_image_generation_tool_fallback_attempts(request_kwargs)
        _trace_module._request_route_trace(
            "image_generation_tool_runtime_fallback_next", request_kwargs,
            request=_trace_module._trace_request_summary(request_kwargs),
            exception=_routing_module._trace_exception(exception),
            attempts=attempts,
        )

    _routing_module._record_image_generation_tool_unsupported(
        exception,
        request_kwargs,
    )

    failed_id = _failed_deployment_id(exception)
    failed_route_key = _failed_deployment_route_key(exception)
    failed_order = _failed_deployment_order(exception)
    if failed_order is None and _routing_module._is_no_deployments_available_error(exception):
        failed_order = _responses_request_module._request_target_order(request_kwargs)
    model_group = _request_model_group(request_kwargs)
    if model_group is None:
        return None

    try:
        metadata = _request_context_module._request_metadata_dict(request_kwargs, "metadata") or {}
        team_id = metadata.get("user_api_key_team_id")
        all_deployments = _routing_module._router_configured_deployments(
            router,
            model_group,
            team_id=team_id,
        )
    except Exception:
        return None
    if _routing_module._image_generation_tool_all_deployments_unsupported(
        list(all_deployments or []),
        request_kwargs,
    ):
        _routing_module._mark_image_generation_all_deployments_unsupported(
            exception
        )
        _trace_module._request_route_trace(
            "image_generation_tool_all_deployments_unsupported", request_kwargs, model_group=model_group,
            request=_trace_module._trace_request_summary(request_kwargs),
            exception=_routing_module._trace_exception(exception),
            attempts=_image_generation_module._request_image_generation_tool_fallback_attempts(
                request_kwargs
            ),
        )
        return None
    if failed_order is None:
        return None

    excluded_ids = _responses_request_module._request_excluded_deployment_ids(request_kwargs)
    no_deployments_available = _routing_module._is_no_deployments_available_error(
        exception
    )
    surface_fallback = (
        _routing_module._next_upstream_surface_for_failed_deployment(
            router, exception, request_kwargs
        )
        if _routing_module._is_upstream_surface_failover_error(exception)
        else None
    )
    if surface_fallback is not None:
        next_surface, target_deployment_id = surface_fallback
        attempted_surfaces = _routing_module._request_attempted_upstream_surfaces(
            request_kwargs
        )
        current_surface = (
            _failed_deployment_surface(exception)
            or _routing_module._request_current_upstream_surface(request_kwargs)
        )
        if current_surface and current_surface not in attempted_surfaces:
            attempted_surfaces.append(current_surface)
        _routing_module._set_request_surface_state(
            request_kwargs,
            surface=next_surface,
            attempted_surfaces=attempted_surfaces,
            deployment_id=target_deployment_id,
            target_deployment_id=target_deployment_id,
        )
        request_kwargs["_target_order"] = failed_order
        excluded_ids.discard(target_deployment_id)
        if excluded_ids:
            request_kwargs["_excluded_deployment_ids"] = sorted(excluded_ids)
        else:
            request_kwargs.pop("_excluded_deployment_ids", None)
        entry = {
            "model": model_group,
            "_target_order": failed_order,
            "_young_router_upstream_url_surface": next_surface,
            "_young_router_attempted_upstream_url_surfaces": attempted_surfaces,
            "_young_router_surface_target_deployment_id": target_deployment_id,
            "_excluded_deployment_ids": sorted(excluded_ids),
        }
        # This entry crosses a **kwargs copy boundary before the retry. Carry
        # the request-scoped protocol state explicitly so a failure on the
        # alternate surface is recognized as the second half of one attempt.
        for key in (
            _PROTOCOL_FALLBACK_FROM_SURFACE_KEY,
            _PROTOCOL_FALLBACK_CLIENT_SURFACE_KEY,
            _PROTOCOL_FALLBACK_CACHE_HIT_KEY,
            _PROTOCOL_FALLBACK_RELAX_TOOL_CHOICE_KEY,
        ):
            if key in request_kwargs:
                entry[key] = request_kwargs[key]
        _trace_module._request_route_trace(
            "same_deployment_protocol_fallback_available", request_kwargs, model_group=model_group,
            failed_deployment_id=failed_id,
            failed_surface=current_surface,
            fallback_surface=next_surface,
            target_order=failed_order,
        )
        return entry

    _routing_module._clear_request_surface_target(request_kwargs)
    if (
        failed_id is None
        and no_deployments_available
        and excluded_ids
        and _routing_module._request_surface_deployment_id(request_kwargs)
        in excluded_ids
    ):
        failed_id = next(iter(sorted(excluded_ids)))
    if failed_id is not None:
        excluded_ids.add(failed_id)

    cooldown_candidates, cooldown_deployments, cooldown_filtered = (
        _routing_module._with_active_deployment_cooldowns(
            list(all_deployments or []),
            request_kwargs=request_kwargs,
        )
    )
    if cooldown_deployments:
        _trace_module._request_route_trace(
            "fallback_deployment_cooldown_filter", request_kwargs, model_group=model_group,
            cooldown_filtered=cooldown_filtered,
            cooldown_all_candidates=bool(cooldown_deployments and not cooldown_candidates),
            cooldown_deployments=cooldown_deployments,
        )

    (
        image_tool_candidates,
        image_tool_unsupported_deployments,
        image_tool_unsupported_filtered,
    ) = _routing_module._with_active_image_generation_tool_unsupported(
        cooldown_candidates,
        request_kwargs=request_kwargs,
    )
    if image_tool_unsupported_deployments:
        _trace_module._request_route_trace(
            "fallback_image_generation_tool_unsupported_filter", request_kwargs, model_group=model_group,
            image_tool_unsupported_filtered=image_tool_unsupported_filtered,
            image_tool_unsupported_all_candidates=bool(
                image_tool_unsupported_deployments and not image_tool_candidates
            ),
            image_tool_unsupported_deployments=image_tool_unsupported_deployments,
        )

    available_deployments = [
        deployment
        for deployment in image_tool_candidates
        if _responses_request_module._deployment_id(deployment) not in excluded_ids
    ]
    constrained_no_deployments = no_deployments_available and any(
        _responses_request_module._deployment_order(deployment) == failed_order
        and _responses_request_module._deployment_id(deployment) in excluded_ids
        for deployment in all_deployments
    )
    peer_deployments = (
        [
            deployment
            for deployment in available_deployments
            if _responses_request_module._deployment_order(deployment) == failed_order
        ]
        if failed_id is not None or constrained_no_deployments
        else []
    )
    if peer_deployments:
        peer_deployment_ids = [
            deployment_id
            for deployment_id in (
                _responses_request_module._deployment_id(deployment)
                for deployment in peer_deployments
            )
            if deployment_id
        ]
        _trace_module._request_route_trace(
            "same_order_peer_fallback_available", request_kwargs, model_group=model_group,
            failed_deployment_id=failed_id,
            failed_route_key=failed_route_key,
            failed_order=failed_order,
            excluded_deployment_ids=sorted(excluded_ids),
            request=_trace_module._trace_request_summary(request_kwargs),
            candidates=_routing_module._trace_deployments(peer_deployments),
        )
        return {
            "model": model_group,
            "_target_order": failed_order,
            "_excluded_deployment_ids": sorted(excluded_ids),
            _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY: peer_deployment_ids,
        }

    available_orders = sorted(
        {
            order
            for order in (_responses_request_module._deployment_order(deployment) for deployment in available_deployments)
            if order is not None
        }
    )
    next_order = None
    for order in available_orders:
        if order > failed_order:
            next_order = order
            break
    wrapped_order = False
    if next_order is None:
        for order in available_orders:
            if order < failed_order:
                next_order = order
                wrapped_order = True
                break

    if next_order is None:
        _trace_module._request_route_trace(
            "same_order_peer_fallback_unavailable", request_kwargs, model_group=model_group,
            failed_deployment_id=failed_id,
            failed_route_key=failed_route_key,
            failed_order=failed_order,
            excluded_deployment_ids=sorted(excluded_ids),
            request=_trace_module._trace_request_summary(request_kwargs),
        )
        return None

    next_deployments = [
        deployment
        for deployment in available_deployments
        if _responses_request_module._deployment_order(deployment) == next_order
    ]
    next_deployment_ids = [
        deployment_id
        for deployment_id in (
            _responses_request_module._deployment_id(deployment)
            for deployment in next_deployments
        )
        if deployment_id
    ]
    _trace_module._request_route_trace(
        "next_order_fallback_available", request_kwargs, model_group=model_group,
        failed_deployment_id=failed_id,
        failed_route_key=failed_route_key,
        failed_order=failed_order,
        target_order=next_order,
        wrapped_order=wrapped_order,
        excluded_deployment_ids=sorted(excluded_ids),
        request=_trace_module._trace_request_summary(request_kwargs),
        candidates=_routing_module._trace_deployments(next_deployments),
    )
    return {
        "model": model_group,
        "_target_order": next_order,
        "_excluded_deployment_ids": sorted(excluded_ids),
        _VERIFIED_FALLBACK_DEPLOYMENT_IDS_KEY: next_deployment_ids,
    }
