from __future__ import annotations

from . import computer_facade as _computer_facade_module
from . import image_generation as _image_generation_module
from . import responses_surfaces as _responses_surfaces_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    Optional,
    _GENERIC_HELPER_PATCH_ATTR,
    _HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY,
    _RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_KEY,
    _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY,
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


def _chat_completion_message_text(response: Any) -> str:
    choice = _chat_stream_first_choice(response)
    message = _chat_stream_object_get(choice, "message")
    content = _chat_stream_object_get(message, "content")
    return content if isinstance(content, str) else ""


def _chat_stream_usage(chunk: Any) -> Any:
    return _chat_stream_object_get(chunk, "usage")


def _chat_bridge_stream_payload(
    bridge_kwargs: dict,
) -> Optional[dict[str, Any]]:
    if bridge_kwargs.get("stream") is not True:
        return None
    if not _responses_web_search_bridge_module._external_web_search_chat_only_route(
        bridge_kwargs,
    ):
        return None
    if _tools_module._request_should_intercept_external_web_search(bridge_kwargs):
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
        "messages": _responses_web_search_bridge_module._external_web_search_chat_tool_messages(
            bridge_kwargs,
        ),
        "stream": True,
        "stream_options": {"include_usage": True},
    }

    max_completion_tokens = _image_generation_module._positive_int_value(
        bridge_kwargs.get("max_completion_tokens")
    )
    if max_completion_tokens is not None:
        payload["max_completion_tokens"] = max_completion_tokens
    else:
        max_output_tokens = _image_generation_module._positive_int_value(
            bridge_kwargs.get("max_output_tokens")
        )
        if max_output_tokens is not None:
            payload["max_completion_tokens"] = max_output_tokens

    for key in (
        "temperature",
        "top_p",
        "reasoning",
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
    return payload


async def _responses_chat_bridge_text_stream_from_chat_stream(
    chat_stream: Any,
    bridge_kwargs: dict,
) -> Any:
    response_id = f"resp_chat_bridge_{time.time_ns()}"
    message_id = f"msg_chat_bridge_{time.time_ns()}"
    model = (
        _request_selected_deployment_model_group(bridge_kwargs)
        or _request_metadata_model_group(bridge_kwargs)
        or _request_model_group(bridge_kwargs)
        or "unknown"
    )
    text_parts: list[str] = []
    usage = None

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
    yield {
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        },
    }
    yield {
        "type": "response.content_part.added",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    }

    async for chunk in chat_stream:
        chunk_usage = _chat_stream_usage(chunk)
        if chunk_usage is not None:
            usage = chunk_usage
        delta = _chat_stream_delta_text(chunk)
        if not delta:
            continue
        text_parts.append(delta)
        yield {
            "type": "response.output_text.delta",
            "item_id": message_id,
            "output_index": 0,
            "content_index": 0,
            "delta": delta,
        }

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
        "output_index": 0,
        "content_index": 0,
        "text": text,
    }
    yield {
        "type": "response.content_part.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": text, "annotations": []},
    }
    yield {
        "type": "response.output_item.done",
        "output_index": 0,
        "item": message,
    }
    completed = {
        "type": "response.completed",
        "response": {
            "id": response_id,
            "object": "response",
            "created_at": int(time.time()),
            "status": "completed",
            "model": model,
            "output_text": text,
            "output": [message],
        },
    }
    if usage is not None:
        completed["response"]["usage"] = usage
    yield _normalize_response_completed_event_usage(completed)


async def _responses_chat_bridge_direct_stream_response(
    bridge_kwargs: dict,
) -> Optional[Any]:
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
    _trace_module._route_trace(
        "responses_chat_bridge_direct_stream_start",
        request_id=_routing_module._trace_request_id(bridge_kwargs),
        session=_routing_module._trace_session_context(bridge_kwargs),
        model_group=_request_model_group(bridge_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(bridge_kwargs),
        route_key=_routing_module._deployment_route_key_from_request(bridge_kwargs),
        retry_request=_trace_module._trace_request_summary(
            payload,
            method_name="acompletion",
        ),
    )
    chat_response = await acompletion(**payload)
    if _image_generation_module._response_is_async_iterable(chat_response):
        return _responses_chat_bridge_text_stream_from_chat_stream(
            chat_response,
            bridge_kwargs,
        )

    text = _chat_completion_message_text(chat_response)
    if not text:
        return None

    async def single_response_stream() -> Any:
        async for chunk in _responses_chat_bridge_text_stream_from_chat_stream(
            _single_chat_completion_chunk_stream(chat_response),
            bridge_kwargs,
        ):
            yield chunk

    return single_response_stream()


async def _single_chat_completion_chunk_stream(response: Any) -> Any:
    text = _chat_completion_message_text(response)
    yield {
        "choices": [
            {
                "delta": {"content": text},
            }
        ],
        "usage": _chat_stream_usage(response),
    }


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
    bridge_metadata = _image_generation_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    if bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
        bridge_kwargs = _responses_web_search_bridge_module._external_web_search_low_reasoning_kwargs(
            bridge_kwargs
        )
        bridge_metadata = _image_generation_module._request_metadata_dict(
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
        "hosted_web_search_unsupported_bridge": bridge_metadata.get(
            _HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY
        ),
        "responses_chat_bridge_tool_sanitized": bridge_metadata.get(
            "responses_chat_bridge_tool_sanitized"
        ),
        "external_web_search_bridge": bridge_metadata.get(
            _WEB_SEARCH_EXTERNAL_BRIDGE_KEY
        ),
        "external_web_search_bridge_stream": bridge_metadata.get(
            _WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY
        ),
        "preemptive_reason": bridge_metadata.get(
            "responses_chat_bridge_preemptive_reason"
        ),
    }
    if original_exception is not None:
        trace_payload["exception"] = _routing_module._trace_exception(original_exception)
    _trace_module._route_trace(start_event, **trace_payload)

    unsupported_message = _computer_facade_module._hosted_tool_unsupported_message(bridge_metadata)
    if unsupported_message is not None:
        response = _computer_facade_module._hosted_tool_unsupported_response(
            bridge_kwargs,
            unsupported_message,
        )
        if bridge_kwargs.get("stream") is True:
            return _computer_facade_module._hosted_web_search_unsupported_stream(response)
        return response

    async def execute_once(active_bridge_kwargs: dict) -> Any:
        active_bridge_metadata = _image_generation_module._request_metadata_dict(
            active_bridge_kwargs,
            "litellm_metadata",
        ) or {}
        direct_stream_response = await _responses_chat_bridge_direct_stream_response(
            active_bridge_kwargs,
        )
        if direct_stream_response is not None:
            return direct_stream_response
        upstream_kwargs = (
            _tools_module._with_external_web_search_post_call_suppressed(active_bridge_kwargs)
            if active_bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True
            else active_bridge_kwargs
        )
        response = None
        if active_bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
            response = await _responses_web_search_bridge_module._external_web_search_chat_tool_response(
                upstream_kwargs,
                active_bridge_kwargs,
                phase="initial",
            )
        if response is None:
            response = original_function(**upstream_kwargs)
            if inspect.isawaitable(response):
                response = await response
        response = _image_generation_module._sanitize_response_echoed_request_images_for_delivery(
            response,
            active_bridge_kwargs,
        )
        should_intercept_external_web_search = _tools_module._request_should_intercept_external_web_search(
            active_bridge_kwargs,
        )
        if (
            should_intercept_external_web_search
            and active_bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_STREAM_KEY) is True
        ):
            if active_bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
                return _computer_facade_module._resolve_litellm_web_search_function_calls_stream_rounds(
                    response,
                    active_bridge_kwargs,
                    original_function,
                )
            response_payload = _streaming_module._jsonable(response)
            if not isinstance(response_payload, dict):
                response_payload = _computer_facade_module._hosted_tool_unsupported_response(
                    active_bridge_kwargs,
                    _image_generation_module._response_text(response),
                )
            return _computer_facade_module._external_web_search_bridge_stream(response_payload)
        if (
            should_intercept_external_web_search
            and active_bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True
        ):
            response = await _responses_web_search_bridge_module._resolve_litellm_web_search_function_calls(
                response,
                active_bridge_kwargs,
                original_function,
            )
        response = await _responses_surfaces_module._ensure_responses_chat_bridge_non_empty_response(
            response,
            active_bridge_kwargs,
            active_bridge_metadata,
            original_function,
        )
        return response

    try:
        return await execute_once(bridge_kwargs)
    except Exception as bridge_exc:
        xhigh_retry_kwargs = _image_generation_module._xhigh_reasoning_compat_retry_kwargs(
            bridge_exc,
            bridge_kwargs,
        )
        if xhigh_retry_kwargs is not None:
            try:
                return await execute_once(xhigh_retry_kwargs)
            except Exception as retry_exc:
                _trace_module._route_trace(
                    "xhigh_reasoning_compat_retry_error",
                    request_id=_routing_module._trace_request_id(trace_request)
                    or _routing_module._trace_request_id(outer_request_kwargs),
                    session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
                    model_group=_request_model_group(trace_request)
                    or _request_model_group(outer_request_kwargs),
                    deployment_id=_routing_module._deployment_id_from_request(trace_request),
                    route_key=_routing_module._deployment_route_key_from_request(trace_request),
                    request=trace_request_summary,
                    retry_request=_trace_module._trace_request_summary(xhigh_retry_kwargs),
                    original_exception=_routing_module._trace_exception(bridge_exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                bridge_exc = retry_exc
        _trace_module._route_trace(
            error_event,
            request_id=_routing_module._trace_request_id(trace_request)
            or _routing_module._trace_request_id(outer_request_kwargs),
            session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
            model_group=_request_model_group(trace_request)
            or _request_model_group(outer_request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(trace_request),
            route_key=_routing_module._deployment_route_key_from_request(trace_request),
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
    response = _image_generation_module._sanitize_response_echoed_request_images_for_delivery(
        response,
        request_kwargs,
    )
    if not _tools_module._request_should_intercept_external_web_search(request_kwargs):
        return response

    bridge_metadata = _image_generation_module._request_metadata_dict(
        request_kwargs,
        "litellm_metadata",
    ) or {}
    if request_kwargs.get("stream") is True:
        if _image_generation_module._response_is_async_iterable(response):
            return response
        if bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
            return _computer_facade_module._resolve_litellm_web_search_function_calls_stream_rounds(
                response,
                request_kwargs,
                original_function,
            )
        response_payload = _streaming_module._jsonable(response)
        if not isinstance(response_payload, dict):
            response_payload = _computer_facade_module._hosted_tool_unsupported_response(
                request_kwargs,
                _image_generation_module._response_text(response),
            )
        return _computer_facade_module._external_web_search_bridge_stream(response_payload)

    return await _responses_web_search_bridge_module._resolve_litellm_web_search_function_calls(
        response,
        request_kwargs,
        original_function,
    )


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


async def _execute_responses_external_web_search_bridge_call(
    original_function: Any,
    bridge_kwargs: dict,
    *,
    original_request_kwargs: Optional[dict] = None,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    bridge_kwargs = _responses_web_search_bridge_module._external_web_search_low_reasoning_kwargs(
        bridge_kwargs
    )
    bridge_metadata = _image_generation_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    trace_request = original_request_kwargs or bridge_kwargs
    trace_request_summary = _trace_module._trace_request_summary(trace_request)
    bridge_request_summary = _trace_module._trace_request_summary(bridge_kwargs)
    _trace_module._route_trace(
        "responses_external_web_search_bridge_start",
        request_id=_routing_module._trace_request_id(trace_request)
        or _routing_module._trace_request_id(outer_request_kwargs),
        session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
        model_group=_request_model_group(trace_request)
        or _request_model_group(outer_request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(trace_request),
        route_key=_routing_module._deployment_route_key_from_request(trace_request),
        request=trace_request_summary,
        retry_request=bridge_request_summary,
        retry_tool_types=_trace_module._trace_tool_types(bridge_kwargs.get("tools")),
        retry_tool_names=_trace_module._trace_tool_names(bridge_kwargs.get("tools")),
        retry_has_web_search_options="web_search_options" in bridge_kwargs,
        external_web_search_bridge=bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY),
        responses_external_web_search_tool_sanitized=bridge_metadata.get(
            "responses_external_web_search_tool_sanitized"
        ),
    )
    async def execute_once(active_bridge_kwargs: dict) -> Any:
        response = original_function(
            **_tools_module._with_external_web_search_post_call_suppressed(active_bridge_kwargs)
        )
        response = await _image_generation_module._await_streaming_fallback_candidate_response(
            response,
            active_bridge_kwargs,
            outer_request_kwargs,
        )
        return await _postprocess_generic_bridge_response(
            response,
            active_bridge_kwargs,
            original_function,
        )

    error_request_summary = bridge_request_summary
    try:
        return await execute_once(bridge_kwargs)
    except Exception as exc:
        original_exc = exc
        active_retry_kwargs = bridge_kwargs
        xhigh_retry_kwargs = _image_generation_module._xhigh_reasoning_compat_retry_kwargs(exc, bridge_kwargs)
        if xhigh_retry_kwargs is not None:
            try:
                return await execute_once(xhigh_retry_kwargs)
            except Exception as retry_exc:
                active_retry_kwargs = xhigh_retry_kwargs
                error_request_summary = _trace_module._trace_request_summary(xhigh_retry_kwargs)
                _trace_module._route_trace(
                    "xhigh_reasoning_compat_retry_error",
                    request_id=_routing_module._trace_request_id(trace_request)
                    or _routing_module._trace_request_id(outer_request_kwargs),
                    session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
                    model_group=_request_model_group(trace_request)
                    or _request_model_group(outer_request_kwargs),
                    deployment_id=_routing_module._deployment_id_from_request(trace_request),
                    route_key=_routing_module._deployment_route_key_from_request(trace_request),
                    request=trace_request_summary,
                    retry_request=error_request_summary,
                    original_exception=_routing_module._trace_exception(exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                exc = retry_exc
        if _routing_module._is_deployment_compatible_bad_request_error(exc):
            _trace_module._route_trace(
                "responses_external_web_search_bridge_transient_retry_start",
                request_id=_routing_module._trace_request_id(trace_request)
                or _routing_module._trace_request_id(outer_request_kwargs),
                session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
                model_group=_request_model_group(trace_request)
                or _request_model_group(outer_request_kwargs),
                deployment_id=_routing_module._deployment_id_from_request(trace_request),
                route_key=_routing_module._deployment_route_key_from_request(trace_request),
                request=trace_request_summary,
                retry_request=error_request_summary,
                exception=_routing_module._trace_exception(exc),
            )
            try:
                return await execute_once(active_retry_kwargs)
            except Exception as retry_exc:
                _trace_module._route_trace(
                    "responses_external_web_search_bridge_transient_retry_error",
                    request_id=_routing_module._trace_request_id(trace_request)
                    or _routing_module._trace_request_id(outer_request_kwargs),
                    session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
                    model_group=_request_model_group(trace_request)
                    or _request_model_group(outer_request_kwargs),
                    deployment_id=_routing_module._deployment_id_from_request(trace_request),
                    route_key=_routing_module._deployment_route_key_from_request(trace_request),
                    request=trace_request_summary,
                    retry_request=error_request_summary,
                    original_exception=_routing_module._trace_exception(exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                exc = retry_exc
        _trace_module._route_trace(
            "responses_external_web_search_bridge_error",
            request_id=_routing_module._trace_request_id(trace_request)
            or _routing_module._trace_request_id(outer_request_kwargs),
            session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
            model_group=_request_model_group(trace_request)
            or _request_model_group(outer_request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(trace_request),
            route_key=_routing_module._deployment_route_key_from_request(trace_request),
            request=trace_request_summary,
            retry_request=error_request_summary,
            exception=_routing_module._trace_exception(exc),
        )
        if exc is original_exc:
            raise
        raise exc


async def _execute_responses_function_tool_bridge_call(
    original_function: Any,
    bridge_kwargs: dict,
    *,
    original_request_kwargs: Optional[dict] = None,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    bridge_metadata = _image_generation_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    if bridge_metadata.get(_WEB_SEARCH_EXTERNAL_BRIDGE_KEY) is True:
        bridge_kwargs = _responses_web_search_bridge_module._external_web_search_low_reasoning_kwargs(
            bridge_kwargs
        )
    bridge_metadata = _image_generation_module._request_metadata_dict(
        bridge_kwargs,
        "litellm_metadata",
    ) or {}
    trace_request = original_request_kwargs or bridge_kwargs
    trace_request_summary = _trace_module._trace_request_summary(trace_request)
    bridge_request_summary = _trace_module._trace_request_summary(bridge_kwargs)
    _trace_module._route_trace(
        "responses_function_tool_bridge_start",
        request_id=_routing_module._trace_request_id(trace_request)
        or _routing_module._trace_request_id(outer_request_kwargs),
        session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
        model_group=_request_model_group(trace_request)
        or _request_model_group(outer_request_kwargs),
        deployment_id=_routing_module._deployment_id_from_request(trace_request),
        route_key=_routing_module._deployment_route_key_from_request(trace_request),
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
    )

    async def execute_once(active_bridge_kwargs: dict) -> Any:
        response = original_function(
            **_tools_module._with_external_web_search_post_call_suppressed(active_bridge_kwargs)
        )
        response = await _image_generation_module._await_streaming_fallback_candidate_response(
            response,
            active_bridge_kwargs,
            outer_request_kwargs,
        )
        return await _postprocess_generic_bridge_response(
            response,
            active_bridge_kwargs,
            original_function,
        )

    error_request_summary = bridge_request_summary
    try:
        return await execute_once(bridge_kwargs)
    except Exception as exc:
        original_exc = exc
        xhigh_retry_kwargs = _image_generation_module._xhigh_reasoning_compat_retry_kwargs(exc, bridge_kwargs)
        if xhigh_retry_kwargs is not None:
            try:
                return await execute_once(xhigh_retry_kwargs)
            except Exception as retry_exc:
                error_request_summary = _trace_module._trace_request_summary(xhigh_retry_kwargs)
                _trace_module._route_trace(
                    "xhigh_reasoning_compat_retry_error",
                    request_id=_routing_module._trace_request_id(trace_request)
                    or _routing_module._trace_request_id(outer_request_kwargs),
                    session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
                    model_group=_request_model_group(trace_request)
                    or _request_model_group(outer_request_kwargs),
                    deployment_id=_routing_module._deployment_id_from_request(trace_request),
                    route_key=_routing_module._deployment_route_key_from_request(trace_request),
                    request=trace_request_summary,
                    retry_request=error_request_summary,
                    original_exception=_routing_module._trace_exception(exc),
                    exception=_routing_module._trace_exception(retry_exc),
                )
                exc = retry_exc
        external_web_search_bridge_kwargs = _responses_surfaces_module._with_responses_external_web_search_bridge_after_native_error(
            exc,
            bridge_kwargs,
            outer_request_kwargs,
        )
        if external_web_search_bridge_kwargs is not None:
            return await _execute_responses_external_web_search_bridge_call(
                original_function,
                external_web_search_bridge_kwargs,
                original_request_kwargs=original_request_kwargs or bridge_kwargs,
                outer_request_kwargs=outer_request_kwargs,
            )
        _trace_module._route_trace(
            "responses_function_tool_bridge_error",
            request_id=_routing_module._trace_request_id(trace_request)
            or _routing_module._trace_request_id(outer_request_kwargs),
            session=_routing_module._trace_session_context(trace_request or outer_request_kwargs),
            model_group=_request_model_group(trace_request)
            or _request_model_group(outer_request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(trace_request),
            route_key=_routing_module._deployment_route_key_from_request(trace_request),
            request=trace_request_summary,
            retry_request=error_request_summary,
            exception=_routing_module._trace_exception(exc),
            preemptive_reason=bridge_metadata.get(
                "responses_function_tool_bridge_preemptive_reason"
            ),
        )
        if exc is original_exc:
            raise
        raise exc

def _wrap_generic_function_for_deployment_failover(
    original_function: Any,
    outer_request_kwargs: Optional[dict] = None,
) -> Any:
    if getattr(original_function, _GENERIC_HELPER_PATCH_ATTR, False):
        return original_function

    async def wrapped_generic_function(**kwargs: Any) -> Any:
        for update_request in (
            _image_generation_module._with_empty_tool_controls_removed,
            _image_generation_module._with_codex_compaction_controls,
            _image_generation_module._with_responses_native_extra_body,
            _image_generation_module._with_codex_compaction_headers,
        ):
            updated_kwargs = update_request(kwargs)
            if updated_kwargs is not None:
                kwargs = updated_kwargs
        responses_function_bridge_kwargs = (
            _responses_surfaces_module._responses_function_tool_bridge_preemptive_kwargs(
                kwargs,
                outer_request_kwargs,
            )
        )
        if responses_function_bridge_kwargs is not None:
            try:
                return await _execute_responses_function_tool_bridge_call(
                    original_function,
                    responses_function_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                )
            except Exception as exc:
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
                    original_function,
                    bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_function_tool_bridge_chat_retry_start",
                    error_event="responses_function_tool_bridge_chat_retry_error",
                )
        external_web_search_bridge_kwargs = _responses_surfaces_module._with_responses_external_web_search_bridge(
            kwargs,
            outer_request_kwargs,
        )
        if external_web_search_bridge_kwargs is not None:
            try:
                return await _execute_responses_external_web_search_bridge_call(
                    original_function,
                    external_web_search_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                )
            except Exception as exc:
                if _routing_module._request_current_upstream_surface(kwargs):
                    raise
                bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_retry_kwargs(
                    exc,
                    kwargs,
                    outer_request_kwargs,
                )
                if bridge_kwargs is None:
                    raise
                return await _execute_responses_chat_bridge_call(
                    original_function,
                    bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_external_web_search_bridge_retry_start",
                    error_event="responses_external_web_search_bridge_retry_error",
                )
        preemptive_bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_preemptive_kwargs(
            kwargs,
            outer_request_kwargs,
            include_hosted_web_search_unsupported=True,
            include_client_tool_unsupported=True,
        )
        if preemptive_bridge_kwargs is not None:
            try:
                return await _execute_responses_chat_bridge_call(
                    original_function,
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
                    original_function,
                    preemptive_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_chat_bridge_preemptive_retry_start",
                    error_event="responses_chat_bridge_preemptive_retry_error",
                )
        try:
            response = original_function(**kwargs)
            response = await _image_generation_module._await_streaming_fallback_candidate_response(
                response,
                kwargs,
                outer_request_kwargs,
            )
            return await _postprocess_generic_bridge_response(
                response,
                kwargs,
                original_function,
            )
        except Exception as exc:
            if _routing_module._is_current_upstream_surface_incompatible_error(
                exc,
                kwargs,
                outer_request_kwargs,
            ):
                _routing_module._mark_exception_for_upstream_surface_failover(
                    exc,
                    kwargs,
                )
                if isinstance(outer_request_kwargs, dict):
                    _routing_module._sync_failed_deployment_exclusions(
                        outer_request_kwargs,
                        exc,
                    )
                raise
            facade_response = await _computer_facade_module._responses_computer_facade_retry_response(
                exc,
                kwargs,
                outer_request_kwargs,
            )
            if facade_response is not None:
                return _image_generation_module._sanitize_response_echoed_request_images_for_delivery(
                    facade_response,
                    kwargs,
                )
            xhigh_retry_kwargs = _image_generation_module._xhigh_reasoning_compat_retry_kwargs(exc, kwargs)
            if xhigh_retry_kwargs is not None:
                try:
                    response = original_function(**xhigh_retry_kwargs)
                    response = await _image_generation_module._await_streaming_fallback_candidate_response(
                        response,
                        xhigh_retry_kwargs,
                        outer_request_kwargs,
                    )
                    return await _postprocess_generic_bridge_response(
                        response,
                        xhigh_retry_kwargs,
                        original_function,
                    )
                except Exception as retry_exc:
                    _trace_module._route_trace(
                        "xhigh_reasoning_compat_retry_error",
                        request_id=_routing_module._trace_request_id(kwargs),
                        session=_routing_module._trace_session_context(kwargs),
                        model_group=_request_model_group(kwargs),
                        deployment_id=_routing_module._deployment_id_from_request(kwargs),
                        route_key=_routing_module._deployment_route_key_from_request(kwargs),
                        original_exception=_routing_module._trace_exception(exc),
                        exception=_routing_module._trace_exception(retry_exc),
                    )
                    exc = retry_exc
            external_web_search_bridge_kwargs = _responses_surfaces_module._with_responses_external_web_search_bridge_after_native_error(
                exc,
                kwargs,
                outer_request_kwargs,
            )
            if external_web_search_bridge_kwargs is not None:
                return await _execute_responses_external_web_search_bridge_call(
                    original_function,
                    external_web_search_bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                )
            bridge_kwargs = None
            if not _routing_module._request_current_upstream_surface(kwargs):
                bridge_kwargs = _responses_surfaces_module._responses_chat_bridge_retry_kwargs(
                    exc, kwargs, outer_request_kwargs
                )
            if bridge_kwargs is not None:
                return await _execute_responses_chat_bridge_call(
                    original_function,
                    bridge_kwargs,
                    original_request_kwargs=kwargs,
                    outer_request_kwargs=outer_request_kwargs,
                    original_exception=exc,
                    start_event="responses_chat_bridge_retry_start",
                    error_event="responses_chat_bridge_retry_error",
                )
            if (
                _routing_module._is_priority_deployment_failover_error(exc)
                and not _routing_module._should_retry_with_browser_compatible_headers(exc, kwargs)
            ):
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


def _failed_deployment_order(exception: Exception) -> Optional[int]:
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
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
        if not metadata:
            continue
        model_group = metadata.get("model_group")
        if isinstance(model_group, str) and model_group.strip():
            return model_group
    return None


def _request_metadata_model_group(request_kwargs: Optional[dict]) -> Optional[str]:
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
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
    model_info = _image_generation_module._request_model_info(request_kwargs)
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
    litellm_metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    if litellm_metadata.get(_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY):
        return
    updated_metadata = litellm_metadata.copy()
    updated_metadata[_RESPONSES_CHAT_BRIDGE_ORIGINAL_MODEL_GROUP_KEY] = model_group
    request_kwargs["litellm_metadata"] = updated_metadata


def _external_web_search_router_model_group(request_kwargs: Optional[dict]) -> Optional[str]:
    model_info = _image_generation_module._request_model_info(request_kwargs)
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
    model_info = _image_generation_module._request_model_info(request_kwargs)
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
        _trace_module._route_trace(
            "fallback_target_order_constraint",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_request_model_group(request_kwargs),
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
        _trace_module._route_trace(
            "terminal_error_fallback_suppressed",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_request_model_group(request_kwargs),
            request=_trace_module._trace_request_summary(request_kwargs),
            exception=_routing_module._trace_exception(exception),
        )
        return None

    if _routing_module._should_retry_same_deployment_before_fallback(exception):
        _trace_module._route_trace(
            "same_deployment_retry_fallback_suppressed",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_request_model_group(request_kwargs),
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
        if not _image_generation_module._request_can_attempt_image_generation_tool_fallback(request_kwargs):
            _trace_module._route_trace(
                "image_generation_tool_runtime_fallback_exhausted",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=_request_model_group(request_kwargs),
                request=_trace_module._trace_request_summary(request_kwargs),
                exception=_routing_module._trace_exception(exception),
                attempts=_image_generation_module._request_image_generation_tool_fallback_attempts(request_kwargs),
                max_attempts=_image_generation_module._image_generation_tool_fallback_max_attempts(),
            )
            return None
        attempts = _image_generation_module._with_incremented_image_generation_tool_fallback_attempts(request_kwargs)
        _trace_module._route_trace(
            "image_generation_tool_runtime_fallback_next",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_request_model_group(request_kwargs),
            request=_trace_module._trace_request_summary(request_kwargs),
            exception=_routing_module._trace_exception(exception),
            attempts=attempts,
            max_attempts=_image_generation_module._image_generation_tool_fallback_max_attempts(),
        )

    failed_id = _failed_deployment_id(exception)
    failed_route_key = _failed_deployment_route_key(exception)
    failed_order = _failed_deployment_order(exception)
    if failed_order is None and _routing_module._is_no_deployments_available_error(exception):
        failed_order = _image_generation_module._request_target_order(request_kwargs)
    model_group = _request_model_group(request_kwargs)
    if failed_order is None or model_group is None:
        return None

    excluded_ids = _image_generation_module._request_excluded_deployment_ids(request_kwargs)
    surface_fallback = (
        None
        if _routing_module._should_retry_same_deployment_before_fallback(exception)
        else _routing_module._next_upstream_surface_for_failed_deployment(
            router, exception, request_kwargs
        )
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
        if failed_order is not None:
            request_kwargs["_target_order"] = failed_order
        excluded_ids.discard(target_deployment_id)
        if excluded_ids:
            request_kwargs["_excluded_deployment_ids"] = sorted(excluded_ids)
        else:
            request_kwargs.pop("_excluded_deployment_ids", None)
        entry = {
            "model": model_group,
            "_target_order": failed_order,
            "_litellm_menu_upstream_url_surface": next_surface,
            "_litellm_menu_attempted_upstream_url_surfaces": attempted_surfaces,
            "_litellm_menu_surface_target_deployment_id": target_deployment_id,
        }
        if excluded_ids:
            entry["_excluded_deployment_ids"] = sorted(excluded_ids)
        _trace_module._route_trace(
            "same_deployment_surface_fallback_available",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=model_group,
            failed_deployment_id=failed_id,
            failed_surface=current_surface,
            next_surface=next_surface,
            target_order=failed_order,
        )
        return entry

    _routing_module._clear_request_surface_target(request_kwargs)
    if failed_id is not None:
        excluded_ids.add(failed_id)

    try:
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, "metadata") or {}
        team_id = metadata.get("user_api_key_team_id")
        all_deployments = _routing_module._router_configured_deployments(
            router,
            model_group,
            team_id=team_id,
        )
    except Exception:
        return None

    cooldown_candidates, cooldown_deployments, cooldown_filtered = (
        _routing_module._with_active_deployment_cooldowns(
            list(all_deployments or []),
            request_kwargs=request_kwargs,
        )
    )
    if cooldown_deployments:
        _trace_module._route_trace(
            "fallback_deployment_cooldown_filter",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=model_group,
            cooldown_filtered=cooldown_filtered,
            cooldown_all_candidates=bool(cooldown_deployments and not cooldown_candidates),
            cooldown_deployments=cooldown_deployments,
        )

    available_deployments = [
        deployment
        for deployment in cooldown_candidates
        if _image_generation_module._deployment_id(deployment) not in excluded_ids
    ]
    peer_deployments = (
        [
            deployment
            for deployment in available_deployments
            if _image_generation_module._deployment_order(deployment) == failed_order
        ]
        if failed_id is not None
        else []
    )
    if peer_deployments:
        _trace_module._route_trace(
            "same_order_peer_fallback_available",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=model_group,
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
        }

    available_orders = sorted(
        {
            order
            for order in (_image_generation_module._deployment_order(deployment) for deployment in available_deployments)
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
        _trace_module._route_trace(
            "same_order_peer_fallback_unavailable",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=model_group,
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
        if _image_generation_module._deployment_order(deployment) == next_order
    ]
    _trace_module._route_trace(
        "next_order_fallback_available",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=model_group,
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
    }
