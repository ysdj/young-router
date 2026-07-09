from __future__ import annotations

from . import image_generation as _image_generation_module
from . import responses_execution as _responses_execution_module
from . import responses_tools as _responses_tools_module
from . import responses_web_search_bridge as _responses_web_search_bridge_module
from . import routing as _routing_module
from . import streaming as _streaming_module
from . import tools as _tools_module
from . import trace as _trace_module


from .base import (
    Any,
    AsyncIterator,
    ComputerAction,
    ComputerObservation,
    HostedToolPlan,
    Optional,
    _COMPUTER_FACADE_ACTION_DENYLIST_ENV,
    _COMPUTER_FACADE_AUTO_BACKEND,
    _COMPUTER_FACADE_BACKENDS,
    _COMPUTER_FACADE_BACKEND_ENV,
    _COMPUTER_FACADE_BROWSER_BACKEND,
    _COMPUTER_FACADE_CHROME_BACKEND,
    _COMPUTER_FACADE_CUA_BACKEND,
    _COMPUTER_FACADE_DEFAULT_MAX_STEPS,
    _COMPUTER_FACADE_EXECUTOR_METADATA_KEY,
    _COMPUTER_FACADE_MAX_STEPS_ENV,
    _COMPUTER_FACADE_MCP_BACKEND,
    _COMPUTER_FACADE_MOCK_BACKEND,
    _COMPUTER_FACADE_MOCK_DONE_MESSAGE,
    _COMPUTER_FACADE_MODEL_ENV,
    _COMPUTER_FACADE_PLANNER_METADATA_KEY,
    _COMPUTER_FACADE_PLAYWRIGHT_BACKEND,
    _COMPUTER_FACADE_REQUIRE_OBSERVATION_ENV,
    _COMPUTER_FACADE_SAFE_FAILURE_MESSAGE,
    _COMPUTER_FACADE_TRACE_ENV,
    _COMPUTER_FACADE_TRACE_SCREENSHOTS_ENV,
    _HOSTED_TOOL_UNSUPPORTED_MESSAGE_KEY,
    _HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY,
    _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE,
    _JSONStreamEvent,
    _ROUTE_TRACE_LOGGER,
    asyncio,
    base64,
    binascii,
    copy,
    datetime,
    inspect,
    json,
    os,
    re,
    time,
    timezone,
    urlparse,
)

def _computer_facade_backend() -> str:
    backend = os.getenv(_COMPUTER_FACADE_BACKEND_ENV, "").strip().lower()
    if backend not in _COMPUTER_FACADE_BACKENDS:
        return _COMPUTER_FACADE_AUTO_BACKEND
    return backend or _COMPUTER_FACADE_AUTO_BACKEND


def _computer_facade_planner_model(request_kwargs: Optional[dict]) -> str:
    configured = os.getenv(_COMPUTER_FACADE_MODEL_ENV, "").strip()
    if configured:
        return configured
    return _computer_facade_model_name(request_kwargs)


def _computer_facade_max_steps() -> int:
    value = os.getenv(_COMPUTER_FACADE_MAX_STEPS_ENV, "").strip()
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = _COMPUTER_FACADE_DEFAULT_MAX_STEPS
    return max(1, min(parsed, 200))


def _computer_facade_trace_enabled() -> bool:
    return _trace_module._route_trace_bool(os.getenv(_COMPUTER_FACADE_TRACE_ENV, ""))


def _computer_facade_trace_screenshots_enabled() -> bool:
    return _trace_module._route_trace_bool(os.getenv(_COMPUTER_FACADE_TRACE_SCREENSHOTS_ENV, ""))


def _computer_facade_require_observation() -> bool:
    value = os.getenv(_COMPUTER_FACADE_REQUIRE_OBSERVATION_ENV, "")
    if not value.strip():
        return True
    return _trace_module._route_trace_bool(value)


def _computer_facade_action_denylist() -> set[str]:
    values = os.getenv(_COMPUTER_FACADE_ACTION_DENYLIST_ENV, "")
    denied: set[str] = set()
    for raw in re.split(r"[,;\s]+", values):
        item = raw.strip().lower()
        if item:
            denied.add(item)
    return denied


def _request_is_computer_facade_planner(request_kwargs: Optional[dict]) -> bool:
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
        if metadata and metadata.get(_COMPUTER_FACADE_PLANNER_METADATA_KEY) is True:
            return True
    return False


def _request_has_native_computer_tool(request_kwargs: Optional[dict]) -> bool:
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs)
    return plan.hosted_computer


def _tools_include_chat_bridge_client_tool(tools: Any) -> bool:
    if not isinstance(tools, list):
        return False
    for tool in tools:
        if not isinstance(tool, dict):
            continue
        tool_type = tool.get("type")
        if tool_type in {"function", "tool_search", "custom"}:
            return True
        if tool_type == "namespace" and _responses_tools_module._responses_bridge_namespace_tools(tool):
            return True
    return False


def _request_has_chat_bridge_client_tool(request_kwargs: Optional[dict]) -> bool:
    request_kwargs = request_kwargs or {}
    if _tools_include_chat_bridge_client_tool(request_kwargs.get("tools")):
        return True
    return bool(_responses_tools_module._responses_input_tool_search_output_tools(request_kwargs.get("input")))


def _request_hosted_browser_computer_blocks_chat_bridge(
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_request_kwargs)
    if not plan.hosted_computer:
        return False
    return not (
        _tools_module._request_has_browser_computer_client_tool(request_kwargs)
        or _tools_module._request_has_browser_computer_client_tool(outer_request_kwargs)
    )


def _can_use_computer_facade_after_native_error(
    plan: HostedToolPlan,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    if _request_is_computer_facade_planner(
        request_kwargs
    ) or _request_is_computer_facade_planner(outer_request_kwargs):
        return False
    if not (
        _image_generation_module._request_is_responses_api(request_kwargs)
        or _image_generation_module._request_is_responses_api(outer_request_kwargs)
    ):
        return False
    return plan.hosted_computer


def _native_hosted_computer_unsupported_error(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict] = None,
) -> bool:
    plan = _responses_tools_module._responses_hosted_tool_plan(
        request_kwargs,
        outer_request_kwargs,
    )
    if not plan.hosted_computer:
        return False
    status_code = _routing_module._exception_status_code(exception)
    if status_code is not None and status_code not in {400, 404, 422}:
        return False
    if _routing_module._is_terminal_prompt_or_policy_error(exception):
        return False
    text = _routing_module._exception_text(exception)
    if not text:
        return False
    tool_markers = (
        "computer",
        "computer_use",
        "computer-use",
        "hosted browser",
        "hosted tool",
        "tool type",
        "invalid_prompt",
        "invalid_union",
        "invalid_type",
    )
    if not any(marker in text for marker in tool_markers):
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
            "invalid responses api request",
            "invalid_prompt",
            "invalid_union",
            "invalid_type",
            "expected string, received array",
            "expected array, received undefined",
            "tool type",
            "invalid_request_error",
            "not found",
            "unrecognized",
            "unknown",
        )
    )


def _hosted_web_search_unsupported_notice() -> str:
    return (
        "WEB_SEARCH_UNSUPPORTED: Codex web_search is not supported by the selected "
        "LiteLLM route. Hosted Responses web_search requires an upstream Responses "
        "endpoint with web search support. Do not claim to have searched the web, "
        "do not answer from memory, and do not use shell/curl/python or other tools "
        "as a substitute. Tell the user that web_search is unavailable for this route."
    )


def _with_hosted_web_search_unsupported_bridge(
    retry_kwargs: dict,
    retry_metadata: dict,
) -> None:
    retry_metadata[_HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY] = True
    retry_metadata[
        _HOSTED_TOOL_UNSUPPORTED_MESSAGE_KEY
    ] = _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE
    notice = _hosted_web_search_unsupported_notice()
    existing = retry_kwargs.get("instructions")
    if isinstance(existing, str) and existing.strip():
        if notice not in existing:
            retry_kwargs["instructions"] = f"{existing.rstrip()}\n\n{notice}"
    else:
        retry_kwargs["instructions"] = notice
    retry_kwargs.pop("tools", None)
    retry_kwargs.pop("tool_choice", None)
    retry_kwargs.pop("parallel_tool_calls", None)
    retry_kwargs.pop("web_search_options", None)


def _hosted_tool_unsupported_message(metadata: Optional[dict]) -> Optional[str]:
    if not isinstance(metadata, dict):
        return None
    message = metadata.get(_HOSTED_TOOL_UNSUPPORTED_MESSAGE_KEY)
    if isinstance(message, str) and message.strip():
        return message
    if metadata.get(_HOSTED_WEB_SEARCH_UNSUPPORTED_BRIDGE_KEY) is True:
        return _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE
    return None


def _hosted_tool_unsupported_response(
    request_kwargs: Optional[dict],
    message: str,
) -> dict[str, Any]:
    request_kwargs = request_kwargs or {}
    model = _routing_module._first_not_none(
        request_kwargs.get("model"),
        _responses_execution_module._request_model_group(request_kwargs),
        _routing_module._deployment_route_key_from_request(request_kwargs),
        "unknown",
    )
    response_id = f"resp_hosted_tool_unsupported_{os.getpid()}_{time.time_ns()}"
    message_id = f"msg_hosted_tool_unsupported_{time.time_ns()}"
    content = {
        "type": "output_text",
        "text": message,
        "annotations": [],
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": model,
        "output_text": message,
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [content],
            }
        ],
        "usage": {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
        },
    }


def _hosted_web_search_unsupported_response(request_kwargs: Optional[dict]) -> dict[str, Any]:
    return _hosted_tool_unsupported_response(
        request_kwargs,
        _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE,
    )


async def _hosted_web_search_unsupported_stream(
    response: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    output_items = response.get("output")
    message = output_items[0] if isinstance(output_items, list) and output_items else {}
    content_items = message.get("content") if isinstance(message, dict) else None
    content = content_items[0] if isinstance(content_items, list) and content_items else {}
    message_id = message.get("id") if isinstance(message, dict) else None
    text = content.get("text") if isinstance(content, dict) else ""
    text = text if isinstance(text, str) else _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE
    if not text.strip():
        text = _image_generation_module._response_text(response)
    if not text.strip():
        text = _HOSTED_WEB_SEARCH_UNSUPPORTED_MESSAGE

    created_response = copy.deepcopy(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    yield encode({"type": "response.created", "response": created_response})
    yield encode({
        "type": "response.output_item.added",
        "output_index": 0,
        "item": {
            "id": message_id,
            "type": "message",
            "status": "in_progress",
            "role": "assistant",
            "content": [],
        },
    })
    yield encode({
        "type": "response.content_part.added",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": {"type": "output_text", "text": "", "annotations": []},
    })
    yield encode({
        "type": "response.output_text.delta",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "delta": text,
    })
    yield encode({
        "type": "response.output_text.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "text": text,
    })
    yield encode({
        "type": "response.content_part.done",
        "item_id": message_id,
        "output_index": 0,
        "content_index": 0,
        "part": content,
    })
    yield encode({
        "type": "response.output_item.done",
        "output_index": 0,
        "item": message,
    })
    if isinstance(output_items, list):
        for output_index, output_item in enumerate(output_items[1:], start=1):
            item = _streaming_module._jsonable(output_item)
            if not isinstance(item, dict):
                continue
            added_item = copy.deepcopy(item)
            if added_item.get("status") == "completed":
                added_item["status"] = "in_progress"
            yield encode(
                {
                    "type": "response.output_item.added",
                    "output_index": output_index,
                    "item": added_item,
                }
            )
            done_item = copy.deepcopy(item)
            if done_item.get("status") == "in_progress":
                done_item["status"] = "completed"
            yield encode(
                {
                    "type": "response.output_item.done",
                    "output_index": output_index,
                    "item": done_item,
                }
            )
    yield encode({"type": "response.completed", "response": response})


async def _external_web_search_bridge_stream(
    response: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    response = _responses_web_search_bridge_module._sanitize_response_web_search_call_items(response)
    output_items = response.get("output")
    output = output_items if isinstance(output_items, list) else []
    created_response = copy.deepcopy(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    yield encode({"type": "response.created", "response": created_response})
    sequence_number = 0
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            sanitized_item = _responses_web_search_bridge_module._sanitize_web_search_call_item(item)
            if sanitized_item is None:
                continue
            item = sanitized_item
        if item.get("type") == "message":
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
            yield encode({
                "type": "response.output_item.added",
                "output_index": index,
                "item": added_message,
            })
            yield encode({
                "type": "response.content_part.added",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            if text:
                yield encode({
                    "type": "response.output_text.delta",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "delta": text,
                })
            yield encode({
                "type": "response.output_text.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "text": text,
            })
            yield encode({
                "type": "response.content_part.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "part": content,
            })
            yield encode({
                "type": "response.output_item.done",
                "output_index": index,
                "item": item,
            })
            continue

        added_item = copy.deepcopy(item)
        if added_item.get("status") == "completed":
            added_item["status"] = "in_progress"
        yield encode({
            "type": "response.output_item.added",
            "output_index": index,
            "item": added_item,
        })
        if item.get("type") == "web_search_call":
            item_id = item.get("id")
            for event_type in (
                "response.web_search_call.in_progress",
                "response.web_search_call.searching",
                "response.web_search_call.completed",
            ):
                sequence_number += 1
                lifecycle_event = {
                    "type": event_type,
                    "item_id": item_id,
                    "output_index": index,
                    "sequence_number": sequence_number,
                }
                action = item.get("action")
                if isinstance(action, dict):
                    lifecycle_event["action"] = copy.deepcopy(action)
                yield encode(lifecycle_event)
        yield encode({
            "type": "response.output_item.done",
            "output_index": index,
            "item": item,
        })
    yield encode({"type": "response.completed", "response": response})

async def _resolve_litellm_web_search_function_calls_stream(
    response: Any,
    request_kwargs: Optional[dict],
    original_function: Optional[Any] = None,
) -> AsyncIterator[dict[str, Any]]:
    actions = _responses_web_search_bridge_module._litellm_web_search_actions_for_request(response, request_kwargs)
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        payload = _hosted_tool_unsupported_response(request_kwargs, _image_generation_module._response_text(response))
    if not actions:
        async for chunk in _external_web_search_bridge_stream(payload):
            yield chunk
        return

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    created_response = copy.deepcopy(payload)
    created_response["status"] = "in_progress"
    created_response["output"] = []
    yield encode({"type": "response.created", "response": created_response})

    sequence_number = 0
    search_sections: list[str] = []
    source_urls: list[str] = []
    completed_search_items: list[dict[str, Any]] = []
    page_cache: dict[str, str] = {}
    page_fetch_tasks: dict[str, asyncio.Task[str]] = {}
    completed_actions: list[dict[str, str]] = []
    started_search_items = [
        (action, search_item)
        for action in actions
        for search_item in [_responses_web_search_bridge_module._external_web_search_call_item_for_action(action)]
        if search_item is not None
    ]
    for output_index, (_action, search_item) in enumerate(started_search_items):
        added_item = copy.deepcopy(search_item)
        added_item["status"] = "in_progress"
        yield encode(
            {
                "type": "response.output_item.added",
                "output_index": output_index,
                "item": added_item,
            }
        )
        for event_type in (
            "response.web_search_call.in_progress",
            "response.web_search_call.searching",
        ):
            sequence_number += 1
            yield encode(
                {
                    "type": event_type,
                    "item_id": search_item.get("id"),
                    "output_index": output_index,
                    "sequence_number": sequence_number,
                    "action": copy.deepcopy(added_item.get("action", {})),
                }
            )

        action = started_search_items[output_index][0]
        section, urls, completed_action = await _responses_web_search_bridge_module._external_web_search_run_action(
            action,
            page_cache,
            page_fetch_tasks,
        )
        search_sections.append(section)
        completed_actions.append(completed_action)
        for url in urls:
            if url not in source_urls:
                source_urls.append(url)
        completed_search_item = _responses_web_search_bridge_module._external_web_search_call_item_for_action(
            completed_action,
            urls,
        )
        if completed_search_item is None:
            continue
        completed_search_item["id"] = search_item.get("id")
        completed_search_items.append(completed_search_item)
        sequence_number += 1
        yield encode(
            {
                "type": "response.web_search_call.completed",
                "item_id": completed_search_item.get("id"),
                "output_index": output_index,
                "sequence_number": sequence_number,
                "action": copy.deepcopy(completed_search_item.get("action", {})),
            }
        )
        yield encode(
            {
                "type": "response.output_item.done",
                "output_index": output_index,
                "item": completed_search_item,
            }
        )

    search_results = "\n\n".join(section for section in search_sections if section.strip())
    completed_labels = _responses_web_search_bridge_module._external_web_search_action_labels(completed_actions)
    synthesis_task = asyncio.create_task(
        _responses_web_search_bridge_module._external_web_search_synthesize_or_fallback(
            request_kwargs=request_kwargs,
            search_results=search_results,
            queries=completed_labels,
            source_urls=source_urls,
            original_function=original_function,
        )
    )
    try:
        async for keepalive in _external_web_search_keepalives_until_done(
            synthesis_task,
            request_kwargs=request_kwargs,
            phase="web_search_synthesis",
        ):
            yield keepalive
        synthesized = await synthesis_task
    finally:
        if not synthesis_task.done():
            synthesis_task.cancel()

    synthesized_payload = _streaming_module._jsonable(synthesized)
    if not isinstance(synthesized_payload, dict):
        synthesized_payload = _hosted_tool_unsupported_response(
            request_kwargs,
            _image_generation_module._response_text(synthesized),
        )
    output_items = synthesized_payload.get("output")
    if not isinstance(output_items, list):
        output_items = []
    if not output_items:
        text = _image_generation_module._response_text(synthesized_payload)
        if text.strip():
            output_items = _hosted_tool_unsupported_response(
                request_kwargs,
                text,
            )["output"]

    final_output = list(completed_search_items)
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            continue
        if _responses_web_search_bridge_module._is_litellm_web_search_call_item(item):
            continue
        final_output.append(item)
        index = len(final_output) - 1
        if item.get("type") == "message":
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
            yield encode(
                {
                    "type": "response.output_item.added",
                    "output_index": index,
                    "item": added_message,
                }
            )
            yield encode(
                {
                    "type": "response.content_part.added",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "part": {"type": "output_text", "text": "", "annotations": []},
                }
            )
            if text:
                yield encode(
                    {
                        "type": "response.output_text.delta",
                        "item_id": item.get("id"),
                        "output_index": index,
                        "content_index": 0,
                        "delta": text,
                    }
                )
            yield encode(
                {
                    "type": "response.output_text.done",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "text": text,
                }
            )
            yield encode(
                {
                    "type": "response.content_part.done",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "part": content,
                }
            )
            yield encode(
                {
                    "type": "response.output_item.done",
                    "output_index": index,
                    "item": item,
                }
            )
            continue

        added_tool_item = copy.deepcopy(item)
        if added_tool_item.get("status") == "completed":
            added_tool_item["status"] = "in_progress"
        yield encode(
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": added_tool_item,
            }
        )
        yield encode(
            {
                "type": "response.output_item.done",
                "output_index": index,
                "item": item,
            }
        )

    final_response = copy.deepcopy(synthesized_payload)
    final_response["status"] = "completed"
    final_response["output"] = final_output
    yield encode({"type": "response.completed", "response": final_response})


async def _external_web_search_keepalives_until_done(
    task: "asyncio.Task[Any]",
    *,
    request_kwargs: Optional[dict],
    phase: str,
) -> AsyncIterator[Any]:
    try:
        from .streaming import (
            _ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS,
            _route_recovery_sse_keepalive,
        )
    except Exception:
        return

    keepalive_seconds = max(0.001, float(_ROUTE_RECOVERY_SSE_KEEPALIVE_SECONDS))
    while not task.done():
        done, _pending = await asyncio.wait({task}, timeout=keepalive_seconds)
        if done:
            break
        yield _route_recovery_sse_keepalive(
            0,
            request_data=request_kwargs,
            phase=phase,
        )


def _external_web_search_stream_events_text(events: list[Any]) -> str:
    from .streaming import _stream_chunk_text_fragment

    parts: list[str] = []
    done_text: Optional[str] = None
    for chunk in events:
        text, is_done = _stream_chunk_text_fragment(chunk)
        if not text:
            continue
        if is_done:
            done_text = text
        else:
            parts.append(text)
    if isinstance(done_text, str) and done_text.strip():
        return done_text
    return "".join(parts)


async def _external_web_search_stream_route_recovery_or_fallback(
    exception: Exception,
    *,
    request_kwargs: Optional[dict],
    search_results: str,
    queries: list[str],
) -> Any:
    has_recovery_context = (
        _responses_web_search_bridge_module._external_web_search_has_recovery_context(
            request_kwargs,
            exception,
        )
    )
    if not (
        _responses_web_search_bridge_module._external_web_search_origin_was_streaming(request_kwargs)
        and (
            _routing_module._is_route_recovery_poll_error(exception)
            or has_recovery_context
        )
    ):
        raise exception

    if isinstance(request_kwargs, dict):
        events: list[Any] = []
        recovery_request: Optional[dict] = request_kwargs
        try:
            from .streaming import (
                _is_route_recovery_sse_keepalive,
                _responses_stream_events_to_completed_payload,
                _stream_route_recovery_poll,
            )

            recovery_request = _responses_web_search_bridge_module._external_web_search_recovery_kwargs(
                request_kwargs,
                search_results,
                exception,
            )
            async for chunk in _stream_route_recovery_poll(
                recovery_request,
                exception,
            ):
                if _is_route_recovery_sse_keepalive(chunk):
                    continue
                events.append(chunk)
        except Exception as recovery_exc:
            if (
                isinstance(recovery_request, dict)
                and _responses_web_search_bridge_module._external_web_search_is_recovery_payload(
                    recovery_request,
                )
            ):
                _responses_web_search_bridge_module._external_web_search_set_recovery_request(
                    exception,
                    recovery_request,
                )
                _responses_web_search_bridge_module._external_web_search_set_recovery_request(
                    recovery_exc,
                    recovery_request,
                )
            _trace_module._route_trace(
                "external_web_search_stream_route_recovery_error",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                model_group=_responses_execution_module._request_model_group(request_kwargs),
                request=_trace_module._trace_request_summary(request_kwargs),
                recovery_request=_trace_module._trace_request_summary(recovery_request),
                recovery_payload_phase=_responses_web_search_bridge_module._external_web_search_recovery_payload_phase(
                    recovery_request
                    if isinstance(recovery_request, dict)
                    else None
                ),
                original_exception=_routing_module._trace_exception(exception),
                exception=_routing_module._trace_exception(recovery_exc),
            )
        if events:
            try:
                recovered_payload = _responses_stream_events_to_completed_payload(
                    events,
                    recovery_request,
                )
            except Exception as recovery_exc:
                recovered_payload = None
                if (
                    isinstance(recovery_request, dict)
                    and _responses_web_search_bridge_module._external_web_search_is_recovery_payload(
                        recovery_request,
                    )
                ):
                    _responses_web_search_bridge_module._external_web_search_set_recovery_request(
                        exception,
                        recovery_request,
                    )
                    _responses_web_search_bridge_module._external_web_search_set_recovery_request(
                        recovery_exc,
                        recovery_request,
                    )
                _trace_module._route_trace(
                    "external_web_search_stream_route_recovery_payload_error",
                    request_id=_routing_module._trace_request_id(request_kwargs),
                    session=_routing_module._trace_session_context(request_kwargs),
                    model_group=_responses_execution_module._request_model_group(request_kwargs),
                    request=_trace_module._trace_request_summary(request_kwargs),
                    recovery_request=_trace_module._trace_request_summary(recovery_request),
                    recovery_payload_phase=_responses_web_search_bridge_module._external_web_search_recovery_payload_phase(
                        recovery_request
                        if isinstance(recovery_request, dict)
                        else None
                    ),
                    original_exception=_routing_module._trace_exception(exception),
                    exception=_routing_module._trace_exception(recovery_exc),
                )
            if _responses_web_search_bridge_module._has_litellm_web_search_actions_for_request(
                recovered_payload,
                recovery_request,
            ):
                return recovered_payload
            if _image_generation_module._response_text(recovered_payload).strip():
                return recovered_payload
            try:
                recovered_text = _external_web_search_stream_events_text(events)
            except Exception:
                recovered_text = ""
            if not recovered_text.strip():
                recovered_text = _image_generation_module._response_text(events)
            if recovered_text.strip():
                return _hosted_tool_unsupported_response(recovery_request, recovered_text)

    raise exception


def _external_web_search_visible_message_items(items: Any) -> list[dict[str, Any]]:
    if not isinstance(items, list):
        return []
    visible_items: list[dict[str, Any]] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("type") != "message":
            continue
        if _image_generation_module._response_text(item).strip():
            visible_items.append(item)
    return visible_items


def _external_web_search_message_stream_events(
    item: dict[str, Any],
    index: int,
) -> list[dict[str, Any]]:
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
    return events


def _external_web_search_missing_final_answer_exception(
    request_kwargs: Optional[dict],
) -> Exception:
    exception = RuntimeError(
        "LiteLLM Menu external web_search completed without a visible assistant answer"
    )
    try:
        exception.status_code = 503  # type: ignore[attr-defined]
    except Exception:
        pass
    try:
        exception.body = {  # type: ignore[attr-defined]
            "reason": "external_web_search_missing_final_answer",
        }
    except Exception:
        pass
    _routing_module._mark_exception_for_deployment_failover(exception, request_kwargs)
    return exception


def _external_web_search_output_items_from_response(
    response: Any,
    request_kwargs: Optional[dict],
) -> tuple[dict[str, Any], list[Any]]:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        payload = _hosted_tool_unsupported_response(request_kwargs, _image_generation_module._response_text(response))
    output_items = payload.get("output")
    if not isinstance(output_items, list):
        output_items = []
    if not output_items:
        text = _image_generation_module._response_text(response)
        if text.strip():
            output_items = _hosted_tool_unsupported_response(
                request_kwargs,
                text,
            )["output"]
    return payload, output_items


async def _resolve_litellm_web_search_function_calls_stream_rounds(
    response: Any,
    request_kwargs: Optional[dict],
    original_function: Optional[Any] = None,
) -> AsyncIterator[dict[str, Any]]:
    payload = _streaming_module._jsonable(response)
    if not isinstance(payload, dict):
        payload = _hosted_tool_unsupported_response(request_kwargs, _image_generation_module._response_text(response))
    initial_actions = _responses_web_search_bridge_module._litellm_web_search_actions_for_request(response, request_kwargs)
    if not initial_actions:
        _responses_web_search_bridge_module._external_web_search_raise_if_invalid_initial_no_action_response(
            response,
            request_kwargs,
        )
        async for chunk in _external_web_search_bridge_stream(payload):
            yield chunk
        return

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    created_response = copy.deepcopy(payload)
    created_response["status"] = "in_progress"
    created_response["output"] = []
    yield encode({"type": "response.created", "response": created_response})

    max_rounds = _responses_web_search_bridge_module._external_web_search_max_rounds()
    sequence_number = 0
    next_output_index = 0
    current_response = response
    _responses_web_search_bridge_module._mark_external_web_search_started(request_kwargs)
    completed_actions: list[dict[str, str]] = (
        _responses_web_search_bridge_module._external_web_search_completed_actions_metadata(request_kwargs)
    )
    existing_search_results = _responses_web_search_bridge_module._external_web_search_search_results_metadata(request_kwargs)
    search_sections: list[str] = [existing_search_results] if existing_search_results.strip() else []
    source_urls: list[str] = []
    source_urls_by_action: list[list[str]] = []
    page_cache: dict[str, str] = {}
    page_fetch_tasks: dict[str, asyncio.Task[str]] = {}
    completed_search_items: list[dict[str, Any]] = []
    final_response: Any = response
    search_results = "\n\n".join(section for section in search_sections if section.strip())
    completed_labels = _responses_web_search_bridge_module._external_web_search_action_labels(completed_actions)
    forced_synthesis = False
    route_recovery_attempted = False

    def collect_action_events(
        actions: list[dict[str, str]],
    ) -> tuple[AsyncIterator[dict[str, Any]], dict[str, Any]]:
        collection: dict[str, Any] = {
            "message": "",
            "source_urls": [],
            "source_urls_by_action": [],
            "completed_actions": [],
            "completed_items": [],
        }

        async def stream_events() -> AsyncIterator[dict[str, Any]]:
            nonlocal sequence_number, next_output_index

            round_items: list[tuple[int, str, dict[str, str]]] = []
            for action in actions:
                search_item = _responses_web_search_bridge_module._external_web_search_call_item_for_action(action)
                if search_item is None:
                    continue
                added_item = copy.deepcopy(search_item)
                added_item["status"] = "in_progress"
                output_index = next_output_index
                next_output_index += 1
                round_items.append((output_index, search_item["id"], action))
                yield encode(
                    {
                        "type": "response.output_item.added",
                        "output_index": output_index,
                        "item": added_item,
                    }
                )
                for event_type in (
                    "response.web_search_call.in_progress",
                    "response.web_search_call.searching",
                ):
                    sequence_number += 1
                    yield encode(
                        {
                            "type": event_type,
                            "item_id": search_item.get("id"),
                            "output_index": output_index,
                            "sequence_number": sequence_number,
                            "action": copy.deepcopy(added_item.get("action", {})),
                        }
                    )

            async def run_round_action(
                index: int,
                action: dict[str, str],
            ) -> tuple[int, str, list[str], dict[str, str]]:
                section, urls, completed_action = await _responses_web_search_bridge_module._external_web_search_run_action(
                    action,
                    page_cache,
                    page_fetch_tasks,
                )
                return index, section, urls, completed_action

            tasks = [
                asyncio.create_task(run_round_action(index, action))
                for index, (_output_index, _item_id, action) in enumerate(round_items)
            ]
            round_results: list[Optional[tuple[str, list[str], dict[str, str]]]] = [
                None
            ] * len(round_items)
            round_completed_items: list[Optional[dict[str, Any]]] = [None] * len(round_items)
            completed_all_tasks = False
            try:
                for task in asyncio.as_completed(tasks):
                    index, section, urls, completed_action = await task
                    round_results[index] = (section, urls, completed_action)
                    output_index, item_id, _action = round_items[index]
                    completed_item = _responses_web_search_bridge_module._external_web_search_call_item_for_action(
                        completed_action,
                        urls,
                    )
                    if completed_item is None:
                        continue
                    completed_item["id"] = item_id
                    round_completed_items[index] = completed_item
                    sequence_number += 1
                    yield encode(
                        {
                            "type": "response.web_search_call.completed",
                            "item_id": item_id,
                            "output_index": output_index,
                            "sequence_number": sequence_number,
                            "action": copy.deepcopy(completed_item.get("action", {})),
                        }
                    )
                    yield encode(
                        {
                            "type": "response.output_item.done",
                            "output_index": output_index,
                            "item": completed_item,
                        }
                    )
                completed_all_tasks = True
            finally:
                if not completed_all_tasks:
                    for task in tasks:
                        if not task.done():
                            task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)

            sections: list[str] = []
            all_urls: list[str] = []
            urls_by_action: list[list[str]] = []
            completed: list[dict[str, str]] = []
            visible_items: list[dict[str, Any]] = []
            for index, _round_item in enumerate(round_items):
                result = round_results[index]
                completed_item = round_completed_items[index]
                if result is None or completed_item is None:
                    continue
                section, urls, completed_action = result
                sections.append(section)
                urls_by_action.append(urls)
                completed.append(completed_action)
                visible_items.append(completed_item)
                for url in urls:
                    if url not in all_urls:
                        all_urls.append(url)
            collection["message"] = "\n\n".join(
                section for section in sections if section.strip()
            )
            collection["source_urls"] = all_urls
            collection["source_urls_by_action"] = urls_by_action
            collection["completed_actions"] = completed
            collection["completed_items"] = visible_items

        return stream_events(), collection

    for round_number in range(1, max_rounds + 1):
        round_actions = _responses_web_search_bridge_module._external_web_search_budgeted_actions(
            _responses_web_search_bridge_module._litellm_web_search_actions_for_request(current_response, request_kwargs),
            completed_actions,
        )
        if not round_actions:
            final_response = current_response
            break

        round_events, round_result = collect_action_events(round_actions)
        async for event in round_events:
            yield event
        round_message = round_result["message"]
        round_source_urls = round_result["source_urls"]
        round_source_urls_by_action = round_result["source_urls_by_action"]
        round_completed_actions = round_result["completed_actions"]
        round_completed_items = round_result["completed_items"]
        _trace_module._route_trace(
            "external_web_search_bridge_actions_executed",
            request_id=_routing_module._trace_request_id(request_kwargs),
            session=_routing_module._trace_session_context(request_kwargs),
            model_group=_responses_execution_module._request_model_group(request_kwargs),
            deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
            route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
            round=round_number,
            actions=_responses_web_search_bridge_module._external_web_search_trace_actions(round_completed_actions),
            source_url_count=len(round_source_urls),
            evidence_chars=len(round_message or ""),
        )
        search_sections.append(round_message)
        completed_actions.extend(round_completed_actions)
        source_urls_by_action.extend(round_source_urls_by_action)
        completed_search_items.extend(round_completed_items)
        for url in round_source_urls:
            if url not in source_urls:
                source_urls.append(url)

        search_results = "\n\n".join(section for section in search_sections if section.strip())
        completed_labels = _responses_web_search_bridge_module._external_web_search_action_labels(completed_actions)
        try:
            if round_number >= max_rounds:
                synthesis_task = asyncio.create_task(
                    _responses_web_search_bridge_module._external_web_search_synthesize_or_fallback(
                        request_kwargs=request_kwargs,
                        search_results=search_results,
                        queries=completed_labels,
                        source_urls=source_urls,
                        original_function=original_function,
                    )
                )
                try:
                    async for keepalive in _external_web_search_keepalives_until_done(
                        synthesis_task,
                        request_kwargs=request_kwargs,
                        phase="web_search_synthesis",
                    ):
                        yield keepalive
                    final_response = await synthesis_task
                finally:
                    if not synthesis_task.done():
                        synthesis_task.cancel()
                forced_synthesis = True
                break

            if _responses_web_search_bridge_module._external_web_search_search_failed_without_sources(
                search_results,
                source_urls,
                completed_actions,
            ):
                raise _responses_web_search_bridge_module._external_web_search_search_failed_without_sources_exception(
                    request_kwargs,
                    search_results=search_results,
                    queries=completed_labels,
                    completed_actions=completed_actions,
                    round_number=round_number,
                )

            auto_source_actions = _responses_web_search_bridge_module._external_web_search_auto_source_inspection_actions(
                request_kwargs,
                completed_actions=completed_actions,
                source_urls=source_urls,
                search_results=search_results,
            )
            if auto_source_actions:
                auto_events, auto_result = collect_action_events(auto_source_actions)
                async for event in auto_events:
                    yield event
                auto_message = auto_result["message"]
                auto_source_urls = auto_result["source_urls"]
                auto_source_urls_by_action = auto_result["source_urls_by_action"]
                auto_completed_actions = auto_result["completed_actions"]
                auto_completed_items = auto_result["completed_items"]
                _trace_module._route_trace(
                    "external_web_search_bridge_auto_source_actions_executed",
                    request_id=_routing_module._trace_request_id(request_kwargs),
                    session=_routing_module._trace_session_context(request_kwargs),
                    model_group=_responses_execution_module._request_model_group(request_kwargs),
                    deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                    route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                    round=round_number,
                    actions=_responses_web_search_bridge_module._external_web_search_trace_actions(auto_completed_actions),
                    source_url_count=len(auto_source_urls),
                    evidence_chars=len(auto_message or ""),
                )
                search_sections.append(auto_message)
                completed_actions.extend(auto_completed_actions)
                source_urls_by_action.extend(auto_source_urls_by_action)
                completed_search_items.extend(auto_completed_items)
                for url in auto_source_urls:
                    if url not in source_urls:
                        source_urls.append(url)
                search_results = "\n\n".join(
                    section for section in search_sections if section.strip()
                )
                synthesis_task = asyncio.create_task(
                    _responses_web_search_bridge_module._external_web_search_synthesize_or_fallback(
                        request_kwargs=request_kwargs,
                        search_results=search_results,
                        queries=_responses_web_search_bridge_module._external_web_search_action_labels(completed_actions),
                        source_urls=source_urls,
                        original_function=original_function,
                    )
                )
                try:
                    async for keepalive in _external_web_search_keepalives_until_done(
                        synthesis_task,
                        request_kwargs=request_kwargs,
                        phase="web_search_synthesis",
                    ):
                        yield keepalive
                    final_response = await synthesis_task
                finally:
                    if not synthesis_task.done():
                        synthesis_task.cancel()
                forced_synthesis = True
                break

            if (
                _responses_web_search_bridge_module._external_web_search_request_needs_source_inspection(request_kwargs)
                and _responses_web_search_bridge_module._external_web_search_has_source_page_action(completed_actions)
                and any(action.get("type") == "search" for action in completed_actions)
            ):
                synthesis_task = asyncio.create_task(
                    _responses_web_search_bridge_module._external_web_search_synthesize_or_fallback(
                        request_kwargs=request_kwargs,
                        search_results=search_results,
                        queries=completed_labels,
                        source_urls=source_urls,
                        original_function=original_function,
                    )
                )
                try:
                    async for keepalive in _external_web_search_keepalives_until_done(
                        synthesis_task,
                        request_kwargs=request_kwargs,
                        phase="web_search_synthesis",
                    ):
                        yield keepalive
                    final_response = await synthesis_task
                finally:
                    if not synthesis_task.done():
                        synthesis_task.cancel()
                forced_synthesis = True
                break

            require_source_inspection = (
                _responses_web_search_bridge_module._external_web_search_source_read_required_for_continuation(
                    request_kwargs,
                    completed_actions,
                    source_urls,
                    search_results,
                )
            )
            _responses_web_search_bridge_module._external_web_search_prepare_continuation_recovery_request(
                request_kwargs=request_kwargs,
                search_results=search_results,
                source_urls=source_urls,
                queries=completed_labels,
                completed_actions=completed_actions,
                round_number=round_number,
                require_source_inspection=require_source_inspection,
            )
            continuation_task = asyncio.create_task(
                _responses_web_search_bridge_module._external_web_search_continue_or_synthesize(
                    request_kwargs=request_kwargs,
                    search_results=search_results,
                    queries=completed_labels,
                    completed_actions=completed_actions,
                    source_urls=source_urls,
                    round_number=round_number,
                    original_function=original_function,
                )
            )
            try:
                async for keepalive in _external_web_search_keepalives_until_done(
                    continuation_task,
                    request_kwargs=request_kwargs,
                    phase="web_search_continuation",
                ):
                    yield keepalive
                current_response = await continuation_task
            finally:
                if not continuation_task.done():
                    continuation_task.cancel()
            if _responses_web_search_bridge_module._external_web_search_response_has_search_only_actions(
                current_response,
                request_kwargs,
            ):
                auto_source_actions = _responses_web_search_bridge_module._external_web_search_auto_source_inspection_actions(
                    request_kwargs,
                    completed_actions=completed_actions,
                    source_urls=source_urls,
                    search_results=search_results,
                )
                if auto_source_actions:
                    auto_events, auto_result = collect_action_events(auto_source_actions)
                    async for event in auto_events:
                        yield event
                    auto_message = auto_result["message"]
                    auto_source_urls = auto_result["source_urls"]
                    auto_source_urls_by_action = auto_result["source_urls_by_action"]
                    auto_completed_actions = auto_result["completed_actions"]
                    auto_completed_items = auto_result["completed_items"]
                    _trace_module._route_trace(
                        "external_web_search_bridge_auto_source_actions_executed",
                        request_id=_routing_module._trace_request_id(request_kwargs),
                        session=_routing_module._trace_session_context(request_kwargs),
                        model_group=_responses_execution_module._request_model_group(request_kwargs),
                        deployment_id=_routing_module._deployment_id_from_request(request_kwargs),
                        route_key=_routing_module._deployment_route_key_from_request(request_kwargs),
                        round=round_number,
                        actions=_responses_web_search_bridge_module._external_web_search_trace_actions(auto_completed_actions),
                        source_url_count=len(auto_source_urls),
                        evidence_chars=len(auto_message or ""),
                    )
                    search_sections.append(auto_message)
                    completed_actions.extend(auto_completed_actions)
                    source_urls_by_action.extend(auto_source_urls_by_action)
                    completed_search_items.extend(auto_completed_items)
                    for url in auto_source_urls:
                        if url not in source_urls:
                            source_urls.append(url)
                    search_results = "\n\n".join(
                        section for section in search_sections if section.strip()
                    )
                    synthesis_task = asyncio.create_task(
                        _responses_web_search_bridge_module._external_web_search_synthesize_or_fallback(
                            request_kwargs=request_kwargs,
                            search_results=search_results,
                            queries=_responses_web_search_bridge_module._external_web_search_action_labels(completed_actions),
                            source_urls=source_urls,
                            original_function=original_function,
                        )
                    )
                    try:
                        async for keepalive in _external_web_search_keepalives_until_done(
                            synthesis_task,
                            request_kwargs=request_kwargs,
                            phase="web_search_synthesis",
                        ):
                            yield keepalive
                        final_response = await synthesis_task
                    finally:
                        if not synthesis_task.done():
                            synthesis_task.cancel()
                    forced_synthesis = True
                    break
        except Exception as exc:
            route_recovery_attempted = True
            recovery_task = asyncio.create_task(
                _external_web_search_stream_route_recovery_or_fallback(
                    exc,
                    request_kwargs=request_kwargs,
                    search_results=search_results,
                    queries=completed_labels,
                )
            )
            try:
                async for keepalive in _external_web_search_keepalives_until_done(
                    recovery_task,
                    request_kwargs=request_kwargs,
                    phase="web_search_route_recovery",
                ):
                    yield keepalive
                final_response = await recovery_task
            finally:
                if not recovery_task.done():
                    recovery_task.cancel()
            if _responses_web_search_bridge_module._has_litellm_web_search_actions_for_request(
                final_response,
                request_kwargs,
            ):
                current_response = final_response
                continue
            forced_synthesis = True
            break
        final_response = current_response

    if not forced_synthesis:
        search_results = "\n\n".join(section for section in search_sections if section.strip())
        completed_labels = _responses_web_search_bridge_module._external_web_search_action_labels(completed_actions)
        try:
            finalize_task = asyncio.create_task(
                _responses_web_search_bridge_module._external_web_search_finalize_response(
                    final_response,
                    request_kwargs=request_kwargs,
                    search_results=search_results,
                    queries=completed_labels,
                    source_urls=source_urls,
                    original_function=original_function,
                )
            )
            try:
                async for keepalive in _external_web_search_keepalives_until_done(
                    finalize_task,
                    request_kwargs=request_kwargs,
                    phase="web_search_finalize",
                ):
                    yield keepalive
                final_response = await finalize_task
            finally:
                if not finalize_task.done():
                    finalize_task.cancel()
        except Exception as exc:
            route_recovery_attempted = True
            recovery_task = asyncio.create_task(
                _external_web_search_stream_route_recovery_or_fallback(
                    exc,
                    request_kwargs=request_kwargs,
                    search_results=search_results,
                    queries=completed_labels,
                )
            )
            try:
                async for keepalive in _external_web_search_keepalives_until_done(
                    recovery_task,
                    request_kwargs=request_kwargs,
                    phase="web_search_route_recovery",
                ):
                    yield keepalive
                final_response = await recovery_task
            finally:
                if not recovery_task.done():
                    recovery_task.cancel()

    synthesized_payload, output_items = _external_web_search_output_items_from_response(
        final_response,
        request_kwargs,
    )

    final_output = list(completed_search_items)
    for item in output_items:
        if not isinstance(item, dict):
            continue
        if item.get("type") == "web_search_call":
            continue
        if _responses_web_search_bridge_module._is_litellm_web_search_call_item(item):
            continue
        final_output.append(item)
        index = len(final_output) - 1
        if item.get("type") == "message":
            for event in _external_web_search_message_stream_events(item, index):
                yield encode(event)
            continue

        added_tool_item = copy.deepcopy(item)
        if added_tool_item.get("status") == "completed":
            added_tool_item["status"] = "in_progress"
        yield encode(
            {
                "type": "response.output_item.added",
                "output_index": index,
                "item": added_tool_item,
            }
        )
        yield encode(
            {
                "type": "response.output_item.done",
                "output_index": index,
                "item": item,
            }
        )

    if not _external_web_search_visible_message_items(final_output):
        exception = _external_web_search_missing_final_answer_exception(request_kwargs)
        if route_recovery_attempted:
            yield _streaming_module._synthesized_failed_response_event(
                request_kwargs or {},
                exception,
            )
            return
        recovery_task = asyncio.create_task(
            _external_web_search_stream_route_recovery_or_fallback(
                exception,
                request_kwargs=request_kwargs,
                search_results=search_results,
                queries=completed_labels,
            )
        )
        try:
            async for keepalive in _external_web_search_keepalives_until_done(
                recovery_task,
                request_kwargs=request_kwargs,
                phase="web_search_route_recovery",
            ):
                yield keepalive
            recovered_response = await recovery_task
        finally:
            if not recovery_task.done():
                recovery_task.cancel()
        recovered_payload, recovered_output = _external_web_search_output_items_from_response(
            recovered_response,
            request_kwargs,
        )
        if isinstance(recovered_output, list):
            for item in recovered_output:
                if not isinstance(item, dict):
                    continue
                if item.get("type") == "web_search_call":
                    continue
                if _responses_web_search_bridge_module._is_litellm_web_search_call_item(item):
                    continue
                final_output.append(item)
                if item.get("type") == "message":
                    index = len(final_output) - 1
                    for event in _external_web_search_message_stream_events(item, index):
                        yield encode(event)
        synthesized_payload = recovered_payload
        if not _external_web_search_visible_message_items(final_output):
            yield _streaming_module._synthesized_failed_response_event(
                request_kwargs or {},
                exception,
            )
            return

    final_response_payload = copy.deepcopy(synthesized_payload)
    final_response_payload["status"] = "completed"
    final_response_payload["output"] = final_output
    yield encode({"type": "response.completed", "response": final_response_payload})


def _computer_facade_model_name(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    return str(
        _routing_module._first_not_none(
            request_kwargs.get("model"),
            _responses_execution_module._request_model_group(request_kwargs),
            _routing_module._deployment_route_key_from_request(request_kwargs),
            "unknown",
        )
    )


def _computer_facade_usage() -> dict[str, int]:
    return {
        "input_tokens": 0,
        "output_tokens": 0,
        "total_tokens": 0,
    }


def _computer_facade_message_response(
    request_kwargs: Optional[dict],
    message: str,
) -> dict[str, Any]:
    response_id = f"resp_computer_facade_{os.getpid()}_{time.time_ns()}"
    message_id = f"msg_computer_facade_{time.time_ns()}"
    content = {
        "type": "output_text",
        "text": message,
        "annotations": [],
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": _computer_facade_model_name(request_kwargs),
        "output_text": message,
        "output": [
            {
                "id": message_id,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [content],
            }
        ],
        "usage": _computer_facade_usage(),
    }


def _computer_call_response(
    request_kwargs: Optional[dict],
    actions: list[dict[str, Any]],
) -> dict[str, Any]:
    response_id = f"resp_computer_facade_{os.getpid()}_{time.time_ns()}"
    call_id = f"call_computer_facade_{time.time_ns()}"
    item_id = f"cu_computer_facade_{time.time_ns()}"
    item = {
        "id": item_id,
        "type": "computer_call",
        "call_id": call_id,
        "status": "completed",
        "actions": copy.deepcopy(actions),
        "pending_safety_checks": [],
    }
    return {
        "id": response_id,
        "object": "response",
        "created_at": int(time.time()),
        "status": "completed",
        "model": _computer_facade_model_name(request_kwargs),
        "output_text": "",
        "output": [item],
        "usage": _computer_facade_usage(),
    }


def _extract_computer_call_outputs(input_value: Any) -> list[dict[str, Any]]:
    outputs: list[dict[str, Any]] = []

    def visit(value: Any, depth: int = 0) -> None:
        if value is None or depth > 8 or len(outputs) >= 40:
            return
        if isinstance(value, list):
            for item in value:
                visit(item, depth + 1)
            return
        if not isinstance(value, dict):
            return
        if value.get("type") == "computer_call_output":
            output = value.get("output")
            output_dict = output if isinstance(output, dict) else {}
            outputs.append(
                {
                    "call_id": value.get("call_id"),
                    "output_type": output_dict.get("type"),
                    "image_url": output_dict.get("image_url"),
                    "text": output_dict.get("text"),
                    "detail": output_dict.get("detail"),
                    "width": output_dict.get("width"),
                    "height": output_dict.get("height"),
                }
            )
            return
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                visit(nested, depth + 1)

    visit(input_value)
    return outputs

def _computer_observation_from_output(
    output: Any,
    *,
    backend: str = "",
) -> Optional[ComputerObservation]:
    if not isinstance(output, dict):
        return None
    width = output.get("width")
    height = output.get("height")
    try:
        width_value = int(width) if width is not None else None
    except (TypeError, ValueError):
        width_value = None
    try:
        height_value = int(height) if height is not None else None
    except (TypeError, ValueError):
        height_value = None
    output_type = output.get("output_type") or output.get("type") or "observation"
    return ComputerObservation(
        type=str(output_type),
        image_url=output.get("image_url") if isinstance(output.get("image_url"), str) else None,
        text=output.get("text") if isinstance(output.get("text"), str) else None,
        detail=output.get("detail") if isinstance(output.get("detail"), str) else None,
        width=width_value,
        height=height_value,
        backend=backend,
        metadata={
            key: _streaming_module._jsonable(value)
            for key, value in output.items()
            if key not in {"image_url", "text", "detail", "width", "height"}
        },
    )


def _latest_computer_observation(
    outputs: list[dict[str, Any]],
) -> Optional[ComputerObservation]:
    if not outputs:
        return None
    for output in reversed(outputs):
        observation = _computer_observation_from_output(output)
        if observation is not None:
            return observation
    return None


def _computer_observation_to_dict(
    observation: Optional[ComputerObservation],
    *,
    include_image: bool = False,
) -> dict[str, Any]:
    if observation is None:
        return {}
    payload = {
        "type": observation.type,
        "text": observation.text,
        "detail": observation.detail,
        "width": observation.width,
        "height": observation.height,
        "backend": observation.backend,
        "metadata": observation.metadata,
    }
    if include_image:
        payload["image_url"] = observation.image_url
    elif observation.image_url:
        payload["image_url"] = "[redacted]"
    return {key: value for key, value in payload.items() if value is not None}


def _computer_task_text(request_kwargs: Optional[dict]) -> str:
    request_kwargs = request_kwargs or {}
    parts: list[str] = []

    def append_text(value: Any, depth: int = 0) -> None:
        if value is None or depth > 7 or len(parts) >= 80:
            return
        if isinstance(value, str):
            if value.strip():
                parts.append(value.strip())
            return
        if isinstance(value, list):
            for item in value:
                append_text(item, depth + 1)
            return
        if isinstance(value, dict):
            if value.get("type") == "computer_call_output":
                return
            role = value.get("role")
            content = value.get("content")
            if isinstance(content, str):
                append_text(content, depth + 1)
                return
            if isinstance(content, list):
                append_text(content, depth + 1)
            elif role:
                for nested in value.values():
                    append_text(nested, depth + 1)
            return

    append_text(request_kwargs.get("instructions"))
    append_text(request_kwargs.get("input"))
    append_text(request_kwargs.get("messages"))
    return "\n".join(parts[-20:])


def _computer_extract_first_url(text: str) -> Optional[str]:
    match = re.search(r"https?://[^\s<>()\"']+", text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,;:!?")
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        return None
    return url


def _computer_facade_bridge_callable(
    request_kwargs: Optional[dict],
    namespace: str,
) -> Optional[Any]:
    request_kwargs = request_kwargs or {}
    containers = [request_kwargs]
    for key in ("litellm_metadata", "metadata"):
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, key)
        if metadata:
            containers.append(metadata)
    for container in containers:
        direct = container.get(_COMPUTER_FACADE_EXECUTOR_METADATA_KEY)
        if callable(direct):
            return direct
        mapping = container.get("computer_facade_executors")
        if isinstance(mapping, dict):
            candidate = mapping.get(namespace)
            if callable(candidate):
                return candidate
        namespace_mapping = container.get("namespace_executors")
        if isinstance(namespace_mapping, dict):
            candidate = namespace_mapping.get(namespace)
            if callable(candidate):
                return candidate
    return None


async def _call_computer_facade_bridge(
    bridge: Any,
    namespace: str,
    function_name: str,
    arguments: dict[str, Any],
) -> Any:
    attempts = [
        (namespace, function_name, arguments),
        (function_name, arguments),
        ({"namespace": namespace, "name": function_name, "arguments": arguments},),
    ]
    last_error: Optional[Exception] = None
    for args in attempts:
        try:
            result = bridge(*args)
            if inspect.isawaitable(result):
                result = await result
            return result
        except TypeError as exc:
            last_error = exc
            continue
    if last_error is not None:
        raise last_error
    raise RuntimeError("computer facade executor bridge did not run")


def _normalize_computer_observation(
    result: Any,
    *,
    backend: str,
) -> ComputerObservation:
    if isinstance(result, ComputerObservation):
        result.backend = result.backend or backend
        return result
    if isinstance(result, dict):
        observation = _computer_observation_from_output(result, backend=backend)
        if observation is not None:
            return observation
        return ComputerObservation(
            type="text",
            text=json.dumps(_streaming_module._jsonable(result), ensure_ascii=False),
            backend=backend,
        )
    if isinstance(result, str):
        return ComputerObservation(type="text", text=result, backend=backend)
    return ComputerObservation(
        type="text",
        text="" if result is None else str(result),
        backend=backend,
    )


class ComputerExecutor:
    name = "base"

    def is_available(self, plan: HostedToolPlan, request_kwargs: dict) -> bool:
        return False

    async def screenshot(self) -> ComputerObservation:
        raise NotImplementedError

    async def execute(self, action: dict[str, Any]) -> ComputerObservation:
        raise NotImplementedError

    def supported_actions(self) -> set[str]:
        return {"screenshot", "wait", "done"}


class NamespaceComputerExecutor(ComputerExecutor):
    namespaces: tuple[str, ...] = ()
    screenshot_function = "screenshot"
    action_function_map: dict[str, str] = {}

    def __init__(self, request_kwargs: dict) -> None:
        self.request_kwargs = request_kwargs
        self.namespace = self._select_namespace()

    def _select_namespace(self) -> str:
        plan = _responses_tools_module._responses_hosted_tool_plan(self.request_kwargs)
        for namespace in self.namespaces:
            if namespace in plan.client_namespaces:
                return namespace
        return self.namespaces[0] if self.namespaces else ""

    def _bridge(self) -> Optional[Any]:
        return _computer_facade_bridge_callable(self.request_kwargs, self.namespace)

    def is_available(self, plan: HostedToolPlan, request_kwargs: dict) -> bool:
        return (
            any(namespace in plan.client_namespaces for namespace in self.namespaces)
            and self._bridge() is not None
        )

    async def _call(self, function_name: str, arguments: dict[str, Any]) -> ComputerObservation:
        bridge = self._bridge()
        if bridge is None:
            raise RuntimeError(
                f"{self.name} namespace is advertised but no server-side executor bridge is available"
            )
        result = await _call_computer_facade_bridge(
            bridge,
            self.namespace,
            function_name,
            arguments,
        )
        return _normalize_computer_observation(result, backend=self.name)

    async def screenshot(self) -> ComputerObservation:
        return await self._call(self.screenshot_function, {})

    async def execute(self, action: dict[str, Any]) -> ComputerObservation:
        action_type = str(action.get("type") or "")
        if action_type == "wait":
            duration = action.get("duration_ms")
            try:
                seconds = max(0.0, min(float(duration or 1000) / 1000.0, 10.0))
            except (TypeError, ValueError):
                seconds = 1.0
            await asyncio.sleep(seconds)
            return await self.screenshot()
        if action_type == "screenshot":
            return await self.screenshot()
        function_name = self.action_function_map.get(action_type)
        if function_name is None:
            raise RuntimeError(f"{self.name} does not support action {action_type}")
        return await self._call(function_name, action)

    def supported_actions(self) -> set[str]:
        return {"screenshot", "wait", "done", *self.action_function_map.keys()}


class MCPComputerUseExecutor(NamespaceComputerExecutor):
    name = _COMPUTER_FACADE_MCP_BACKEND
    namespaces = ("mcp__computer_use",)
    screenshot_function = "get_app_state"
    action_function_map = {
        "click": "click",
        "double_click": "click",
        "drag": "drag",
        "scroll": "scroll",
        "type": "type_text",
        "keypress": "press_key",
    }


class BrowserNamespaceExecutor(NamespaceComputerExecutor):
    name = _COMPUTER_FACADE_BROWSER_BACKEND
    namespaces = ("browser", "browser_use", "mcp__browser", "mcp__browser_use")
    screenshot_function = "screenshot"
    action_function_map = {
        "click": "click",
        "double_click": "double_click",
        "move": "move",
        "drag": "drag",
        "scroll": "scroll",
        "type": "type",
        "keypress": "press_key",
    }


class ChromeNamespaceExecutor(NamespaceComputerExecutor):
    name = _COMPUTER_FACADE_CHROME_BACKEND
    namespaces = ("chrome", "chrome_browser", "mcp__chrome")
    screenshot_function = "screenshot"
    action_function_map = BrowserNamespaceExecutor.action_function_map


class PlaywrightComputerExecutor(ComputerExecutor):
    name = _COMPUTER_FACADE_PLAYWRIGHT_BACKEND

    def __init__(self, request_kwargs: dict) -> None:
        self.request_kwargs = request_kwargs
        self._playwright = None
        self._browser = None
        self._page = None
        self._url = _computer_extract_first_url(_computer_task_text(request_kwargs))

    def is_available(self, plan: HostedToolPlan, request_kwargs: dict) -> bool:
        if _computer_facade_backend() == self.name:
            return self._dependency_available()
        task = _computer_task_text(request_kwargs).lower()
        if not self._url and not any(token in task for token in ("browser", "url", "http")):
            return False
        return self._dependency_available()

    def _dependency_available(self) -> bool:
        try:
            import importlib.util

            return importlib.util.find_spec("playwright.async_api") is not None
        except Exception:
            return False

    async def _ensure_page(self) -> Any:
        if self._page is not None:
            return self._page
        from playwright.async_api import async_playwright

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=True)
        self._page = await self._browser.new_page(viewport={"width": 1280, "height": 800})
        await self._page.goto(self._url or "about:blank", wait_until="domcontentloaded")
        return self._page

    async def screenshot(self) -> ComputerObservation:
        page = await self._ensure_page()
        viewport = page.viewport_size or {"width": 1280, "height": 800}
        data = await page.screenshot(type="png", full_page=False)
        image_url = "data:image/png;base64," + base64.b64encode(data).decode("ascii")
        return ComputerObservation(
            type="computer_screenshot",
            image_url=image_url,
            detail="original",
            width=int(viewport.get("width") or 1280),
            height=int(viewport.get("height") or 800),
            backend=self.name,
            metadata={"url": page.url},
        )

    async def execute(self, action: dict[str, Any]) -> ComputerObservation:
        page = await self._ensure_page()
        action_type = str(action.get("type") or "")
        if action_type == "wait":
            duration = action.get("duration_ms")
            try:
                milliseconds = max(0, min(int(duration or 1000), 10000))
            except (TypeError, ValueError):
                milliseconds = 1000
            await page.wait_for_timeout(milliseconds)
        elif action_type == "screenshot":
            pass
        elif action_type == "click":
            await page.mouse.click(int(action["x"]), int(action["y"]), button=action.get("button") or "left")
        elif action_type == "double_click":
            await page.mouse.dblclick(int(action["x"]), int(action["y"]), button=action.get("button") or "left")
        elif action_type == "move":
            await page.mouse.move(int(action["x"]), int(action["y"]))
        elif action_type == "drag":
            x = int(action["x"])
            y = int(action["y"])
            await page.mouse.move(x, y)
            await page.mouse.down()
            await page.mouse.move(x + int(action.get("dx") or 0), y + int(action.get("dy") or 0))
            await page.mouse.up()
        elif action_type == "scroll":
            await page.mouse.wheel(
                int(action.get("scroll_x") or 0),
                int(action.get("scroll_y") or action.get("dy") or 0),
            )
        elif action_type == "type":
            await page.keyboard.type(str(action.get("text") or ""))
        elif action_type == "keypress":
            keys = action.get("keys")
            if isinstance(keys, list):
                for key in keys:
                    await page.keyboard.press(str(key))
            else:
                await page.keyboard.press(str(action.get("text") or "Enter"))
        else:
            raise RuntimeError(f"playwright does not support action {action_type}")
        return await self.screenshot()

    def supported_actions(self) -> set[str]:
        return {
            "screenshot",
            "wait",
            "click",
            "double_click",
            "move",
            "drag",
            "scroll",
            "type",
            "keypress",
            "done",
        }


class UnavailableComputerExecutor(ComputerExecutor):
    def __init__(self, name: str) -> None:
        self.name = name


class MockComputerExecutor(ComputerExecutor):
    name = _COMPUTER_FACADE_MOCK_BACKEND

    def __init__(self, request_kwargs: dict) -> None:
        self.request_kwargs = request_kwargs

    def is_available(self, plan: HostedToolPlan, request_kwargs: dict) -> bool:
        if _computer_facade_backend() == self.name:
            return True
        metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
        return metadata.get("computer_facade_allow_auto_mock") is True

    async def screenshot(self) -> ComputerObservation:
        return ComputerObservation(
            type="computer_screenshot",
            image_url="data:image/png;base64,",
            detail="low",
            width=1,
            height=1,
            backend=self.name,
            metadata={"mock": True},
        )

    async def execute(self, action: dict[str, Any]) -> ComputerObservation:
        if action.get("type") == "wait":
            await asyncio.sleep(0)
        return await self.screenshot()


def _computer_facade_mock_actions(
    request_kwargs: Optional[dict],
    observations: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if observations:
        return []
    return [{"type": "screenshot"}]


_COMPUTER_FACADE_ALLOWED_ACTIONS = {
    "screenshot",
    "wait",
    "click",
    "double_click",
    "move",
    "drag",
    "scroll",
    "type",
    "keypress",
    "done",
}


def _computer_facade_actions_are_valid(actions: Any) -> bool:
    if not isinstance(actions, list) or not actions:
        return False
    for action in actions:
        if not isinstance(action, dict):
            return False
        # This validator is intentionally narrow because it guards the diagnostic
        # mock backend, which must never claim a real click/type/drag happened.
        if action.get("type") not in {"screenshot", "wait"}:
            return False
    return True


def _computer_facade_action_to_dict(action: ComputerAction) -> dict[str, Any]:
    payload = {
        "type": action.type,
        "x": action.x,
        "y": action.y,
        "button": action.button,
        "text": action.text,
        "keys": action.keys,
        "dx": action.dx,
        "dy": action.dy,
        "scroll_x": action.scroll_x,
        "scroll_y": action.scroll_y,
        "duration_ms": action.duration_ms,
        "message": action.message,
    }
    return {key: value for key, value in payload.items() if value is not None}


def _computer_facade_int(value: Any) -> Optional[int]:
    if isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _computer_facade_parse_action(value: Any) -> tuple[Optional[ComputerAction], Optional[str]]:
    if not isinstance(value, dict):
        return None, "planner output must contain an action object"
    action_value = value.get("action", value)
    if not isinstance(action_value, dict):
        return None, "action must be an object"
    action_type = action_value.get("type")
    if not isinstance(action_type, str) or not action_type.strip():
        return None, "action.type is required"
    action_type = action_type.strip().lower()
    keys = action_value.get("keys")
    normalized_keys: Optional[list[str]] = None
    if isinstance(keys, list):
        normalized_keys = [str(key) for key in keys if str(key)]
    elif isinstance(keys, str) and keys.strip():
        normalized_keys = [keys.strip()]
    text = action_value.get("text")
    message = action_value.get("message")
    if message is None and action_type == "done":
        message = action_value.get("text")
    return (
        ComputerAction(
            type=action_type,
            x=_computer_facade_int(action_value.get("x")),
            y=_computer_facade_int(action_value.get("y")),
            button=str(action_value.get("button") or "left")
            if action_value.get("button") is not None
            else None,
            text=str(text) if text is not None else None,
            keys=normalized_keys,
            dx=_computer_facade_int(action_value.get("dx")),
            dy=_computer_facade_int(action_value.get("dy")),
            scroll_x=_computer_facade_int(action_value.get("scroll_x")),
            scroll_y=_computer_facade_int(action_value.get("scroll_y")),
            duration_ms=_computer_facade_int(action_value.get("duration_ms")),
            message=str(message) if message is not None else None,
        ),
        None,
    )


def _computer_facade_validate_action(
    action: ComputerAction,
    observation: Optional[ComputerObservation],
    executor: ComputerExecutor,
) -> Optional[str]:
    if action.type not in _COMPUTER_FACADE_ALLOWED_ACTIONS:
        return f"unsupported action: {action.type}"
    if action.type in _computer_facade_action_denylist():
        return f"denylisted action: {action.type}"
    if action.type not in executor.supported_actions():
        return f"backend {executor.name} does not support action: {action.type}"
    if action.type == "done":
        if not action.message or not action.message.strip():
            return "done action requires message"
        return None
    if action.type in {"click", "double_click", "move", "drag"}:
        if action.x is None or action.y is None:
            return f"{action.type} requires x and y"
        if observation is not None and observation.width and observation.height:
            if not (0 <= action.x < observation.width and 0 <= action.y < observation.height):
                return "coordinates outside observation bounds"
    if action.type == "type" and (action.text is None or action.text == ""):
        return "type action requires text"
    if action.type == "keypress" and not action.keys and not action.text:
        return "keypress action requires keys"
    if action.type == "scroll":
        if (
            action.scroll_x is None
            and action.scroll_y is None
            and action.dx is None
            and action.dy is None
        ):
            return "scroll action requires scroll delta"
    return None


def _computer_facade_action_types(actions: Any) -> list[str]:
    if not isinstance(actions, list):
        return []
    action_types: list[str] = []
    for action in actions:
        if not isinstance(action, dict):
            continue
        action_type = action.get("type")
        if isinstance(action_type, str) and action_type not in action_types:
            action_types.append(action_type)
    return action_types


def _computer_facade_trace_dir() -> Optional[str]:
    runtime_dir = os.getenv("LITELLM_RUNTIME_DIR", "").strip()
    if not runtime_dir:
        root = os.getenv("LITELLM_RUNTIME_ROOT", "").strip()
        if root:
            runtime_dir = os.path.join(root, ".litellm-runtime")
    if not runtime_dir:
        return None
    return os.path.join(runtime_dir, "computer-facade-traces")


def _computer_facade_store_screenshot(value: str) -> Optional[str]:
    split = _image_generation_module._split_image_data_url(value)
    if split is None:
        return None
    trace_dir = _computer_facade_trace_dir()
    if trace_dir is None:
        return None
    try:
        os.makedirs(trace_dir, mode=0o700, exist_ok=True)
        suffix, encoded = split
        image_bytes = base64.b64decode(encoded, validate=True)
        extension = "jpg" if "jpeg" in suffix.lower() else "png"
        path = os.path.join(trace_dir, f"screenshot-{time.time_ns()}.{extension}")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(image_bytes)
        return path
    except (OSError, ValueError, binascii.Error):
        return None

def _computer_facade_sanitize_trace_value(value: Any) -> Any:
    if isinstance(value, str):
        if value.startswith("data:image/"):
            if _computer_facade_trace_screenshots_enabled():
                stored = _computer_facade_store_screenshot(value)
                return {"screenshot_path": stored or "<unwritten>"}
            return "data:image/...;base64,<redacted>"
        return _trace_module._sanitize_trace_text(value)
    if isinstance(value, list):
        return [_computer_facade_sanitize_trace_value(item) for item in value]
    if isinstance(value, dict):
        clean: dict[str, Any] = {}
        for key, item in value.items():
            lowered = str(key).lower()
            if lowered in {"authorization", "api_key", "x-api-key", "cookie"}:
                clean[str(key)] = "<redacted>"
            else:
                clean[str(key)] = _computer_facade_sanitize_trace_value(item)
        return clean
    return value


def _computer_facade_trace(event: str, **fields: Any) -> None:
    if not _computer_facade_trace_enabled():
        return
    try:
        timestamp = datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace(
            "+00:00",
            "Z",
        )
        payload = {
            "timestamp": timestamp,
            "event": event,
            **{
                key: _computer_facade_sanitize_trace_value(value)
                for key, value in fields.items()
            },
        }
        _ROUTE_TRACE_LOGGER.warning(
            "litellm_computer_facade_trace %s",
            json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        )
    except Exception:
        pass


async def _computer_facade_stream(
    response: dict[str, Any],
) -> AsyncIterator[dict[str, Any]]:
    output_items = response.get("output")
    output = output_items if isinstance(output_items, list) else []
    created_response = copy.deepcopy(response)
    created_response["status"] = "in_progress"
    created_response["output"] = []

    def encode(event: dict[str, Any]) -> dict[str, Any]:
        return _JSONStreamEvent(event)

    yield encode({"type": "response.created", "response": created_response})
    for index, item in enumerate(output):
        if not isinstance(item, dict):
            continue
        if item.get("type") == "message":
            content_items = item.get("content")
            content = (
                content_items[0]
                if isinstance(content_items, list) and content_items and isinstance(content_items[0], dict)
                else {"type": "output_text", "text": "", "annotations": []}
            )
            text = content.get("text") if isinstance(content.get("text"), str) else ""
            added_message = copy.deepcopy(item)
            added_message["status"] = "in_progress"
            added_message["content"] = []
            yield encode({
                "type": "response.output_item.added",
                "output_index": index,
                "item": added_message,
            })
            yield encode({
                "type": "response.content_part.added",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "part": {"type": "output_text", "text": "", "annotations": []},
            })
            if text:
                yield encode({
                    "type": "response.output_text.delta",
                    "item_id": item.get("id"),
                    "output_index": index,
                    "content_index": 0,
                    "delta": text,
                })
            yield encode({
                "type": "response.output_text.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "text": text,
            })
            yield encode({
                "type": "response.content_part.done",
                "item_id": item.get("id"),
                "output_index": index,
                "content_index": 0,
                "part": content,
            })
            yield encode({
                "type": "response.output_item.done",
                "output_index": index,
                "item": item,
            })
            continue
        added_item = copy.deepcopy(item)
        if added_item.get("status") == "completed":
            added_item["status"] = "in_progress"
        yield encode({
            "type": "response.output_item.added",
            "output_index": index,
            "item": added_item,
        })
        yield encode({
            "type": "response.output_item.done",
            "output_index": index,
            "item": item,
        })
    yield encode({"type": "response.completed", "response": response})


def _computer_facade_parse_planner_json(raw: Any) -> tuple[Optional[dict], Optional[str]]:
    if isinstance(raw, dict):
        return raw, None
    text = _image_generation_module._response_text(raw) if not isinstance(raw, str) else raw
    text = str(text or "").strip()
    if not text:
        return None, "planner returned empty output"
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE)
        text = re.sub(r"\s*```$", "", text)
    try:
        parsed = json.loads(text)
    except Exception as exc:
        return None, f"planner returned invalid JSON: {exc}"
    if not isinstance(parsed, dict):
        return None, "planner JSON must be an object"
    return parsed, None


def _computer_facade_planner_payload(
    request_kwargs: dict,
    observation: Optional[ComputerObservation],
    executor: ComputerExecutor,
    history: list[dict[str, Any]],
    *,
    invalid_reason: Optional[str] = None,
) -> dict[str, Any]:
    task = _computer_task_text(request_kwargs)
    schema = {
        "action": {
            "type": "click|double_click|move|drag|scroll|type|keypress|wait|screenshot|done",
            "x": 123,
            "y": 456,
            "button": "left",
            "text": "text to type or done message",
            "keys": ["Enter"],
            "dx": 10,
            "dy": 10,
            "scroll_x": 0,
            "scroll_y": 500,
            "duration_ms": 1000,
            "message": "final answer only for done",
        },
        "rationale": "short private rationale",
    }
    planner_input = {
        "task": task,
        "latest_observation": _computer_observation_to_dict(observation),
        "action_history": history[-10:],
        "backend": executor.name,
        "supported_actions": sorted(executor.supported_actions()),
        "denylist": sorted(_computer_facade_action_denylist()),
        "remaining_step_budget": max(
            0,
            _computer_facade_max_steps() - len(history),
        ),
        "action_schema": schema,
    }
    if invalid_reason:
        planner_input["previous_invalid_reason"] = invalid_reason
    return planner_input


async def _computer_facade_call_planner(
    request_kwargs: dict,
    observation: Optional[ComputerObservation],
    executor: ComputerExecutor,
    history: list[dict[str, Any]],
    *,
    invalid_reason: Optional[str] = None,
) -> tuple[Optional[dict], Optional[str]]:
    injected = request_kwargs.get("computer_facade_planner_outputs")
    if isinstance(injected, list) and injected:
        raw = injected.pop(0)
        return _computer_facade_parse_planner_json(raw)
    metadata = _image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}
    injected = metadata.get("computer_facade_planner_outputs")
    if isinstance(injected, list) and injected:
        raw = injected.pop(0)
        return _computer_facade_parse_planner_json(raw)

    from litellm.proxy.proxy_server import llm_router

    if llm_router is None:
        return None, "LiteLLM router is unavailable for planner"

    planner_input = _computer_facade_planner_payload(
        request_kwargs,
        observation,
        executor,
        history,
        invalid_reason=invalid_reason,
    )
    model = _computer_facade_planner_model(request_kwargs)
    planner_metadata = (_image_generation_module._request_metadata_dict(request_kwargs, "litellm_metadata") or {}).copy()
    planner_metadata[_COMPUTER_FACADE_PLANNER_METADATA_KEY] = True
    instructions = (
        "You are a computer-use planner. Return exactly one JSON object and no prose. "
        "Do not call hosted tools. Pick only one validated action. Use done only when "
        "the task is complete and include a concise message."
    )
    prompt = json.dumps(planner_input, ensure_ascii=False, default=str)
    response = None
    aresponses = getattr(llm_router, "aresponses", None)
    if callable(aresponses):
        try:
            response = await aresponses(
                model=model,
                input=prompt,
                instructions=instructions,
                litellm_metadata=planner_metadata,
                stream=False,
            )
        except Exception as exc:
            _computer_facade_trace(
                "computer_facade_planner_responses_error",
                exception=_routing_module._trace_exception(exc),
            )
            response = None
    if response is None:
        acompletion = getattr(llm_router, "acompletion", None)
        if not callable(acompletion):
            return None, "LiteLLM router has no planner-compatible method"
        response = await acompletion(
            model=model,
            messages=[
                {"role": "system", "content": instructions},
                {"role": "user", "content": prompt},
            ],
            litellm_metadata=planner_metadata,
            stream=False,
        )
    return _computer_facade_parse_planner_json(response)


async def _computer_facade_next_action(
    request_kwargs: dict,
    observation: Optional[ComputerObservation],
    executor: ComputerExecutor,
    history: list[dict[str, Any]],
) -> tuple[Optional[ComputerAction], Optional[str]]:
    invalid_reason: Optional[str] = None
    for _attempt in range(2):
        payload, parse_error = await _computer_facade_call_planner(
            request_kwargs,
            observation,
            executor,
            history,
            invalid_reason=invalid_reason,
        )
        if parse_error is not None:
            invalid_reason = parse_error
            continue
        action, action_error = _computer_facade_parse_action(payload)
        if action_error is not None or action is None:
            invalid_reason = action_error or "invalid action"
            continue
        validation_error = _computer_facade_validate_action(
            action,
            observation,
            executor,
        )
        if validation_error is not None:
            invalid_reason = validation_error
            continue
        return action, None
    return None, invalid_reason or "invalid planner action"


def _computer_facade_executor_registry(request_kwargs: dict) -> dict[str, ComputerExecutor]:
    return {
        _COMPUTER_FACADE_MCP_BACKEND: MCPComputerUseExecutor(request_kwargs),
        _COMPUTER_FACADE_BROWSER_BACKEND: BrowserNamespaceExecutor(request_kwargs),
        _COMPUTER_FACADE_CHROME_BACKEND: ChromeNamespaceExecutor(request_kwargs),
        _COMPUTER_FACADE_PLAYWRIGHT_BACKEND: PlaywrightComputerExecutor(request_kwargs),
        _COMPUTER_FACADE_CUA_BACKEND: UnavailableComputerExecutor(_COMPUTER_FACADE_CUA_BACKEND),
        _COMPUTER_FACADE_MOCK_BACKEND: MockComputerExecutor(request_kwargs),
    }


def _computer_facade_select_executor(
    plan: HostedToolPlan,
    request_kwargs: dict,
) -> Optional[ComputerExecutor]:
    backend = _computer_facade_backend()
    registry = _computer_facade_executor_registry(request_kwargs)
    if backend != _COMPUTER_FACADE_AUTO_BACKEND:
        executor = registry.get(backend)
        if executor is not None and executor.is_available(plan, request_kwargs):
            return executor
        return None
    for name in (
        _COMPUTER_FACADE_MCP_BACKEND,
        _COMPUTER_FACADE_BROWSER_BACKEND,
        _COMPUTER_FACADE_CHROME_BACKEND,
        _COMPUTER_FACADE_PLAYWRIGHT_BACKEND,
        _COMPUTER_FACADE_CUA_BACKEND,
        _COMPUTER_FACADE_MOCK_BACKEND,
    ):
        executor = registry[name]
        if executor.is_available(plan, request_kwargs):
            return executor
    return None


async def _run_mock_computer_facade(
    request_kwargs: dict,
    plan: HostedToolPlan,
) -> dict[str, Any]:
    observations = _extract_computer_call_outputs(request_kwargs.get("input"))
    if observations:
        response = _computer_facade_message_response(
            request_kwargs,
            _COMPUTER_FACADE_MOCK_DONE_MESSAGE,
        )
    else:
        actions = _computer_facade_mock_actions(request_kwargs, observations)
        if not _computer_facade_actions_are_valid(actions):
            response = _computer_facade_message_response(
                request_kwargs,
                _COMPUTER_FACADE_SAFE_FAILURE_MESSAGE,
            )
        else:
            response = _computer_call_response(request_kwargs, actions)

    first_output = response.get("output")
    first_item = first_output[0] if isinstance(first_output, list) and first_output else {}
    call_id = first_item.get("call_id") if isinstance(first_item, dict) else None
    _computer_facade_trace(
        "computer_facade_mock_response",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        backend=_computer_facade_backend(),
        hosted_computer=plan.hosted_computer,
        observations=len(observations),
        action_types=_computer_facade_action_types(
            first_item.get("actions") if isinstance(first_item, dict) else None
        ),
        call_id=call_id,
    )
    return response


async def _run_computer_facade(
    request_kwargs: dict,
    plan: HostedToolPlan,
) -> Any:
    if _computer_facade_backend() == _COMPUTER_FACADE_MOCK_BACKEND:
        response = await _run_mock_computer_facade(request_kwargs, plan)
        if request_kwargs.get("stream") is True:
            return _computer_facade_stream(response)
        return response

    observations = _extract_computer_call_outputs(request_kwargs.get("input"))
    if len(observations) >= _computer_facade_max_steps():
        response = _computer_facade_message_response(
            request_kwargs,
            _COMPUTER_FACADE_SAFE_FAILURE_MESSAGE,
        )
    else:
        executor = _computer_facade_select_executor(plan, request_kwargs)
        if executor is None:
            response = _computer_facade_message_response(
                request_kwargs,
                _COMPUTER_FACADE_SAFE_FAILURE_MESSAGE,
            )
            _computer_facade_trace(
                "computer_facade_no_backend",
                request_id=_routing_module._trace_request_id(request_kwargs),
                session=_routing_module._trace_session_context(request_kwargs),
                configured_backend=_computer_facade_backend(),
                hints=plan.available_executor_hints,
            )
        else:
            observation = _latest_computer_observation(observations)
            history = [
                {"type": item.get("output_type"), "call_id": item.get("call_id")}
                for item in observations
            ]
            try:
                if observation is None:
                    observation = await executor.screenshot()
                if _computer_facade_require_observation() and observation is None:
                    raise RuntimeError("executor did not return observation")
                action, invalid_reason = await _computer_facade_next_action(
                    request_kwargs,
                    observation,
                    executor,
                    history,
                )
                if action is None:
                    response = _computer_facade_message_response(
                        request_kwargs,
                        _COMPUTER_FACADE_SAFE_FAILURE_MESSAGE,
                    )
                    _computer_facade_trace(
                        "computer_facade_invalid_planner_action",
                        request_id=_routing_module._trace_request_id(request_kwargs),
                        backend=executor.name,
                        reason=invalid_reason,
                        observation=_computer_observation_to_dict(
                            observation,
                            include_image=_computer_facade_trace_screenshots_enabled(),
                        ),
                    )
                elif action.type == "done":
                    response = _computer_facade_message_response(
                        request_kwargs,
                        action.message or "",
                    )
                elif action.type == "screenshot":
                    response = _computer_call_response(
                        request_kwargs,
                        [_computer_facade_action_to_dict(action)],
                    )
                else:
                    executed_observation = await executor.execute(
                        _computer_facade_action_to_dict(action)
                    )
                    if _computer_facade_require_observation() and executed_observation is None:
                        raise RuntimeError("executor action returned no observation")
                    response = _computer_call_response(
                        request_kwargs,
                        [{"type": "screenshot"}],
                    )
                    _computer_facade_trace(
                        "computer_facade_action_executed",
                        request_id=_routing_module._trace_request_id(request_kwargs),
                        session=_routing_module._trace_session_context(request_kwargs),
                        backend=executor.name,
                        action_type=action.type,
                        observation=_computer_observation_to_dict(
                            executed_observation,
                            include_image=_computer_facade_trace_screenshots_enabled(),
                        ),
                    )
            except Exception as exc:
                response = _computer_facade_message_response(
                    request_kwargs,
                    _COMPUTER_FACADE_SAFE_FAILURE_MESSAGE,
                )
                _computer_facade_trace(
                    "computer_facade_executor_error",
                    request_id=_routing_module._trace_request_id(request_kwargs),
                    session=_routing_module._trace_session_context(request_kwargs),
                    backend=executor.name,
                    exception=_routing_module._trace_exception(exc),
                )

    first_output = response.get("output")
    first_item = first_output[0] if isinstance(first_output, list) and first_output else {}
    _computer_facade_trace(
        "computer_facade_response",
        request_id=_routing_module._trace_request_id(request_kwargs),
        session=_routing_module._trace_session_context(request_kwargs),
        model_group=_responses_execution_module._request_model_group(request_kwargs),
        backend=_computer_facade_backend(),
        hosted_computer=plan.hosted_computer,
        observations=len(observations),
        output_type=first_item.get("type") if isinstance(first_item, dict) else None,
        action_types=_computer_facade_action_types(
            first_item.get("actions") if isinstance(first_item, dict) else None
        ),
        call_id=first_item.get("call_id") if isinstance(first_item, dict) else None,
    )
    if request_kwargs.get("stream") is True:
        return _computer_facade_stream(response)
    return response


async def _responses_computer_facade_retry_response(
    exception: Exception,
    request_kwargs: Optional[dict],
    outer_request_kwargs: Optional[dict],
) -> Optional[Any]:
    if not isinstance(request_kwargs, dict):
        return None
    if _image_generation_module._request_already_attempted_responses_chat_bridge(
        request_kwargs
    ) or _image_generation_module._request_already_attempted_responses_chat_bridge(outer_request_kwargs):
        return None
    if not _native_hosted_computer_unsupported_error(
        exception,
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    plan = _responses_tools_module._responses_hosted_tool_plan(request_kwargs, outer_request_kwargs)
    if not _can_use_computer_facade_after_native_error(
        plan,
        request_kwargs,
        outer_request_kwargs,
    ):
        return None
    return await _run_computer_facade(request_kwargs, plan)
